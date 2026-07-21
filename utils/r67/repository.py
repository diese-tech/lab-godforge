"""Durable SQLite repository for `.r67` runtime state.

State lives in the existing GodForge party database (``GODFORGE_PARTY_DB_PATH``)
rather than the temporary dashboard settings bridge, because Survivor grants and
cooldown expirations require durable, restart-safe lifecycle data (Issue #47,
Gate 7).

Two tables are owned here:

``r67_guild_state``
    per-guild opt-in flag plus passive/Survivor cooldown expirations.

``r67_role_grants``
    active temporary ``67 Survivor`` role grants awaiting removal, with retry
    bookkeeping for restart recovery.

All timestamps are stored as timezone-aware UTC ISO-8601 strings.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from utils.party import ensure_utc, utc_now


def _encode_time(value: datetime | None) -> str | None:
    value = ensure_utc(value)
    return value.isoformat() if value else None


def _decode_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class GuildState:
    guild_id: int
    reactions_enabled: bool = False
    passive_cooldown_until: datetime | None = None
    survivor_cooldown_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class RoleGrant:
    guild_id: int
    user_id: int
    role_id: int
    expires_at: datetime
    created_at: datetime
    removal_attempts: int = 0
    last_error: str | None = None


class SQLiteR67Repository:
    """Restart-safe repository for r67 guild state and Survivor role grants."""

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
            CREATE TABLE IF NOT EXISTS r67_guild_state (
              guild_id INTEGER PRIMARY KEY,
              reactions_enabled INTEGER NOT NULL DEFAULT 0,
              passive_cooldown_until TEXT,
              survivor_cooldown_until TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS r67_role_grants (
              guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              role_id INTEGER NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              removal_attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              PRIMARY KEY (guild_id, user_id, role_id)
            );
            CREATE INDEX IF NOT EXISTS r67_role_grants_expiry
              ON r67_role_grants(expires_at);
            """
        )

    # -- Guild state ------------------------------------------------------

    def get_guild_state(self, guild_id: int) -> GuildState:
        """Return stored state for *guild_id*, or defaults (opt-in disabled).

        Existing guilds with no row are treated as reactions-disabled with no
        active cooldowns, satisfying the Gate 5 migration requirement without a
        write.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM r67_guild_state WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()
        if row is None:
            return GuildState(guild_id=int(guild_id))
        return GuildState(
            guild_id=row["guild_id"],
            reactions_enabled=bool(row["reactions_enabled"]),
            passive_cooldown_until=_decode_time(row["passive_cooldown_until"]),
            survivor_cooldown_until=_decode_time(row["survivor_cooldown_until"]),
        )

    def set_reactions_enabled(self, guild_id: int, enabled: bool) -> GuildState:
        """Enable or disable passive reactions for a guild.

        Disabling clears the passive cooldown so a later re-enable starts clean;
        the Survivor cooldown is preserved to prevent farming across toggles.
        """
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT survivor_cooldown_until FROM r67_guild_state WHERE guild_id = ?",
                (int(guild_id),),
            ).fetchone()
            survivor_until = existing["survivor_cooldown_until"] if existing else None
            passive_until = None  # reset passive cooldown on any toggle
            conn.execute(
                """
                INSERT INTO r67_guild_state (
                  guild_id, reactions_enabled, passive_cooldown_until,
                  survivor_cooldown_until, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  reactions_enabled = excluded.reactions_enabled,
                  passive_cooldown_until = excluded.passive_cooldown_until,
                  updated_at = excluded.updated_at
                """,
                (
                    int(guild_id),
                    1 if enabled else 0,
                    passive_until,
                    survivor_until,
                    _encode_time(utc_now()),
                ),
            )
        return self.get_guild_state(guild_id)

    def set_passive_cooldown(self, guild_id: int, until: datetime) -> None:
        self._touch_cooldown(guild_id, "passive_cooldown_until", until)

    def set_survivor_cooldown(self, guild_id: int, until: datetime) -> None:
        self._touch_cooldown(guild_id, "survivor_cooldown_until", until)

    def _touch_cooldown(self, guild_id: int, column: str, until: datetime) -> None:
        encoded = _encode_time(until)
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO r67_guild_state (
                  guild_id, reactions_enabled, {column}, updated_at
                ) VALUES (?, 0, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  {column} = excluded.{column},
                  updated_at = excluded.updated_at
                """,
                (int(guild_id), encoded, _encode_time(utc_now())),
            )

    # -- Role grants ------------------------------------------------------

    def add_role_grants(self, grants: list[RoleGrant]) -> None:
        if not grants:
            return
        with self._transaction() as conn:
            conn.executemany(
                """
                INSERT INTO r67_role_grants (
                  guild_id, user_id, role_id, expires_at, created_at,
                  removal_attempts, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET
                  expires_at = excluded.expires_at,
                  created_at = excluded.created_at,
                  removal_attempts = 0,
                  last_error = NULL
                """,
                [
                    (
                        g.guild_id,
                        g.user_id,
                        g.role_id,
                        _encode_time(g.expires_at),
                        _encode_time(g.created_at),
                        g.removal_attempts,
                        g.last_error,
                    )
                    for g in grants
                ],
            )

    def all_role_grants(self) -> list[RoleGrant]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM r67_role_grants ORDER BY expires_at"
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def due_role_grants(self, now: datetime | None = None) -> list[RoleGrant]:
        """Return grants whose 67-minute expiration has passed."""
        cutoff = _encode_time(now or utc_now())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM r67_role_grants WHERE expires_at <= ? ORDER BY expires_at",
                (cutoff,),
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def remove_role_grant(self, guild_id: int, user_id: int, role_id: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                DELETE FROM r67_role_grants
                WHERE guild_id = ? AND user_id = ? AND role_id = ?
                """,
                (int(guild_id), int(user_id), int(role_id)),
            )

    def record_removal_failure(
        self, guild_id: int, user_id: int, role_id: int, error: str
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE r67_role_grants
                SET removal_attempts = removal_attempts + 1, last_error = ?
                WHERE guild_id = ? AND user_id = ? AND role_id = ?
                """,
                (error[:500], int(guild_id), int(user_id), int(role_id)),
            )

    @staticmethod
    def _grant_from_row(row: sqlite3.Row) -> RoleGrant:
        return RoleGrant(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            role_id=row["role_id"],
            expires_at=_decode_time(row["expires_at"]),
            created_at=_decode_time(row["created_at"]),
            removal_attempts=row["removal_attempts"],
            last_error=row["last_error"],
        )
