## What & why

<!-- What does this change and why? Link the issue: Closes #123 -->

## How I tested it

<!-- Commands you ran and the result, e.g. run_report.md lines for the Sauce Demo project. -->

```
python agent.py plan   --project swaglabs
python agent.py videos --project swaglabs --only login,checkout
```

## Checklist

- [ ] One focused change; issue linked
- [ ] `python agent.py plan` passes; a real recording was run if recording/docs/export changed
- [ ] README / ROADMAP updated for new settings, tools or features
- [ ] Existing `guides.yaml` files keep working (or a migration note is included)
- [ ] **No secrets, `.env` files, real credentials or private recordings** in the diff
