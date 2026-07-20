"""Idempotent post-match continuity across history, teams, queue, rooms, and drafts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from utils.match_history import MatchOutcome, MatchPlayer, MatchRecord, MatchTeam
from utils.party_queue import PartyQueueService, QueueMember
from utils.team_formation import FormationMode, FormationPlayer, form_smite_teams


class ContinuityAction(StrEnum):
    RUN_IT_BACK = "run_it_back"
    SHUFFLE_TEAMS = "shuffle_teams"
    RETURN_TO_QUEUE = "return_to_queue"
    INVITE_SUBSTITUTES = "invite_substitutes"
    CONTINUE_SERIES = "continue_series"


class ContinuityStatus(StrEnum):
    READY = "ready"
    QUEUED = "queued"
    AWAITING_SUBSTITUTES = "awaiting_substitutes"


@dataclass(frozen=True, slots=True)
class AssignmentChange:
    user_id: int
    previous_team: int | None
    next_team: int | None
    previous_role: str = ""
    next_role: str = ""


@dataclass(frozen=True, slots=True)
class ContinuityResult:
    source_match_id: str
    next_match_id: str | None
    lobby_id: str
    action: ContinuityAction
    status: ContinuityStatus
    team_one: MatchTeam | None
    team_two: MatchTeam | None
    changes: tuple[AssignmentChange, ...]
    promoted_ids: tuple[int, ...] = ()
    reused_rooms: bool = False
    queue_projected: bool = False
    rooms_projected: bool = False
    draft_projected: bool = False


class ContinuityError(ValueError):
    pass


RoomReconciler = Callable[[str, tuple[int, ...]], Awaitable[bool]]
DraftStarter = Callable[[ContinuityResult], Awaitable[None]]


class MatchContinuityRepository:
    """Stores the single next-state decision for each completed match."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS match_continuity (
                  guild_id INTEGER NOT NULL,
                  source_match_id TEXT NOT NULL,
                  lobby_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  status TEXT NOT NULL,
                  next_match_id TEXT,
                  team_one_json TEXT,
                  team_two_json TEXT,
                  changes_json TEXT NOT NULL,
                  promoted_ids_json TEXT NOT NULL,
                  reused_rooms INTEGER NOT NULL DEFAULT 0,
                  queue_projected INTEGER NOT NULL DEFAULT 0,
                  rooms_projected INTEGER NOT NULL DEFAULT 0,
                  draft_projected INTEGER NOT NULL DEFAULT 0,
                  operation_id TEXT NOT NULL,
                  PRIMARY KEY(guild_id, source_match_id),
                  UNIQUE(operation_id),
                  UNIQUE(guild_id, next_match_id)
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(match_continuity)"
                ).fetchall()
            }
            for name in ("queue_projected", "rooms_projected", "draft_projected"):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE match_continuity ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self):
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def get(self, guild_id: int, source_match_id: str) -> ContinuityResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM match_continuity WHERE guild_id=? AND source_match_id=?",
                (guild_id, source_match_id),
            ).fetchone()
        return _decode(row) if row else None

    def reserve(self, guild_id: int, result: ContinuityResult, operation_id: str):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM match_continuity WHERE guild_id=? AND source_match_id=?",
                (guild_id, result.source_match_id),
            ).fetchone()
            if row:
                prior = _decode(row)
                if prior.action is not result.action:
                    raise ContinuityError(
                        f"{prior.action.value.replace('_', ' ')} already selected"
                    )
                return prior, False
            conn.execute(
                """INSERT INTO match_continuity
                   (guild_id,source_match_id,lobby_id,action,status,next_match_id,
                    team_one_json,team_two_json,changes_json,promoted_ids_json,
                    reused_rooms,queue_projected,rooms_projected,draft_projected,
                    operation_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    guild_id, result.source_match_id, result.lobby_id,
                    result.action, result.status, result.next_match_id,
                    _team_json(result.team_one), _team_json(result.team_two),
                    json.dumps([asdict(change) for change in result.changes]),
                    json.dumps(result.promoted_ids), int(result.reused_rooms),
                    int(result.queue_projected), int(result.rooms_projected),
                    int(result.draft_projected), operation_id,
                ),
            )
            return result, True

    def mark_projection(
        self,
        guild_id: int,
        source_match_id: str,
        stage: str,
        *,
        reused_rooms: bool | None = None,
    ) -> ContinuityResult:
        if stage not in {"queue", "rooms", "draft"}:
            raise ValueError("unknown continuity projection stage")
        assignments = [f"{stage}_projected=1"]
        params: list[object] = []
        if reused_rooms is not None:
            assignments.append("reused_rooms=?")
            params.append(int(reused_rooms))
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE match_continuity SET {','.join(assignments)} "
                "WHERE guild_id=? AND source_match_id=?",
                (*params, guild_id, source_match_id),
            )
            row = conn.execute(
                "SELECT * FROM match_continuity WHERE guild_id=? AND source_match_id=?",
                (guild_id, source_match_id),
            ).fetchone()
        return _decode(row)


class MatchContinuityService:
    def __init__(
        self,
        repository: MatchContinuityRepository,
        queue_service: PartyQueueService,
        *,
        room_reconciler: RoomReconciler | None = None,
        draft_starter: DraftStarter | None = None,
    ):
        self.repository = repository
        self.queue_service = queue_service
        self.room_reconciler = room_reconciler
        self.draft_starter = draft_starter

    async def continue_match(
        self,
        record: MatchRecord,
        *,
        lobby_id: str,
        action: ContinuityAction | str,
        operation_id: str,
        departing_ids: Iterable[int] = (),
    ) -> ContinuityResult:
        action = ContinuityAction(action)
        if record.outcome not in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}:
            raise ContinuityError("a confirmed winner is required before continuing")
        prior = self.repository.get(record.guild_id, record.match_id)
        if prior:
            if prior.action is not action:
                raise ContinuityError(
                    f"{prior.action.value.replace('_', ' ')} already selected"
                )
            return await self._project(record.guild_id, prior)

        queue = await self.queue_service.get(lobby_id)
        if queue is None:
            raise ContinuityError("the source lobby queue is unavailable")
        departures = set(departing_ids)
        roster = [
            player for player in record.participants if player.user_id not in departures
        ]
        promoted: list[int] = []
        waiting = list(queue.waitlist)
        while len(roster) < len(record.participants) and waiting:
            substitute = _best_substitute(roster, waiting)
            waiting.remove(substitute)
            prior_player = next(
                (player for player in record.participants if player.user_id in departures),
                None,
            )
            roster.append(
                MatchPlayer(substitute.user_id, _preferred_role(substitute, prior_player))
            )
            promoted.append(substitute.user_id)

        if action is ContinuityAction.RETURN_TO_QUEUE:
            result = ContinuityResult(
                record.match_id, None, lobby_id, action, ContinuityStatus.QUEUED,
                None, None, (),
            )
        elif action is ContinuityAction.INVITE_SUBSTITUTES or len(roster) < 10:
            result = ContinuityResult(
                record.match_id, None, lobby_id, action,
                ContinuityStatus.AWAITING_SUBSTITUTES, None, None, (),
                tuple(promoted),
            )
        else:
            if (
                action is ContinuityAction.CONTINUE_SERIES
                and not _has_series_context(record)
            ):
                raise ContinuityError(
                    "Continue Series requires an existing series score or series marker."
                )
            team_one, team_two = _teams(record, roster, shuffle=(
                action is ContinuityAction.SHUFFLE_TEAMS
            ))
            next_match_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"godforge:continuity:{record.guild_id}:{record.match_id}:{action.value}",
            ).hex
            result = ContinuityResult(
                record.match_id, next_match_id, lobby_id, action,
                ContinuityStatus.READY, team_one, team_two,
                _changes(record, team_one, team_two), tuple(promoted),
            )

        result, created = self.repository.reserve(
            record.guild_id, result, operation_id
        )
        if not created:
            return await self._project(record.guild_id, result)
        return await self._project(record.guild_id, result)

    async def _project(
        self, guild_id: int, result: ContinuityResult
    ) -> ContinuityResult:
        """Replay every missing projection; each checkpoint is durable."""
        if not result.queue_projected:
            if result.status is ContinuityStatus.QUEUED:
                await self.queue_service.reset_roster(result.lobby_id)
            elif result.status is ContinuityStatus.READY:
                departing_ids = tuple(
                    change.user_id
                    for change in result.changes
                    if change.previous_team is not None and change.next_team is None
                )
                for user_id in departing_ids:
                    await self.queue_service.leave(result.lobby_id, user_id)
                active_ids = tuple(
                    player.user_id
                    for player in (*result.team_one.players, *result.team_two.players)
                )
                await self.queue_service.reset_roster(result.lobby_id, active_ids)
            result = self.repository.mark_projection(
                guild_id, result.source_match_id, "queue"
            )

        if not result.rooms_projected:
            reused = False
            if self.room_reconciler and result.status is ContinuityStatus.READY:
                ids = tuple(
                    player.user_id
                    for player in (*result.team_one.players, *result.team_two.players)
                )
                reused = await self.room_reconciler(result.lobby_id, ids)
            result = self.repository.mark_projection(
                guild_id,
                result.source_match_id,
                "rooms",
                reused_rooms=reused,
            )

        if not result.draft_projected:
            if self.draft_starter and result.status is ContinuityStatus.READY:
                await self.draft_starter(result)
            result = self.repository.mark_projection(
                guild_id, result.source_match_id, "draft"
            )
        return result


def _teams(record: MatchRecord, roster: list[MatchPlayer], *, shuffle: bool):
    if not shuffle:
        by_id = {player.user_id: player for player in roster}
        first = [by_id[p.user_id] for p in record.team_one.players if p.user_id in by_id]
        second = [by_id[p.user_id] for p in record.team_two.players if p.user_id in by_id]
        newcomers = [p for p in roster if p.user_id not in {
            member.user_id for member in (*record.team_one.players, *record.team_two.players)
        }]
        for player in newcomers:
            (first if len(first) < 5 else second).append(player)
        return (
            MatchTeam("Blue", first[0].user_id, tuple(first)),
            MatchTeam("Red", second[0].user_id, tuple(second)),
        )
    formation = form_smite_teams(
        (
            FormationPlayer(
            player.user_id,
            primary_role=player.role or None,
            fill=True,
            captain=player.user_id in {
                record.team_one.captain_id, record.team_two.captain_id
            },
            )
            for player in roster
        ),
        FormationMode.BALANCED,
    )
    def convert(name, team):
        return MatchTeam(
            name, team.captain_id,
            tuple(MatchPlayer(item.user_id, item.role) for item in team.assignments),
        )
    blue, red = convert("Blue", formation.blue), convert("Red", formation.red)
    old_blue = {player.user_id for player in record.team_one.players}
    if {player.user_id for player in blue.players} == old_blue:
        # The optimizer is deterministic and can legitimately reproduce a
        # perfectly balanced prior split. Invert its two role-complete teams so
        # Shuffle remains a meaningful, still role-aware action.
        blue, red = (
            MatchTeam("Blue", red.captain_id, red.players),
            MatchTeam("Red", blue.captain_id, blue.players),
        )
    return blue, red


def _changes(record, team_one, team_two):
    before = {
        p.user_id: (number, p.role)
        for number, team in ((1, record.team_one), (2, record.team_two))
        for p in team.players
    }
    after = {
        p.user_id: (number, p.role)
        for number, team in ((1, team_one), (2, team_two))
        for p in team.players
    }
    return tuple(
        AssignmentChange(
            user_id,
            before.get(user_id, (None, ""))[0],
            after.get(user_id, (None, ""))[0],
            before.get(user_id, (None, ""))[1],
            after.get(user_id, (None, ""))[1],
        )
        for user_id in sorted(before.keys() | after.keys())
        if before.get(user_id) != after.get(user_id)
    )


def _best_substitute(roster: list[MatchPlayer], waitlist: list[QueueMember]):
    roles = {player.role for player in roster if player.role}
    return min(
        waitlist,
        key=lambda member: (
            -sum(role not in roles for role in member.preferred_roles),
            member.joined_sequence,
            member.user_id,
        ),
    )


def _preferred_role(member: QueueMember, departed: MatchPlayer | None) -> str:
    if departed and departed.role in member.preferred_roles:
        return departed.role
    return member.preferred_roles[0] if member.preferred_roles else (
        departed.role if departed else ""
    )


def _has_series_context(record: MatchRecord) -> bool:
    reference = (record.draft_reference or "").strip().lower()
    return record.series_score is not None or reference.startswith("series:")


def _team_json(team):
    if team is None:
        return None
    return json.dumps({
        "name": team.name, "captain_id": team.captain_id,
        "players": [{"user_id": p.user_id, "role": p.role} for p in team.players],
    }, separators=(",", ":"), sort_keys=True)


def _decode_team(value):
    if not value:
        return None
    data = json.loads(value)
    return MatchTeam(
        data["name"], data["captain_id"],
        tuple(MatchPlayer(**player) for player in data["players"]),
    )


def _decode(row):
    return ContinuityResult(
        row["source_match_id"], row["next_match_id"], row["lobby_id"],
        ContinuityAction(row["action"]), ContinuityStatus(row["status"]),
        _decode_team(row["team_one_json"]), _decode_team(row["team_two_json"]),
        tuple(AssignmentChange(**item) for item in json.loads(row["changes_json"])),
        tuple(json.loads(row["promoted_ids_json"])), bool(row["reused_rooms"]),
        bool(row["queue_projected"]), bool(row["rooms_projected"]),
        bool(row["draft_projected"]),
    )
