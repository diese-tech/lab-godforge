"""Durable, idempotent transition from a ready party to a fearless draft."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from utils.party import LobbyState, PartyLobby, utc_now


class PartyDraftError(RuntimeError):
    """A party cannot currently launch a draft."""


@dataclass(frozen=True, slots=True)
class DraftTeam:
    captain_id: int
    participant_ids: tuple[int, ...]


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


def form_teams(lobby: PartyLobby) -> tuple[DraftTeam, DraftTeam]:
    """Make deterministic teams while spreading captain volunteers."""
    if lobby.state is not LobbyState.FORMING:
        raise PartyDraftError("the lobby must finish its ready check first")
    if len(lobby.participants) < 2:
        raise PartyDraftError("at least two ready participants are required")

    volunteers = [p for p in lobby.participants if p.captain]
    others = [p for p in lobby.participants if not p.captain]
    ordered = volunteers + others
    blue_players = ordered[::2]
    red_players = ordered[1::2]
    if not red_players:
        raise PartyDraftError("both teams need at least one participant")

    def captain(players):
        return next((p for p in players if p.captain), players[0])

    return (
        DraftTeam(captain(blue_players).user_id, tuple(p.user_id for p in blue_players)),
        DraftTeam(captain(red_players).user_id, tuple(p.user_id for p in red_players)),
    )


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
    ) -> tuple[PartyDraftLaunch, bool]:
        blue, red = form_teams(lobby)
        snapshot = _snapshot(lobby)
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


def _snapshot(lobby: PartyLobby) -> dict[str, object]:
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
    }


def _team_json(team: DraftTeam) -> str:
    return json.dumps(
        {"captain_id": team.captain_id, "participant_ids": team.participant_ids}
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
        blue=DraftTeam(blue["captain_id"], tuple(blue["participant_ids"])),
        red=DraftTeam(red["captain_id"], tuple(red["participant_ids"])),
        snapshot=json.loads(row["snapshot_json"]),
        error=row["error"],
    )
