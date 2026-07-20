"""
Draft system for fearless competitive drafting.

Manages per-channel draft state with enforced turn order, unlimited undo,
semi-fearless carry-over (picks only, bans reset each game), and structured
data export.
"""

import asyncio
import time
from datetime import datetime, timezone

from utils import match_ids

DRAFT_TTL_SECONDS = 60 * 60
DRAFT_EXPORT_SCHEMA_VERSION = 2

TURN_SEQUENCE = [
    ("blue", "ban"), ("red", "ban"), ("blue", "ban"),
    ("red", "ban"), ("blue", "ban"), ("red", "ban"),
    ("blue", "pick"), ("red", "pick"), ("red", "pick"),
    ("blue", "pick"), ("blue", "pick"), ("red", "pick"),
    ("red", "ban"), ("blue", "ban"), ("red", "ban"), ("blue", "ban"),
    ("red", "pick"), ("blue", "pick"), ("blue", "pick"), ("red", "pick"),
]

STEPS_PER_GAME = len(TURN_SEQUENCE)

PHASE_RANGES = [
    (0, 5, "Bans 1"),
    (6, 11, "Picks 1"),
    (12, 15, "Bans 2"),
    (16, 19, "Picks 2"),
]


def export_draft_order() -> list[dict]:
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
    for start, end, label in PHASE_RANGES:
        if start <= step <= end:
            return label
    return "Complete"


class GameState:
    def __init__(self, game_number: int):
        self.game_number = game_number
        self.bans = {"blue": [], "red": []}
        self.picks = {"blue": [], "red": []}
        self.step = 0
        self.claims = {"blue": {}, "red": {}}

    def is_complete(self) -> bool:
        return self.step >= STEPS_PER_GAME

    def is_fully_claimed(self) -> bool:
        for side in ("blue", "red"):
            if len(self.claims[side]) < len(self.picks[side]):
                return False
        return self.is_complete()

    def claim(self, team: str, god: str, user_id: int, user_name: str) -> bool:
        if god not in self.picks[team]:
            return False
        if god in self.claims[team]:
            return False
        for info in self.claims[team].values():
            if info["user_id"] == user_id:
                return False
        self.claims[team][god] = {
            "user_id": user_id,
            "name": user_name,
        }
        return True

    def unclaim(self, team: str, god: str) -> dict | None:
        return self.claims[team].pop(god, None)

    def current_turn(self) -> tuple[str, str] | None:
        if self.is_complete():
            return None
        return TURN_SEQUENCE[self.step]

    def get_all_gods(self) -> set:
        gods = set()
        for side in ("blue", "red"):
            gods.update(self.bans[side])
            gods.update(self.picks[side])
        return gods

    def execute(self, god: str) -> tuple[str, str]:
        team, action = TURN_SEQUENCE[self.step]
        if action == "ban":
            self.bans[team].append(god)
        else:
            self.picks[team].append(god)
        self.step += 1
        return team, action

    def undo(self) -> tuple[str, str, str] | None:
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
    def __init__(
        self,
        blue_captain_id: int,
        blue_captain_name: str,
        red_captain_id: int,
        red_captain_name: str,
        guild_id: int,
        guild_name: str,
        channel_id: int,
        channel_name: str,
        forgelens_match_id: str | None = None,
        game_number: int = 1,
        draft_sequence: int = 1,
        match_id: str | None = None,
        party_context: dict | None = None,
    ):
        self.draft_id = match_id or match_ids.reserve_match_id()
        self.match_id = self.draft_id
        self.party_context = dict(party_context or {})
        self.forgelens_match_id = forgelens_match_id or ""
        self.draft_sequence = draft_sequence
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

        self.fearless_pool = set()
        self.completed_games = []
        self.current_game = GameState(game_number=game_number)
        self._undo_stack = []
        self.board_message_id = None
        self.claim_message_ids = {"blue": None, "red": None}

    def current_status(self) -> str:
        if not self.active:
            return "draft_complete" if self.current_game.is_fully_claimed() else "draft_abandoned"
        if self.current_game.is_fully_claimed():
            return "draft_complete"
        if self.current_game.is_complete():
            has_claims = any(self.current_game.claims[side] for side in ("blue", "red"))
            return "claiming" if has_claims else "picks_bans_complete"
        return "drafting"

    def _touch(self):
        self.last_updated = time.monotonic()

    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_updated) > DRAFT_TTL_SECONDS

    def is_claiming(self) -> bool:
        return self.current_game.is_complete() and not self.current_game.is_fully_claimed()

    def get_unavailable_gods(self) -> set:
        unavailable = set(self.fearless_pool)
        unavailable.update(self.current_game.get_all_gods())
        return unavailable

    def get_current_captain_id(self) -> int | None:
        turn = self.current_game.current_turn()
        if turn is None:
            return None
        team, _ = turn
        return self.blue_captain["user_id"] if team == "blue" else self.red_captain["user_id"]

    def get_current_team_and_action(self) -> tuple[str, str] | None:
        return self.current_game.current_turn()

    def execute_step(self, god: str) -> tuple[str, str]:
        team, action = self.current_game.execute(god)
        self._undo_stack.append(("step", {"team": team, "action": action, "god": god}))
        self._touch()
        return team, action

    def claim_god(self, team: str, god: str, user_id: int, user_name: str) -> bool:
        if not self.current_game.claim(team, god, user_id, user_name):
            return False
        self._undo_stack.append(("claim", {"team": team, "god": god}))
        self._touch()
        return True

    def undo(self) -> dict | None:
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
                return {"type": "claim", "team": data["team"], "god": data["god"], "user_name": info["name"]}
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
        if not self.current_game.is_complete():
            return "Current game isn't complete yet. Finish all bans and picks first."
        if not self.current_game.is_fully_claimed():
            return "Not all players have claimed their gods yet."

        self._undo_stack.append(
            ("next_game", {"previous_game": self.current_game, "previous_fearless": set(self.fearless_pool)})
        )

        for side in ("blue", "red"):
            self.fearless_pool.update(self.current_game.picks[side])

        self.completed_games.append(self.current_game)
        self.current_game = GameState(game_number=self.completed_games[-1].game_number + 1)
        self.claim_message_ids = {"blue": None, "red": None}
        self._touch()
        return None

    def end(self) -> dict:
        self.active = False
        self.ended_at = datetime.now(timezone.utc).isoformat()
        return self.to_export_dict()

    def to_export_dict(self) -> dict:
        all_games = [game.to_dict() for game in self.completed_games]
        if self.current_game.step > 0:
            all_games.append(self.current_game.to_dict())

        pick_rows = []
        ban_rows = []
        selected_rows = []
        for game in all_games:
            for team in ("blue", "red"):
                pick_rows.append({"game_number": game["game_number"], "team": team, "gods": list(game["picks"][team])})
                ban_rows.append({"game_number": game["game_number"], "team": team, "gods": list(game["bans"][team])})
                for god, info in game["claims"][team].items():
                    selected_rows.append(
                        {
                            "game_number": game["game_number"],
                            "team": team,
                            "god": god,
                            "claimed_by": dict(info),
                        }
                    )

        return {
            "schema_version": DRAFT_EXPORT_SCHEMA_VERSION,
            "producer": "GodForge",
            "event_type": "draft_export",
            "status": self.current_status(),
            "draft_id": self.draft_id,
            "forgelens_match_id": self.forgelens_match_id,
            "match_id": self.match_id,
            "party": dict(self.party_context),
            "guild_id": self.guild_id,
            "guild_name": self.guild_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "game_number": self.current_game.game_number,
            "draft_sequence": self.draft_sequence,
            "teams": {
                "blue": {"label": "blue", "captain": dict(self.blue_captain)},
                "red": {"label": "red", "captain": dict(self.red_captain)},
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
            "picks": pick_rows,
            "bans": ban_rows,
            "selected_gods": selected_rows,
            "games": all_games,
            "fearless_pool": sorted(self.fearless_pool),
        }

    def sanitized_filename(self) -> str:
        import re

        guild = re.sub(r"[^\w\-]", "", self.guild_name.replace(" ", "-"))
        channel = re.sub(r"[^\w\-]", "", self.channel_name.replace(" ", "-"))
        return f"{guild}_{channel}_{self.draft_id}.json"


class DraftManager:
    def __init__(self):
        self._drafts = {}
        self._locks = {}

    def get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    def start(
        self,
        channel_id: int,
        blue_captain_id: int,
        blue_captain_name: str,
        red_captain_id: int,
        red_captain_name: str,
        guild_id: int,
        guild_name: str,
        channel_name: str,
        forgelens_match_id: str | None = None,
        game_number: int = 1,
        draft_sequence: int = 1,
        match_id: str | None = None,
        party_context: dict | None = None,
    ) -> DraftState | None:
        if channel_id in self._drafts and self._drafts[channel_id].active:
            return None
        draft = DraftState(
            blue_captain_id,
            blue_captain_name,
            red_captain_id,
            red_captain_name,
            guild_id,
            guild_name,
            channel_id,
            channel_name,
            forgelens_match_id=forgelens_match_id,
            game_number=game_number,
            draft_sequence=draft_sequence,
            match_id=match_id,
            party_context=party_context,
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
        expired = [cid for cid, draft in self._drafts.items() if draft.is_expired()]
        for cid in expired:
            self._drafts.pop(cid, None)
            self._locks.pop(cid, None)
        return expired
