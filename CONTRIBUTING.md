# 🤝 Contributing to Jev Docs Agent

Thanks for helping! Every kind of contribution counts — ideas, bug reports, docs, example projects and code.

**Quick links:** [💡 Ideas](https://github.com/kaivalpatel6350/jev-docs-agent/discussions/categories/ideas) ·
[🐞 Report a bug](https://github.com/kaivalpatel6350/jev-docs-agent/issues/new?template=bug_report.yml) ·
[✨ Request a feature](https://github.com/kaivalpatel6350/jev-docs-agent/issues/new?template=feature_request.yml) ·
[🗺️ Roadmap](ROADMAP.md) ·
[🌱 Good first issues](https://github.com/kaivalpatel6350/jev-docs-agent/issues?q=is%3Aopen+label%3A%22good+first+issue%22)

---

## Ways to contribute

| You have… | Do this |
|---|---|
| 💡 An idea or a "what if…" | Start a discussion in **[Ideas](https://github.com/kaivalpatel6350/jev-docs-agent/discussions/categories/ideas)** — rough ideas welcome |
| ✨ A clear feature request | Open a **feature request** issue (template asks for problem, proposal, alternatives) |
| 🐞 Something broken | Open a **bug report** with the command, `actions.json` status and log lines (no secrets!) |
| 🧪 A public demo app it should handle | Add an example project under `projects/` (see below) |
| 📝 Docs fixes | PRs to `README.md`, `docs/` or this file are always welcome |
| 🛠️ Code | Pick an issue labelled `good first issue` or `help wanted`, comment that you're on it, open a PR |

---

## Development setup

```bash
git clone https://github.com/kaivalpatel6350/jev-docs-agent.git && cd jev-docs-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                                     # OPENROUTER_API_KEY (or TYPESAFE_API_KEY)
cp projects/swaglabs/.env.example projects/swaglabs/.env  # public Sauce Demo login
```

Or with Docker: `docker build -t jev-docs-agent .`

### Project layout

| Path | What it is |
|---|---|
| `agent.py` | Recording loop (page → Jev decision → Playwright action), coach, docs, improve, export CLI |
| `mcp_server.py` | MCP tools for Claude Desktop — thin wrappers around `agent.py` |
| `exporter.py` | HTML guide / ZIP / MP4 packaging |
| `projects/<name>/guides.yaml` | One app under test: `app_url`, `test_data`, `forbid_labels`, `videos`, `docs` |
| `projects/<name>/out/` | Generated output (git-ignored) |

### Testing your change

There's no unit-test suite yet (contributions welcome!). Before opening a PR:

```bash
python -m py_compile agent.py mcp_server.py exporter.py
python agent.py plan    --project swaglabs          # free: config + credentials check
python agent.py videos  --project swaglabs --only login,checkout   # ~1 min, a cent or two of API usage
python agent.py export  --project swaglabs          # if you touched docs/export
```

For MCP changes, check the tool list and call your tool with the MCP client:

```bash
python -c "import mcp_server; print(len(mcp_server.mcp._tool_manager.list_tools()), 'tools')"
```

Paste the resulting `run_report.md` (or the relevant lines) into your PR.

### Adding an example project

Use **public demo apps only** (e.g. Sauce Demo, demo.playwright.dev, the-internet.herokuapp.com).

```
projects/<app>/guides.yaml      # goals + docs
projects/<app>/.env.example     # public demo credentials only, if the app shows them itself
```

---

## Pull request checklist

- [ ] One focused change per PR; linked issue (`Closes #123`)
- [ ] `python agent.py plan` passes; a real recording was run if recording/docs/export changed
- [ ] New settings or tools are documented in `README.md` (and `ROADMAP.md` status updated if relevant)
- [ ] Existing `guides.yaml` files keep working (no breaking config changes without a migration note)
- [ ] **No secrets, `.env` files, real credentials or recordings of private apps** in the diff

## Code style

- Follow the existing style: small functions, plain Python, comments that explain *why*.
- Keep dependencies minimal — prefer what's already in `requirements.txt` and the Docker image.
- Never log or return secret values; name them instead ("Type the password").
- Safety first: respect `forbid_labels`, budgets (`max_steps`, `max_wait_seconds`) and the honest-done rule.

## 🔐 Security

Found a way secrets could leak, or a way around `forbid_labels`? Please **don't open a public issue** — use
[GitHub private vulnerability reporting](https://github.com/kaivalpatel6350/jev-docs-agent/security/advisories/new).

## Code of conduct

Be kind, assume good intent, keep feedback about the work. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

By contributing you agree your contribution is licensed under the [MIT License](LICENSE).
