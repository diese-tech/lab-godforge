# Historical GodForge Web Data Contract

> Archived during the v2.3 standalone transition. Economy, wallet, betting, and
> companion-specific sections below describe migration-era behavior, not the
> supported product surface.

The web dashboard consumes these JSON shapes from `web_api` and from demo fallback data in `web/js/api.js`.

## Match

```ts
type MatchStatus = "betting_open" | "in_progress" | "completed" | "settled";

type Match = {
  match_id: string;
  teams: {
    team1: string;
    team2: string;
  };
  status: MatchStatus;
  bets: Bet[];
  result: string | null;
  winner: string | null;
  resolved_props: ResolvedProp[];
};

type Ledger = {
  matches: Match[];
  embed_message_id: number | null;
  embed_channel_id: number | null;
};
```

## Bet

```ts
type WinBet = {
  type: "win";
  user_id: number;
  username: string;
  team: string;
  amount: number;
};

type PropBet = {
  type: "prop";
  user_id: number;
  username: string;
  player: string;
  stat: string;
  direction: "over" | "under";
  threshold: number;
  amount: number;
};

type Bet = WinBet | PropBet;

type ResolvedProp = {
  player: string;
  stat: string;
  actual_value: number;
  threshold: number;
  winning_direction: "over" | "under" | null;
};
```

`POST /api/bet/place` returns the placed `Bet`, the updated `Match`, and the bettor's updated wallet summary:

```ts
type PlaceBetResult = {
  ok: true;
  bet: Bet;
  match: Match;
  wallet: {
    user_id: number;
    username: string;
    balance: number;
  };
};
```

## Wallet

`data/wallets.json` is keyed by Discord user id. The local web API also accepts a plain username and creates a stable local-only numeric id for dashboard testing.

```ts
type Wallets = Record<string, Wallet>;

type Wallet = {
  username: string;
  balance: number;
};
```

## God

```ts
type God = {
  name: string;
  role: "jungle" | "mid" | "adc" | "support" | "solo" | null;
  source: "website" | "tab";
  command: string;
  imageUrl: string;
  pantheon?: string;
  class?: string;
};
```

## Build

```ts
type Build = {
  role: "adc" | "mid" | "jungle" | "solo" | "support" | "chaos";
  type: "standard" | "str" | "int" | "hyb" | "" | null;
  count: number;
  items: string[];
  command: string;
};
```

## Draft State

```ts
type DraftState = {
  draftId: string;
  matchId: string;
  forgelensMatchId: string;
  gameNumber: number;
  draftSequence: number;
  draftStatus:
    | "drafting"
    | "picks_bans_complete"
    | "claiming"
    | "draft_complete"
    | "draft_abandoned";
  phase: string;
  step: number;
  complete: boolean;
  currentTurn: null | {
    team: "blue" | "red";
    action: "ban" | "pick";
  };
  blueCaptain: string;
  redCaptain: string;
  bans: {
    blue: string[];
    red: string[];
  };
  picks: {
    blue: string[];
    red: string[];
  };
  fearlessPool: string[];
  unavailableGods: string[];
};
```

GodForge owns `draftId`. `matchId` is a legacy alias for older consumers. ForgeLens integrations should link via optional `forgelensMatchId` instead of assuming the two ids are the same.

## Admin Status

`GET /api/admin/status` is protected and is used by the Overview operations monitor.

`GET /api/auth/status` also reports whether Discord OAuth env vars are configured:

```ts
type AuthStatus = {
  ok: true;
  authenticated: boolean;
  configured: boolean;
  discordOAuthConfigured: boolean;
};
```

```ts
type AdminStatusResponse = {
  ok: true;
  status: AdminStatus;
};

type AdminStatus = {
  bot: {
    connected: boolean;
    user: string | null;
    guildCount: number;
    guilds: Array<{
      id: string;
      name: string;
    }>;
    latencyMs: number | null;
    error?: string;
  };
  data: {
    ledgerPath: string;
    walletsPath: string;
    matchCount: number;
    walletCount: number;
    statusCounts: Record<MatchStatus | string, number>;
    embedConfigured: boolean;
  };
  modules: ModuleHealth[];
  storage: {
    kind: "json" | "sqlite";
    path: string;
    available: boolean;
    error?: string;
  };
  draftRooms: number;
  checkedAt: number;
};

type ModuleHealth = {
  key: string;
  label: string;
  state: "ready" | "needs_setup" | "staged" | "disabled";
  enabled: boolean;
  detail: string;
  needs: string[];
};
```

`POST /api/admin/sync/ledger` is protected and returns whether a Discord embed refresh could be queued on the running bot loop:

```ts
type LedgerSyncResponse = {
  ok: true;
  discord_embed_update: boolean;
};
```

## Guild Settings

`GET /api/settings?guild_id=global` and `POST /api/settings` are protected. The current Railway milestone uses JSON persistence in `data/guild_settings.json`; the shape is intentionally compatible with a future database row/document keyed by Discord guild id.

```ts
type GuildSettings = {
  guild_id: string;
  features: {
    botEnabled: boolean;
    randomizerEnabled: boolean;
    draftsEnabled: boolean;
    bettingEnabled: boolean;
  };
  channels: {
    matchChannel: string;
    bettingChannel: string;
    adminChannel: string;
  };
  roles: {
    adminRole: string;
    captainRole: string;
  };
  permissions: {
    monetizeAccess: "none" | "read" | "manage";
  };
  updated_at: number | null;
  updated_by: string | null;
};
```

## Admin Audit Event

`GET /api/admin/audit?limit=25` is protected and returns newest events first.

```ts
type AdminAuditEvent = {
  ts: number;
  actor: string;
  action: string;
  target: string;
  status: string;
  metadata: Record<string, string | number | boolean | null>;
};
```

## Custom Command Config

`GET /api/commands/custom?guild_id=global`, `POST /api/commands/custom`, and `POST /api/commands/custom/delete` are protected. These records are configuration only until the bot-side custom command resolver is implemented.

```ts
type CustomCommandConfig = {
  trigger: string;
  response: string;
  channel: string;
  role_gate: "Everyone" | "Captains" | "Admins";
  cooldown: string;
  enabled: boolean;
  updated_at: number;
};
```
