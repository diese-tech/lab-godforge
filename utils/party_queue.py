"""Concurrency-safe party capacity, waitlist, and ready-check domain service."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class ReadyStatus(StrEnum):
    READY = "ready"
    NEED_5 = "need_5"
    DROP = "drop"


class QueueStatus(StrEnum):
    OPEN = "open"
    READY_CHECK = "ready_check"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class QueueMember:
    user_id: int
    preferred_roles: tuple[str, ...] = ()
    joined_sequence: int = 0


@dataclass
class PartyQueue:
    lobby_id: str
    capacity: int
    active: list[QueueMember] = field(default_factory=list)
    waitlist: list[QueueMember] = field(default_factory=list)
    ready: dict[int, ReadyStatus] = field(default_factory=dict)
    status: QueueStatus = QueueStatus.OPEN
    ready_deadline: datetime | None = None
    extensions_used: int = 0
    next_sequence: int = 1


class PartyQueueRepository(Protocol):
    async def load(self, lobby_id: str) -> PartyQueue | None: ...

    async def save(self, queue: PartyQueue) -> None: ...


class InMemoryPartyQueueRepository:
    """Test/dev repository demonstrating the injectable persistence boundary."""

    def __init__(self) -> None:
        self._queues: dict[str, PartyQueue] = {}

    async def load(self, lobby_id: str) -> PartyQueue | None:
        queue = self._queues.get(lobby_id)
        return _copy_queue(queue) if queue else None

    async def save(self, queue: PartyQueue) -> None:
        self._queues[queue.lobby_id] = _copy_queue(queue)


class SQLitePartyQueueRepository:
    """Durable queue adapter that can share an existing GodForge SQLite file."""

    def __init__(self, path: str | Path) -> None:
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

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        # Queue-specific names make this migration additive when the supplied
        # path already contains party lifecycle or dashboard tables.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS party_queue_state (
              lobby_id TEXT PRIMARY KEY,
              capacity INTEGER NOT NULL,
              status TEXT NOT NULL,
              ready_deadline TEXT,
              extensions_used INTEGER NOT NULL DEFAULT 0,
              next_sequence INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS party_queue_members (
              lobby_id TEXT NOT NULL
                REFERENCES party_queue_state(lobby_id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL,
              lane TEXT NOT NULL CHECK(lane IN ('active','waitlist')),
              lane_position INTEGER NOT NULL,
              joined_sequence INTEGER NOT NULL,
              preferred_roles_json TEXT NOT NULL DEFAULT '[]',
              ready_status TEXT,
              PRIMARY KEY(lobby_id,user_id),
              UNIQUE(lobby_id,lane,lane_position)
            );
            CREATE INDEX IF NOT EXISTS party_queue_members_order
              ON party_queue_members(lobby_id,lane,lane_position);
            """
        )

    async def load(self, lobby_id: str) -> PartyQueue | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM party_queue_state WHERE lobby_id=?", (lobby_id,)
            ).fetchone()
            if row is None:
                return None
            members = conn.execute(
                "SELECT * FROM party_queue_members WHERE lobby_id=? "
                "ORDER BY CASE lane WHEN 'active' THEN 0 ELSE 1 END,lane_position",
                (lobby_id,),
            ).fetchall()

        active: list[QueueMember] = []
        waitlist: list[QueueMember] = []
        ready: dict[int, ReadyStatus] = {}
        for member_row in members:
            member = QueueMember(
                user_id=member_row["user_id"],
                preferred_roles=tuple(json.loads(member_row["preferred_roles_json"])),
                joined_sequence=member_row["joined_sequence"],
            )
            (active if member_row["lane"] == "active" else waitlist).append(member)
            if member_row["ready_status"]:
                ready[member.user_id] = ReadyStatus(member_row["ready_status"])
        return PartyQueue(
            lobby_id=row["lobby_id"],
            capacity=row["capacity"],
            active=active,
            waitlist=waitlist,
            ready=ready,
            status=QueueStatus(row["status"]),
            ready_deadline=_decode_datetime(row["ready_deadline"]),
            extensions_used=row["extensions_used"],
            next_sequence=row["next_sequence"],
        )

    async def save(self, queue: PartyQueue) -> None:
        deadline = _utc(queue.ready_deadline).isoformat() if queue.ready_deadline else None
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO party_queue_state
                   (lobby_id,capacity,status,ready_deadline,extensions_used,next_sequence)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(lobby_id) DO UPDATE SET
                     capacity=excluded.capacity,status=excluded.status,
                     ready_deadline=excluded.ready_deadline,
                     extensions_used=excluded.extensions_used,
                     next_sequence=excluded.next_sequence""",
                (
                    queue.lobby_id,
                    queue.capacity,
                    queue.status.value,
                    deadline,
                    queue.extensions_used,
                    queue.next_sequence,
                ),
            )
            conn.execute(
                "DELETE FROM party_queue_members WHERE lobby_id=?", (queue.lobby_id,)
            )
            for lane, members in (("active", queue.active), ("waitlist", queue.waitlist)):
                for position, member in enumerate(members):
                    ready_status = queue.ready.get(member.user_id)
                    conn.execute(
                        """INSERT INTO party_queue_members
                           (lobby_id,user_id,lane,lane_position,joined_sequence,
                            preferred_roles_json,ready_status)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            queue.lobby_id,
                            member.user_id,
                            lane,
                            position,
                            member.joined_sequence,
                            json.dumps(member.preferred_roles, separators=(",", ":")),
                            ready_status.value if ready_status else None,
                        ),
                    )


class QueueError(ValueError):
    pass


class PartyQueueService:
    def __init__(
        self,
        repository: PartyQueueRepository,
        *,
        ready_timeout: timedelta = timedelta(seconds=60),
        extension: timedelta = timedelta(minutes=5),
        max_extensions: int = 1,
        cancel_on_timeout: bool = True,
    ) -> None:
        if ready_timeout <= timedelta(0) or extension <= timedelta(0):
            raise ValueError("ready timeout and extension must be positive")
        if max_extensions < 0:
            raise ValueError("max_extensions cannot be negative")
        self._repository = repository
        self._ready_timeout = ready_timeout
        self._extension = extension
        self._max_extensions = max_extensions
        self._cancel_on_timeout = cancel_on_timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def create(self, lobby_id: str, capacity: int) -> PartyQueue:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        async with await self._lock_for(lobby_id):
            if await self._repository.load(lobby_id):
                raise QueueError("lobby queue already exists")
            queue = PartyQueue(lobby_id=lobby_id, capacity=capacity)
            await self._repository.save(queue)
            return _copy_queue(queue)

    async def get(self, lobby_id: str) -> PartyQueue | None:
        return await self._repository.load(lobby_id)

    async def resize(self, lobby_id: str, capacity: int) -> tuple[PartyQueue, tuple[int, ...]]:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            if capacity < len(queue.active):
                raise QueueError("capacity cannot be below the active roster")
            queue.capacity = capacity
            promoted: list[int] = []
            while len(queue.active) < capacity and queue.waitlist:
                member = self._promote(queue)
                if member:
                    promoted.append(member.user_id)
            await self._repository.save(queue)
            return _copy_queue(queue), tuple(promoted)

    async def reset_roster(
        self, lobby_id: str, active_ids: tuple[int, ...] | None = None
    ) -> PartyQueue:
        """Reopen a queue after a match and optionally project its next roster."""
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            members = {member.user_id: member for member in (*queue.active, *queue.waitlist)}
            if active_ids is not None:
                if len(active_ids) != len(set(active_ids)):
                    raise QueueError("active roster contains duplicate players")
                unknown = set(active_ids) - members.keys()
                if unknown:
                    raise QueueError("active roster contains players outside the queue")
                queue.active = [members[user_id] for user_id in active_ids]
                selected = set(active_ids)
                queue.waitlist = sorted(
                    (
                        member for user_id, member in members.items()
                        if user_id not in selected
                    ),
                    key=lambda member: member.joined_sequence,
                )
            queue.status = QueueStatus.OPEN
            queue.ready = {}
            queue.ready_deadline = None
            queue.extensions_used = 0
            await self._repository.save(queue)
            return _copy_queue(queue)

    async def join(
        self, lobby_id: str, user_id: int, preferred_roles: tuple[str, ...] = ()
    ) -> tuple[PartyQueue, str]:
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            if queue.status is QueueStatus.CANCELLED:
                raise QueueError("queue is cancelled")
            if _find_member(queue, user_id):
                return _copy_queue(queue), "unchanged"
            member = QueueMember(
                user_id=user_id,
                preferred_roles=tuple(dict.fromkeys(preferred_roles)),
                joined_sequence=queue.next_sequence,
            )
            queue.next_sequence += 1
            destination = "active" if len(queue.active) < queue.capacity else "waitlist"
            getattr(queue, destination).append(member)
            await self._repository.save(queue)
            return _copy_queue(queue), destination

    async def leave(self, lobby_id: str, user_id: int) -> tuple[PartyQueue, int | None]:
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            was_active = _remove_member(queue.active, user_id)
            was_waitlisted = _remove_member(queue.waitlist, user_id)
            if not was_active and not was_waitlisted:
                return _copy_queue(queue), None
            queue.ready.pop(user_id, None)
            promoted = self._promote(queue) if was_active else None
            await self._repository.save(queue)
            return _copy_queue(queue), promoted.user_id if promoted else None

    async def start_ready_check(
        self, lobby_id: str, *, now: datetime | None = None
    ) -> PartyQueue:
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            if not queue.active:
                raise QueueError("cannot ready-check an empty lobby")
            queue.status = QueueStatus.READY_CHECK
            queue.ready = {}
            queue.extensions_used = 0
            queue.ready_deadline = _utc(now) + self._ready_timeout
            await self._repository.save(queue)
            return _copy_queue(queue)

    async def respond(
        self,
        lobby_id: str,
        user_id: int,
        status: ReadyStatus | str,
        *,
        now: datetime | None = None,
    ) -> tuple[PartyQueue, int | None]:
        status = ReadyStatus(status)
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            if queue.status is not QueueStatus.READY_CHECK:
                raise QueueError("ready check is not active")
            if queue.ready_deadline and _utc(now) >= queue.ready_deadline:
                raise QueueError("ready check has expired")
            if not any(member.user_id == user_id for member in queue.active):
                raise QueueError("user is not an active member")
            promoted_id = None
            if status is ReadyStatus.DROP:
                _remove_member(queue.active, user_id)
                queue.ready.pop(user_id, None)
                promoted = self._promote(queue)
                promoted_id = promoted.user_id if promoted else None
                queue.status = QueueStatus.OPEN
                queue.ready = {}
                queue.ready_deadline = None
            elif status is ReadyStatus.NEED_5:
                if queue.extensions_used >= self._max_extensions:
                    raise QueueError("ready-check extension limit reached")
                queue.extensions_used += 1
                queue.ready_deadline = queue.ready_deadline + self._extension
                queue.ready[user_id] = status
            else:
                queue.ready[user_id] = status
            await self._repository.save(queue)
            return _copy_queue(queue), promoted_id

    async def expire(
        self, lobby_id: str, *, now: datetime | None = None
    ) -> tuple[PartyQueue, tuple[int, ...]]:
        async with await self._lock_for(lobby_id):
            queue = await self._required(lobby_id)
            if queue.status is not QueueStatus.READY_CHECK or queue.ready_deadline is None:
                return _copy_queue(queue), ()
            if _utc(now) < queue.ready_deadline:
                return _copy_queue(queue), ()
            non_ready = tuple(
                member.user_id
                for member in queue.active
                if queue.ready.get(member.user_id) is not ReadyStatus.READY
            )
            if self._cancel_on_timeout:
                queue.status = QueueStatus.CANCELLED
            else:
                for user_id in non_ready:
                    _remove_member(queue.active, user_id)
                    queue.ready.pop(user_id, None)
                while len(queue.active) < queue.capacity and queue.waitlist:
                    self._promote(queue)
                queue.status = QueueStatus.OPEN
                queue.ready_deadline = None
            await self._repository.save(queue)
            return _copy_queue(queue), non_ready

    async def _required(self, lobby_id: str) -> PartyQueue:
        queue = await self._repository.load(lobby_id)
        if queue is None:
            raise QueueError("lobby queue does not exist")
        return queue

    async def _lock_for(self, lobby_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(lobby_id, asyncio.Lock())

    @staticmethod
    def _promote(queue: PartyQueue) -> QueueMember | None:
        if len(queue.active) >= queue.capacity or not queue.waitlist:
            return None
        covered = Counter(
            role for member in queue.active for role in member.preferred_roles
        )

        def rank(member: QueueMember) -> tuple[int, int]:
            missing_role_score = sum(
                1 for role in member.preferred_roles if covered[role] == 0
            )
            return (-missing_role_score, member.joined_sequence)

        promoted = min(queue.waitlist, key=rank)
        queue.waitlist.remove(promoted)
        queue.active.append(promoted)
        return promoted


def _find_member(queue: PartyQueue, user_id: int) -> QueueMember | None:
    return next(
        (member for member in (*queue.active, *queue.waitlist) if member.user_id == user_id),
        None,
    )


def _remove_member(members: list[QueueMember], user_id: int) -> bool:
    for index, member in enumerate(members):
        if member.user_id == user_id:
            members.pop(index)
            return True
    return False


def _copy_queue(queue: PartyQueue) -> PartyQueue:
    return replace(
        queue,
        active=list(queue.active),
        waitlist=list(queue.waitlist),
        ready=dict(queue.ready),
    )


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _decode_datetime(value: str | None) -> datetime | None:
    return _utc(datetime.fromisoformat(value)) if value else None
