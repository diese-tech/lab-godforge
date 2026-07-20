"""Durable, idempotent transition from a ready party to a fearless draft."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from utils.party import LobbyState, PartyLobby, utc_now
from utils.team_formation import (
    FormationMode,
    FormationResult,
    TeamFormationError,
    form_smite_teams,
    profiles_from_preferences,
)


class PartyDraftError(RuntimeError):
    """A party cannot currently launch a draft."""


@dataclass(frozen=True, slots=True)
class DraftTeam:
    captain_id: int
    participant_ids: tuple[int, ...]
    role_assignments: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PartyDraftLaunch:
    lobby_id: str
    guild_id: int
    operation_id: str
    status: str
    match_id: str
    channel_id: int
    blue: DraftTeam
    red: DraftTeam
    snapshot: dict[str, object]
    error: str = ""


def form_teams(
    lobby: PartyLobby,
    *,
    mode: FormationMode | str = FormationMode.ROLE_FIT,
    organizer_inputs: dict[int, dict[str, object]] | None = None,
) -> tuple[DraftTeam, DraftTeam]:
    blue, red, _ = form_teams_with_result(
        lobby, mode=mode, organizer_inputs=organizer_inputs
    )
    return blue, red


def form_teams_with_result(
    lobby: PartyLobby,
    *,
    mode: FormationMode | str = FormationMode.ROLE_FIT,
    organizer_inputs: dict[int, dict[str, object]] | None = None,
) -> tuple[DraftTeam, DraftTeam, FormationResult]:
    """Make deterministic, role-complete teams from GodForge-owned inputs."""
    if lobby.state is not LobbyState.FORMING:
        raise PartyDraftError("the lobby must finish its ready check first")
    try:
        result = form_smite_teams(
            profiles_from_preferences(
                lobby.participants,
                organizer_inputs=organizer_inputs,
            ),
            mode,
        )
    except TeamFormationError as exc:
        raise PartyDraftError(str(exc)) from exc

    def draft_team(team):
        return DraftTeam(
            team.captain_id,
            team.participant_ids,
            tuple((assignment.user_id, assignment.role) for assignment in team.assignments),
        )

    return draft_team(result.blue), draft_team(result.red), result


class PartyDraftLaunchRepository:
    """SQLite launch intent/outcome store shared with the party repository."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS party_draft_launches (
                  lobby_id TEXT PRIMARY KEY,
                  guild_id INTEGER NOT NULL,
                  operation_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  match_id TEXT NOT NULL DEFAULT '',
                  channel_id INTEGER NOT NULL,
                  blue_json TEXT NOT NULL,
                  red_json TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS party_draft_launch_operation
                  ON party_draft_launches(operation_id);
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def begin(
        self,
        lobby: PartyLobby,
        *,
        operation_id: str,
        channel_id: int,
        match_id_factory: Callable[[], str],
        formation_mode: FormationMode | str = FormationMode.ROLE_FIT,
        organizer_inputs: dict[int, dict[str, object]] | None = None,
    ) -> tuple[PartyDraftLaunch, bool]:
        blue, red, formation = form_teams_with_result(
            lobby,
            mode=formation_mode,
            organizer_inputs=organizer_inputs,
        )
        snapshot = _snapshot(lobby, formation)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM party_draft_launches WHERE lobby_id=? OR operation_id=?",
                (lobby.lobby_id, operation_id),
            ).fetchone()
            if row and row["lobby_id"] != lobby.lobby_id:
                conn.rollback()
                raise PartyDraftError("interaction ID was already used by another lobby")
            if row and row["status"] in {"pending", "active"}:
                conn.commit()
                return _decode(row), False

            match_id = match_id_factory()
            now = utc_now().isoformat()
            values = (
                lobby.lobby_id,
                lobby.guild_id,
                operation_id,
                "pending",
                match_id,
                channel_id,
                _team_json(blue),
                _team_json(red),
                json.dumps(snapshot, sort_keys=True),
                "",
                now,
                now,
            )
            conn.execute(
                """INSERT INTO party_draft_launches
                   (lobby_id,guild_id,operation_id,status,match_id,channel_id,
                    blue_json,red_json,snapshot_json,error,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(lobby_id) DO UPDATE SET
                     operation_id=excluded.operation_id,
                     status=excluded.status,
                     match_id=excluded.match_id,
                     channel_id=excluded.channel_id,
                     blue_json=excluded.blue_json,
                     red_json=excluded.red_json,
                     snapshot_json=excluded.snapshot_json,
                     error='',
                     updated_at=excluded.updated_at""",
                values,
            )
            row = conn.execute(
                "SELECT * FROM party_draft_launches WHERE lobby_id=?", (lobby.lobby_id,)
            ).fetchone()
            conn.commit()
            return _decode(row), True

    def mark_active(self, lobby_id: str, *, match_id: str | None = None) -> PartyDraftLaunch:
        return self._finish(lobby_id, "active", match_id=match_id)

    def mark_failed(self, lobby_id: str, error: str) -> PartyDraftLaunch:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE party_draft_launches
                   SET status='failed', error=?, updated_at=?
                   WHERE lobby_id=? AND status='pending'""",
                (error[:500], utc_now().isoformat(), lobby_id),
            )
            row = conn.execute(
                "SELECT * FROM party_draft_launches WHERE lobby_id=?", (lobby_id,)
            ).fetchone()
            if row is None:
                raise PartyDraftError("draft launch was not reserved")
            return _decode(row)

    def get(self, lobby_id: str) -> PartyDraftLaunch | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM party_draft_launches WHERE lobby_id=?", (lobby_id,)
            ).fetchone()
            return _decode(row) if row else None

    def _finish(
        self,
        lobby_id: str,
        status: str,
        *,
        match_id: str | None = None,
        error: str = "",
    ) -> PartyDraftLaunch:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE party_draft_launches
                   SET status=?, match_id=COALESCE(?, match_id), error=?, updated_at=?
                   WHERE lobby_id=?""",
                (status, match_id, error, utc_now().isoformat(), lobby_id),
            )
            row = conn.execute(
                "SELECT * FROM party_draft_launches WHERE lobby_id=?", (lobby_id,)
            ).fetchone()
            if row is None:
                raise PartyDraftError("draft launch was not reserved")
            return _decode(row)


def _snapshot(
    lobby: PartyLobby, formation: FormationResult | None = None
) -> dict[str, object]:
    return {
        "lobby_id": lobby.lobby_id,
        "guild_id": lobby.guild_id,
        "organizer_id": lobby.organizer_id,
        "rules": {
            "mode": lobby.mode,
            "region": lobby.region,
            "format": lobby.format,
            "voice_required": lobby.voice_required,
            "skill_band": lobby.skill_band,
            "notes": lobby.notes,
        },
        "participants": [
            {
                "user_id": player.user_id,
                "assigned_roles": list(player.preferences),
                "primary_role": player.primary_role,
                "secondary_role": player.secondary_role,
                "fill": player.fill,
                "captain": player.captain,
            }
            for player in lobby.participants
        ],
        "formation": _formation_snapshot(formation) if formation else None,
    }


def _formation_snapshot(result: FormationResult) -> dict[str, object]:
    return {
        "mode": result.mode.value,
        "first_choices": result.first_choices,
        "second_choices": result.second_choices,
        "fills": result.fills,
        "strength_difference": result.strength_difference,
        "explanation": result.explanation,
        "draft_order": list(result.draft_order),
        "blue": [
            {
                "user_id": assignment.user_id,
                "role": assignment.role,
                "preference": assignment.preference,
                "strength": assignment.strength,
            }
            for assignment in result.blue.assignments
        ],
        "red": [
            {
                "user_id": assignment.user_id,
                "role": assignment.role,
                "preference": assignment.preference,
                "strength": assignment.strength,
            }
            for assignment in result.red.assignments
        ],
    }


def _team_json(team: DraftTeam) -> str:
    return json.dumps(
        {
            "captain_id": team.captain_id,
            "participant_ids": team.participant_ids,
            "role_assignments": team.role_assignments,
        }
    )


def _decode(row: sqlite3.Row) -> PartyDraftLaunch:
    blue = json.loads(row["blue_json"])
    red = json.loads(row["red_json"])
    return PartyDraftLaunch(
        lobby_id=row["lobby_id"],
        guild_id=row["guild_id"],
        operation_id=row["operation_id"],
        status=row["status"],
        match_id=row["match_id"],
        channel_id=row["channel_id"],
        blue=DraftTeam(
            blue["captain_id"],
            tuple(blue["participant_ids"]),
            tuple(tuple(item) for item in blue.get("role_assignments", ())),
        ),
        red=DraftTeam(
            red["captain_id"],
            tuple(red["participant_ids"]),
            tuple(tuple(item) for item in red.get("role_assignments", ())),
        ),
        snapshot=json.loads(row["snapshot_json"]),
        error=row["error"],
    )
