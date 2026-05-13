"""
Draft system for fearless competitive drafting.

Manages per-channel draft state with enforced turn order, unlimited undo,
semi-fearless carry-over (picks only, bans reset each game), and structured
data export.

Turn sequence per game (Smite 1 classic):
  Bans 1:  B R B R B R        (steps 0-5)
  Picks 1: R B B R R B        (steps 6-11)
  Bans 2:  R B R B            (steps 12-15)
  Picks 2: R B B R            (steps 16-19)

Production hardening:
- Per-channel asyncio.Lock (shared with sessions via bot.py)
- TTL-based cleanup for abandoned drafts
- Session ID generation for record-keeping
"""

import asyncio
import random
import string
import time
from datetime import datetime, timezone

from utils import match_ids

# Drafts expire after 60 minutes of inactivity.
DRAFT_TTL_SECONDS = 60 * 60
DRAFT_EXPORT_SCHEMA_VERSION = 1

# Turn sequence: (team, action) for each of the 20 steps per game.
TURN_SEQUENCE = [
    # Bans 1: B R B R B R
    ("blue", "ban"), ("red", "ban"), ("blue", "ban"),
    ("red", "ban"), ("blue", "ban"), ("red", "ban"),
    # Picks 1: R B B R R B
    ("red", "pick"), ("blue", "pick"), ("blue", "pick"),
    ("red", "pick"), ("red", "pick"), ("blue", "pick"),
    # Bans 2: R B R B
    ("red", "ban"), ("blue", "ban"), ("red", "ban"), ("blue", "ban"),
    # Picks 2: B R R B
    ("blue", "pick"), ("red", "pick"), ("red", "pick"), ("blue", "pick"),
]

STEPS_PER_GAME = len(TURN_SEQUENCE)

# Phase boundaries for display labels.
PHASE_RANGES = [
    (0, 5, "Bans 1"),
    (6, 11, "Picks 1"),
    (12, 15, "Bans 2"),
    (16, 19, "Picks 2"),
]


def _generate_draft_id() -> str:
    """Generate a short unique draft ID like 'GF-A7K2'."""
    chars = random.choices(string.ascii_uppercase + string.digits, k=4)
    return f"GF-{''.join(chars)}"


def export_draft_order() -> list[dict]:
    """Return the canonical pick/ban order used by every game."""
    return [
        {
            "step": index,
            "team": team,
            "action": action,
            "phase": get_phase_label(index),
        }
        for index, (team, action) in enumerate(TURN_SEQUENCE)
    ]


def get_phase_label(step: int) -> str:
    """Return the current phase name for a given step index."""
    for start, end, label in PHASE_RANGES:
        if start <= step <= end:
            return label
    return "Complete"


class GameState:
    """State for a single game within a draft set."""

    def __init__(self, game_number: int):
        self.game_number = game_number
        self.bans = {"blue": [], "red": []}
        self.picks = {"blue": [], "red": []}
        self.step = 0
        # Claims: god_name -> {"user_id": int, "name": str}
        # Player-to-god assignment is draft context; ForgeLens owns stats/OCR.
        self.claims = {"blue": {}, "red": {}}

    def is_complete(self) -> bool:
        return self.step >= STEPS_PER_GAME

    def is_fully_claimed(self) -> bool:
        """True when all 10 picks have been claimed by players."""
        for side in ("blue", "red"):
            if len(self.claims[side]) < len(self.picks[side]):
                return False
        return self.is_complete()

    def claim(self, team: str, god: str, user_id: int, user_name: str) -> bool:
        """
        Claim a god for a player. Returns True on success.
        Fails if god isn't in this team's picks, already claimed,
        or user already claimed a god on this team.
        """
        if god not in self.picks[team]:
            return False
        if god in self.claims[team]:
            return False  # already claimed
        # Check if user already claimed on this team
        for info in self.claims[team].values():
            if info["user_id"] == user_id:
                return False
        self.claims[team][god] = {
            "user_id": user_id,
            "name": user_name,
        }
        return True

    def unclaim(self, team: str, god: str) -> dict | None:
        """Remove a claim. Returns the claim info or None."""
        return self.claims[team].pop(god, None)

    def current_turn(self) -> tuple[str, str] | None:
        """Return (team, action) for current step, or None if game complete."""
        if self.is_complete():
            return None
        return TURN_SEQUENCE[self.step]

    def get_all_gods(self) -> set:
        """All gods picked or banned in this game."""
        gods = set()
        for side in ("blue", "red"):
            gods.update(self.bans[side])
            gods.update(self.picks[side])
        return gods

    def execute(self, god: str) -> tuple[str, str]:
        """Execute the current turn step. Returns (team, action)."""
        team, action = TURN_SEQUENCE[self.step]
        if action == "ban":
            self.bans[team].append(god)
        else:
            self.picks[team].append(god)
        self.step += 1
        return team, action

    def undo(self) -> tuple[str, str, str] | None:
        """Undo the last action. Returns (team, action, god) or None."""
        if self.step <= 0:
            return None
        self.step -= 1
        team, action = TURN_SEQUENCE[self.step]
        if action == "ban":
            god = self.bans[team].pop()
        else:
            god = self.picks[team].pop()
        return team, action, god

    def to_dict(self) -> dict:
        """Export for JSON serialization."""
        claims_export = {}
        for side in ("blue", "red"):
            claims_export[side] = {}
            for god, info in self.claims[side].items():
                claims_export[side][god] = {
                    "user_id": info["user_id"],
                    "name": info["name"],
                }
        return {
            "game_number": self.game_number,
            "bans": {"blue": list(self.bans["blue"]), "red": list(self.bans["red"])},
            "picks": {"blue": list(self.picks["blue"]), "red": list(self.picks["red"])},
            "claims": claims_export,
        }


class DraftState:
    """Full draft state for a fearless set in a single channel."""

    def __init__(self, blue_captain_id: int, blue_captain_name: str,
                 red_captain_id: int, red_captain_name: str,
                 guild_id: int, guild_name: str,
                 channel_id: int, channel_name: str,
                 match_id: str | None = None):
        self.match_id = match_id or match_ids.reserve_match_id()
        # Keep draft_id as a compatibility alias for existing embeds and tests.
        self.draft_id = self.match_id
        self.active = True
        self.last_updated = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.ended_at = None

        self.blue_captain = {"user_id": blue_captain_id, "name": blue_captain_name}
        self.red_captain = {"user_id": red_captain_id, "name": red_captain_name}

        self.guild_id = guild_id
        self.guild_name = guild_name
        self.channel_id = channel_id
        self.channel_name = channel_name

        # Fearless pool: picks (NOT bans) from completed games.
        self.fearless_pool = set()

        # Completed games history.
        self.completed_games = []

        # Current game.
        self.current_game = GameState(game_number=1)

        # Undo stack: list of (action_type, data) for unlimited undo.
        self._undo_stack = []

        # Living embed message ID (for editing).
        self.board_message_id = None

        # Claim embed message IDs (blue, red) — for reaction tracking.
        self.claim_message_ids = {"blue": None, "red": None}

    def _touch(self):
        self.last_updated = time.monotonic()

    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_updated) > DRAFT_TTL_SECONDS

    def is_claiming(self) -> bool:
        """True when game is complete but claims are still open."""
        return (self.current_game.is_complete()
                and not self.current_game.is_fully_claimed())

    def get_unavailable_gods(self) -> set:
        """Gods that cannot be picked or banned in the current game."""
        unavailable = set(self.fearless_pool)
        unavailable.update(self.current_game.get_all_gods())
        return unavailable

    def get_current_captain_id(self) -> int | None:
        """User ID of whoever should act next, or None if game complete."""
        turn = self.current_game.current_turn()
        if turn is None:
            return None
        team, _ = turn
        if team == "blue":
            return self.blue_captain["user_id"]
        return self.red_captain["user_id"]

    def get_current_team_and_action(self) -> tuple[str, str] | None:
        return self.current_game.current_turn()

    def execute_step(self, god: str) -> tuple[str, str]:
        """Execute a ban or pick. Returns (team, action)."""
        team, action = self.current_game.execute(god)
        self._undo_stack.append(("step", {"team": team, "action": action, "god": god}))
        self._touch()
        return team, action

    def claim_god(self, team: str, god: str, user_id: int, user_name: str) -> bool:
        """Claim a god. Returns True on success."""
        if not self.current_game.claim(team, god, user_id, user_name):
            return False
        self._undo_stack.append(("claim", {"team": team, "god": god}))
        self._touch()
        return True

    def undo(self) -> dict | None:
        """Undo the last action. Returns info dict or None."""
        if not self._undo_stack:
            return None

        action_type, data = self._undo_stack.pop()

        if action_type == "step":
            result = self.current_game.undo()
            if result:
                team, action, god = result
                self._touch()
                return {"type": "step", "team": team, "action": action, "god": god}
        elif action_type == "claim":
            info = self.current_game.unclaim(data["team"], data["god"])
            if info:
                self._touch()
                return {"type": "claim", "team": data["team"], "god": data["god"],
                        "user_name": info["name"]}
        elif action_type == "next_game":
            prev_game = data["previous_game"]
            prev_fearless = data["previous_fearless"]
            self.completed_games.pop()
            self.current_game = prev_game
            self.fearless_pool = prev_fearless
            self._touch()
            return {"type": "next_game", "game_number": prev_game.game_number}

        return None

    def advance_game(self) -> str | None:
        """
        Advance to the next game. Current game's picks go to fearless pool.
        Returns error string if not ready, None on success.
        """
        if not self.current_game.is_complete():
            return "Current game isn't complete yet. Finish all bans and picks first."
        if not self.current_game.is_fully_claimed():
            return "Not all players have claimed their gods yet."

        self._undo_stack.append(("next_game", {
            "previous_game": self.current_game,
            "previous_fearless": set(self.fearless_pool),
        }))

        for side in ("blue", "red"):
            self.fearless_pool.update(self.current_game.picks[side])

        self.completed_games.append(self.current_game)
        self.current_game = GameState(
            game_number=len(self.completed_games) + 1
        )
        self.claim_message_ids = {"blue": None, "red": None}
        self._touch()
        return None

    def end(self) -> dict:
        """End the draft. Returns the full export dict."""
        self.active = False
        self.ended_at = datetime.now(timezone.utc).isoformat()
        return self.to_export_dict()

    def to_export_dict(self) -> dict:
        """Full structured export for JSON serialization."""
        all_games = [g.to_dict() for g in self.completed_games]
        if self.current_game.step > 0:
            all_games.append(self.current_game.to_dict())

        return {
            "schema_version": DRAFT_EXPORT_SCHEMA_VERSION,
            "producer": "GodForge",
            "match_id": self.match_id,
            "draft_id": self.draft_id,
            "guild_id": self.guild_id,
            "guild_name": self.guild_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "teams": {
                "blue": {"captain": dict(self.blue_captain)},
                "red": {"captain": dict(self.red_captain)},
            },
            "blue_captain": dict(self.blue_captain),
            "red_captain": dict(self.red_captain),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "timestamps": {
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            },
            "draft_order": export_draft_order(),
            "games": all_games,
            "fearless_pool": sorted(self.fearless_pool),
        }

    def sanitized_filename(self) -> str:
        """Generate a safe filename for the JSON export."""
        import re
        guild = re.sub(r'[^\w\-]', '', self.guild_name.replace(' ', '-'))
        channel = re.sub(r'[^\w\-]', '', self.channel_name.replace(' ', '-'))
        return f"{guild}_{channel}_{self.draft_id}.json"


class DraftManager:
    """Manages per-channel draft state."""

    def __init__(self):
        self._drafts = {}
        self._locks = {}

    def get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    def start(self, channel_id: int,
              blue_captain_id: int, blue_captain_name: str,
              red_captain_id: int, red_captain_name: str,
              guild_id: int, guild_name: str,
              channel_name: str,
              match_id: str | None = None) -> DraftState | None:
        """Start a draft. Returns DraftState or None if one is already active."""
        if channel_id in self._drafts and self._drafts[channel_id].active:
            return None
        draft = DraftState(
            blue_captain_id, blue_captain_name,
            red_captain_id, red_captain_name,
            guild_id, guild_name,
            channel_id, channel_name,
            match_id=match_id,
        )
        self._drafts[channel_id] = draft
        return draft

    def get(self, channel_id: int) -> DraftState | None:
        draft = self._drafts.get(channel_id)
        if draft and draft.active:
            return draft
        return None

    def end(self, channel_id: int) -> DraftState | None:
        draft = self._drafts.pop(channel_id, None)
        self._locks.pop(channel_id, None)
        if draft and draft.active:
            draft.end()
            return draft
        return None

    def cleanup_expired(self) -> list[int]:
        expired = [
            cid for cid, d in self._drafts.items()
            if d.is_expired()
        ]
        for cid in expired:
            self._drafts.pop(cid, None)
            self._locks.pop(cid, None)
        return expired
