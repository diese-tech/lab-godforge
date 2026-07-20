# Architecture refactor roadmap (Issue #48)

This is the implementation roadmap for turning GodForge into a feature-oriented
system with a thin composition layer. It is an *architectural improvement
initiative, not a rewrite*: existing features are migrated incrementally with a
strangler approach, one feature per PR, with behavior held functionally identical
unless a change is intentional.

Read alongside [`FEATURE_ARCHITECTURE.md`](FEATURE_ARCHITECTURE.md), which is the
review contract for the target pattern, and [`R67_BRAINROT.md`](R67_BRAINROT.md),
the reference implementation.

## Current state

- `bot.py` (~3.9k lines) is the Discord entrypoint and today owns event
  orchestration, dot-command routing, slash commands, startup recovery,
  scheduled cleanup, and service initialization.
- Feature logic already lives under `utils/` (party, match, scrims, schedule,
  draft, sessions, custom commands, managed roles), but Discord adapters,
  routing, and lifecycle still sit in `bot.py`.
- Durable state uses the shared `GODFORGE_PARTY_DB_PATH` SQLite database through
  per-feature repositories.

## Target state

- `bot.py` is a composition root: it wires repositories/services, registers
  features, routes events to a single feature entry point each, and orchestrates
  shared lifecycle. No feature business logic remains in `bot.py`.
- Each feature owns its Discord adapter, service, repository (when persistent),
  lifecycle hooks, and tests, and registers lifecycle through the shared
  registry.

## Phases

Phases are ordered by dependency and risk (lowest first). Each is its own PR with
its own acceptance criteria; later phases depend on the shared seams built in the
earlier ones.

### Phase 0 — Reference feature (DONE, Issue #47)

`utils/r67/` is built to the target pattern and serves as the reference. No
migration risk (new code).

### Phase 1 — Shared lifecycle infrastructure (DONE)

- `utils/lifecycle.py`: `FeatureModule`, `LifecycleContext`, `FeatureRegistry`.
- `bot.py` builds one registry, registers `R67Feature`, and drives startup/cleanup
  through it instead of feature-specific calls.
- **Depends on:** Phase 0. **Risk:** low (additive; r67 only). **Rollback:** revert
  the registry wiring; features keep working via direct calls.

### Phase 2 — Shared command routing seam (DONE)

`utils/routing.CommandRegistry` lets features register the exact dot-command
token(s) they own; `on_message` resolves them in one lookup. `.r67` and the
deprecated-economy tokens are migrated. Parser-driven commands stay in the parser
fallback until their own phases.

- **Depends on:** Phase 1. **Risk:** low–medium (touches the hot `on_message`
  path). **Rollback:** revert the registry lookup; explicit routing returns.

### Phase 3 — Sessions & local drafts feature (DONE)

- **3a** — `utils/active_drafts.ActiveDraftStore` owns the restart-pointer JSON
  persistence.
- **3b** — `utils/session_commands.SessionCommandHandler` owns the `.session`
  command family.
- **3c** — `utils/draft_render.DraftRenderer` (board/claim rendering),
  `utils/activity_backend.ActivityBackendClient` (activity HTTP),
  `utils/draft_support` (pure helpers), and `utils/draft_coordinator.
  DraftCoordinator` (local + activity `.draft`/`.ban`/`.pick` handlers, activity
  draft state, WS listener, export posting, claim reactions). `bot.py` keeps thin
  delegators; the party-draft launch registers activity drafts through the
  coordinator.
- **Remaining polish:** the draft-restart notice in `on_ready` is still inline; it
  can become a coordinator startup hook once `LifecycleContext` exposes a channel
  lookup.
- **Depends on:** Phases 1–2. **Risk:** medium (reaction handlers, WS listener,
  activity-backend + `_match_ids`/`_ws_tasks` shared state).

### Phase 4 — Custom commands feature (DONE)

`utils/custom_command_runtime.CustomCommandRuntime` owns matching, channel/role
gating, and per-user cooldowns, with dependencies injected. `bot.py` keeps a thin
delegator.

- **Depends on:** Phase 2. **Risk:** low.

### Phase 5 — Party lifecycle feature(s) (IN PROGRESS)

The largest surface: play panel, lobby cards, queue/ready-check, temporary rooms,
party drafts, and their reconciliation/cleanup in `on_ready` and the cleanup task.
Split into sub-phases (setup, lobby, rooms, queue) so each is a reviewable PR.
Ready-check expiry and room reconciliation become lifecycle hooks.

- **5a (DONE)** — `utils/match_room_factory.MatchRoomServiceFactory` owns per-guild
  temporary-room service construction.
- **Remaining:** the `/party` slash-command group, play-panel/lobby-card handlers,
  queue/ready-check orchestration, and moving `on_ready`/cleanup-task party & room
  reconciliation into feature lifecycle hooks (needs `LifecycleContext` to carry
  the client). This is Discord-adapter-heavy and load-bearing for restart
  recovery — the natural next set of focused PRs.
- **Depends on:** Phases 1–3. **Risk:** high (restart recovery + background
  cleanup are load-bearing here).

### Phase 6 — Match results, history & continuity feature (IN PROGRESS)

Move match result/continuity interaction handlers and history writes behind a
feature module.

- **6a (DONE)** — `utils/match_results` owns result-card rendering, card-identity
  parsing, and history-record creation.
- **Remaining:** the result/continuity button-interaction handlers (`_handle_
  match_result_action`, `_handle_match_continuity_action`).
- **Depends on:** Phase 5. **Risk:** medium.

### Phase 7 — Scrims & scheduled nights features

Move scrim challenge/lock/launch and scheduled-night handlers plus their reminder
cleanup behind feature modules with lifecycle hooks.

- **Depends on:** Phases 5–6. **Risk:** medium.

### Phase 8 — Shared infrastructure hardening & bot.py reduction

Extract remaining cross-cutting helpers (permission checks, guild-settings access,
match-room service factory) into shared infrastructure, and confirm `bot.py`
contains only composition, wiring, registration, and shared orchestration.

- **Depends on:** all prior. **Risk:** low–medium (mechanical).

## Per-phase execution checklist

Every phase PR:

1. Opens with a short implementation plan and explicit acceptance criteria.
2. Moves code *with its responsibility* into the feature package (a move that does
   not transfer ownership does not count).
3. Keeps public command behavior, startup recovery, and background cleanup
   functionally identical unless a change is intentional and documented.
4. Keeps existing tests passing and adds unit + integration tests for the migrated
   surface (integration via the real `bot.py` event with Discord mocked).
5. Registers any lifecycle behavior through the shared registry.
6. Updates `FEATURE_ARCHITECTURE.md` / feature docs.
7. Ends with a summary of completed work and any architectural decisions.

## Migration risks & mitigations

- **Restart recovery / background cleanup regressions** (party, rooms, schedules,
  survivor roles are load-bearing). *Mitigation:* migrate lifecycle to the shared
  registry with characterization tests before moving handlers; keep the 5-minute
  cleanup fallback.
- **Hot-path routing regressions** in `on_message`. *Mitigation:* dispatcher falls
  back to existing routing; integration tests assert unchanged behavior per command.
- **Hidden coupling via shared globals** (`_tracked_messages`, settings, managed
  roles). *Mitigation:* pass dependencies through `LifecycleContext`/constructors;
  forbid new feature-to-feature internal imports.
- **Large, unreviewable diffs.** *Mitigation:* one feature (or sub-surface) per PR;
  Phase 5 is explicitly sub-divided.

## Rollback strategy

Each phase is additive-then-switch: the shared seam is introduced with a fallback
to the existing path, the feature is migrated behind it, then the old path is
removed in the same PR only after tests pass. Reverting a single phase PR restores
the prior working behavior without touching other features.

## Definition of Done (overall refactor)

The initiative is complete when:

- `bot.py` performs only composition, wiring, feature registration, and shared
  lifecycle orchestration; no feature business logic remains.
- Every feature owns its adapter, service, repository (when persistent), lifecycle
  hooks, and tests, and registers lifecycle through the shared registry.
- All persistent state goes through a repository abstraction.
- Startup recovery and background cleanup are feature-owned and registered
  through shared interfaces, with no behavior regressions.
- Public command behavior is unchanged except where intentionally and documentedly
  altered.
- `FEATURE_ARCHITECTURE.md` and contributor guidance describe the pattern, and each
  migrated feature is documented.
