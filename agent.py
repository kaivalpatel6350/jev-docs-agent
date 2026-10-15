"""
agent.py — runs everything declared in guides.yaml.

  Video agent (Jev + Playwright): reaches each goal in a real browser, records video + screenshots.
  Docs agent  (Claude):           turns recordings + your reference doc into end-user documentation.

Setup:
    pip install -r requirements.txt && playwright install chromium
    projects/<name>/guides.yaml + projects/<name>/.env (login for that app); shared keys in ./.env
    Claude Desktop can drive all of this through mcp_server.py (see README).

Commands (add --project <name> when there is more than one project):
    python agent.py plan                    # check config, show run order + cost (no browser, no API)
    python agent.py videos [--only a,b]     # record (all enabled videos, or just a,b)
    python agent.py docs   [--only a,b]     # write docs from existing recordings
    python agent.py docs --dry              # build Claude requests without calling the API
    python agent.py all                     # videos, then docs
    python agent.py improve [--only a,b]    # let Claude fix hints of failed videos and re-record (max 3 rounds)
    python agent.py export [--only a,b] [--mp4]   # one HTML guide + zip in out/exports/ for sharing

Output (inside projects/<name>/):
    out/videos/<name>/  video.webm, step_N.png, actions.json, downloads/
    out/docs/<name>.md
    out/run_report.md
"""
import argparse, base64, json, os, re, shutil, sys, time
from pathlib import Path

import yaml
from dotenv import dotenv_values

# One folder per app under test: projects/<name>/guides.yaml (+ .env, out/). Filled in by load().
ROOT = Path(__file__).resolve().parent
PROJECTS = Path(os.environ.get("PROJECTS_DIR", ROOT / "projects"))
CFG_PATH, CFG, S, OUT = None, {}, {}, ROOT / "out"
OPENROUTER_KEY = ""


def project_config(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or ""):
        raise ValueError(f"project name must be lowercase letters, digits, - or _: {name!r}")
    return PROJECTS / name / "guides.yaml"


_REAL_ENV, _FROM_FILES = set(os.environ), set()
PROFILE = "default"   # which credential set (test login) is loaded


def env_file(project_dir, profile=None):
    """A project's credentials: .env for the default profile, .env.<profile> for others (e.g. .env.admin)."""
    if profile in (None, "", "default"): return Path(project_dir) / ".env"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile):
        raise ValueError(f"profile must be lowercase letters, digits, - or _: {profile!r}")
    return Path(project_dir) / f".env.{profile}"


def profiles(project_dir):
    """Credential profiles a project has on disk."""
    d = Path(project_dir)
    return (["default"] if (d / ".env").exists() else []) + sorted(
        p.name[5:] for p in d.glob(".env.*") if p.name[5:] not in ("example", "bak") and not p.name.endswith(".bak"))


def load_env(project_dir=None, profile=None):
    """Credentials + keys, highest first: real environment (docker -e / --env-file, shell) > projects/<name>/.env.<profile>
    > projects/<name>/.env > .env next to agent.py. Switching project or profile drops the previous file values,
    so one project's login never leaks into another."""
    global OPENROUTER_KEY, PROFILE
    for k in _FROM_FILES: os.environ.pop(k, None)
    _FROM_FILES.clear()
    files = dotenv_values(ROOT / ".env", interpolate=False)
    if project_dir:
        files.update(dotenv_values(Path(project_dir) / ".env", interpolate=False))
        if profile not in (None, "", "default"):
            f = env_file(project_dir, profile)
            if not f.exists(): raise FileNotFoundError(f"no credentials profile '{profile}' ({f})")
            files.update(dotenv_values(f, interpolate=False))
    for k, v in files.items():
        if v is not None and k not in _REAL_ENV:
            os.environ[k] = v; _FROM_FILES.add(k)
    PROFILE = profile or "default"
    OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()


def use_project(name, profile=None):
    """Load projects/<name>: its credentials (default or `profile`), then its guides.yaml."""
    path = project_config(name)
    if not path.exists(): raise FileNotFoundError(f"no project '{name}' ({path})")
    load_env(path.parent, profile)
    return load(path)


def load(path):
    """Load a guides.yaml. Its relative paths (upload_file, reference_doc, output_dir) are relative to the file."""
    global CFG_PATH, CFG, S, OUT
    CFG_PATH = Path(path).resolve()
    CFG = yaml.safe_load(CFG_PATH.read_text()) or {}
    CFG["videos"], CFG["docs"] = CFG.get("videos") or [], CFG.get("docs") or []
    for k in ("upload_file", "reference_doc"):
        if CFG.get(k): CFG[k] = str(CFG_PATH.parent / CFG[k])
    OUT = CFG_PATH.parent / CFG.get("output_dir", "out")
    S = CFG.setdefault("settings", {}) or {}
    CFG["settings"] = S
    if os.environ.get("HEADLESS"): S["headless"] = True  # containers have no screen
    return CFG
TEXT_TYPES = {"", "text", "email", "password", "search", "number", "tel", "url", "date", "datetime-local", "textarea"}

# The popup/dialog covering the page, if any: an element with a dialog role, or — when a full-screen backdrop
# greys out the page — the fixed layer under the screen centre. Must hold < 60% of the page text (so an app shell
# that happens to be position:fixed never counts).
FIND_POPUP = r"""() => {
  // on screen = at least half of it inside the window (slid-away drawers and closed panels don't count)
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0), h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    return r.width > 0 && r.height > 0 && w > r.width * 0.5 && h > 0 && s.visibility !== 'hidden' && +s.opacity > 0.1; };
  const fixedAt = (x, y, full) => { let hit = null;
    for (let n = document.elementFromPoint(x, y); n && n !== document.body; n = n.parentElement) {
      const r = n.getBoundingClientRect();
      if (getComputedStyle(n).position === 'fixed' && (!full || (r.width >= innerWidth * 0.95 && r.height >= innerHeight * 0.95))) hit = n; }
    return hit; };
  let d = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]')].filter(vis).pop();
  if (!d && (fixedAt(3, innerHeight - 3, true) || fixedAt(innerWidth - 3, innerHeight - 3, true)))
    d = fixedAt(innerWidth / 2, innerHeight / 2, false);
  return d && d.innerText.trim() && d.innerText.length < document.body.innerText.length * 0.6 ? d : null;
}"""

# Tags every interactive element with an id, plus the context Jev needs to tell
# "Edit (Billing address)" from "Edit (Shipping address)" or one table row's "View" button from another's.
COLLECT_JS = r"""() => {
  const q = 'a,button,input,select,textarea,[contenteditable=true],[role=button],[role=link],[role=tab],[role=checkbox],[role=switch],[role=option],[role=menuitem]';
  const visible = e => e.type === 'file' || !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const section = e => { for (let n = e.parentElement, i = 0; n && i < 8; n = n.parentElement, i++) {
      const h = n.querySelector(':scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > legend,:scope > header');
      if (h && !h.contains(e)) return h.innerText.trim().replace(/\s+/g, ' ').slice(0, 50); } return ''; };
  // While a popup is open, elements behind it can't really be clicked — list only what is inside the popup.
  const pop = (""" + FIND_POPUP + r""")();
  const all = [...document.querySelectorAll(q)].filter(e => visible(e) && !e.disabled);
  const inPop = pop ? all.filter(e => pop.contains(e)) : [];
  document.querySelectorAll('[data-jev-id]').forEach(e => e.removeAttribute('data-jev-id'));
  return (inPop.length ? inPop : all).slice(0, 240).map((e, i) => {
    e.setAttribute('data-jev-id', 'el' + i);
    const own = l => [...l.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim() || l.innerText;
    const label = (e.labels && e.labels[0] && own(e.labels[0])) || e.getAttribute('aria-label') || e.title ||
                  e.innerText || e.placeholder || e.name || (['submit', 'button'].includes(e.type) ? e.value : '') || '';
    const row = e.closest('tr,li,[role=row]');
    const near = e.parentElement ? [...e.parentElement.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim().slice(0, 40) : '';
    return { id: 'el' + i, tag: e.tagName.toLowerCase(), type: e.isContentEditable ? 'editor' : (e.type || e.getAttribute('role') || ''),
             label: label.trim().replace(/\s+/g, ' ').slice(0, 80), section: section(e),
             near, row: row ? row.innerText.trim().replace(/\s+/g, ' ').slice(0, 70) : '',
             checked: ('checked' in e && ['checkbox', 'radio'].includes(e.type)) ? e.checked : null,
             filled: e.isContentEditable ? !!e.innerText.trim() : (typeof e.value === 'string' ? !!e.value : null),
             text: (typeof e.value === 'string' && e.type !== 'password' && e.tagName !== 'BUTTON') ? e.value.trim().slice(0, 60) : '',
             options: e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim()).slice(0, 25) : null };
  });
}"""

# What a person would read: the popup on top (if any), and the whole page.
VIEW_JS = r"""() => {
  const clean = s => (s || '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  const d = (""" + FIND_POPUP + r""")();
  return { popup: d ? clean(d.innerText) : '', text: clean(document.body.innerText) };
}"""


# Visible mouse pointer for the recordings (Playwright's video doesn't capture the OS cursor).
# Follows real mouse events, survives page navigations, and shows a ripple on each click.
CURSOR_JS = r"""(() => {
  if (window.__docsCursor) return; window.__docsCursor = true;
  const KEY = '__docsCursorPos';
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 24 24">' +
              '<path d="M3 2l7.5 19 2.7-7.8L21 10.5z" fill="#111" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const add = () => {
    const c = document.createElement('div');
    c.setAttribute('aria-hidden', 'true');
    c.style.cssText = 'position:fixed;left:0;top:0;margin:-2px 0 0 -3px;width:26px;height:26px;' +
                      'z-index:2147483647;pointer-events:none;display:none';
    c.innerHTML = svg;
    document.documentElement.appendChild(c);
    const put = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; c.style.display = 'block'; };
    try { const p = JSON.parse(sessionStorage.getItem(KEY) || 'null'); if (p) put(p[0], p[1]); } catch (e) {}
    addEventListener('mousemove', e => {
      put(e.clientX, e.clientY);
      try { sessionStorage.setItem(KEY, JSON.stringify([e.clientX, e.clientY])); } catch (err) {}
    }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div');
      r.setAttribute('aria-hidden', 'true');
      r.style.cssText = `position:fixed;left:${e.clientX - 14}px;top:${e.clientY - 14}px;width:28px;height:28px;` +
        'border-radius:50%;background:rgba(8,145,178,.35);z-index:2147483646;pointer-events:none;' +
        'transition:transform .45s ease-out,opacity .45s ease-out';
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(2)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 500);
    }, true);
  };
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', add) : add();
})()"""


# ---------- helpers ----------
def point_at(page, loc):
    """Glide the visible pointer to the element so viewers can follow each step."""
    if not S.get("show_cursor", True): return
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
        b = loc.bounding_box()
        if b and b["width"] and b["height"]:  # hidden inputs (e.g. file pickers) have no box
            page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, steps=S.get("cursor_steps", 12))
    except Exception: pass


def test_data():
    return {k: os.path.expandvars(str(v)) for k, v in CFG.get("test_data", {}).items()}


def is_text_field(el):
    return el["type"] in ("editor",) or (el["tag"] in ("input", "textarea") and (el["type"] or "text") in TEXT_TYPES)


def describe(el):
    d = f'{el["tag"]}{"/" + el["type"] if el["type"] else ""} "{el["label"]}"'
    if el["section"]: d += f' · section "{el["section"]}"'
    if el.get("near") and el["near"] != el["label"]: d += f' · next to "{el["near"]}"'
    if el["row"] and el["row"] != el["label"]: d += f' · row "{el["row"]}"'
    if el["checked"] is not None: d += " (checked)" if el["checked"] else " (unchecked)"
    if is_text_field(el):
        d += f' (has text "{el["text"]}")' if el.get("text") else (" (has text)" if el["filled"] else " (empty)")
    return d


def build_targets(els, forbid):
    """Every clickable thing becomes one Jev choice; each <select> option becomes its own choice."""
    t = {}
    for e in els:
        if any(f in e["label"].lower() for f in forbid):
            continue
        if e["options"]:
            for j, o in enumerate(e["options"]):
                t[f'{e["id"]}:{j}'] = f'{describe(e)} → choose option "{o}"'
        else:
            t[e["id"]] = describe(e)
    t = dict(list(t.items())[:253])  # Jev Choice cap is 255
    t["wait"] = "Nothing to do yet: the page is loading, uploading, generating or still processing"
    return t


def build_values(data):
    v = {k: (f"{k} (the account password)" if "pass" in k else f'{k}: "{val}"') for k, val in data.items()}
    v["none"] = "The next element is not a text field"
    return v


def order_videos(videos):
    """Videos that depend on another (start_after) run after it."""
    by, done, out = {v["name"]: v for v in videos}, set(), []
    def visit(v):
        if v["name"] in done: return
        if v.get("start_after") in by: visit(by[v["start_after"]])
        done.add(v["name"]); out.append(v)
    for v in videos: visit(v)
    return out


def settle(page, ms=8000):
    try: page.wait_for_load_state("networkidle", timeout=ms)
    except Exception: pass  # apps with live polling never go idle


# ---------- the Jev decision (one call per step, several questions) ----------
# Besides "what next?", Jev checks itself every step: where am I in the plan, and did my last action work?
LAST_STEP = {
    "worked": "It worked: the screen moved toward the goal",
    "nothing": "Nothing useful changed",
    "wrong": "It went the wrong way (wrong page, wrong popup, wrong field or wrong text)",
    "none": "No step has been taken yet",
}
_jev = None
def jev_decide(state, targets, values):
    global _jev
    from typesafe_sdk import Choice, Noul, TypeSafeClient
    if _jev is None:  # one OpenRouter key can run Jev and Claude; otherwise use TypeSafe directly
        _jev = TypeSafeClient(api_key=OPENROUTER_KEY, base_url="https://openrouter.ai/api") \
            if OPENROUTER_KEY and not os.environ.get("TYPESAFE_API_KEY") else TypeSafeClient()
    stages = {f"step_{i + 1}": h for i, h in enumerate(state["expected_steps"][:200])}
    stages["other"] = "None of the expected steps fits what is on screen"
    a = _jev.system_one(model=S.get("jev_model", "jev-latest"), state=state, questions={
        "done": Noul(instructions="Is the goal fully completed right now? Check the goal's finish condition against "
                                  "the popup and page_text on screen, not against steps_taken alone."),
        "stage": Choice(instructions="Which expected step should be worked on now, judging by the screen and steps_taken?",
                        criteria=stages),
        "last_step": Choice(instructions="Look at the last entry of steps_taken and the screen now. How did that step go?",
                            criteria=LAST_STEP),
        "target": Choice(instructions="Which element should be used next to carry out the current expected step? "
                                      "If a popup is open, only its elements are listed. Follow notes and coach_tip "
                                      "when present. Avoid elements marked 'did nothing'.", criteria=targets),
        "value": Choice(instructions="If the next element is a text field, which value should be typed into it?",
                        criteria=values),
    }).answers
    return {"done": a["done"].noul, "target": a["target"].choice,
            "confidence": a["target"].confidence, "value": a["value"].choice,
            "stage": a["stage"].choice, "last_step": a["last_step"].choice, "last_conf": a["last_step"].confidence}


# ---------- the coach (Claude looks at the screen when Jev is stuck) ----------
_coach = None
def coach(page, state, targets, values, forbid):
    """One short instruction for the next move, or DONE if the finish condition is already on screen."""
    global _coach
    _coach = _coach or claude_client()
    shot = base64.b64encode(page.screenshot()).decode()
    lines = lambda xs: "\n".join(f"- {x}" for x in xs) or "- (none)"
    secret = {k for k, raw in CFG.get("test_data", {}).items() if "${" in str(raw)}  # .env values: name only
    values = {k: (f"{k} (from .env)" if k in secret else val) for k, val in values.items()}
    text = (
        "You coach a browser agent that is recording a walkthrough video of a web app. It is stuck. Look at the "
        f"screenshot and tell it the single next move.\n\nGOAL: {state['goal']}\n\nEXPECTED STEPS:\n"
        f"{lines(state['expected_steps'])}\n\nSTEPS TAKEN (with what happened):\n{lines(state['steps_taken'][-15:])}\n\n"
        f"NOTES:\n{lines(state.get('notes', []))}\n\nELEMENTS IT CAN USE NOW:\n{lines(list(targets.values())[:150])}\n\n"
        f"VALUES IT CAN TYPE (it cannot type anything else):\n{lines(v for k, v in values.items() if k != 'none')}\n\n"
        "Reply with ONE instruction of at most two sentences that names the exact element (and the value to type, "
        "if any). If the text it must type is not in the list, put it on a last line as  TYPE: <exact text>  "
        "(demo data only, never passwords). If the goal's finish condition is already met on screen, reply exactly DONE. "
        f"Never suggest: {', '.join(forbid)}.")
    return _coach([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": shot}},
                   {"type": "text", "text": text}]).strip()


DECIDE = jev_decide  # swap in a stand-in (same signature) for offline tests


# ---------- VIDEO AGENT ----------
def record_video(p, v, app_url=None):
    app_url = app_url or CFG["app_url"]
    out = OUT / "videos" / v["name"]
    shutil.rmtree(out, ignore_errors=True)
    (out / "downloads").mkdir(parents=True)
    data, forbid = test_data(), [f.lower() for f in CFG.get("forbid_labels", []) if f not in v.get("allow", [])]
    values = build_values(data)
    prev = v.get("start_after")
    state_file = OUT / "videos" / prev / "session.json" if prev else None
    start = app_url + v.get("start_path", "")
    if prev and "start_path" not in v:
        # Continue where the previous video ended (e.g. the page after sign-in). Opening the app root instead
        # shows the login form on apps that don't redirect signed-in users, so Jev would log in again.
        f = OUT / "videos" / prev / "actions.json"
        last = json.loads(f.read_text()).get("last_screen", {}).get("url", "") if f.exists() else ""
        if last.startswith(app_url.rstrip("/")): start = last

    browser = p.chromium.launch(headless=S.get("headless", False), slow_mo=S.get("slow_mo", 400))
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800}, accept_downloads=True,
        record_video_dir=str(out), record_video_size={"width": 1280, "height": 800},
        storage_state=str(state_file) if state_file and state_file.exists() else None)
    if S.get("show_cursor", True): ctx.add_init_script(CURSOR_JS)
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    page.on("download", lambda d: d.save_as(out / "downloads" / d.suggested_filename))
    page.goto(start)
    settle(page)
    page.screenshot(path=out / "step_1.png")
    steps, history, log = [{"shot": "step_1.png", "action": "Open the app"}], [], []
    waited, status = 0, "max_steps"
    state, targets = {}, {}
    max_wait = v.get("max_wait_seconds", S.get("max_wait_seconds", 120))
    poll = S.get("wait_poll_seconds", 5)

    # Self-checking loop. Every step Jev also answers: which expected step am I on, and did my last action work?
    # Actions that changed nothing or went the wrong way are marked, so Jev avoids them. When Jev is stuck
    # (unsure, repeating, failing twice, retrying a failed element, or waiting on a frozen screen) the coach
    # looks at a screenshot and gives one concrete instruction; Jev then carries on by itself.
    notes, tip, tried = [], "", {}           # tried: element description -> what happened when it was used
    bad_streak, last_view, still_since = 0, "", 0
    coach_max = S.get("coach_max_calls", 6) if DECIDE is jev_decide else 0   # no coach in offline rehearsal
    coach_log, coached, stall_asked = [], False, False
    min_conf, done_at = S.get("min_confidence", 0.5), S.get("done_threshold", 0.8)

    for _ in range(v.get("max_steps", S.get("max_steps", 30)) + coach_max):  # coach turns don't eat the budget
        settle(page, 3000)
        els = page.evaluate(COLLECT_JS)
        targets = build_targets(els, forbid)
        for k, desc in targets.items():  # show Jev what already failed
            if desc in tried: targets[k] = f"{desc} — tried before: {tried[desc]}"
        view = page.evaluate(VIEW_JS)
        state = {"goal": v["goal"], "expected_steps": v.get("hints", []), "steps_taken": history[-25:],
                 "notes": notes[-8:], "coach_tip": tip, "url": page.url,
                 "popup": view["popup"][:1500], "page_text": view["text"][:3500]}
        d = DECIDE(state, targets, values)
        log.append({"step": len(steps), "done": round(d["done"], 3), "target": d["target"],
                    "target_desc": targets.get(d["target"], ""), "confidence": round(d["confidence"], 3),
                    "value": d["value"], "stage": d.get("stage"), "last_step": d.get("last_step"),
                    **{k: d[k] for k in d if k.startswith("_")}})

        # 1. did the last action work? (Jev's judgement — catches clicks that "worked" but did the wrong thing)
        if d.get("last_step") in ("nothing", "wrong") and len(steps) > 1 and d.get("last_conf", 0) >= 0.5:
            verdict = "did nothing useful" if d["last_step"] == "nothing" else "went the wrong way"
            if not history[-1].endswith(")"): history[-1] += f"  ({verdict})"
            tried.setdefault(steps[-1]["desc"], verdict)
            bad_streak += 1
        elif d.get("last_step") == "worked":
            bad_streak = 0

        if d["done"] >= done_at:
            if len(steps) > 1 or v.get("allow_no_actions"):
                status = "done"; break
            # Nothing done yet: the start screen already matches the goal (e.g. "login page shown" on the login
            # page), which would record an empty video. Tell Jev to perform the task instead.
            if not notes or not notes[-1].startswith("No action yet"):
                notes.append("No action yet: the start screen only looks like the finish. Do the task the goal "
                             "describes from here, step by step.")

        # 2. is Jev stuck? → ask the coach, then let Jev decide again with the tip
        waiting = d["target"] == "wait"
        if view["text"] != last_view: last_view, still_since = view["text"], waited
        pick = "" if waiting else targets.get(d["target"], "").split(" — tried before: ")[0]
        stuck = ("unsure" if d["confidence"] < min_conf else
                 "repeating" if pick and len(steps) > 2 and steps[-1].get("desc") == steps[-2].get("desc") == pick else
                 "failing" if bad_streak >= 2 else
                 "retrying a failed element" if " — tried before: " in targets.get(d["target"], "") else
                 "waiting, screen unchanged" if waiting and not stall_asked
                                                 and waited - still_since >= S.get("stall_seconds", 45) else "")
        if coached and not stuck.startswith("waiting"):
            stuck = ""  # the coach just spoke: act on its suggestion even if Jev is unsure or it was tried before
        coached = False
        if stuck.startswith("waiting"):
            stall_asked = True  # ask about a frozen screen once; after that long jobs just wait up to max_wait
            if len(coach_log) >= coach_max: stuck = ""
        if not waiting: stall_asked = False
        if stuck:
            if len(coach_log) < coach_max:
                try: tip = coach(page, state, targets, values, forbid)
                except Exception as e: tip = f"(coach unavailable: {type(e).__name__})"
                m = re.search(r"^\s*TYPE:\s*(.+?)\s*$", tip, re.M)
                if m:  # the coach can hand Jev text that isn't in test_data (e.g. a new provider name)
                    data["coach_text"] = m.group(1).strip("\"'")
                    values["coach_text"] = f'coach_text: "{data["coach_text"]}" (suggested by the coach)'
                    tip = tip[:m.start()].strip() + f' (type the coach_text value: "{data["coach_text"]}")'
                coach_log.append({"step": len(steps), "why": stuck, "tip": tip})
                print(f"    🧭 coach ({stuck}): {tip[:160]}")
                if tip.upper().startswith("DONE"):
                    status = "done"; break
                notes.append(f"Coach after step {len(steps)}: {tip}")
                bad_streak, still_since, coached = 0, waited, True
                continue
            status = {"unsure": "unsure", "waiting, screen unchanged": "timeout"}.get(stuck, "loop"); break

        if waiting:
            if waited >= max_wait: status = "timeout"; break
            time.sleep(poll); waited += poll; continue

        eid, _, opt = d["target"].partition(":")
        el = next(e for e in els if e["id"] == eid)
        desc = targets[d["target"]].split(" — tried before: ")[0]
        loc = page.locator(f'[data-jev-id="{eid}"]')
        point_at(page, loc)
        if not loc.count():  # page re-rendered while the pointer moved → look again instead of clicking blind
            continue
        name = el["label"] or el["section"] or el["type"]
        before = view["text"]
        try:
            if el["type"] == "file":
                loc.set_input_files(CFG["upload_file"])
                action = f'Upload {Path(CFG["upload_file"]).name}'
            elif opt:
                loc.select_option(label=el["options"][int(opt)], timeout=10000)
                action = f'Choose "{el["options"][int(opt)]}" in "{name}"'
            elif is_text_field(el) and d["value"] != "none":
                key = d["value"]
                loc.fill(data.get(key, ""), timeout=10000)
                secret = "${" in str(CFG.get("test_data", {}).get(key, ""))  # values from .env never go in logs
                # name the value that was typed (never the value itself) so a wrong pick shows up in the log
                shown = f"the {key}" if (secret or "pass" in key or el["type"] == "password") else repr(data.get(key, ""))
                action = f'Type {shown} into "{name}"'
            else:
                loc.click(timeout=10000)
                ctx_txt = f' (row: {el["row"][:40]})' if el["row"] and el["row"] != el["label"] else (
                          f' in "{el["section"]}"' if el["section"] else "")
                action = f'Click "{name}"{ctx_txt}'
        except Exception:  # disabled or covered element: remember it and look again
            history.append(f'Could not use "{name}" (disabled or covered)')
            tried[desc] = "could not be used (disabled or covered)"
            bad_streak += 1
            continue
        settle(page)
        page.wait_for_timeout(700)  # popups/animations often draw just after the network settles
        note = "  (nothing changed on screen)" if not is_text_field(el) and page.evaluate(VIEW_JS)["text"] == before else ""
        history.append(action + note)
        shot = f"step_{len(steps) + 1}.png"
        page.screenshot(path=out / shot)
        steps.append({"shot": shot, "action": action, "desc": desc})
        print(f"    → {action}{note}")

    # Last look before calling it a failure: Jev is sometimes unsure about "done" when the goal is plainly on screen.
    if status != "done" and state and len(coach_log) < coach_max + 1 and coach_max:
        try: tip = coach(page, state, targets, values, forbid)
        except Exception as e: tip = f"(coach unavailable: {type(e).__name__})"
        coach_log.append({"step": len(steps), "why": f"final check ({status})", "tip": tip})
        print(f"    🧭 coach (final check, {status}): {tip[:160]}")
        if tip.upper().startswith("DONE"): status = "done"

    page.screenshot(path=out / "final.png")
    ctx.storage_state(path=out / "session.json")  # lets later videos start signed in
    vid = page.video.path()
    ctx.close(); browser.close()
    Path(vid).replace(out / "video.webm")
    downloads = [f.name for f in (out / "downloads").iterdir()]
    result = {"name": v["name"], "goal": v["goal"], "status": status, "steps": steps, "profile": PROFILE,
              "waited_seconds": waited, "downloads": downloads, "decisions": log, "coach": coach_log,
              # what Jev saw at the last decision — lets `improve` diagnose without reopening the app
              "last_screen": {"url": state.get("url", ""), "page_text": state.get("page_text", "")[:2500],
                              "elements": list(targets.values())[:120]}}
    (out / "actions.json").write_text(json.dumps(result, indent=2))
    print(f"  {'✓' if status == 'done' else '⚠'} {v['name']}: {status} · {len(steps) - 1} actions · waited {waited}s"
          + (f" · coach ×{len(coach_log)}" if coach_log else "") + (f" · downloads {downloads}" if downloads else ""))
    return result


def run_videos(only=None, app_url=None):
    from playwright.sync_api import sync_playwright
    vids = [v for v in order_videos(CFG["videos"]) if v.get("enabled", True) and (not only or v["name"] in only)]
    results = []
    with sync_playwright() as p:
        for v in vids:
            print(f"🎥 {v['name']}")
            results.append(record_video(p, v, app_url))
    write_report()
    return results


def latest_results():
    """Newest result of every enabled video on disk, in run order (so --only runs keep the full table)."""
    out = []
    for v in order_videos(CFG["videos"]):
        f = OUT / "videos" / v["name"] / "actions.json"
        if v.get("enabled", True) and f.exists():
            out.append(json.loads(f.read_text()))
    return out


def write_report(results=None, path=None):
    results = latest_results() if results is None else results
    lines = ["| Video | Status | Actions | Waited | Downloads |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {'✅' if r['status'] == 'done' else '⚠️'} {r['status']} | {len(r['steps']) - 1} "
                     f"| {r['waited_seconds']}s | {', '.join(r['downloads'])} |")
    (path or OUT / "run_report.md").write_text("\n".join(lines) + "\n")


# ---------- DOCS AGENT ----------
def reference_text():
    ref = CFG.get("reference_doc")
    if not ref or not Path(ref).exists(): return ""
    if ref.endswith(".docx"):
        import docx
        lines = []
        for par in docx.Document(ref).paragraphs:
            t = par.text.strip()
            if t: lines.append(("## " if par.style.name.startswith("Heading") else "") + t)
        return "\n".join(lines)
    return Path(ref).read_text()


def build_doc_request(d, ref):
    content = []
    if ref:
        content.append({"type": "text", "text": "REFERENCE DOCUMENT (existing product docs; keep its terminology and button names):\n" + ref})
    max_imgs = S.get("max_images_per_doc", 40)
    shots = []
    for name in d["videos"]:
        f = OUT / "videos" / name / "actions.json"
        if not f.exists():
            content.append({"type": "text", "text": f"\nRecording '{name}': not recorded — use the reference document only."})
            continue
        rec = json.loads(f.read_text())
        content.append({"type": "text", "text": f"\n## Recording '{name}' — goal: {rec['goal']} — result: {rec['status']}"})
        for s in rec["steps"]:
            shots.append((name, s))
    step = max(1, -(-len(shots) // max_imgs))  # spread the image budget evenly
    for i, (name, s) in enumerate(shots):
        if i % step == 0:
            img = base64.b64encode((OUT / "videos" / name / s["shot"]).read_bytes()).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}})
        content.append({"type": "text", "text": f"{name}/{s['shot']}: {s['action']}"})
    content.append({"type": "text", "text": doc_instructions(d)})
    return content


def doc_instructions(d):
    """How a doc is written — shared by the API docs agent and Claude Desktop (MCP)."""
    return (f"Write a '{d.get('type', 'how-to guide')}' document for {d.get('audience', 'all users')} in Markdown"
            + (f", covering these reference sections: {', '.join(d['sections'])}" if d.get("sections") else "")
            + ". Use the reference document's wording and button names; use the recordings to confirm each step "
            "and to choose screenshots. Plain language, no element ids or selectors. Embed useful screenshots as "
            "![caption](../videos/<recording>/<step_N.png>) and link each recording as [▶ Watch](../videos/<recording>/video.webm). "
            "Where a recording contradicts the reference, follow the recording and add <!-- CHECK: what differs --> "
            "for a human reviewer. Return only the Markdown.")


def claude_client():
    """Returns write(content) -> markdown. Uses OpenRouter if OPENROUTER_API_KEY is set, else Anthropic directly."""
    if OPENROUTER_KEY and not os.environ.get("ANTHROPIC_API_KEY"):
        import httpx
        def write(content):
            parts = [{"type": "text", "text": c["text"]} if c["type"] == "text" else
                     {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{c['source']['data']}"}} for c in content]
            r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=300,
                           headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
                           json={"model": S.get("claude_model_openrouter", "anthropic/claude-sonnet-5.5"), "max_tokens": 8000,
                                 "messages": [{"role": "user", "content": parts}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        return write
    import anthropic
    client = anthropic.Anthropic()
    return lambda content: client.messages.create(model=S.get("claude_model", "claude-sonnet-5-5"), max_tokens=8000,
                                                  messages=[{"role": "user", "content": content}]).content[0].text


def run_docs(only=None, dry=False):
    ref = reference_text()
    claude = None if dry else claude_client()
    (OUT / "docs").mkdir(parents=True, exist_ok=True)
    for d in CFG["docs"]:
        if only and d["name"] not in only: continue
        print(f"📄 {d['name']}")
        content = build_doc_request(d, ref)
        if dry:
            summary = [c["text"] if c["type"] == "text" else "[image]" for c in content]
            n_img = summary.count("[image]")
            (OUT / "docs" / f"{d['name']}.request.md").write_text(
                f"# Claude request for '{d['name']}' ({n_img} screenshots)\n\n" + "\n".join(
                    s if not s.startswith("REFERENCE") else "REFERENCE DOCUMENT … (" + str(len(s)) + " chars)" for s in summary))
            print(f"  ✓ dry run: {n_img} screenshots → {OUT}/docs/{d['name']}.request.md")
            continue
        (OUT / "docs" / f"{d['name']}.md").write_text(claude(content))
        print(f"  ✓ out/docs/{d['name']}.md")


# ---------- IMPROVE (self-repair failed videos) ----------
# Claude looks at a failed recording (screenshots, Jev's decisions, what was on screen) and rewrites that
# video's goal + hints in guides.yaml; the video is then re-recorded. Only goal/hints/limits ever change.
# Per project in guides.yaml:  never_improve: [videos never re-recorded automatically]
#                              never_improve_after: [action text that means "this run spent something"]
def improve_eligible(v, rec):
    steps = " ".join(s["action"] for s in rec.get("steps", []))
    return (v["name"] not in CFG.get("never_improve", ["login"])
            and not any(a in steps for a in CFG.get("never_improve_after", [])))


def build_fix_request(v, rec):
    forbid = CFG.get("forbid_labels", [])
    dec = [f'{d["step"]}: picked {d["target_desc"][:90] or d["target"]} · confidence {d["confidence"]} · done {d["done"]}'
           for d in rec.get("decisions", [])[-14:]]
    screen = rec.get("last_screen", {})
    content = [{"type": "text", "text": (
        "You repair walkthrough recordings of a web app. A model called Jev records each video: at every step it "
        "sees the goal, the hints (expected_steps) and a list of on-screen elements, picks one element, and says how "
        "sure it is that the goal is done. The run stops as 'unsure' when Jev's confidence is low, 'loop' when the "
        "same action repeats 3 times, 'max_steps' when steps run out, 'timeout' when waiting runs out. "
        "A video passes only when Jev judges the goal finished, so the goal must end with a clear, visible finish "
        "condition (e.g. 'Done when the three files have downloaded' or 'Done when the answer is shown').\n\n"
        f"VIDEO '{v['name']}'\nGOAL: {v['goal']}\nHINTS:\n" + "\n".join(f"- {h}" for h in v.get("hints", [])) +
        f"\n\nRESULT: {rec['status']} after {len(rec['steps']) - 1} actions, waited {rec['waited_seconds']}s, "
        f"downloads: {rec.get('downloads') or 'none'}\nACTIONS TAKEN:\n" +
        "\n".join(f"- {s['action']}" for s in rec["steps"]) +
        "\nJEV'S LAST DECISIONS:\n" + "\n".join(dec) +
        f"\n\nLAST SCREEN ({screen.get('url', '?')}) — elements Jev could pick:\n" +
        "\n".join(f"- {e}" for e in screen.get("elements", [])) +
        f"\nVisible text (trimmed):\n{screen.get('page_text', '')[:1800]}")}]
    for shot in ([rec["steps"][-1]["shot"]] if rec.get("steps") else []) + ["final.png"]:
        f = OUT / "videos" / v["name"] / shot
        if f.exists():
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(f.read_bytes()).decode()}})
    content.append({"type": "text", "text": (
        "Rewrite the goal and hints so the next recording succeeds. Rules: use labels exactly as they appear in the "
        "element list (**Label** for exact text, 'text' for which row/section); if the video starts on a sign-in page, "
        "keep the sign-in hint first; keep the walkthrough's purpose; keep it short; never mention or click "
        f"{', '.join(forbid)}; never start anything that costs money or creates real records; if part of the goal is impossible in this "
        "app, drop that part. The previous attempt may have left data behind (e.g. an added entry or uploaded file) — "
        "write hints that still work if so.\n"
        'Reply with JSON only: {"diagnosis": "one sentence", "goal": "...", "hints": ["..."], '
        '"max_wait_seconds": null or number, "max_steps": null or number}')})
    return content


def parse_fix(text):
    m = re.search(r"\{.*\}", text, re.S)
    fix = json.loads(m.group(0)) if m else None
    if not fix or not isinstance(fix.get("goal"), str) or not isinstance(fix.get("hints"), list) or not fix["hints"]:
        raise ValueError("Claude did not return a usable fix")
    words = (fix["goal"] + " " + " ".join(map(str, fix["hints"]))).lower()
    bad = [f for f in CFG.get("forbid_labels", []) if f.lower() in words]
    if bad: raise ValueError(f"fix mentions forbidden label(s) {bad}")
    return fix


def apply_fix(v, fix):
    """Update this one video in memory and in guides.yaml (only goal, hints, max_wait_seconds, max_steps)."""
    v["goal"], v["hints"] = fix["goal"].strip(), [str(h).strip() for h in fix["hints"]]
    if fix.get("max_wait_seconds"): v["max_wait_seconds"] = min(int(fix["max_wait_seconds"]), 900)
    if fix.get("max_steps"): v["max_steps"] = min(int(fix["max_steps"]), 200)
    save_entry("videos", v)


# ---------- guides.yaml writers (keep the file's comments; only the touched block is rewritten) ----------
_q = lambda s: json.dumps(s, ensure_ascii=False)  # JSON values are valid YAML


def _entry_span(text, section, name):
    """(start, end) of '  - name: <name>' inside `section:`, or None."""
    sec = re.search(rf"^{section}:[^\n]*\n", text, re.M)
    if not sec: return None
    sec_end = (re.search(r"^\S", text[sec.end():], re.M) or re.search(r"\Z", text[sec.end():])).start() + sec.end()
    m = re.search(rf"^  - name: [\"']?{re.escape(name)}[\"']?[ \t]*(#.*)?\n", text[sec.end():sec_end], re.M)
    if not m: return None
    start = sec.end() + m.start()
    nxt = re.search(r"\n(?=  - name: |  # |\S)", text[start + 1:sec_end])
    return start, (start + 1 + nxt.start() + 1 if nxt else sec_end)


def _write_checked(text):
    """Write guides.yaml only if it still parses, then reload it."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict): raise ValueError("guides.yaml would no longer be a mapping")
    CFG_PATH.write_text(text)
    load(CFG_PATH)


def save_entry(section, entry):
    """Add or replace one video/doc block in `section` ('videos' or 'docs')."""
    lines = [f"  - name: {entry['name']}"]
    for k, val in entry.items():
        if k == "name" or val is None: continue
        if isinstance(val, list) and val and k in ("hints",):
            lines += [f"    {k}:"] + [f"      - {_q(h)}" for h in val]
        else:
            lines.append(f"    {k}: {_q(val)}")
    block = "\n".join(lines) + "\n\n"
    text = CFG_PATH.read_text()
    if not text.endswith("\n"): text += "\n"
    span = _entry_span(text, section, entry["name"])
    if span:
        text = text[:span[0]] + block + text[span[1]:].lstrip("\n")
    else:
        sec = re.search(rf"^{section}:[^\n]*\n?", text, re.M)
        if not sec:
            text = text.rstrip("\n") + f"\n\n{section}:\n" + block
        else:
            head = text[:sec.start()] + f"{section}:\n"          # also turns `videos: []` into a block list
            rest = text[sec.end():]
            end = (re.search(r"^\S", rest, re.M) or re.search(r"\Z", rest)).start()
            body = rest[:end].rstrip("\n")
            text = head + (body + "\n\n" if body else "") + block + rest[end:]
    _write_checked(text)


def delete_entry(section, name):
    text = CFG_PATH.read_text()
    span = _entry_span(text, section, name)
    if not span: raise KeyError(f"no {section[:-1]} named '{name}'")
    _write_checked(text[:span[0]] + text[span[1]:].lstrip("\n"))


def save_top(key, value):
    """Replace one top-level key (app_url, settings, test_data, ...). Comments inside that block are not kept."""
    text = CFG_PATH.read_text()
    if not text.endswith("\n"): text += "\n"
    if isinstance(value, (dict, list)) and value:
        block = f"{key}:\n" + "".join(
            (f"  {k}: {_q(v)}\n" for k, v in value.items()) if isinstance(value, dict) else (f"  - {_q(v)}\n" for v in value))
    else:
        block = f"{key}: {_q(value)}\n"
    m = re.search(rf"^{re.escape(key)}:", text, re.M)
    if m:
        rest = text[m.end():]
        end = m.end() + (re.search(r"^\S", rest, re.M) or re.search(r"\Z", rest)).start()
        region = text[m.start():end]
        gap = "\n" * max(0, len(region) - len(region.rstrip("\n")) - 1)  # keep the blank lines that were there
        text = text[:m.start()] + block + gap + text[end:]
    else:
        vids = re.search(r"^videos:", text, re.M)
        text = text[:vids.start()] + block + "\n" + text[vids.start():] if vids else text + block
    _write_checked(text)


def improve(only=None, rounds=3):
    from playwright.sync_api import sync_playwright
    shutil.copy(CFG_PATH, CFG_PATH.with_suffix(CFG_PATH.suffix + ".bak"))
    claude, log = claude_client(), [f"# Improve log ({time.strftime('%Y-%m-%d %H:%M')})", ""]
    seen = {}  # video -> failure signatures already tried
    with sync_playwright() as p:
        for rnd in range(1, rounds + 1):
            todo = []
            for v in order_videos(CFG["videos"]):
                f = OUT / "videos" / v["name"] / "actions.json"
                if not v.get("enabled", True) or (only and v["name"] not in only) or not f.exists(): continue
                rec = json.loads(f.read_text())
                if rec["status"] == "done" or not improve_eligible(v, rec): continue
                sig = (rec["status"], rec["steps"][-1]["action"] if rec["steps"] else "")
                if sig in seen.setdefault(v["name"], []):
                    print(f"  ✗ {v['name']}: failed the same way twice — needs a human"); continue
                seen[v["name"]].append(sig)
                todo.append((v, rec))
            if not todo: break
            print(f"\n🔧 Round {rnd}: {', '.join(v['name'] for v, _ in todo)}")
            log += [f"## Round {rnd}", ""]
            for v, rec in todo:
                old = {"goal": v["goal"], "hints": list(v.get("hints", []))}
                try:
                    fix = parse_fix(claude(build_fix_request(v, rec)))
                except Exception as e:
                    print(f"  ✗ {v['name']}: {e}"); log += [f"### {v['name']} — skipped: {e}", ""]; continue
                apply_fix(v, fix)
                print(f"  ✎ {v['name']} ({rec['status']}): {fix.get('diagnosis', '')}")
                print(f"🎥 {v['name']}")
                new = record_video(p, v)
                write_report()
                log += [f"### {v['name']}: {rec['status']} → {new['status']}", f"Diagnosis: {fix.get('diagnosis', '')}", "",
                        f"Old goal: {old['goal']}", "Old hints:", *[f"- {h}" for h in old["hints"]], "",
                        f"New goal: {v['goal']}", "New hints:", *[f"- {h}" for h in v["hints"]], "",
                        "Possible leftovers from this retry: check the app for records, uploads or entries it added.", ""]
                (OUT / "improve_log.md").write_text("\n".join(log) + "\n")
    write_report()
    left = [r["name"] for r in latest_results() if r["status"] != "done"]
    print(f"\n{'✓ all videos done' if not left else '⚠ still failing: ' + ', '.join(left)}"
          f"  → {OUT}/run_report.md, {OUT}/improve_log.md (backup: {CFG_PATH}.bak)")


# ---------- PLAN (no browser, no API) ----------
def plan():
    ok, vids = True, CFG["videos"]
    names = [v["name"] for v in vids]
    def err(m):
        nonlocal ok; ok = False; print("  ✗", m)
    print(f"Config: {CFG_PATH}   App: {CFG.get('app_url')}\n")
    if len(set(names)) != len(names): err("duplicate video names")
    for v in vids:
        if v.get("start_after") and v["start_after"] not in names: err(f"{v['name']}: start_after '{v['start_after']}' not found")
    for d in CFG["docs"]:
        for n in d["videos"]:
            if n not in names: err(f"doc {d['name']}: unknown video '{n}'")
    if not CFG.get("app_url"): err("app_url not set")
    for v in vids:
        if not v.get("goal"): err(f"{v['name']}: no goal")
    if CFG.get("upload_file") and not Path(CFG["upload_file"]).exists(): err(f"upload_file missing: {CFG['upload_file']}")
    if CFG.get("reference_doc") and not Path(CFG["reference_doc"]).exists(): err(f"reference_doc missing: {CFG['reference_doc']}")
    creds = env_file(CFG_PATH.parent, PROFILE).name
    print(f"Credentials profile: {PROFILE} ({creds}; docker -e / --env-file values win)")
    for k, val in CFG.get("test_data", {}).items():
        for var in re.findall(r"\$\{(\w+)\}", str(val)):
            if os.environ.get(var): print(f"  ✓ test_data.{k} ← {var}")
            else: err(f"test_data.{k}: {var} not set ({creds}, or docker -e {var}=...)")
    if OPENROUTER_KEY: print("  ✓ OPENROUTER_API_KEY set — Jev and Claude both run through OpenRouter")
    else:
        for var in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY"):
            if not os.environ.get(var): print(f"  ! {var} not set (or set OPENROUTER_API_KEY for both)")

    print("\nRun order:")
    calls = 0
    for v in order_videos(vids):
        on = v.get("enabled", True)
        est = len(v["hints"]) + 3 if v.get("hints") else 10   # goal-only: Jev plans every step itself
        calls += est if on else 0
        dep = f"  ← after {v['start_after']}" if v.get("start_after") else ""
        print(f"  {'▶' if on else '⏸'} {v['name']:<26} ~{est:>2} Jev calls{dep}" + ("" if on else f"   (skipped: {v.get('reason', '')})"))
    # ~6k input tokens per call (page text + element list) at $0.042/M tokens
    print(f"\nEstimated Jev calls: ~{calls}  (~${calls * 6000 * 0.042 / 1e6:.3f})")
    print(f"Docs: {len(CFG['docs'])} → " + ", ".join(d["name"] for d in CFG["docs"]))
    print("\n✓ config OK" if ok else "\n✗ fix the errors above")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "videos", "docs", "all", "improve", "export"], nargs="?", default="plan")
    ap.add_argument("--mp4", action="store_true", help="export: also add .mp4 copies of the videos")
    ap.add_argument("--only", help="comma-separated video or doc names")
    ap.add_argument("--dry", action="store_true", help="docs: build requests without calling Claude")
    ap.add_argument("--rounds", type=int, default=3, help="improve: max fix-and-retry rounds")
    ap.add_argument("--project", default=os.environ.get("PROJECT"), help="projects/<name> (default: $PROJECT, or the only project)")
    ap.add_argument("--profile", default=os.environ.get("LOGIN_PROFILE"),
                    help="credential set: projects/<name>/.env.<profile> (default: .env)")
    a = ap.parse_args()
    try:
        if os.environ.get("GUIDES"):
            load_env(Path(os.environ["GUIDES"]).resolve().parent, a.profile); load(os.environ["GUIDES"])
        else:
            names = sorted(p.parent.name for p in PROJECTS.glob("*/guides.yaml"))
            if not a.project and len(names) != 1:
                sys.exit(f"choose a project with --project ({', '.join(names) or f'none yet in {PROJECTS}'})")
            use_project(a.project or names[0], a.profile)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(f"✗ {e}")
    if a.cmd in ("videos", "all", "improve"): print(f"🔑 credentials profile: {PROFILE}")
    only = set(a.only.split(",")) if a.only else None
    if a.cmd == "plan": sys.exit(0 if plan() else 1)
    if a.cmd == "improve": improve(only, a.rounds); sys.exit(0)
    if a.cmd == "export":
        import exporter
        r = exporter.export_project(OUT, CFG_PATH.parent.name, only, a.mp4)
        print(f"✓ {len(r['docs'])} doc(s), {r['media_files']} media files\n  {r['html']}  ({r['html_mb']} MB)\n"
              f"  {r['zip']}  ({r['zip_mb']} MB)" + (f"\n  ⚠ broken links: {r['missing_links']}" if r["missing_links"] else ""))
        sys.exit(0)
    if a.cmd in ("videos", "all"): run_videos(only)
    if a.cmd in ("docs", "all"): run_docs(only if a.cmd == "docs" else None, a.dry)
