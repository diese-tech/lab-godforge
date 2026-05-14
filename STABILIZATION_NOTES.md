# Stabilization Notes

## v2.2.0

## What changed and why

- **Atomic JSON persistence for wallets and ledger.** `save_wallets()` and `save_ledger()` now write to a temp file on the same volume and then swap into place with `os.replace()`. This prevents partially-written JSON files during crashes/interruption and keeps readers safe with all-or-nothing file replacement semantics.

- **Write serialization locks added for cross-thread/process-mode stability.** Module-level `threading.Lock` guards were added to wallet and ledger save paths so the asyncio bot and `ThreadingHTTPServer` write flows cannot interleave writes in combined mode. This hardens file integrity under concurrent command/API mutation bursts without changing persistence format.

- **Idempotency guards for resolution paths.** `resolve_win_bets()` now exits early when the match is already `completed` or `settled`, and `resolve_prop_bets()` exits early when the same player/stat prop key was already resolved (case-insensitive). This prevents duplicate payout computation and duplicate resolution entries from repeated admin actions/retries.

- **Explicit wallet mutation guard.** `update_balance()` now raises a clear `KeyError` message when no wallet exists for a user, making failures actionable and explicit. Existing call sites in `bot.py` already seed/ensure wallets before updates, so behavior remains compatible while diagnostics improve.

- **Backups before destructive resets.** `reset_all()` writes `data/wallets.bak.json` and `reset_ledger()` writes `data/weekly_ledger.bak.json` via atomic-write semantics before wiping active state. This provides operational rollback points for accidental admin wipes.

- **Startup/runtime clarity improvements.** `bot.py` now fails fast when `DISCORD_TOKEN` is missing, and comments document intentionally single-tenant constants (`_GOD_USER_ID`, `REPORTS_CHANNELS`) for future multi-guild migration planning. `.env.example` and README now document bot-only, web-only, and combined runtime modes plus the admin-password placeholder warning.

- **Targeted regression tests added.** New unit tests cover atomic write behavior, cleanup on write failure, idempotent resolution behavior, destructive reset backups, missing-wallet errors, and concurrent wallet updates. These tests pin down this stabilization pass and reduce regression risk.

## Explicitly not changed (per RELEASE_PROCESS.md v2.1.0 gates)

- **No SQLite migration for core wallet/ledger persistence.** JSON storage remains the current production-staged persistence strategy for v2.1.0-candidate and was hardened rather than replaced.
- **No Discord OAuth completion/migration.** Existing staged auth posture remains intact; only documentation and startup validation were improved.
- **No payout rounding-model rewrite.** Current rounding behavior (`round`) was preserved; drift risks are acknowledged and monitored rather than changed in this bounded pass.

## v2.1.0

- Atomic JSON persistence for wallets and ledger via `os.replace()` temp-file swap.
- Write serialization locks for cross-thread safety between asyncio bot and `ThreadingHTTPServer`.
- Idempotency guards on win and prop bet resolution paths.
- Explicit `KeyError` on missing wallet in `update_balance()`.
- Backup files written before destructive ledger/wallet resets.
- Fast-fail startup when `DISCORD_TOKEN` is missing.

## Remaining risks requiring runtime monitoring

- **Draft room isolation between web and bot remains process-local by design.** State is still not cross-process synchronized beyond current architecture; monitor for operator confusion in multi-instance scenarios.
- **Payout rounding drift remains possible.** Pool-proportional payouts still use integer rounding and may produce small aggregate drift; acknowledged but intentionally unchanged.
- **`GODFORGE_ADMIN_PASSWORD` placeholder is not runtime-enforced as unsafe.** The default placeholder value is now documented as invalid operational posture, but no hard runtime block was added in this pass.
- **Hardcoded owner ID remains a single-tenant constant.** This is documented but still a deployment-specific static value that must be updated deliberately for other environments.
