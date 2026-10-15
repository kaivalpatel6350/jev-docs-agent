"""
exporter.py — package a project's docs for sharing (used by `agent.py export` and the MCP tool export_project).

  <project>/out/exports/<project>-guide.html   one file: all docs, screenshots embedded (open in a browser → Print → PDF)
  <project>/out/exports/<project>-<date>.zip   docs (.md + .html) + the screenshots/videos they use, links intact
  mp4=True                                     also adds .mp4 copies of the videos to the zip (needs ffmpeg)

Docs link media as ../videos/<video>/<file> (relative to out/docs/); only files that exist are packaged.
"""
import base64, html, re, shutil, subprocess, time, zipfile
from pathlib import Path

MEDIA = re.compile(r"(!?)\[([^\]]*)\]\((\.\./videos/([a-z0-9_-]+)/([A-Za-z0-9_.-]+))\)")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
CSS = """body{font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1f2328}
img{max-width:100%;border:1px solid #d0d7de;border-radius:6px;margin:.5rem 0}
a.video{display:inline-block;padding:.3rem .7rem;border:1px solid #0969da;border-radius:6px;text-decoration:none}
nav{background:#f6f8fa;padding:1rem 1.5rem;border-radius:6px}nav li{margin:.2rem 0}
section.doc{border-top:2px solid #d0d7de;margin-top:3rem;padding-top:1rem}
code{background:#f6f8fa;padding:.1rem .3rem;border-radius:4px}table{border-collapse:collapse}
td,th{border:1px solid #d0d7de;padding:.3rem .6rem}
@media print{section.doc{page-break-before:always}nav{display:none}a.video{border:none}}"""


def ffmpeg_bin():
    """An ffmpeg that can encode H.264 (system ffmpeg; Playwright's bundled one only does VP8)."""
    exe = shutil.which("ffmpeg")
    if not exe: return None
    try:
        enc = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    return exe if "libx264" in enc else None


def to_mp4(src, dst):
    exe = ffmpeg_bin()
    if not exe: raise RuntimeError("ffmpeg with H.264 is not available (rebuild the Docker image or install ffmpeg)")
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart", "-an", str(dst)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode or not Path(dst).exists():
        raise RuntimeError(f"mp4 conversion failed for {Path(src).name}: {r.stderr.strip()[-300:]}")
    return Path(dst)


def md_to_html(md_text):
    import markdown
    return markdown.markdown(md_text, extensions=["tables", "fenced_code", "toc", "sane_lists"])


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def title_of(md_text, fallback):
    m = re.search(r"^#\s+(.+)$", md_text, re.M)
    return m.group(1).strip() if m else fallback


def export_project(out_dir, project, docs=None, mp4=False, host_out=None):
    """Build the one-file HTML guide and the zip. out_dir = <project>/out. host_out = the same folder as seen on the
    host (paths shown to the user when running in Docker). Returns what was written."""
    out_dir = Path(out_dir)
    docs_dir, vids_dir, exp = out_dir / "docs", out_dir / "videos", out_dir / "exports"
    files = sorted(docs_dir.glob("*.md")) if docs_dir.exists() else []
    if docs: files = [f for f in files if f.stem in set(docs)]
    if not files:
        raise FileNotFoundError(f"no written docs to export in {docs_dir}" + (f" matching {sorted(docs)}" if docs else ""))
    exp.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    texts = {f: f.read_text() for f in files}

    used, missing = {}, set()   # "video/file" -> path of every linked media file
    for md in texts.values():
        for _, _, rel, vid, name in MEDIA.findall(md):
            p = vids_dir / vid / name
            if p.exists(): used[f"{vid}/{name}"] = p
            else: missing.add(rel)

    tmp, mp4s = exp / "_mp4", {}
    if mp4:
        tmp.mkdir(exist_ok=True)
        for key, p in used.items():
            if p.suffix == ".webm": mp4s[key] = to_mp4(p, tmp / f"{p.parent.name}.mp4")
    target = lambda key, vid: f"media/{vid}/{vid}.mp4" if key in mp4s else f"media/{key}"

    try:
        # 1. one HTML file: screenshots embedded; video links point into the zip's media/ folder
        def embed(m):
            bang, alt, rel, vid, name = m.groups()
            key, p = f"{vid}/{name}", vids_dir / vid / name
            if not p.exists(): return f"*[missing: {rel}]*"
            if bang and p.suffix.lower() in IMAGE_TYPES:
                data = base64.b64encode(p.read_bytes()).decode()
                return f'<img alt="{html.escape(alt)}" src="data:{IMAGE_TYPES[p.suffix.lower()]};base64,{data}">'
            return f'<a class="video" href="{target(key, vid)}">{html.escape(alt or "▶ Watch")}</a>'

        toc, sections = [], []
        for f, md in texts.items():
            toc.append(f'<li><a href="#doc-{f.stem}">{html.escape(title_of(md, f.stem))}</a></li>')
            sections.append(f'<section class="doc" id="doc-{f.stem}">{md_to_html(MEDIA.sub(embed, md))}</section>')
        guide = exp / f"{project}-guide.html"
        guide.write_text(page(f"{project} — user guide",
                              f"<h1>{html.escape(project)} — user guide</h1><p>Generated {stamp}. Screenshots are "
                              "built in; video links work when this file sits next to the zip's media/ folder.</p>"
                              f"<nav><b>Contents</b><ol>{''.join(toc)}</ol></nav>{''.join(sections)}"))

        # 2. zip: guide + docs (.md and .html, links rewritten to ../media/) + only the media they use
        relink = lambda m: f"{m.group(1)}[{m.group(2)}](../{target(f'{m.group(4)}/{m.group(5)}', m.group(4))})"
        zpath = exp / f"{project}-{stamp}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(guide, guide.name)
            for f, md in texts.items():
                md2 = MEDIA.sub(relink, md)
                z.writestr(f"docs/{f.name}", md2)
                z.writestr(f"docs/{f.stem}.html", page(title_of(md2, f.stem), md_to_html(md2)))
            for key, p in used.items():
                if key in mp4s: z.write(mp4s[key], target(key, p.parent.name), compress_type=zipfile.ZIP_STORED)
                else: z.write(p, f"media/{key}", compress_type=zipfile.ZIP_STORED if p.suffix == ".webm" else zipfile.ZIP_DEFLATED)
            z.writestr("README.txt", f"{project} documentation, exported {stamp}.\n\n"
                                     f"{guide.name}  everything in one page (browser: Print -> Save as PDF for a PDF)\n"
                                     "docs/        each guide as .html and .md\n"
                                     "media/       screenshots and walkthrough videos the guides link to\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    show = (lambda p: str(Path(host_out) / p.relative_to(out_dir))) if host_out else str
    return {"docs": [f.stem for f in files], "html": show(guide), "zip": show(zpath), "folder": show(exp),
            "html_mb": round(guide.stat().st_size / 1e6, 1), "zip_mb": round(zpath.stat().st_size / 1e6, 1),
            "media_files": len(used), "videos": sum(p.suffix == ".webm" for p in used.values()),
            "mp4": bool(mp4s), "missing_links": sorted(missing)}

