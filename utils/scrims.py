"""Guild-scoped teams and scrim coordination over GodForge's party pipeline."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from utils.party import PlayerPreferences, ensure_utc
from utils.party_schedule import Recurrence, ScheduleRepository, convert_to_lobby


class ScrimError(ValueError):
    pass


class ChallengeState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CHECKED_IN = "checked_in"
    LOCKED = "locked"
    LAUNCHED = "launched"


@dataclass(frozen=True, slots=True)
class ScrimTeam:
    team_id: str
    guild_id: int
    captain_id: int
    name: str
    roster: tuple[int, ...]
    substitutes: tuple[int, ...]
    region: str
    availability: str


@dataclass(frozen=True, slots=True)
class ScrimChallenge:
    challenge_id: str
    guild_id: int
    challenger_team_id: str
    recipient_team_id: str
    starts_at: datetime
    timezone_name: str
    organizer_id: int
    state: ChallengeState
    checked_in_team_ids: tuple[str, ...] = ()
    locked_rosters: dict[str, tuple[int, ...]] | None = None
    event_id: str | None = None
    lobby_id: str | None = None


class ScrimRepository:
    """SQLite persistence with deterministic IDs and idempotent mutations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scrim_teams (
                  team_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
                  captain_id INTEGER NOT NULL, name TEXT NOT NULL,
                  roster_json TEXT NOT NULL, substitutes_json TEXT NOT NULL,
                  region TEXT NOT NULL, availability TEXT NOT NULL,
                  UNIQUE(guild_id, name COLLATE NOCASE)
                );
                CREATE INDEX IF NOT EXISTS scrim_teams_guild
                  ON scrim_teams(guild_id);
                CREATE TABLE IF NOT EXISTS scrim_challenges (
                  challenge_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
                  challenger_team_id TEXT NOT NULL, recipient_team_id TEXT NOT NULL,
                  starts_at TEXT NOT NULL, timezone_name TEXT NOT NULL,
                  organizer_id INTEGER NOT NULL, state TEXT NOT NULL,
                  checked_in_json TEXT NOT NULL DEFAULT '[]',
                  locked_rosters_json TEXT, event_id TEXT, lobby_id TEXT,
                  FOREIGN KEY(challenger_team_id) REFERENCES scrim_teams(team_id),
                  FOREIGN KEY(recipient_team_id) REFERENCES scrim_teams(team_id)
                );
                CREATE TABLE IF NOT EXISTS scrim_operations (
                  operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                  entity_id TEXT NOT NULL
                );
                """
            )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self):
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def save_team(
        self, *, guild_id: int, captain_id: int, name: str,
        roster: tuple[int, ...], substitutes: tuple[int, ...] = (),
        region: str, availability: str, operation_id: str,
    ) -> ScrimTeam:
        name, region, availability = name.strip(), region.strip(), availability.strip()
        roster, substitutes = tuple(dict.fromkeys(roster)), tuple(dict.fromkeys(substitutes))
        if not name or not region or not availability:
            raise ScrimError("name, region, and availability are required")
        if captain_id not in roster:
            raise ScrimError("the captain must be on the active roster")
        if not 2 <= len(roster) <= 10:
            raise ScrimError("active rosters must contain 2-10 players")
        if set(roster) & set(substitutes):
            raise ScrimError("a player cannot be both active and a substitute")
        if any(user_id <= 0 for user_id in roster + substitutes):
            raise ScrimError("Discord user IDs must be positive")
        team_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"godforge:scrim-team:{guild_id}:{name.casefold()}"
        ).hex[:12]
        with self._transaction() as conn:
            prior = conn.execute(
                "SELECT entity_id FROM scrim_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if prior and prior["entity_id"] != team_id:
                raise ScrimError("operation ID was already used for another team")
            conn.execute(
                """INSERT INTO scrim_teams VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(team_id) DO UPDATE SET
                   captain_id=excluded.captain_id, roster_json=excluded.roster_json,
                   substitutes_json=excluded.substitutes_json, region=excluded.region,
                   availability=excluded.availability""",
                (team_id, guild_id, captain_id, name, json.dumps(roster),
                 json.dumps(substitutes), region, availability),
            )
            conn.execute(
                "INSERT OR IGNORE INTO scrim_operations VALUES (?,?,?)",
                (operation_id, "save_team", team_id),
            )
        return self.get_team(team_id)

    def get_team(self, team_id: str) -> ScrimTeam | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scrim_teams WHERE team_id=?", (team_id,)
            ).fetchone()
        return self._team(row) if row else None

    def list_teams(self, guild_id: int) -> list[ScrimTeam]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scrim_teams WHERE guild_id=? ORDER BY name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
        return [self._team(row) for row in rows]

    def challenge(
        self, *, challenger_team_id: str, recipient_team_id: str,
        actor_id: int, starts_at: datetime, timezone_name: str, operation_id: str,
    ) -> ScrimChallenge:
        starts_at = ensure_utc(starts_at)
        if starts_at <= datetime.now(timezone.utc):
            raise ScrimError("scrim time must be in the future")
        with self._transaction() as conn:
            challenger = self._required_team(conn, challenger_team_id)
            recipient = self._required_team(conn, recipient_team_id)
            if challenger["guild_id"] != recipient["guild_id"]:
                raise ScrimError("teams must belong to the same guild")
            if challenger_team_id == recipient_team_id:
                raise ScrimError("a team cannot challenge itself")
            if challenger["captain_id"] != actor_id:
                raise ScrimError("only the challenging captain can issue this challenge")
            challenge_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"godforge:scrim-challenge:{challenger['guild_id']}:{operation_id}",
            ).hex[:12]
            conn.execute(
                """INSERT OR IGNORE INTO scrim_challenges
                   (challenge_id,guild_id,challenger_team_id,recipient_team_id,
                    starts_at,timezone_name,organizer_id,state)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (challenge_id, challenger["guild_id"], challenger_team_id,
                 recipient_team_id, starts_at.isoformat(), timezone_name, actor_id,
                 ChallengeState.PROPOSED),
            )
            self._record_operation(conn, operation_id, "challenge", challenge_id)
        return self.get_challenge(challenge_id)

    def respond(
        self, challenge_id: str, *, actor_id: int, response: str,
        operation_id: str, proposed_at: datetime | None = None,
    ) -> ScrimChallenge:
        response = response.strip().lower()
        if response not in {"accept", "reject", "propose"}:
            raise ScrimError("response must be accept, reject, or propose")
        with self._transaction() as conn:
            row = self._required_challenge(conn, challenge_id)
            recipient = self._required_team(conn, row["recipient_team_id"])
            if recipient["captain_id"] != actor_id:
                raise ScrimError("only the challenged captain can respond")
            if row["state"] != ChallengeState.PROPOSED:
                self._record_operation(conn, operation_id, "respond", challenge_id)
            elif response == "propose":
                if proposed_at is None or ensure_utc(proposed_at) <= datetime.now(timezone.utc):
                    raise ScrimError("a proposed replacement time must be in the future")
                conn.execute(
                    """UPDATE scrim_challenges SET starts_at=?,challenger_team_id=?,
                       recipient_team_id=?,organizer_id=? WHERE challenge_id=?""",
                    (ensure_utc(proposed_at).isoformat(), row["recipient_team_id"],
                     row["challenger_team_id"], actor_id, challenge_id),
                )
            elif row["state"] == ChallengeState.PROPOSED:
                state = ChallengeState.ACCEPTED if response == "accept" else ChallengeState.REJECTED
                conn.execute(
                    "UPDATE scrim_challenges SET state=? WHERE challenge_id=?",
                    (state, challenge_id),
                )
            self._record_operation(conn, operation_id, "respond", challenge_id)
        return self.get_challenge(challenge_id)

    def check_in(self, challenge_id: str, *, actor_id: int, operation_id: str) -> ScrimChallenge:
        with self._transaction() as conn:
            row = self._required_challenge(conn, challenge_id)
            if row["state"] not in (ChallengeState.ACCEPTED, ChallengeState.CHECKED_IN):
                raise ScrimError("only accepted challenges can check in")
            teams = [
                self._required_team(conn, row["challenger_team_id"]),
                self._required_team(conn, row["recipient_team_id"]),
            ]
            team = next((item for item in teams if item["captain_id"] == actor_id), None)
            if team is None:
                raise ScrimError("only a participating captain can check in")
            checked = set(json.loads(row["checked_in_json"]))
            checked.add(team["team_id"])
            state = ChallengeState.CHECKED_IN if len(checked) == 2 else ChallengeState.ACCEPTED
            conn.execute(
                "UPDATE scrim_challenges SET checked_in_json=?,state=? WHERE challenge_id=?",
                (json.dumps(sorted(checked)), state, challenge_id),
            )
            self._record_operation(conn, operation_id, "check_in", challenge_id)
        return self.get_challenge(challenge_id)

    def lock_rosters(
        self, challenge_id: str, *, actor_id: int, operation_id: str,
        organizer_override: bool = False,
    ) -> ScrimChallenge:
        with self._transaction() as conn:
            row = self._required_challenge(conn, challenge_id)
            if row["state"] != ChallengeState.CHECKED_IN:
                raise ScrimError("both captains must check in before roster lock")
            if actor_id != row["organizer_id"] and not organizer_override:
                raise ScrimError("only the organizer can lock rosters")
            teams = (
                self._required_team(conn, row["challenger_team_id"]),
                self._required_team(conn, row["recipient_team_id"]),
            )
            snapshot = {team["team_id"]: json.loads(team["roster_json"]) for team in teams}
            conn.execute(
                "UPDATE scrim_challenges SET locked_rosters_json=?,state=? WHERE challenge_id=?",
                (json.dumps(snapshot, sort_keys=True), ChallengeState.LOCKED, challenge_id),
            )
            self._record_operation(conn, operation_id, "lock", challenge_id)
        return self.get_challenge(challenge_id)

    def mark_launched(
        self, challenge_id: str, *, event_id: str, lobby_id: str, operation_id: str
    ) -> ScrimChallenge:
        with self._transaction() as conn:
            row = self._required_challenge(conn, challenge_id)
            if row["state"] not in (ChallengeState.LOCKED, ChallengeState.LAUNCHED):
                raise ScrimError("lock rosters before launch")
            if row["lobby_id"] and row["lobby_id"] != lobby_id:
                raise ScrimError("challenge already launched into another lobby")
            conn.execute(
                """UPDATE scrim_challenges SET state=?,event_id=?,lobby_id=?
                   WHERE challenge_id=?""",
                (ChallengeState.LAUNCHED, event_id, lobby_id, challenge_id),
            )
            self._record_operation(conn, operation_id, "launch", challenge_id)
        return self.get_challenge(challenge_id)

    def get_challenge(self, challenge_id: str) -> ScrimChallenge | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scrim_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
        if not row:
            return None
        locked = json.loads(row["locked_rosters_json"]) if row["locked_rosters_json"] else None
        return ScrimChallenge(
            row["challenge_id"], row["guild_id"], row["challenger_team_id"],
            row["recipient_team_id"], datetime.fromisoformat(row["starts_at"]),
            row["timezone_name"], row["organizer_id"], ChallengeState(row["state"]),
            tuple(json.loads(row["checked_in_json"])),
            {key: tuple(value) for key, value in locked.items()} if locked else None,
            row["event_id"], row["lobby_id"],
        )

    @staticmethod
    def _team(row) -> ScrimTeam:
        return ScrimTeam(
            row["team_id"], row["guild_id"], row["captain_id"], row["name"],
            tuple(json.loads(row["roster_json"])), tuple(json.loads(row["substitutes_json"])),
            row["region"], row["availability"],
        )

    @staticmethod
    def _required_team(conn, team_id):
        row = conn.execute("SELECT * FROM scrim_teams WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            raise ScrimError("team not found")
        return row

    @staticmethod
    def _required_challenge(conn, challenge_id):
        row = conn.execute(
            "SELECT * FROM scrim_challenges WHERE challenge_id=?", (challenge_id,)
        ).fetchone()
        if not row:
            raise ScrimError("challenge not found")
        return row

    @staticmethod
    def _record_operation(conn, operation_id, kind, entity_id):
        prior = conn.execute(
            "SELECT kind,entity_id FROM scrim_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if prior and (prior["kind"], prior["entity_id"]) != (kind, entity_id):
            raise ScrimError("operation ID was already used for another mutation")
        conn.execute(
            "INSERT OR IGNORE INTO scrim_operations VALUES (?,?,?)",
            (operation_id, kind, entity_id),
        )


async def launch_scrim(
    challenge: ScrimChallenge, scrims: ScrimRepository,
    schedules: ScheduleRepository, parties, queues, *, operation_id: str,
):
    """Convert a locked scrim into the canonical scheduling and lobby services."""
    if challenge.state not in (ChallengeState.LOCKED, ChallengeState.LAUNCHED):
        raise ScrimError("lock rosters before launch")
    if challenge.state is ChallengeState.LAUNCHED:
        return parties.get(challenge.guild_id, challenge.lobby_id)
    teams = [scrims.get_team(challenge.challenger_team_id),
             scrims.get_team(challenge.recipient_team_id)]
    members = tuple(
        user_id
        for team in teams
        for user_id in challenge.locked_rosters[team.team_id]
    )
    if len(members) != len(set(members)):
        raise ScrimError("locked rosters overlap")
    event = schedules.create(
        guild_id=challenge.guild_id, organizer_id=challenge.organizer_id,
        title=f"{teams[0].name} vs {teams[1].name}",
        starts_at=challenge.starts_at, timezone_name=challenge.timezone_name,
        recurrence=Recurrence.ONCE, capacity=len(members),
        operation_id=f"scrim:{challenge.challenge_id}:schedule",
    )
    event = schedules.confirm(event.event_id, challenge.organizer_id)
    for user_id in members:
        event = schedules.rsvp(event.event_id, user_id, PlayerPreferences())
    lobby = await convert_to_lobby(event, schedules, parties, queues)
    scrims.mark_launched(
        challenge.challenge_id, event_id=event.event_id, lobby_id=lobby.lobby_id,
        operation_id=operation_id,
    )
    return lobby
