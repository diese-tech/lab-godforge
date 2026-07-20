# Match Results And Game-Night History

GodForge records recreational matches that it organizes. This standalone
feature does not call ForgeLens and has no wallet, wager, odds, payout, betting,
or economy behavior.

## Result lifecycle

`utils.match_history.MatchHistoryRepository` uses the same SQLite deployment
database as the party workflow:

1. Draft launch creates a record containing the guild, organizer, two named
   teams, assigned roles, participants, and optional GodForge draft reference.
2. Each registered captain reports `team_one` or `team_two`.
3. Matching reports complete the match. Conflicting reports set `disputed`.
4. Only the stored organizer can resolve a dispute or explicitly record
   `cancelled` or `no_contest`.
5. Every mutation carries the Discord interaction ID as `operation_id`, making
   retries safe while rejecting reuse for different input.

The optional draft reference is opaque, so history can consume the forthcoming
GodForge draft-launch record without depending on its implementation or a
companion service.

## Views and statistics

The repository exposes recent records for a guild, named team, or player.
`player_stats` calculates GodForge-run game-night appearances, wins, current
winning streak, role frequency, and teammate frequency.

Cancelled, no-contest, pending, and disputed matches remain visible in history
but do not count as played appearances or wins. Queries are guild scoped and
capped at 500 records.

## Discord integration

When a party draft becomes active, `bot._ensure_match_history` idempotently
creates its authoritative match record from the durable `PartyDraftLaunch`
teams and the lobby's assigned roles. GodForge posts a persistent result card:
captains can report Blue or Red, while the organizer can resolve a result,
cancel it, or mark no contest. The result card is registered again after bot
restarts.

Recent-match surfaces use `recent_for_guild`, `recent_for_team`, and
`recent_for_player`.

Any future analytics export must subscribe after the authoritative GodForge
transaction and must never block result recording.
