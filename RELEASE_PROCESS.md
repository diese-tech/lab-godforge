# GodForge Release Process

Use this checklist whenever GodForge moves from a release candidate to a tagged release.

## Version Sources To Update

- `utils/formatter.py`
  - `GODFORGE_VERSION`
  - This appears at the bottom of the `.help` embeds.
- `VERSION_HISTORY.md`
  - Canonical product milestone notes.
- `README.md`
  - Public bot command/version notes.
- `web/README.md`
  - Dashboard deployment, storage, and OAuth notes.
- `web/TODO.md`
  - Move completed work out of future/staged sections.
- `web_api/README.md`
  - API/env/storage notes.

## v2.1.0 Release Gate — Completed

All gates passed. v2.1.0 shipped with the live dashboard bridge, Discord OAuth, SQLite storage, and admin surfaces.

## v2.2.0 Release Gate — Completed

All gates passed. v2.2.0 shipped with the ForgeLens handoff contract, orchestration bot hardening, CSRF protection, crash fixes, and dependency pinning. See `VERSION_HISTORY.md` for full scope.

## v2.3.0 Release Gate

Tag `v2.3.0` only after:

- Per-guild settings storage is durable (not JSON files wiped on redeploy).
- Dashboard admin actions require verified Discord guild permissions (not just a session cookie).
- Legacy economy block (`ledger.py`, `wallet.py`, deprecated bot commands) removed or fully migrated to ForgeLens.
- Full local tests pass before push.
- GitHub/Railway deployment status is green after push.

## Tagging

When a release gate passes:

```powershell
git tag v<version>
git push origin v<version>
```

Do not tag a release candidate as stable until the live smoke test passes.
