# `.r67` brainrot command and passive 67 reactions

GodForge's `.r67` feature is a deliberately unserious meme surface that gives the
bot personality and an organic discovery hook. It ships three visible pieces —
the `.r67` command, opt-in passive chat reactions, and a per-guild status view —
plus one hidden Easter egg, the `67 Survivor` event.

The behavior below is the locked specification from Issue #47. GodForge never
explains what "67" means, and the feature is styled as community brainrot, not an
advertisement.

## Commands

| Command | Who | Effect |
| --- | --- | --- |
| `.r67` | everyone | Post one random approved response. Always works, even when passive reactions are off. |
| `.r67 reactions on` | Manage Server | Enable passive 67 reactions for the guild. |
| `.r67 reactions off` | Manage Server | Disable passive reactions (and Survivor tracking). |
| `.r67 status` | everyone | Show only whether passive reactions are enabled. |

Passive reactions are **disabled by default**. Status output intentionally
reveals nothing about probabilities, cooldowns, accepted patterns, or the hidden
event.

## Passive reactions

When a guild has opted in, ordinary (non-command) messages are checked for a
standalone 67 reference. Eligible forms:

```
67   6 7   6-7   6/7   6.7
six seven   six-seven   sixty seven   sixty-seven
```

Dates and scores such as `6/7` are intentionally eligible — the accidental
appearance of 67 is part of the joke. The following never match: 67 embedded in a
longer number (`167`, `670`, `1.67.0`), letters touching 67 (`abc67`, `67th`),
URLs, inline code, fenced code blocks, bot messages, and edited messages.

Each eligible message gets one flat **7%** roll when the guild is not on
cooldown. A successful reaction starts a **5-minute guild-wide cooldown**; a
failed roll starts no cooldown. Direct `.r67` commands are independent of the
passive cooldown. Responses are drawn from weighted pools (common / rare /
ultra-rare, plus a rare command-discovery hook only on passive reactions) and the
same response never repeats twice in a row within a guild.

None of these values are admin-configurable in v1.

## Hidden `67 Survivor` event

> Internal documentation only. This event must never appear in user-facing help,
> command output, or `.r67 status`. Do not reveal its trigger conditions in chat.

When six unique human users each post a qualifying passive 67 reference in the
same guild **and channel** within a rolling **seven-second** window, GodForge
grants each of them a cosmetic `67 Survivor` role for **67 minutes** and posts a
single dramatic announcement. Repeated messages from the same user do not count,
`.r67` command usage does not count, and Survivor tracking runs even while passive
reactions are on cooldown. Triggering starts a **67-hour per-guild** cooldown.

The role is cosmetic only (no permissions, not hoisted, not mentionable). An
existing `67 Survivor` role is reused rather than duplicated, and created only
when absent and permissions allow. If GodForge lacks `Manage Roles` or the role
sits above its own top role, the event still announces — it just notes the
survivors could not be marked — and the cooldown still begins. Temporary roles are
removed after 67 minutes; a startup recovery pass and the periodic cleanup task
guarantee a missed timer can never make the role permanent.

## Architecture

`.r67` is the reference implementation of GodForge's feature-oriented module
pattern (see [`FEATURE_ARCHITECTURE.md`](FEATURE_ARCHITECTURE.md)). All logic
lives under `utils/r67/`; `bot.py` only composes and routes.

```
utils/r67/
├── responses.py   approved response pools (no dependencies)
├── matcher.py     standalone-67 detection, URL/code stripping (pure)
├── selector.py    weighted, injectable-RNG selection with no-repeat (pure)
├── tracker.py     in-memory rolling 7s Survivor window (pure)
├── repository.py  durable guild state + role grants in the party SQLite DB
├── roles.py       Discord role adapter (the only Discord-coupled module)
└── service.py     coordinates the above; the single entry the adapter calls
```

`bot.py` integration is intentionally narrow:

- constructs `SQLiteR67Repository` and `R67Service` beside the other repositories;
- routes `.r67` to `service.handle_command(...)`;
- forwards ordinary guild messages to `service.process_passive(...)` and runs the
  Survivor announcement/role grant when one fires;
- calls `service.recover_role_grants(...)` from `on_ready` and
  `service.cleanup_expired_role_grants(...)` from the 5-minute cleanup task.

### Persistence

Durable state lives in the existing `GODFORGE_PARTY_DB_PATH` SQLite database, not
the temporary dashboard settings bridge:

- `r67_guild_state` — opt-in flag, passive cooldown, Survivor cooldown.
- `r67_role_grants` — active temporary role grants with removal-retry
  bookkeeping.

Existing guilds require no migration: a missing row reads as reactions-disabled
with no active cooldowns or grants. Rolling participant windows and failed rolls
are temporary matching state and are never persisted.

## Tests

- `tests/unit/test_r67_matcher.py` — accepted/rejected forms and exclusions.
- `tests/unit/test_r67_selector.py` — weights, tier boundaries, reachability,
  no-repeat.
- `tests/unit/test_r67_repository.py` — persistence, migration defaults, grants.
- `tests/unit/test_r67_service_commands.py` — command routing, permissions,
  status-copy privacy.
- `tests/unit/test_r67_passive.py` — opt-in, roll, cooldown behavior.
- `tests/unit/test_r67_survivor.py` — tracker boundaries, cooldown, role
  lifecycle, cleanup/recovery.
- `tests/integration/test_r67_flow.py` — end-to-end through `bot.on_message`.
