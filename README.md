# GodForge — Smite 2 Discord Bot

A Discord bot for randomizing Smite 2 god picks and item builds, with session tracking, competitive drafting, and a match betting system.

God picks render as colored embed cards with the god's portrait. Build commands return plaintext numbered lists.

## Architecture Note

Stat tracking, OCR parsing, and future betting/ledger systems have been moved to:

👉 https://github.com/diese-tech/smite2-stat-bot

GodForge is now intentionally scoped to:

* Session tracking
* Draft logic (picks/bans)
* Match ID generation

All post-match processing (stats, exports, ledger, betting) lives in the stat bot.

This separation is deliberate to keep match orchestration lightweight and stat processing scalable.

## Required AI Workflow Review

Before beginning AI-assisted implementation, debugging, refactoring, migration, or production fix work in this repository, review [docs/AI_WORKFLOW_GUARDRAILS.md](./docs/AI_WORKFLOW_GUARDRAILS.md).

Default behavior: smallest safe change, lowest blast radius, no unrelated file edits, no speculative rewrites, and explicit consideration of scale, queues, caching, indexes, retries, idempotency, rollback, and operational safety.

## Commands

### Gods

| Command   | Result                                       |
|-----------|----------------------------------------------|
| `.rg`     | Random god from the full Smite 2 roster      |
| `.rgj`    | Random jungle god (website pool)             |
| `.rgm`    | Random mid god (website pool)                |
| `.rga`    | Random ADC god (website pool)                |
| `.rgs`    | Random support god (website pool)            |
| `.rgo`    | Random solo god (website pool)               |
| `.roll5`  | 5 random gods from the full roster           |
| `.roll5j` | 5 random jungle gods (website pool)          |

Append `w` for explicit website source or `t` for the in-game tab pool. For example: `.rgjw` (jungle, website), `.rgjt` (jungle, tab), `.rgmt` (mid, tab), `.roll5jt` (5 jungle gods from tab). Default source when omitted is website. Same source/role pattern works for `.roll5{role}{source}` as for `.rg{role}{source}`.

The **website pool** mirrors the curated god list from [smitedraft.com](https://smitedraft.com), the competitive draft tool. The **tab pool** mirrors the gods currently available in the in-game god select tab.

God embeds use buff-themed colors: jungle = yellow, mid = red, ADC = purple, support = green, solo = blue, random = white.

**Weighting:** `.roll5` (no role) uses role-based weights to bias picks toward support/solo gods (weight 1.0) over mid/jungle/adc gods (weight 0.75). Gods in multiple role pools inherit their highest weight. Weights are configurable in `data/gods.json` under the `"weights"` section.

### Builds

Each build is 6 unique items pulled from the relevant pool, listed in the order players should buy them. Players choose their own starters, relics, and aspects.

| Command     | Result                                                 |
|-------------|--------------------------------------------------------|
| `.rc`       | Chaos build — 6 random items from the full master pool |
| `.midint`   | Mid intelligence build                                 |
| `.midstr`   | Mid strength build                                     |
| `.jungint`  | Jungle intelligence build                              |
| `.jungstr`  | Jungle strength build                                  |
| `.soloint`  | Solo intelligence build                                |
| `.solostr`  | Solo strength build                                    |
| `.solohyb`  | Solo hybrid build                                      |
| `.adc`      | Standard ADC build                                     |
| `.adcstr`   | Strength-leaning ADC build                             |
| `.adchyb`   | Hybrid ADC build                                       |
| `.sup`      | Support build (single pool)                            |

Mid and jungle do not have hybrid pools; `.midhyb` and `.junghyb` are silently ignored.

**Optional count:** Any build command can take a trailing number 1-5 for fewer items. For example: `.adcstr 3` returns 3 strength items, `.rc 1` returns a single chaos item, `.sup 4` returns 4 support items. Without a number, the default is 6. Numbers outside 1-5 are silently ignored.

### Sessions

Sessions enable draft tracking in a channel. When a session is active, `.rg` and `.roll5` produce interactive embeds with reaction buttons for locking picks. Picked gods are excluded from all future rolls in that channel until the session ends or resets.

| Command          | Result                                          |
|------------------|-------------------------------------------------|
| `.session start` | Start a draft session in this channel           |
| `.session show`  | Show all picks made so far                      |
| `.session reset` | Clear picks, keep session active (new game)     |
| `.session end`   | End session, show final summary                 |

**How it works during an active session:**

1. Someone runs `.roll5jw` — bot posts 5 gods with 1️⃣-5️⃣ reactions
2. Anyone taps a number — that god is locked to the person who reacted
3. The embed updates to show the locked pick; reactions are removed
4. That god will not appear in any future rolls for this session
5. `.rg` works similarly but with ✅ (lock) and ❌ (discard) reactions

Gods in open (unresolved) rolls are also excluded from new rolls, preventing the same god from appearing in two active rolls at once.

Without an active session, `.rg` and `.roll5` behave normally (no reactions, no tracking).

### Draft (fearless competitive drafting)

The bot supports two draft modes. When `ACTIVITY_BACKEND_URL` is configured it connects to the Activity backend over HTTP and WebSocket. When the URL is not set it falls back to the local draft engine built into the bot.

| Command                        | Result                                    |
|--------------------------------|-------------------------------------------|
| `.draft start @blue @red`      | Start a fearless draft set                |
| `.ban GodName`                 | Ban a god (must be your turn)             |
| `.pick GodName`                | Pick a god (must be your turn)            |
| `.draft show`                  | Full draft history + fearless pool        |
| `.draft next`                  | Lock current game, advance to next        |
| `.draft undo`                  | Undo the last ban, pick, or game advance  |
| `.draft end`                   | End set, post summary + JSON export       |

**How it works:**

1. Two captains are assigned (blue and red) when the draft starts
2. Bot posts a living embed (draft board) showing bans, picks, and whose turn it is
3. Turn order follows Smite 1 classic format (6 bans, 6 picks, 4 bans, 4 picks per game)
4. Bot enforces turn order — only the correct captain can ban/pick on their turn
5. When a game completes, `.draft next` advances to the next game. All **picks** (not bans) from completed games go into the fearless pool and are unavailable for the rest of the set
6. `.draft end` posts a summary embed and attaches a JSON file with the full draft record

**Activity backend mode:** When `ACTIVITY_BACKEND_URL` is set, `.draft start` registers the draft with the backend and opens a WebSocket listener. The draft board embed updates live as captains interact with the Discord Activity. Text commands (`.ban`, `.pick`, etc.) are forwarded to the backend.

**Local fallback mode:** When `ACTIVITY_BACKEND_URL` is not set, the bot runs the draft engine locally. After `.draft start`, captains type `.ban` and `.pick` directly in the channel. When a game completes, claim embeds are posted with 1️⃣-5️⃣ reactions so players can assign gods to themselves.

**God name matching:** Captains can type full names (`Baron Samedi`), aliases (`bs`, `mlf`, `swk`), or prefixes (`baron`, `pos`). Matching is case-insensitive. Aliases are defined in `data/aliases.json`.

Sessions and drafts are mutually exclusive per channel — you cannot have both active at once.

### Match betting (admin only)

The betting system lets admins schedule matches and lets players wager points on outcomes. Betting commands are restricted to specific channels configured via environment variables (see Setup).

#### Match lifecycle (admin)

| Command                                        | Result                                                  |
|------------------------------------------------|---------------------------------------------------------|
| `.match create @TeamA @TeamB`                  | Create a match and open betting                         |
| `.match draft GF-XXXX`                         | Lock betting, mark match in progress (run in handshake channel) |
| `.match resolve GF-XXXX winner @Team`          | Pay out win bets and mark match completed               |
| `.match resolve GF-XXXX prop @player stat val` | Settle an over/under prop bet                           |

Match IDs are auto-assigned (`GF-0001`, `GF-0002`, …) and displayed in all responses.

**Match statuses:**
- `betting_open` — bets accepted
- `in_progress` — draft active, betting locked
- `completed` — winner resolved, win bets paid
- `settled` — all props resolved, match fully closed

#### Placing bets (players)

Bets can only be placed in `#place-bets` while a match is `betting_open`.

| Command | Result |
|---------|--------|
| `.bet GF-XXXX amount @Team win` | Bet on a team to win |
| `.bet GF-XXXX amount @player stat over\|under threshold` | Bet on a player stat prop |

Example: `.bet GF-0001 100 @Omega win` — bet 100 pts on Omega to win match GF-0001.
Example: `.bet GF-0001 50 @SabrinaG kills over 10.5` — bet 50 pts that SabrinaG gets more than 10.5 kills.

**Wallets:** Each player starts with 500 points, auto-seeded on their first bet. Points are deducted when a bet is placed and returned with winnings on resolution. Payouts use pool-proportional math: `payout = (your_bet / winning_side_pool) × total_pool`.

#### Wallet management (admin)

| Command                         | Result                                      |
|---------------------------------|---------------------------------------------|
| `.wallet give @player amount`   | Add points to a player's balance            |
| `.wallet take @player amount`   | Remove points from a player's balance       |
| `.wallet set @player amount`    | Set a player's balance to an exact amount   |
| `.wallet check @player`         | Show a player's current balance             |
| `.wallet wipe`                  | Reset all wallets to 500 pts (backs up first) |

#### Ledger

| Command        | Result                                               |
|----------------|------------------------------------------------------|
| `.ledger reset`| Clear all matches for a new week (wallets untouched) |

The `#betting-ledger` channel displays a persistent paginated embed showing each match with its status, win pools, and prop breakdowns. Use ⬅️ ➡️ to page between matches. The embed updates automatically whenever a match is created, a bet is placed, or a result is posted.

### Utility

| Command | Result                  |
|---------|-------------------------|
| `.help` | Show all commands       |

## Setup

> Runtime modes:
> - Bot-only mode: `DISCORD_TOKEN` required. Run `python bot.py`.
> - Web API only (local dev): `PORT` optional. Run `python web_api/server.py` (protected endpoints require `GODFORGE_ADMIN_PASSWORD`).
> - Combined Railway mode: all environment variables required. Run `python railway_app.py`.
> - Warning: `GODFORGE_ADMIN_PASSWORD=change_me_for_dashboard` is a placeholder; dashboard auth will not be accepted until changed.

1. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

2. **Create a Discord bot:**
   - Go to https://discord.com/developers/applications
   - New Application → Bot → copy the token
   - Under Bot settings, enable **Message Content Intent** and **Server Members Intent**
   - Use OAuth2 URL Generator with scopes `bot` and permissions: `Send Messages`, `Read Message History`, `View Channels`, `Add Reactions`, `Manage Messages`, `Embed Links`
   - The bot needs `Add Reactions` to post reaction buttons and `Manage Messages` to clear reactions after a pick is locked

3. **Configure environment variables:**

   For **local testing:**
   ```
   cp .env.example .env
   ```
   Edit `.env` and fill in your values.

   For **Railway / production:** set each variable in the platform's environment settings. The `.env` file is gitignored and only used locally.

   | Variable | Required | Description |
   |----------|----------|-------------|
   | `DISCORD_TOKEN` | Yes | Bot token from the Developer Portal |
   | `BETTING_LEDGER_CHANNEL_ID` | Yes | Channel ID for `#betting-ledger` |
   | `PLACE_BETS_CHANNEL_ID` | Yes | Channel ID for `#place-bets` |
   | `ACTIVITY_BACKEND_URL` | No | URL of the Activity draft backend — omit to use local draft mode |
   | `ACTIVITY_API_KEY` | No | API key for the Activity backend |
   | `GODFORGE_ADMIN_PASSWORD` | Web dashboard only | Temporary password gate for protected dashboard actions |

4. **Run:**
   ```
   python bot.py
   ```

   To run the combined bot + dashboard service locally or on Railway after deployment approval:
   ```
   python railway_app.py
   ```

## File layout

```
godforge/
  bot.py              # entry point, event handlers, reaction listener
  requirements.txt
  Procfile            # tells Railway/Heroku to run as a worker
  .env.example
  .gitignore
  README.md
  test_bot.py         # local tests (run with --sim for weight simulation)
  data/
    gods.json         # roster, role pools, weights
    builds.json       # item master + role/type build pools
    aliases.json      # god name shorthand aliases for draft commands
    weekly_ledger.json# match history + bet records (reset each week)
    wallets.json      # player point balances (persist across resets)
  utils/
    parser.py         # command string -> intent dict
    picker.py         # random selection with exclusion + weighting
    loader.py         # JSON loading + caching
    formatter.py      # intent + result -> Discord embed or string
    session.py        # per-channel random draft session tracking
    draft.py          # per-channel fearless competitive draft system (local mode)
    resolver.py       # god name resolution (exact/alias/prefix matching)
    ledger.py         # match lifecycle and bet logic
    wallet.py         # player point balance persistence
```

## Updating data

When new gods drop or item pools shift, edit `data/gods.json` or `data/builds.json` directly. The bot caches data on first read, so a restart (or pushing a new commit on Railway) is required to pick up changes.

God portraits are pulled from SmiteFire's CDN (`smitefire.com/images/v2/god/icon/{slug}.png`). The slug is generated from the god's name (lowercase, spaces → hyphens). If a new god's portrait doesn't load, check that SmiteFire has added them to their site.

**Tuning weights:** Edit the `"weights"` section in `data/gods.json` to adjust how heavily `.roll5` biases toward certain roles. Higher weight = more likely to appear. Run `python test_bot.py --sim` locally to see the effect of weight changes on pick distribution before deploying.

## Notes

- Unknown commands are silently ignored, by design — keeps the bot quiet in busy channels.
- Errors (empty pool, malformed JSON, etc.) are surfaced to the user with a `⚠️` prefix and logged to the console.
- Session and draft state lives in memory only — it resets if the bot restarts or redeploys. This is fine for sessions that last ~15 minutes.
- Betting ledger and wallet data persist in `data/` JSON files and survive restarts.
- Run `python test_bot.py` from a terminal to verify logic locally before deploying.
- Run `python test_bot.py --sim` (optionally `--sim 5000`) to simulate weighted `.roll5` distribution.

## Versioning

Version bumps happen when user-facing behavior changes — new commands, changed command behavior, or new features. Internal changes (hardening, data edits, README updates, bug fixes) do not bump the version.

The current version is displayed at the bottom of the `.help` command embeds. Update `GODFORGE_VERSION` in `utils/formatter.py` when shipping a new feature.

Release notes and release gates live in:

- `VERSION_HISTORY.md`
- `RELEASE_PROCESS.md`
- `web/README.md`
- `web_api/README.md`

| Version | Changes |
|---------|---------|
| 1.0 | Initial bot — god picks, builds, all pools populated |
| 1.1 | Embed formatting with god portraits and buff-themed colors |
| 1.2 | `.roll5`, `.rc`, optional build count (1-5) |
| 1.3 | `.help` command |
| 1.4 | Role-based weighting for `.roll5` |
| 1.5 | Sessions — reaction-based picks, god exclusion tracking |
| 1.6 | Fearless draft system, god name resolver with aliases |
| 2.0 | Match betting system — `.match`, `.bet`, `.wallet`, `.ledger`; persistent paginated ledger embed; Activity backend draft integration with local fallback |
| 2.1.0-rc | Release candidate for live dashboard bridge — Discord OAuth staging, SQLite dashboard storage, MEE6-style admin surface |
