# GodForge Local Web API

Development and Railway bridge for GodForge's standalone dashboard.

The API reuses the shared parser, loader, picker, draft, resolver, settings, and
custom-command modules. It can run directly or inside the combined Railway
launcher.

## Run

```powershell
python web_api/server.py
```

The server binds to `HOST` and `PORT` (`8787` by default). Production dashboard
access requires `GODFORGE_ADMIN_PASSWORD`, `GODFORGE_SESSION_SECRET`, and a
restricted `GODFORGE_ALLOWED_ORIGIN`.

## Supported routes

Public routes cover health, god rolls, Roll Team, builds, and staged Discord
OAuth. Authenticated routes cover drafts, settings, custom commands, command
execution, admin status, and audit history.

See [`../web/DATA_CONTRACT.md`](../web/DATA_CONTRACT.md) for current contracts.

## Security

- Request bodies are capped at 64 KB.
- Protected mutations require an authenticated session and CSRF token.
- CORS is restricted by `GODFORGE_ALLOWED_ORIGIN`.
- Discord client secrets belong in deployment secrets, never source control.
- Dashboard guild-permission enforcement is still a release blocker.

## Unsupported legacy compatibility

Migration-era match, betting, wallet, and ledger routes remain only to protect
rollback and migration paths. Their mutations are disabled unless
`GODFORGE_ENABLE_LEGACY_ECONOMY=true`. They are not supported GodForge
workflows. Historical details live in
[`../docs/archive/LEGACY_WEB_DATA_CONTRACT.md`](../docs/archive/LEGACY_WEB_DATA_CONTRACT.md).
