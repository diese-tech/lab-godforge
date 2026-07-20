# Match continuity

Completed matches expose five persistent organizer actions:

- **Run It Back** keeps both role assignments.
- **Shuffle Teams** runs role-aware balanced formation and reports every change.
- **Return to Queue** keeps the durable queue without creating a match.
- **Invite Substitutes** consumes the role-aware waitlist and waits when fewer
  than ten players are available.
- **Continue Series** keeps assignments while reserving the next series match.

`match_continuity` is the transaction boundary. Its `(guild_id,
source_match_id)` primary key permits exactly one next-state decision.
Deterministic next-match IDs and operation IDs make Discord retries harmless.
For a ready next state, GodForge reconciles the existing temporary rooms and
creates the next authoritative match-history record. The result card reports
substitutions, team moves, and role moves.

Controls use stable `godforge:match:continuity:*:v1` custom IDs and are
registered during startup, so they survive process restarts.
