"""Standalone GodForge match results and recreational game-night statistics."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


class MatchOutcome(StrEnum):
    PENDING = "pending"
    DISPUTED = "disputed"
    TEAM_ONE = "team_one"
    TEAM_TWO = "team_two"
    CANCELLED = "cancelled"
    NO_CONTEST = "no_contest"


TERMINAL_OUTCOMES = frozenset(
    {
        MatchOutcome.TEAM_ONE,
        MatchOutcome.TEAM_TWO,
        MatchOutcome.CANCELLED,
        MatchOutcome.NO_CONTEST,
    }
)


@dataclass(frozen=True, slots=True)
class MatchPlayer:
    user_id: int
    role: str = ""

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("player user_id must be positive")
        object.__setattr__(self, "role", self.role.strip().lower())


@dataclass(frozen=True, slots=True)
class MatchTeam:
    name: str
    captain_id: int
    players: tuple[MatchPlayer, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        if not self.name:
            raise ValueError("team name is required")
        ids = [player.user_id for player in self.players]
        if self.captain_id not in ids:
            raise ValueError("captain must be on the team")
        if len(ids) != len(set(ids)):
            raise ValueError("team players must be unique")


@dataclass(frozen=True, slots=True)
class SeriesScore:
    team_one: int
    team_two: int

    def __post_init__(self) -> None:
        if self.team_one < 0 or self.team_two < 0:
            raise ValueError("series scores cannot be negative")
        if self.team_one == self.team_two:
            raise ValueError("a completed series score cannot be tied")


@dataclass(frozen=True, slots=True)
class MatchRecord:
    match_id: str
    guild_id: int
    organizer_id: int
    team_one: MatchTeam
    team_two: MatchTeam
    draft_reference: str | None = None
    outcome: MatchOutcome = MatchOutcome.PENDING
    series_score: SeriesScore | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    resolved_by: int | None = None

    def __post_init__(self) -> None:
        if not self.match_id.strip():
            raise ValueError("match_id is required")
        overlap = {p.user_id for p in self.team_one.players} & {
            p.user_id for p in self.team_two.players
        }
        if overlap:
            raise ValueError("a player cannot appear on both teams")
        object.__setattr__(self, "outcome", MatchOutcome(self.outcome))

    @property
    def participants(self) -> tuple[MatchPlayer, ...]:
        return self.team_one.players + self.team_two.players


@dataclass(frozen=True, slots=True)
class PlayerGameNightStats:
    user_id: int
    appearances: int
    wins: int
    current_streak: int
    role_frequency: dict[str, int]
    teammate_frequency: dict[int, int]


class MatchNotFoundError(LookupError):
    pass


class MatchHistoryRepository:
    """SQLite-backed result confirmation and history service.

    All mutations require an operation ID. Repeating an operation returns the
    current record; reusing it for different input is rejected.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            self._schema(conn)

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
    def _schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS godforge_matches (
              guild_id INTEGER NOT NULL,
              match_id TEXT NOT NULL,
              organizer_id INTEGER NOT NULL,
              team_one_json TEXT NOT NULL,
              team_two_json TEXT NOT NULL,
              draft_reference TEXT,
              outcome TEXT NOT NULL,
              score_one INTEGER,
              score_two INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_by INTEGER,
              PRIMARY KEY(guild_id, match_id)
            );
            CREATE INDEX IF NOT EXISTS godforge_matches_guild_recent
              ON godforge_matches(guild_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS godforge_match_participants (
              guild_id INTEGER NOT NULL,
              match_id TEXT NOT NULL,
              user_id INTEGER NOT NULL,
              team_number INTEGER NOT NULL,
              role TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(guild_id, match_id, user_id),
              FOREIGN KEY(guild_id, match_id)
                REFERENCES godforge_matches(guild_id, match_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS godforge_match_player_recent
              ON godforge_match_participants(guild_id, user_id, match_id);
            CREATE TABLE IF NOT EXISTS godforge_result_confirmations (
              guild_id INTEGER NOT NULL,
              match_id TEXT NOT NULL,
              captain_id INTEGER NOT NULL,
              reported_outcome TEXT NOT NULL,
              reported_at TEXT NOT NULL,
              PRIMARY KEY(guild_id, match_id, captain_id),
              FOREIGN KEY(guild_id, match_id)
                REFERENCES godforge_matches(guild_id, match_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS godforge_history_operations (
              operation_id TEXT PRIMARY KEY,
              fingerprint TEXT NOT NULL,
              match_id TEXT NOT NULL,
              occurred_at TEXT NOT NULL
            );
            """
        )

    def create(
        self,
        *,
        guild_id: int,
        organizer_id: int,
        team_one: MatchTeam,
        team_two: MatchTeam,
        operation_id: str,
        draft_reference: str | None = None,
        match_id: str | None = None,
        at: datetime | None = None,
    ) -> MatchRecord:
        match_id = match_id or uuid.uuid5(
            uuid.NAMESPACE_URL, f"godforge:history:{guild_id}:{operation_id}"
        ).hex
        team_one_json = self._team(team_one)
        team_two_json = self._team(team_two)
        canonical_draft_reference = draft_reference.strip() if draft_reference else None
        fingerprint = (
            f"create:{guild_id}:{match_id}:{organizer_id}:{team_one_json}:"
            f"{team_two_json}:{canonical_draft_reference or ''}"
        )
        with self._transaction() as conn:
            prior = self._operation(conn, operation_id, fingerprint)
            if prior:
                return self._require(conn, prior["match_id"], guild_id)
            now = at or utc_now()
            record = MatchRecord(
                match_id, guild_id, organizer_id, team_one, team_two,
                canonical_draft_reference,
                created_at=now, updated_at=now,
            )
            conn.execute(
                """INSERT INTO godforge_matches
                   (match_id,guild_id,organizer_id,team_one_json,team_two_json,
                    draft_reference,outcome,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    record.match_id, guild_id, organizer_id, team_one_json,
                    team_two_json, record.draft_reference, record.outcome,
                    _time(now), _time(now),
                ),
            )
            conn.executemany(
                """INSERT INTO godforge_match_participants
                   (guild_id,match_id,user_id,team_number,role)
                   VALUES (?,?,?,?,?)""",
                [
                    (guild_id, match_id, player.user_id, team_number, player.role)
                    for team_number, team in ((1, team_one), (2, team_two))
                    for player in team.players
                ],
            )
            self._save_operation(conn, operation_id, fingerprint, match_id, now)
            return record

    def report_winner(
        self,
        guild_id: int,
        match_id: str,
        *,
        captain_id: int,
        winner: MatchOutcome,
        operation_id: str,
        score: SeriesScore | None = None,
        at: datetime | None = None,
    ) -> MatchRecord:
        winner = MatchOutcome(winner)
        if winner not in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}:
            raise ValueError("captains must report team_one or team_two")
        fingerprint = f"report:{guild_id}:{match_id}:{captain_id}:{winner}:{score}"
        with self._transaction() as conn:
            if self._operation(conn, operation_id, fingerprint):
                return self._require(conn, match_id, guild_id)
            record = self._require(conn, match_id, guild_id)
            if record.outcome in TERMINAL_OUTCOMES:
                raise ValueError("match already has a terminal outcome")
            if record.outcome is MatchOutcome.DISPUTED:
                raise ValueError("disputed matches require organizer resolution")
            captains = {record.team_one.captain_id, record.team_two.captain_id}
            if captain_id not in captains:
                raise PermissionError("only a team captain can confirm the winner")
            now = at or utc_now()
            conn.execute(
                """INSERT INTO godforge_result_confirmations
                   (guild_id,match_id,captain_id,reported_outcome,reported_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(guild_id,match_id,captain_id) DO UPDATE SET
                     reported_outcome=excluded.reported_outcome,
                     reported_at=excluded.reported_at""",
                (guild_id, match_id, captain_id, winner, _time(now)),
            )
            reports = conn.execute(
                "SELECT reported_outcome FROM godforge_result_confirmations "
                "WHERE guild_id=? AND match_id=? ORDER BY captain_id",
                (guild_id, match_id),
            ).fetchall()
            outcome = MatchOutcome.PENDING
            if len(reports) == 2:
                values = {MatchOutcome(row["reported_outcome"]) for row in reports}
                outcome = values.pop() if len(values) == 1 else MatchOutcome.DISPUTED
            score_values = (None, None)
            if outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO} and score:
                score_values = (score.team_one, score.team_two)
                self._validate_score(outcome, score)
            conn.execute(
                """UPDATE godforge_matches SET outcome=?,score_one=?,score_two=?,
                   updated_at=? WHERE guild_id=? AND match_id=?""",
                (outcome, *score_values, _time(now), guild_id, match_id),
            )
            self._save_operation(conn, operation_id, fingerprint, match_id, now)
            return self._require(conn, match_id, guild_id)

    def resolve(
        self,
        guild_id: int,
        match_id: str,
        *,
        organizer_id: int,
        outcome: MatchOutcome,
        operation_id: str,
        score: SeriesScore | None = None,
        at: datetime | None = None,
    ) -> MatchRecord:
        outcome = MatchOutcome(outcome)
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError("organizer resolution must be a terminal outcome")
        fingerprint = f"resolve:{guild_id}:{match_id}:{organizer_id}:{outcome}:{score}"
        with self._transaction() as conn:
            if self._operation(conn, operation_id, fingerprint):
                return self._require(conn, match_id, guild_id)
            record = self._require(conn, match_id, guild_id)
            if record.organizer_id != organizer_id:
                raise PermissionError("only the organizer can resolve this match")
            if record.outcome in TERMINAL_OUTCOMES:
                raise ValueError("match already has a terminal outcome")
            if (
                outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}
                and record.outcome is not MatchOutcome.DISPUTED
            ):
                raise ValueError(
                    "organizer winner resolution requires conflicting captain reports"
                )
            if outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}:
                if score:
                    self._validate_score(outcome, score)
            elif score:
                raise ValueError("cancelled and no-contest matches cannot have a score")
            now = at or utc_now()
            conn.execute(
                """UPDATE godforge_matches SET outcome=?,score_one=?,score_two=?,
                   updated_at=?,resolved_by=? WHERE guild_id=? AND match_id=?""",
                (
                    outcome, score.team_one if score else None,
                    score.team_two if score else None, _time(now), organizer_id,
                    guild_id, match_id,
                ),
            )
            self._save_operation(conn, operation_id, fingerprint, match_id, now)
            return self._require(conn, match_id, guild_id)

    def recent_for_guild(self, guild_id: int, limit: int = 10) -> list[MatchRecord]:
        return self._recent("guild_id=?", (guild_id,), limit)

    def recent_for_player(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[MatchRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.match_id,m.guild_id
                   FROM godforge_matches m
                   JOIN godforge_match_participants p
                     ON p.guild_id=m.guild_id AND p.match_id=m.match_id
                   WHERE p.guild_id=? AND p.user_id=?
                   ORDER BY m.created_at DESC,m.match_id DESC LIMIT ?""",
                (guild_id, user_id, limit),
            ).fetchall()
            return [
                self._require(conn, row["match_id"], row["guild_id"]) for row in rows
            ]

    def recent_for_team(
        self, guild_id: int, team_name: str, limit: int = 10
    ) -> list[MatchRecord]:
        target = team_name.strip().casefold()
        records = self.recent_for_guild(guild_id, 500)
        return [
            r for r in records
            if target in {r.team_one.name.casefold(), r.team_two.name.casefold()}
        ][:limit]

    def player_stats(self, guild_id: int, user_id: int) -> PlayerGameNightStats:
        matches = [
            record for record in self.recent_for_player(guild_id, user_id, 500)
            if record.outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}
        ]
        wins = 0
        streak = 0
        roles: dict[str, int] = {}
        teammates: dict[int, int] = {}
        streak_open = True
        for record in matches:
            on_one = user_id in {p.user_id for p in record.team_one.players}
            won = (on_one and record.outcome == MatchOutcome.TEAM_ONE) or (
                not on_one and record.outcome == MatchOutcome.TEAM_TWO
            )
            wins += int(won)
            if streak_open and won:
                streak += 1
            else:
                streak_open = False
            team = record.team_one if on_one else record.team_two
            player = next(p for p in team.players if p.user_id == user_id)
            if player.role:
                roles[player.role] = roles.get(player.role, 0) + 1
            for teammate in team.players:
                if teammate.user_id != user_id:
                    teammates[teammate.user_id] = teammates.get(teammate.user_id, 0) + 1
        return PlayerGameNightStats(user_id, len(matches), wins, streak, roles, teammates)

    def get(self, guild_id: int, match_id: str) -> MatchRecord | None:
        with self._connect() as conn:
            return self._get(conn, match_id, guild_id)

    def _recent(self, where: str, params: tuple[object, ...], limit: int):
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT match_id,guild_id FROM godforge_matches WHERE {where} "
                "ORDER BY created_at DESC,match_id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._require(conn, row["match_id"], row["guild_id"]) for row in rows]

    def _require(self, conn, match_id: str, guild_id: int) -> MatchRecord:
        record = self._get(conn, match_id, guild_id)
        if record is None:
            raise MatchNotFoundError(match_id)
        return record

    def _get(self, conn, match_id: str, guild_id: int) -> MatchRecord | None:
        row = conn.execute(
            "SELECT * FROM godforge_matches WHERE match_id=? AND guild_id=?",
            (match_id, guild_id),
        ).fetchone()
        if row is None:
            return None
        score = (
            SeriesScore(row["score_one"], row["score_two"])
            if row["score_one"] is not None else None
        )
        return MatchRecord(
            row["match_id"], row["guild_id"], row["organizer_id"],
            self._decode_team(row["team_one_json"]),
            self._decode_team(row["team_two_json"]), row["draft_reference"],
            MatchOutcome(row["outcome"]), score,
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]), row["resolved_by"],
        )

    @staticmethod
    def _team(team: MatchTeam) -> str:
        return json.dumps(
            {
                "name": team.name, "captain_id": team.captain_id,
                "players": [
                    {"user_id": player.user_id, "role": player.role}
                    for player in team.players
                ],
            },
            separators=(",", ":"), sort_keys=True,
        )

    @staticmethod
    def _decode_team(value: str) -> MatchTeam:
        data = json.loads(value)
        return MatchTeam(
            data["name"], data["captain_id"],
            tuple(MatchPlayer(**player) for player in data["players"]),
        )

    @staticmethod
    def _validate_score(outcome: MatchOutcome, score: SeriesScore) -> None:
        if outcome == MatchOutcome.TEAM_ONE and score.team_one <= score.team_two:
            raise ValueError("series score does not match team_one winner")
        if outcome == MatchOutcome.TEAM_TWO and score.team_two <= score.team_one:
            raise ValueError("series score does not match team_two winner")

    @staticmethod
    def _operation(conn, operation_id: str, fingerprint: str):
        row = conn.execute(
            "SELECT * FROM godforge_history_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise ValueError("operation ID was reused for different input")
        return row

    @staticmethod
    def _save_operation(conn, operation_id, fingerprint, match_id, at):
        conn.execute(
            "INSERT INTO godforge_history_operations VALUES (?,?,?,?)",
            (operation_id, fingerprint, match_id, _time(at)),
        )
