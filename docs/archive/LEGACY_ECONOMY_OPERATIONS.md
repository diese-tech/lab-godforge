# Legacy Economy Operations

> Archived during the v2.3 standalone transition. This is migration and
> rollback context, not supported GodForge setup guidance.

GodForge v2.0 and v2.1 stored match-ledger, wallet, betting, and dashboard
documents in JSON files under `data/`, with an optional SQLite dashboard store.
The associated Discord commands are unavailable in the standalone product.
Guarded web mutations remain in the codebase only as a failsafe while old data
is retained.

Historical files and settings include:

| Item | Historical purpose |
| --- | --- |
| `data/weekly_ledger.json` | Match ledger and bets |
| `data/wallets.json` | Wallet balances |
| `BETTING_LEDGER_CHANNEL_ID` | Ledger delivery channel |
| `PLACE_BETS_CHANNEL_ID` | Bet placement channel |
| `GODFORGE_ENABLE_LEGACY_ECONOMY` | Explicitly unlock legacy web mutations |

Do not enable or modify these surfaces for a normal GodForge deployment.
Preserve existing files until a separately reviewed export/removal migration is
available. The archived API shapes are in
[`LEGACY_WEB_DATA_CONTRACT.md`](LEGACY_WEB_DATA_CONTRACT.md).
