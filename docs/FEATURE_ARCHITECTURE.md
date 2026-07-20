# Feature-oriented architecture

GodForge is migrating from a single large `bot.py` toward a feature-oriented
system with a thin composition layer (Issue #48). The `.r67` package
(`utils/r67/`) is the first feature built entirely to this pattern and is the
reference implementation for new work and for reviewing migrations.

This document is the review contract. Deviations are allowed only when documented
with rationale, tradeoffs, and follow-up actions.

## The composition-root contract for `bot.py`

`bot.py` may perform composition, dependency wiring, feature registration, and
shared lifecycle orchestration **only**. Concretely, for a feature it may:

- construct the feature's repository and service next to the others;
- route a matched command to a single feature entry point;
- forward eligible events (messages, reactions, voice updates) to a single
  feature handler;
- call the feature's startup-recovery and cleanup hooks from the shared
  `on_ready` and periodic cleanup task.

`bot.py` must **not** contain feature business logic: no matcher regexes, weighted
pools, rolling-window logic, permission policy, persistence access, or role
cleanup. Moving code into another file without transferring responsibility does
not satisfy the pattern.

## What a feature owns

Each feature owns, under its own package:

| Concern | Module (r67 example) | Notes |
| --- | --- | --- |
| Pure domain logic | `matcher.py`, `selector.py`, `tracker.py`, `responses.py` | No Discord, no I/O. Dependency-inject randomness/clock for deterministic tests. |
| Persistence | `repository.py` | A repository abstraction over the shared SQLite DB — never ad hoc JSON/SQLite from handlers. |
| Discord adapter | `roles.py` (and the thin handlers in `bot.py`) | Discord objects stay at this boundary. |
| Service | `service.py` | Coordinates the above; operates on explicit inputs and returns explicit results. The single surface `bot.py` calls. |
| Tests | `tests/unit/test_r67_*.py`, `tests/integration/test_r67_flow.py` | Unit-test pure logic and the service; integration-test the real `bot.py` routing with Discord mocked. |

### Boundaries

- **Discord objects at the edge.** Service methods take ids and primitives and
  return dataclasses (e.g. `PassiveOutcome`, `SurvivorGrantResult`); the adapter
  translates to/from `discord.*`. The one exception is methods that must perform
  Discord role work, which accept a `guild` and delegate every raw call to the
  adapter module.
- **Persistence through a repository.** New durable state goes in a repository
  with restart-safe CRUD. Temporary matching state (rolling windows, failed
  rolls) is never persisted.
- **Feature-to-feature via interfaces.** Direct feature-to-feature internal
  imports are disallowed; depend on shared infrastructure or a defined interface.
- **Feature-owned lifecycle.** Startup recovery and background cleanup live in the
  feature's service and are *registered* through the shared `on_ready` / cleanup
  task — the orchestration is shared, the behavior is feature-owned.

## Adding a new feature

1. Create `utils/<feature>/` with pure logic modules first; unit-test them with
   injected randomness/clock.
2. Add a `repository.py` if the feature needs durable state; reuse
   `GODFORGE_PARTY_DB_PATH` and mirror the `SQLiteR67Repository` connection and
   `_ensure_schema` pattern (idempotent `CREATE TABLE IF NOT EXISTS`).
3. Add an adapter module for any Discord-coupled operations.
4. Add a `service.py` that coordinates the modules and exposes small methods for
   command handling, event handling, startup recovery, and cleanup.
5. Wire `bot.py`: construct the repository/service, route the command/event to
   the service, and register recovery/cleanup hooks. Keep every change narrow.
6. Add unit tests for logic and service, plus an integration test that drives the
   real `bot.on_message` (or relevant event) with Discord mocked.
7. Update `docs/` — a feature doc and this file when the pattern evolves. No PR is
   complete until documentation is updated.

## Migration approach

Existing features are migrated incrementally using a strangler approach rather
than a rewrite: new work follows this pattern, and existing surfaces are moved
behind feature packages one at a time while behavior stays functionally identical
(existing tests continue to pass, startup recovery and background cleanup keep
working, and public command behavior is unchanged). The phase-by-phase roadmap is
tracked in Issue #48.
