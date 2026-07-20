# Team rosters and scrims

GodForge supports lightweight, guild-scoped premade teams without becoming a
tournament or economy bot.

## Workflow

1. A captain registers or updates a team with `/scrim team-create`. The captain
   is always part of the active roster; substitutes, region, and human-readable
   availability are stored with the team.
2. `/scrim challenge` posts a durable challenge card. The receiving captain can
   accept or reject with its persistent buttons. `/scrim respond` also supports
   a counterproposal; this swaps proposing/responding teams and keeps the same
   challenge ID.
3. Both captains check in. The original organizer, or a member with Manage
   Server, snapshots both active rosters with `/scrim lock`.
4. `/scrim launch` creates a one-time scheduled night and converts it through
   the existing party repository and queue. From there the ordinary lobby,
   ready check, temporary rooms, draft, and result workflow owns the match.

Team edits after roster lock do not affect the match snapshot. Mutations use
Discord interaction IDs and deterministic entity IDs so retries and restarts do
not create duplicate teams, challenges, schedules, or lobbies.

## Commands

- `/scrim team-create`
- `/scrim teams`
- `/scrim challenge`
- `/scrim respond`
- `/scrim checkin`
- `/scrim lock`
- `/scrim launch`

Bracket generation, standings, wagers, betting, and other economy features are
explicitly out of scope.
