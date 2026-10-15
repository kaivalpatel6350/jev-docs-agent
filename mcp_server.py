"""
mcp_server.py — lets Claude Desktop drive the docs agent: set up any app as a project, write goals,
record videos (Jev + Playwright) and write the user docs itself from the recordings.

  Run locally:  python mcp_server.py                 (stdio — Claude Desktop starts it)
  In Docker:    docker run -i --rm -v ./projects:/app/projects --env-file .env jev-docs-agent

Recording runs as a background job (`python agent.py videos ...`) so long runs never hit a tool timeout;
check it with job_status. Docs are written by Claude Desktop: get_doc_brief → save_doc (no API key needed).
"""
import io, json, os, re, shutil, signal, subprocess, sys, time
from contextlib import redirect_stdout

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import agent

INSTRUCTIONS = """Docs agent: records how-to videos of a web app and turns them into user documentation.
Each app under test is a *project* (projects/<name>/: guides.yaml, .env with its login, out/).

Typical flow:
1. list_projects → use an existing one, or create_project(name, app_url, ...) for a new app.
2. add_video(name, goal, ...) for each task. The goal is what matters: say what "finished" looks like.
   Hints are optional steps, only if Jev gets lost. The first video should be `login` with start_path
   (e.g. /login); others use start_after="login" to start signed in. Secrets are ${VAR} in test_data — never in
   guides.yaml.
   Logins: get_credentials shows what is missing; set_credentials saves test-account values (default profile =
   .env; extra roles like "admin"/"viewer" = their own profile). run_videos(profile="admin") records as that user.
3. check_project (free) → run_videos → job_status until finished (a run takes minutes).
4. Failed videos: get_recording(name) to see what happened, then edit the goal/hints with add_video and re-run.
5. add_doc(name, videos, type, audience) → get_doc_brief(name) (screenshots + instructions) → write the
   Markdown → save_doc(name, markdown).
6. export_project → one HTML guide + a zip (docs, screenshots, videos; mp4=True for .mp4). Give the user the paths.
Never weaken forbid_labels (they stop clicks on payments, deletes, sign-out) unless the user asks."""

mcp = MCPServer("docs-agent", instructions=INSTRUCTIONS, version="1.0")
JOBS = {}  # job id -> {"proc", "log", "project", "videos", "started"}
VIDEO_FIELDS = ("goal", "hints", "start_path", "start_after", "enabled", "reason", "allow",
                "max_steps", "max_wait_seconds")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
SECRET_KEY = re.compile(r"pass|secret|token|key", re.I)


def _project(name, profile=None):
    """Load a project (fresh from disk every call, so manual edits are picked up)."""
    try:
        return agent.use_project(name, profile)
    except (ValueError, FileNotFoundError) as e:
        raise ToolError(f"{e}. Use list_projects / create_project, or get_credentials for profiles.")


def _name(name):
    if not NAME.fullmatch(name or ""): raise ToolError(f"name must be lowercase letters, digits, - or _: {name!r}")


def _find(section, name):
    return next((e for e in agent.CFG[section] if e["name"] == name), None)


def _backup():
    shutil.copy(agent.CFG_PATH, agent.CFG_PATH.with_suffix(".yaml.bak"))


def _no_plain_secrets(test_data):
    plain = [k for k, v in (test_data or {}).items() if SECRET_KEY.search(k) and v and "${" not in str(v)]
    if plain: raise ToolError(f"put secrets in the project's .env and reference them as ${{VAR}}: {plain}")


def _clip(text, n):
    return text if len(text) <= n else text[:n] + f"\n… ({len(text) - n} more characters)"


# ---------------- projects ----------------
@mcp.tool()
def list_projects() -> dict:
    """List projects (one per app under test) with their app URL and how many videos/docs they have."""
    out = {}
    for p in sorted(agent.PROJECTS.glob("*/guides.yaml")):
        try:
            cfg = agent.load(p)
            out[p.parent.name] = {"app_url": cfg.get("app_url"), "videos": len(cfg["videos"]),
                                  "docs": len(cfg["docs"]), "has_env_file": (p.parent / ".env").exists()}
        except Exception as e:
            out[p.parent.name] = {"error": f"guides.yaml does not load: {e}"}
    return {"projects_dir": str(agent.PROJECTS), "projects": out}


@mcp.tool()
def create_project(name: str, app_url: str, test_data: dict[str, str] | None = None,
                   forbid_labels: list[str] | None = None, upload_file: str | None = None) -> str:
    """Create projects/<name> for a new app. test_data = values Jev may type; secrets as "${VAR}". If omitted it is
    {"username": "${LOGIN_USERNAME}", "password": "${LOGIN_PASSWORD}"} — then supply the login with set_credentials
    (or docker -e LOGIN_USERNAME=... -e LOGIN_PASSWORD=...). forbid_labels = button texts never clicked (payments,
    delete, sign out, ...). upload_file = file path (relative to the project folder) used by upload steps."""
    _name(name)
    path = agent.project_config(name)
    if path.exists(): raise ToolError(f"project '{name}' already exists")
    if not re.match(r"https?://", app_url): raise ToolError("app_url must start with http:// or https://")
    _no_plain_secrets(test_data)
    if test_data is None:  # standard names, so `docker run -e LOGIN_USERNAME=... -e LOGIN_PASSWORD=...` works for any app
        test_data = {"username": "${LOGIN_USERNAME}", "password": "${LOGIN_PASSWORD}"}
    settings = {"headless": True, "slow_mo": 400, "show_cursor": True, "max_steps": 30, "min_confidence": 0.3,
                "done_threshold": 0.8, "wait_poll_seconds": 5, "max_wait_seconds": 120,
                "max_images_per_doc": 40, "coach_max_calls": 6, "stall_seconds": 45, "jev_model": "jev-1.13"}
    q = lambda v: json.dumps(v, ensure_ascii=False)
    text = (f"# guides.yaml — {name}. Edit here or through Claude Desktop (MCP).\n"
            "# goal = what 'finished' looks like (Jev plans the clicks); hints = optional steps.\n"
            "# start_after: run after that video and reuse its session (e.g. stay signed in)\n\n"
            f"app_url: {q(app_url)}\noutput_dir: out\n"
            + (f"upload_file: {q(upload_file)}\n" if upload_file else "")
            + "\nsettings:\n" + "".join(f"  {k}: {q(v)}\n" for k, v in settings.items())
            + "\nnever_improve: [\"login\"]\n"
            + f"forbid_labels: {q(forbid_labels or ['Sign Out', 'Log Out', 'Delete Account', 'Pay Now', 'Purchase'])}\n"
            + "\ntest_data:\n" + ("".join(f"  {k}: {q(v)}\n" for k, v in test_data.items()) if test_data else "  {}\n")
            + "\nvideos: []\n\ndocs: []\n")
    if not test_data: text = text.replace("test_data:\n  {}\n", "test_data: {}\n")
    agent.yaml.safe_load(text)  # sanity check before writing
    path.parent.mkdir(parents=True)
    path.write_text(text)
    env_vars = sorted({m for v in (test_data or {}).values() for m in re.findall(r"\$\{(\w+)\}", str(v))})
    if env_vars:
        env = path.parent / ".env"
        env.write_text("# credentials for this project only\n" + "".join(f"{v}=\n" for v in env_vars))
        os.chmod(env, 0o600)
    return (f"Created {path}." + (f" Credentials needed: {', '.join(env_vars)} — ask the user for test-account values "
                                  "and save them with set_credentials (or they edit .env / use docker -e)."
                                  if env_vars else "") + " Next: add_video for `login`, then the tasks.")


@mcp.tool()
def get_project(project: str) -> str:
    """Full guides.yaml of a project (settings, test_data, forbid_labels, videos, docs) plus latest results."""
    _project(project)
    res = {r["name"]: r["status"] for r in agent.latest_results()}
    docs = sorted(p.stem for p in (agent.OUT / "docs").glob("*.md"))
    return (agent.CFG_PATH.read_text() + f"\n# --- latest video results: {json.dumps(res)}"
            f"\n# --- written docs: {docs}")


@mcp.tool()
def update_settings(project: str, app_url: str | None = None, settings: dict | None = None,
                    test_data: dict[str, str] | None = None, forbid_labels: list[str] | None = None,
                    upload_file: str | None = None, reference_doc: str | None = None) -> str:
    """Change project-level config. settings/test_data are merged into the existing values (set a test_data key
    to "" to remove it). forbid_labels replaces the list. reference_doc = existing product docs (.docx/.md, in the
    project folder) whose wording and button names the docs should keep."""
    _project(project)
    _no_plain_secrets(test_data)
    raw = agent.yaml.safe_load(agent.CFG_PATH.read_text())  # unresolved values, as written
    _backup()
    if app_url: agent.save_top("app_url", app_url)
    if settings: agent.save_top("settings", {**(raw.get("settings") or {}), **settings})
    if test_data is not None:
        agent.save_top("test_data", {k: v for k, v in {**(raw.get("test_data") or {}), **test_data}.items() if v != ""})
    if forbid_labels is not None: agent.save_top("forbid_labels", forbid_labels)
    if upload_file is not None: agent.save_top("upload_file", upload_file)
    if reference_doc is not None: agent.save_top("reference_doc", reference_doc)
    return "Saved (backup: guides.yaml.bak). Run check_project to validate."


@mcp.tool()
def check_project(project: str, profile: str | None = None) -> str:
    """Free validation (no browser, no API): config errors, missing credentials, run order and cost estimate.
    profile = credential set to check (default: the project's .env)."""
    _project(project, profile)
    buf = io.StringIO()
    with redirect_stdout(buf):
        agent.plan()
    return buf.getvalue()


# ---------------- credentials (test logins) ----------------
def _needed_vars():
    return sorted({v for val in agent.CFG.get("test_data", {}).values() for v in re.findall(r"\$\{(\w+)\}", str(val))})


@mcp.tool()
def get_credentials(project: str) -> dict:
    """Which test logins (credential profiles) a project has and whether each variable it needs is filled in.
    Values are never shown. 'default' = projects/<name>/.env; other profiles = .env.<profile> (e.g. admin, viewer)."""
    _project(project)
    need, folder = _needed_vars(), agent.CFG_PATH.parent
    out = {}
    for p in agent.profiles(folder) or ["default"]:
        agent.load_env(folder, p)
        out[p] = {v: ("set by docker/shell" if v in agent._REAL_ENV else "set") if os.environ.get(v) else "MISSING"
                  for v in need}
    agent.load_env(folder)
    return {"needed_by_test_data": need, "profiles": out,
            "how_to_supply": "set_credentials(project, values, profile), or edit the file, or docker -e VAR=..."}


@mcp.tool()
def set_credentials(project: str, values: dict[str, str], profile: str = "default") -> str:
    """Save test-login values for a profile, e.g. {"LOGIN_USERNAME": "qa@acme.com", "LOGIN_PASSWORD": "..."}.
    Written to projects/<name>/.env (default) or .env.<profile> — never to guides.yaml and never echoed back.
    A new profile only needs the values that differ from default. Set a value to "" to remove it.
    Only use test/demo accounts. For real secrets prefer editing the file or docker -e."""
    from dotenv import set_key, unset_key
    _project(project)
    bad = [k for k in values if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k)]
    if bad: raise ToolError(f"variable names must be UPPER_CASE: {bad}")
    try:
        path = agent.env_file(agent.CFG_PATH.parent, profile)
    except ValueError as e:
        raise ToolError(str(e))
    if not path.exists():
        path.write_text(f"# test login — profile '{profile}' (overrides .env)\n" if profile != "default" else "")
    os.chmod(path, 0o600)
    for k, v in values.items():
        if v == "": unset_key(path, k)
        else: set_key(path, k, v)
    _project(project, profile)
    missing = [v for v in _needed_vars() if not os.environ.get(v)]
    unused = [k for k in values if values[k] and k not in _needed_vars()]
    agent.load_env(agent.CFG_PATH.parent)
    return (f"Saved {len(values)} value(s) to {path.name} (profile '{profile}')."
            + (f" Still missing for this profile: {missing}." if missing else " All credentials this project needs are set.")
            + (f" Not used by test_data (add them there as ${{VAR}} if Jev should type them): {unused}." if unused else ""))


@mcp.tool()
def remove_credentials_profile(project: str, profile: str) -> str:
    """Delete a non-default credential profile (.env.<profile>)."""
    _project(project)
    if profile == "default": raise ToolError("the default profile (.env) can't be removed; clear values with set_credentials")
    try:
        path = agent.env_file(agent.CFG_PATH.parent, profile)
    except ValueError as e:
        raise ToolError(str(e))
    if not path.exists(): raise ToolError(f"no profile '{profile}'")
    path.unlink()
    return f"Removed profile '{profile}'."



# ---------------- videos (goals) ----------------
@mcp.tool()
def add_video(project: str, name: str, goal: str | None = None, hints: list[str] | None = None,
              start_path: str | None = None, start_after: str | None = None, enabled: bool | None = None,
              reason: str | None = None, allow: list[str] | None = None, max_steps: int | None = None,
              max_wait_seconds: int | None = None) -> str:
    """Add a video, or edit it if `name` exists (fields you leave out are kept; hints=[] removes hints).
    goal = what "finished" looks like, checkable on screen — Jev plans the clicks from it. hints = optional steps:
    **Label** for exact button text, 'text' for which row/section. start_path = URL path to open (e.g. /login).
    start_after = video whose signed-in session to reuse. enabled=false + reason = documented from the reference
    doc only (e.g. sends real emails or payments). allow = forbid_labels this one video may click."""
    _project(project)
    _name(name)
    old = _find("videos", name) or {}
    if not old and not goal: raise ToolError("a new video needs a goal")
    if start_after and (start_after == name or not _find("videos", start_after)):
        raise ToolError(f"start_after: no other video named '{start_after}'")
    new = dict(old, name=name)
    args = locals()
    for k in VIDEO_FIELDS:
        if args[k] is not None: new[k] = args[k]
    if not new.get("hints"): new.pop("hints", None)
    words = (new["goal"] + " " + " ".join(new.get("hints", []))).lower()
    allowed = {a.lower() for a in new.get("allow", [])}
    bad = [f for f in agent.CFG.get("forbid_labels", []) if f.lower() in words and f.lower() not in allowed]
    _backup()
    agent.save_entry("videos", new)
    return (f"{'Updated' if old else 'Added'} video '{name}'."
            + (f" Warning: mentions forbidden label(s) {bad}; Jev will not click them unless listed in allow." if bad else ""))


@mcp.tool()
def remove_video(project: str, name: str) -> str:
    """Delete a video from guides.yaml (its recording on disk is kept). Refused while a doc or video depends on it."""
    _project(project)
    users = [d["name"] for d in agent.CFG["docs"] if name in d.get("videos", [])] + \
            [v["name"] for v in agent.CFG["videos"] if v.get("start_after") == name]
    if users: raise ToolError(f"still used by {users}; change those first")
    _backup()
    try:
        agent.delete_entry("videos", name)
    except KeyError as e:
        raise ToolError(str(e))
    return f"Removed video '{name}'."



# ---------------- recording runs (background jobs) ----------------
@mcp.tool()
def run_videos(project: str, videos: list[str] | None = None, profile: str | None = None) -> str:
    """Record videos in a headless browser — all enabled ones, or just `videos` (their start_after chain is added
    so they start signed in). profile = which test account to sign in with (see get_credentials; default .env).
    Runs in the background: poll job_status(job_id) about once a minute.
    Uses the Jev key (OPENROUTER_API_KEY or TYPESAFE_API_KEY) — run check_project first."""
    _project(project, profile)
    missing = [var for val in agent.CFG.get("test_data", {}).values()
               for var in re.findall(r"\$\{(\w+)\}", str(val)) if not os.environ.get(var)]
    if missing: raise ToolError(f"credentials not set for profile '{agent.PROFILE}': {missing} — use set_credentials")
    if any(j["proc"].poll() is None and j["project"] == project for j in JOBS.values()):
        raise ToolError(f"a run for '{project}' is already going; see job_status")
    by = {v["name"]: v for v in agent.CFG["videos"]}
    unknown = [v for v in videos or [] if v not in by]
    if unknown: raise ToolError(f"unknown video(s): {unknown}")
    want = set(videos or [])
    for n in list(want):  # pull in start_after chains so sessions exist
        while by[n].get("start_after") in by and by[n]["start_after"] not in want:
            n = by[n]["start_after"]; want.add(n)
    order = [v["name"] for v in agent.order_videos(agent.CFG["videos"])
             if v.get("enabled", True) and (not want or v["name"] in want)]
    if not order: raise ToolError("nothing to record (no enabled videos)")
    job = time.strftime("%H%M%S") + f"-{project}"
    agent.OUT.mkdir(parents=True, exist_ok=True)
    log = agent.OUT / f"run_{job}.log"
    # only the real environment (docker -e / --env-file) is passed down; the run loads its own credential files
    env = {k: v for k, v in os.environ.items() if k not in agent._FROM_FILES}
    env.update(HEADLESS=os.environ.get("HEADLESS", "1"), PYTHONUNBUFFERED="1")
    with log.open("w") as fh:
        proc = subprocess.Popen([sys.executable, "-u", str(agent.ROOT / "agent.py"), "videos", "--project", project,
                                 "--profile", agent.PROFILE, "--only", ",".join(order)], cwd=agent.ROOT, env=env,
                                stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    JOBS[job] = {"proc": proc, "log": log, "project": project, "videos": order, "started": time.time(),
                 "profile": agent.PROFILE}
    return (f"Started job {job} as profile '{agent.PROFILE}': {', '.join(order)}. "
            f"Check job_status('{job}') in a minute.")


@mcp.tool()
def job_status(job_id: str | None = None) -> str:
    """Progress of a recording run (latest job if no id): finished videos, the current one, last log lines."""
    if not JOBS: return "No runs started since the server started."
    job_id = job_id or list(JOBS)[-1]
    j = JOBS.get(job_id)
    if not j: raise ToolError(f"no job {job_id}; jobs: {list(JOBS)}")
    text = j["log"].read_text(errors="replace") if j["log"].exists() else ""
    done = re.findall(r"^  [✓⚠] (\S+): (.+)$", text, re.M)
    current = re.findall(r"^🎥 (\S+)", text, re.M)
    rc = j["proc"].poll()
    state = "running" if rc is None else ("finished" if rc == 0 else f"failed (exit {rc})")
    lines = [f"Job {job_id} [{j['project']} as {j.get('profile', 'default')}]: {state} · {int(time.time() - j['started'])}s · "
             f"{len(done)}/{len(j['videos'])} videos"]
    lines += [f"  {'✓' if r.startswith('done') else '⚠'} {n}: {r}" for n, r in done]
    if rc is None and current and current[-1] not in {n for n, _ in done}: lines.append(f"  … recording {current[-1]}")
    lines += ["", "Last log lines:", *text.strip().splitlines()[-12:]]
    if rc is not None: lines.append("\nNext: get_recording(name) for anything not done, then write docs.")
    return "\n".join(lines)


@mcp.tool()
def stop_job(job_id: str) -> str:
    """Stop a running recording job."""
    j = JOBS.get(job_id)
    if not j: raise ToolError(f"no job {job_id}")
    if j["proc"].poll() is not None: return f"{job_id} had already ended."
    os.killpg(j["proc"].pid, signal.SIGTERM)
    return f"Stopped {job_id}."



@mcp.tool()
def get_recording(project: str, name: str, screenshots: int = 4) -> list:
    """What happened in a recorded video: result, every action, coach tips, (for failures) Jev's last decisions
    and the last screen, plus up to `screenshots` images spread from first to final. Use it to fix goals/hints."""
    _project(project)
    folder = agent.OUT / "videos" / name
    if not (folder / "actions.json").exists(): raise ToolError(f"'{name}' has not been recorded yet")
    rec = json.loads((folder / "actions.json").read_text())
    text = [f"Video '{name}' — goal: {rec['goal']}",
            f"Result: {rec['status']} · {len(rec['steps']) - 1} actions · waited {rec['waited_seconds']}s"
            + (f" · downloads {rec['downloads']}" if rec["downloads"] else ""), "", "Steps:"]
    text += [f"  {s['shot']}: {s['action']}" for s in rec["steps"]]
    if rec.get("coach"):
        text += ["", "Coach tips:"] + [f"  step {c['step']} ({c['why']}): {c['tip']}" for c in rec["coach"]]
    if rec["status"] != "done":
        text += ["", "Last decisions:"] + [f"  {json.dumps(d, ensure_ascii=False)}" for d in rec.get("decisions", [])[-5:]]
        ls = rec.get("last_screen", {})
        text += ["", f"Last screen ({ls.get('url', '')}):", _clip(ls.get("page_text", ""), 2500)]
    out = ["\n".join(text)]
    shots = [s["shot"] for s in rec["steps"]] + ["final.png"]
    n = max(0, min(screenshots, 12, len(shots)))
    for i in sorted({round(i * (len(shots) - 1) / max(n - 1, 1)) for i in range(n)}):
        if (folder / shots[i]).exists(): out += [f"{shots[i]}:", Image(path=folder / shots[i])]
    return out


# ---------------- docs ----------------
@mcp.tool()
def add_doc(project: str, name: str, videos: list[str], type: str = "how-to guide", audience: str = "all users",
            sections: list[str] | None = None) -> str:
    """Add or replace a doc definition: which recordings it is built from, its type (how-to guide, quick-start,
    FAQ, ...) and audience. sections = reference doc headings it should cover."""
    _project(project)
    _name(name)
    unknown = [v for v in videos if not _find("videos", v)]
    if unknown: raise ToolError(f"unknown video(s): {unknown}")
    entry = {"name": name, "type": type, "audience": audience, "sections": sections or None, "videos": videos}
    _backup()
    agent.save_entry("docs", entry)
    return f"Saved doc '{name}'. Next: get_doc_brief('{project}', '{name}'), write it, save_doc."


@mcp.tool()
def remove_doc(project: str, name: str) -> str:
    """Delete a doc definition (an already written out/docs/<name>.md is kept)."""
    _project(project)
    _backup()
    try:
        agent.delete_entry("docs", name)
    except KeyError as e:
        raise ToolError(str(e))
    return f"Removed doc '{name}'."


@mcp.tool()
def get_doc_brief(project: str, name: str, max_images: int = 12, include_reference: bool = True) -> list:
    """Everything needed to write a doc: instructions, the reference doc text, each recording's steps and the most
    useful screenshots. Write the Markdown yourself, then save it with save_doc."""
    _project(project)
    d = _find("docs", name)
    if not d: raise ToolError(f"no doc '{name}'; docs: {[x['name'] for x in agent.CFG['docs']]}")
    out = [f"DOC '{name}'. {agent.doc_instructions(d)}\nThe doc is saved in out/docs/, so links look like "
           "../videos/<recording>/step_3.png — use only screenshots listed in this brief."]
    ref = agent.reference_text() if include_reference else ""
    if ref: out.append("REFERENCE DOCUMENT (keep its terminology and button names):\n" + _clip(ref, 20000))
    shots = []
    for v in d.get("videos", []):
        f = agent.OUT / "videos" / v / "actions.json"
        if not f.exists():
            out.append(f"Recording '{v}': not recorded — use the reference document only."); continue
        rec = json.loads(f.read_text())
        out.append(f"## Recording '{v}' — goal: {rec['goal']} — result: {rec['status']}\n"
                   + "\n".join(f"{v}/{s['shot']}: {s['action']}" for s in rec["steps"]))
        shots += [(v, s) for s in rec["steps"][1:]]  # step_N shows the screen after action N-1
    step = -(-len(shots) // max_images) if max_images > 0 else 0
    for i, (v, s) in enumerate(shots):
        if step and i % step == 0:
            out += [f"{v}/{s['shot']} — after: {s['action']}", Image(path=agent.OUT / "videos" / v / s["shot"])]
    return out


@mcp.tool()
def save_doc(project: str, name: str, markdown: str) -> str:
    """Save a written doc to out/docs/<name>.md (previous version kept as <name>.md.bak) and check that every
    linked screenshot/video exists."""
    _project(project)
    _name(name)
    docs = agent.OUT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / f"{name}.md"
    if path.exists(): shutil.copy(path, path.with_suffix(".md.bak"))
    path.write_text(markdown)
    links = re.findall(r"\]\((\.\./videos/[^)\s]+)\)", markdown)
    missing = sorted({l for l in links if not (docs / l).resolve().exists()})
    return f"Saved {path} ({len(markdown)} chars, {len(links)} media links)." + (
        f" Broken links — fix and save again: {missing}" if missing else "")


@mcp.tool()
def export_project(project: str, docs: list[str] | None = None, mp4: bool = False) -> str:
    """Package written docs for download/sharing: one self-contained HTML guide (screenshots embedded; open in a
    browser, Print → Save as PDF for a PDF) and a zip with every doc (.md + .html) plus the screenshots and videos
    they use. docs = only these (default all). mp4 = also convert the videos to .mp4 (slower; for PowerPoint/wikis).
    Returns file paths on the user's computer — give them to the user as-is."""
    import exporter
    _project(project)
    host_root = os.environ.get("HOST_PROJECTS_DIR")  # set in the Claude Desktop config so paths open on the Mac
    host_out = str(os.path.join(host_root, project, os.path.relpath(agent.OUT, agent.CFG_PATH.parent))) if host_root else None
    try:
        r = exporter.export_project(agent.OUT, project, docs, mp4, host_out)
    except (FileNotFoundError, RuntimeError) as e:
        raise ToolError(f"{e}. Write docs first (get_doc_brief → save_doc)." if isinstance(e, FileNotFoundError) else str(e))
    return (f"Exported {len(r['docs'])} doc(s): {', '.join(r['docs'])}\n"
            f"• One-page guide ({r['html_mb']} MB): {r['html']}\n"
            f"• Everything, zipped ({r['zip_mb']} MB, {r['media_files']} media files, {r['videos']} videos"
            f"{', as .mp4' if r['mp4'] else ''}): {r['zip']}\n"
            f"• Folder: {r['folder']}"
            + (f"\n⚠ Broken links (not included): {r['missing_links']}" if r["missing_links"] else "")
            + ("" if host_root else "\n(Paths are inside the container; on the computer they are under the mounted "
                                    "projects folder.)"))


@mcp.tool()
def get_doc(project: str, name: str) -> str:
    """Read a written doc (out/docs/<name>.md) to review or edit it; save changes with save_doc."""
    _project(project)
    path = agent.OUT / "docs" / f"{name}.md"
    if not path.exists(): raise ToolError(f"no written doc '{name}'")
    return path.read_text()


if __name__ == "__main__":
    agent.load_env()
    mcp.run()  # stdio

