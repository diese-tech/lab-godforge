# GodForge Version History

This file tracks product-level milestones so dashboard and bot work does not blur together across releases.

When a release changes, update:

- `utils/formatter.py` (`GODFORGE_VERSION`, shown at the bottom of `.help`)
- `VERSION_HISTORY.md`
- `RELEASE_PROCESS.md`
- `README.md`
- `web/README.md`
- `web_api/README.md`

## v2.0 - Ledger System

The ledger and wallet system brought GodForge to v2.0.

Included scope:

- Match lifecycle commands and ledger JSON persistence.
- Wallet balances and betting payouts.
- Discord ledger posting and embed update behavior.
- Tests for match creation, team matching, ledger posting, normal-user bets, and admin commands.

## v2.1.0 - Live Dashboard Bridge

Released. Scope included:

- Railway single-service launcher for bot plus web/API.
- Public dashboard domain: `https://godforge-hub.up.railway.app`.
- Discord OAuth start/callback endpoints for `identify guilds`.
- Temporary password-protected admin dashboard.
- SQLite dashboard document storage behind `GODFORGE_STORAGE=sqlite`.
- Match Ops, Betting, Wallets, Settings, and Admin Overview panels.
- Manual Discord ledger sync from the dashboard.
- Module health, managed-server selector, JSON-backed guild settings, and admin audit feed.
- Documentation and tests for the temporary web/API bridge.

## v2.2.0 - ForgeLens Handoff + Hardening

Latest stable release. Historical scope:

- **ForgeLens draft handoff contract**: stable GodForge → ForgeLens JSON export schema with `forgelens_match_id`, `game_number`, and `draft_sequence`.
- **GodForge as orchestration bot**: economy commands (`.match`, `.bet`, `.wallet`, `.ledger`) formally deprecated; ForgeLens owns betting, wallets, ledgers, and settlement.
- **Env-var routing**: hardcoded owner user ID and reports channel map moved to `GODFORGE_OWNER_USER_ID` and `GODFORGE_REPORTS_CHANNELS`.
- **Crash fix**: `options` undefined `NameError` in `_handle_draft_local()` repaired — `.draft start` in local mode was crashing on every invocation.
- **CSRF protection**: dashboard POST endpoints now require a matching `X-CSRF-Token` header and `godforge_csrf` cookie set at login.
- **Cleanup task supervision**: `@cleanup_task.error` handler logs and restarts the task on crash instead of silently dying.
- **Orphan draft recovery**: active local draft channel IDs persisted to `data/active_local_drafts.json`; `on_ready()` notifies orphaned channels after a restart.
- **Web API hardening**: request body capped at 64 KB; CORS restricted via `GODFORGE_ALLOWED_ORIGIN`; session secret warns when falling back to insecure default.
- **Dependency pinning**: `requirements.txt` pinned to discord.py 2.7.1, python-dotenv 1.2.2, aiohttp 3.13.5.
- **Concurrent write hardening**: ledger and wallet locks upgraded to `RLock` covering full read-modify-write cycles (deprecated code; will be removed with legacy economy block).

## v2.3.0 - Standalone Party Foundation

Current release candidate (`v2.3.0-rc.1`). Foundation introduced:

- **Standalone product boundary**: normal GodForge workflows and help surfaces no
  longer require or advertise a companion service.
- **Durable party lifecycle**: explicit open, full, ready-check, forming, active,
  completed, cancelled, and expired states.
- **Restart recovery**: guild-scoped SQLite party records retain participants,
  readiness, preferences, and Discord delivery references.
- **Safe retries**: operation IDs and an audit trail make Discord interaction
  retries idempotent and diagnosable.
- **Optional compatibility**: the portable adapter is disabled by default and
  cannot block core GodForge work if delivery fails.
- **Command-page cleanup**: Discord help and the public feature/command area
  describe only active standalone functionality.
- **Zero-config guild setup**: `/party setup` verifies permissions, creates or
  refreshes stored-ID-managed Play resources, and supports a short-lived test
  mode.
- **Managed role cosmetics**: permissionless role cosmetics and restart-safe
  self-assignment buttons project durable GodForge player preferences onto
  Discord roles without adopting same-named administrator roles.
- **Reusable lobby cards**: Discord modals capture lobby rules and structured
  role preferences; persistent cards support joining, leaving, organizer edits,
  cancellation, and sharing while retaining state across restarts.
- **Reliable ready rosters**: concurrency-safe capacity, durable waitlists,
  role-aware substitute promotion, bounded ready-check extensions, and timeout
  cancellation survive process restarts.

## Future Version Gates

- `v2.3`: Complete zero-config guild setup and managed cosmetic roles, then
  validate the standalone foundation in a live Discord guild.
- `v3.0`: Full dual-use platform milestone with standalone web users and production-grade assets.
