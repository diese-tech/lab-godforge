"""SQLite repository for party lobbies and restart recovery."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from utils.party import (
    ACTIVE_STATES,
    AuditEvent,
    DiscordDelivery,
    LobbyState,
    Participant,
    PartyLobby,
    RecoveryRecord,
    ensure_utc,
    utc_now,
    validate_transition,
)


class LobbyNotFoundError(LookupError):
    pass


class OperationConflictError(RuntimeError):
    """An operation ID was reused for a different command."""


def _encode_time(value: datetime | None) -> str | None:
    value = ensure_utc(value)
    return value.isoformat() if value else None


def _decode_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SQLitePartyRepository:
    """Per-guild durable lobby repository.

    Public mutating methods accept an ``operation_id`` supplied by the Discord
    handler. Retrying the same interaction is a no-op; reusing its ID for a
    different command is rejected.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS party_lobbies (
              lobby_id TEXT PRIMARY KEY,
              guild_id INTEGER NOT NULL,
              organizer_id INTEGER NOT NULL,
              capacity INTEGER NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              expires_at TEXT,
              version INTEGER NOT NULL,
              delivery_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS party_lobbies_guild_state
              ON party_lobbies(guild_id, state);
            CREATE TABLE IF NOT EXISTS party_participants (
              lobby_id TEXT NOT NULL REFERENCES party_lobbies(lobby_id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL,
              preferences_json TEXT NOT NULL DEFAULT '[]',
              ready INTEGER NOT NULL DEFAULT 0,
              joined_at TEXT NOT NULL,
              PRIMARY KEY(lobby_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS party_audit (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              lobby_id TEXT NOT NULL,
              guild_id INTEGER NOT NULL,
              operation_id TEXT NOT NULL UNIQUE,
              command_fingerprint TEXT NOT NULL,
              event_type TEXT NOT NULL,
              from_state TEXT,
              to_state TEXT NOT NULL,
              actor_id INTEGER,
              occurred_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )

    def create(
        self,
        *,
        guild_id: int,
        organizer_id: int,
        capacity: int,
        expires_at: datetime | None = None,
        lobby_id: str | None = None,
        operation_id: str,
        delivery: DiscordDelivery | None = None,
    ) -> PartyLobby:
        # A Discord interaction retry must resolve to the same domain identity
        # even if its caller did not pre-allocate a lobby ID.
        lobby_id = lobby_id or uuid.uuid5(
            uuid.NAMESPACE_URL, f"godforge:party:{guild_id}:{operation_id}"
        ).hex
        fingerprint = f"create:{guild_id}:{lobby_id}"
        with self._transaction() as conn:
            prior = self._operation(conn, operation_id, fingerprint)
            if prior:
                lobby = self._get(conn, prior["lobby_id"], guild_id)
                if lobby is None:
                    raise LobbyNotFoundError(prior["lobby_id"])
                return lobby
            now = utc_now()
            lobby = PartyLobby(
                lobby_id=lobby_id,
                guild_id=guild_id,
                organizer_id=organizer_id,
                capacity=capacity,
                delivery=delivery or DiscordDelivery(),
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            conn.execute(
                """INSERT INTO party_lobbies
                   (lobby_id,guild_id,organizer_id,capacity,state,created_at,
                    updated_at,expires_at,version,delivery_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    lobby.lobby_id, guild_id, organizer_id, capacity, lobby.state,
                    _encode_time(now), _encode_time(now), _encode_time(expires_at),
                    lobby.version, self._delivery_json(lobby.delivery),
                ),
            )
            self._audit(conn, lobby, operation_id, fingerprint, "created", None, None)
            return lobby

    def get(self, guild_id: int, lobby_id: str) -> PartyLobby | None:
        with self._connect() as conn:
            return self._get(conn, lobby_id, guild_id)

    def transition(
        self,
        guild_id: int,
        lobby_id: str,
        target: LobbyState,
        *,
        operation_id: str,
        actor_id: int | None = None,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> PartyLobby:
        target = LobbyState(target)
        fingerprint = f"transition:{guild_id}:{lobby_id}:{target}"
        with self._transaction() as conn:
            prior = self._operation(conn, operation_id, fingerprint)
            if prior:
                lobby = self._get(conn, lobby_id, guild_id)
                if lobby is None:
                    raise LobbyNotFoundError(lobby_id)
                return lobby
            lobby = self._require(conn, guild_id, lobby_id)
            validate_transition(lobby.state, target)
            if lobby.state == target:
                self._audit(
                    conn,
                    lobby,
                    operation_id,
                    fingerprint,
                    "state_transition_noop",
                    lobby.state,
                    actor_id,
                    {"reason": reason} if reason else {},
                )
                return lobby
            changed = lobby.transitioned(target, at=at)
            conn.execute(
                "UPDATE party_lobbies SET state=?,updated_at=?,version=? "
                "WHERE lobby_id=? AND guild_id=?",
                (target, _encode_time(changed.updated_at), changed.version, lobby_id, guild_id),
            )
            self._audit(
                conn, changed, operation_id, fingerprint, "state_transition",
                lobby.state, actor_id, {"reason": reason} if reason else {},
            )
            return changed

    def save_participant(
        self,
        guild_id: int,
        lobby_id: str,
        participant: Participant,
        *,
        operation_id: str,
        actor_id: int | None = None,
    ) -> PartyLobby:
        fingerprint = f"participant:{guild_id}:{lobby_id}:{participant.user_id}:{participant.preferences}:{participant.ready}"
        with self._transaction() as conn:
            prior = self._operation(conn, operation_id, fingerprint)
            if prior:
                return self._require(conn, guild_id, lobby_id)
            lobby = self._require(conn, guild_id, lobby_id)
            if lobby.is_terminal:
                raise ValueError("cannot change participants in a terminal lobby")
            existing = lobby.participant(participant.user_id)
            if existing is None and len(lobby.participants) >= lobby.capacity:
                raise ValueError("lobby is full")
            conn.execute(
                """INSERT INTO party_participants
                   (lobby_id,user_id,preferences_json,ready,joined_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(lobby_id,user_id) DO UPDATE SET
                     preferences_json=excluded.preferences_json, ready=excluded.ready""",
                (
                    lobby_id, participant.user_id,
                    json.dumps(participant.preferences), int(participant.ready),
                    _encode_time(participant.joined_at),
                ),
            )
            now = utc_now()
            conn.execute(
                "UPDATE party_lobbies SET updated_at=?,version=version+1 WHERE lobby_id=?",
                (_encode_time(now), lobby_id),
            )
            changed = self._require(conn, guild_id, lobby_id)
            self._audit(conn, changed, operation_id, fingerprint, "participant_saved",
                        lobby.state, actor_id)
            return changed

    def set_delivery(
        self,
        guild_id: int,
        lobby_id: str,
        delivery: DiscordDelivery,
        *,
        operation_id: str,
    ) -> PartyLobby:
        encoded = self._delivery_json(delivery)
        fingerprint = f"delivery:{guild_id}:{lobby_id}:{encoded}"
        with self._transaction() as conn:
            prior = self._operation(conn, operation_id, fingerprint)
            if prior:
                return self._require(conn, guild_id, lobby_id)
            lobby = self._require(conn, guild_id, lobby_id)
            now = utc_now()
            conn.execute(
                "UPDATE party_lobbies SET delivery_json=?,updated_at=?,version=version+1 "
                "WHERE lobby_id=?",
                (encoded, _encode_time(now), lobby_id),
            )
            changed = self._require(conn, guild_id, lobby_id)
            self._audit(conn, changed, operation_id, fingerprint, "delivery_updated",
                        lobby.state, None)
            return changed

    def recover_active(self, guild_id: int | None = None) -> list[RecoveryRecord]:
        self._expire_due_recruitment(guild_id)
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        params: list[object] = [state.value for state in ACTIVE_STATES]
        query = f"SELECT lobby_id,guild_id FROM party_lobbies WHERE state IN ({placeholders})"
        if guild_id is not None:
            query += " AND guild_id=?"
            params.append(guild_id)
        query += " ORDER BY created_at,lobby_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                RecoveryRecord(self._require(conn, row["guild_id"], row["lobby_id"]))
                for row in rows
            ]

    def _expire_due_recruitment(self, guild_id: int | None = None) -> None:
        """Expire elapsed pre-active lobbies before returning recovery work."""
        expirable = (
            LobbyState.OPEN,
            LobbyState.FULL,
            LobbyState.READY_CHECK,
        )
        placeholders = ",".join("?" for _ in expirable)
        params: list[object] = [state.value for state in expirable]
        query = (
            "SELECT lobby_id,guild_id,expires_at FROM party_lobbies "
            f"WHERE state IN ({placeholders}) AND expires_at IS NOT NULL "
            "AND expires_at<=?"
        )
        params.append(_encode_time(utc_now()))
        if guild_id is not None:
            query += " AND guild_id=?"
            params.append(guild_id)

        with self._transaction() as conn:
            for row in conn.execute(query, params).fetchall():
                lobby = self._require(conn, row["guild_id"], row["lobby_id"])
                operation_id = (
                    f"expiry:{lobby.guild_id}:{lobby.lobby_id}:{row['expires_at']}"
                )
                fingerprint = (
                    f"transition:{lobby.guild_id}:{lobby.lobby_id}:"
                    f"{LobbyState.EXPIRED}"
                )
                if self._operation(conn, operation_id, fingerprint):
                    continue
                changed = lobby.transitioned(LobbyState.EXPIRED)
                conn.execute(
                    "UPDATE party_lobbies SET state=?,updated_at=?,version=? "
                    "WHERE lobby_id=? AND guild_id=?",
                    (
                        LobbyState.EXPIRED,
                        _encode_time(changed.updated_at),
                        changed.version,
                        changed.lobby_id,
                        changed.guild_id,
                    ),
                )
                self._audit(
                    conn,
                    changed,
                    operation_id,
                    fingerprint,
                    "expired",
                    lobby.state,
                    None,
                    {"reason": "expires_at elapsed"},
                )

    def audit_events(self, guild_id: int, lobby_id: str) -> list[AuditEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM party_audit WHERE guild_id=? AND lobby_id=? ORDER BY event_id",
                (guild_id, lobby_id),
            ).fetchall()
        return [
            AuditEvent(
                event_id=row["event_id"], lobby_id=row["lobby_id"],
                guild_id=row["guild_id"], operation_id=row["operation_id"],
                event_type=row["event_type"],
                from_state=LobbyState(row["from_state"]) if row["from_state"] else None,
                to_state=LobbyState(row["to_state"]), actor_id=row["actor_id"],
                occurred_at=_decode_time(row["occurred_at"]),
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    @staticmethod
    def _operation(conn, operation_id: str, fingerprint: str):
        if not operation_id.strip():
            raise ValueError("operation_id is required")
        row = conn.execute(
            "SELECT lobby_id,command_fingerprint FROM party_audit WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row and row["command_fingerprint"] != fingerprint:
            raise OperationConflictError(f"operation ID {operation_id!r} was already used")
        return row

    def _audit(
        self, conn, lobby, operation_id, fingerprint, event_type, from_state,
        actor_id, metadata=None,
    ):
        conn.execute(
            """INSERT INTO party_audit
               (lobby_id,guild_id,operation_id,command_fingerprint,event_type,
                from_state,to_state,actor_id,occurred_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                lobby.lobby_id, lobby.guild_id, operation_id, fingerprint, event_type,
                from_state.value if from_state else None, lobby.state.value, actor_id,
                _encode_time(utc_now()), json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    def _require(self, conn, guild_id, lobby_id):
        lobby = self._get(conn, lobby_id, guild_id)
        if lobby is None:
            raise LobbyNotFoundError(lobby_id)
        return lobby

    def _get(self, conn, lobby_id, guild_id):
        row = conn.execute(
            "SELECT * FROM party_lobbies WHERE lobby_id=? AND guild_id=?",
            (lobby_id, guild_id),
        ).fetchone()
        if row is None:
            return None
        participants = conn.execute(
            "SELECT * FROM party_participants WHERE lobby_id=? ORDER BY joined_at,user_id",
            (lobby_id,),
        ).fetchall()
        delivery = json.loads(row["delivery_json"])
        return PartyLobby(
            lobby_id=row["lobby_id"], guild_id=row["guild_id"],
            organizer_id=row["organizer_id"], capacity=row["capacity"],
            state=LobbyState(row["state"]),
            participants=tuple(
                Participant(
                    user_id=p["user_id"],
                    preferences=tuple(json.loads(p["preferences_json"])),
                    ready=bool(p["ready"]), joined_at=_decode_time(p["joined_at"]),
                )
                for p in participants
            ),
            delivery=DiscordDelivery(
                panel_channel_id=delivery.get("panel_channel_id"),
                panel_message_id=delivery.get("panel_message_id"),
                voice_channel_id=delivery.get("voice_channel_id"),
                team_channel_ids=tuple(delivery.get("team_channel_ids", ())),
            ),
            created_at=_decode_time(row["created_at"]),
            updated_at=_decode_time(row["updated_at"]),
            expires_at=_decode_time(row["expires_at"]), version=row["version"],
        )

    @staticmethod
    def _delivery_json(delivery):
        return json.dumps(
            {
                "panel_channel_id": delivery.panel_channel_id,
                "panel_message_id": delivery.panel_message_id,
                "voice_channel_id": delivery.voice_channel_id,
                "team_channel_ids": list(delivery.team_channel_ids),
            },
            separators=(",", ":"), sort_keys=True,
        )
