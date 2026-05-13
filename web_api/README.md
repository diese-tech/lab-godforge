# Godforge Local Web API

Development and Railway API bridge for the Godforge dashboard.

This API reuses the existing Python bot logic where practical:

- `utils.parser`
- `utils.loader`
- `utils.picker`
- `utils.draft`
- `utils.resolver`
- `utils.ledger`
- `utils.wallet`

It can run locally through `web_api/server.py` or live through the combined Railway launcher. It implements temporary password sessions, staged Discord OAuth login, protected admin endpoints, and dashboard persistence through JSON or SQLite. Full guild permission enforcement is still future work.

Discord OAuth token/user requests use `aiohttp`, which is already required by the Discord bot dependency chain. The remaining API surface stays dependency-light and uses the standard-library HTTP server.

## Run

From the repo root:

```powershell
python web_api/server.py
```

Default URL:

```text
http://localhost:8787
```

The server also serves the static dashboard from `../web`, so `http://localhost:8787/` opens the site directly.

For the combined Railway process, use:

```powershell
python railway_app.py
```

That starts the web/API server on `$PORT` and the Discord bot in the same container.

## Admin Auth

Set `GODFORGE_ADMIN_PASSWORD` to enable protected dashboard actions.

Public endpoints remain available without login:

```text
GET /api/health
GET /api/auth/status
GET /api/gods/roll
GET /api/gods/roll5
GET /api/builds/roll
```

Admin actions require a session cookie from:

```text
POST /api/auth/login
POST /api/auth/logout
```

Discord OAuth login is staged through:

```text
GET /api/auth/discord/start
GET /api/auth/discord/callback
```

Required Railway variables:

```text
DISCORD_CLIENT_ID=1493371999031136318
DISCORD_CLIENT_SECRET=<set in Railway>
DISCORD_OAUTH_REDIRECT_URI=https://godforge-hub.up.railway.app/api/auth/discord/callback
```

Protected admin telemetry and sync endpoints are also available after login:

```text
GET /api/admin/status
POST /api/admin/sync/ledger
```

`GET /api/admin/status` reports bot connection state, Discord user, guild count, recent guild names, ledger counts, wallet count, embed pointer state, module health, active web draft rooms, and a check timestamp.

`POST /api/admin/sync/ledger` asks the running bot loop to refresh the Discord betting embed when the API and bot are running in the combined Railway process.

`GET /api/admin/audit` returns the most recent protected dashboard/admin actions from `data/admin_audit.json`.

`GET /api/settings` and `POST /api/settings` provide temporary JSON-backed guild settings for the admin dashboard. They are scoped by `guild_id`, default to `global`, and are intended to be replaced by Discord OAuth, guild permissions, and database-backed settings later.

`GET /api/commands/custom`, `POST /api/commands/custom`, and `POST /api/commands/custom/delete` provide temporary custom command configuration. Unknown dot commands in Discord can execute matching enabled configs with channel gates, role gates, per-user cooldowns, and mention suppression.

Dashboard settings, audit events, and custom command configs default to JSON. To use the stdlib SQLite document store instead:

```text
GODFORGE_STORAGE=sqlite
GODFORGE_DB_PATH=/app/data/godforge_dashboard.db
```

## Endpoints

```text
GET  /api/health
GET  /api/auth/status
GET  /api/auth/discord/start
GET  /api/auth/discord/callback
GET  /api/admin/audit?limit=25
GET  /api/admin/status
GET  /api/commands/custom?guild_id=global
GET  /api/gods/roll?role=jungle&source=website
GET  /api/gods/roll5?role=jungle&source=website
GET  /api/builds/roll?role=adc&type=standard&count=6
GET  /api/ledger
GET  /api/settings?guild_id=global
GET  /api/wallets
POST /api/auth/login
POST /api/auth/logout
POST /api/admin/sync/ledger
POST /api/command
POST /api/commands/custom
POST /api/commands/custom/delete
POST /api/draft/start
POST /api/draft/action
POST /api/draft/undo
POST /api/draft/next
POST /api/draft/end
POST /api/match/create
POST /api/match/status
POST /api/match/resolve/winner
POST /api/match/resolve/prop
POST /api/bet/place
POST /api/settings
POST /api/wallet/adjust
POST /api/ledger/reset
```

Legacy economy mutation endpoints stay behind `GODFORGE_ENABLE_LEGACY_ECONOMY=true`. Leave that unset in normal production so GodForge cannot create matches, accept bets, resolve payouts, adjust wallets, or reset ledgers.

## Admin Payloads

```json
POST /api/match/create
{ "team1": "Solaris", "team2": "Onyx" }

POST /api/match/status
{ "match_id": "GF-0007", "status": "in_progress" }

POST /api/match/resolve/winner
{ "match_id": "GF-0007", "winner": "Solaris" }

POST /api/match/resolve/prop
{ "match_id": "GF-0007", "player": "Solaris ADC", "stat": "kills", "actual_value": 8 }

POST /api/bet/place
{ "match_id": "GF-0007", "username": "AtlasMain", "type": "win", "team": "Solaris", "amount": 100 }

POST /api/bet/place
{ "match_id": "GF-0007", "username": "WardBoss", "type": "prop", "player": "Solaris ADC", "stat": "kills", "direction": "over", "threshold": 7.5, "amount": 50 }

POST /api/wallet/adjust
{ "target": "AtlasMain", "action": "give", "amount": 100 }

POST /api/settings
{
  "guild_id": "global",
  "updated_by": "web-dashboard",
  "features": {
    "botEnabled": true,
    "randomizerEnabled": true,
    "draftsEnabled": true,
    "bettingEnabled": true
  },
  "channels": {
    "matchChannel": "#matches",
    "bettingChannel": "#place-bets",
    "adminChannel": "#admin"
  },
  "roles": {
    "adminRole": "Admins",
    "captainRole": "Captains"
  },
  "permissions": {
    "monetizeAccess": "none"
  }
}

POST /api/commands/custom
{
  "guild_id": "global",
  "trigger": ".scrimrules",
  "response": "Post lobby code, captains, and draft reminder.",
  "channel": "#captains",
  "role_gate": "Captains",
  "cooldown": "15s",
  "enabled": true
}

POST /api/commands/custom/delete
{ "guild_id": "global", "trigger": ".scrimrules" }
```

`/api/wallet/adjust` accepts `give`, `take`, or `set`. Wallets are stored in the existing `data/wallets.json` shape keyed by user id. For local dashboard testing without Discord ids, the API derives a stable local id from `target`.

`/api/bet/place` seeds new bettor wallets at the same 500 point starting balance as the Discord bot, debits the bet amount, and appends the bet to the target match. It only accepts bets while the match status is `betting_open`.

`POST /api/ledger/reset` clears matches and returns:

```json
{ "ok": true, "cleared": 2, "discord_embed_update": true }
```

## Notes

- Draft rooms are in memory and disappear when the API process stops.
- `POST /api/draft/start` accepts optional `forgelensMatchId` / `forgelens_match_id` and `gameNumber` / `game_number` values for ForgeLens-linked drafts.
- The website falls back to demo data when this API is not running.
- Ledger and wallet endpoints use the existing local JSON files in `data/`.
- Production deployment and persistent state are intentionally deferred.
