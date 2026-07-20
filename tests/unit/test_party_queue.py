import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from utils.party_queue import (
    InMemoryPartyQueueRepository,
    PartyQueueService,
    QueueError,
    QueueStatus,
    ReadyStatus,
)


@pytest.mark.asyncio
async def test_concurrent_joins_respect_capacity_and_order_waitlist():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(repo)
    await service.create("lobby", 2)

    await asyncio.gather(*(service.join("lobby", user_id) for user_id in range(1, 7)))
    queue = await repo.load("lobby")

    assert len(queue.active) == 2
    assert len(queue.waitlist) == 4
    assert {member.user_id for member in queue.active + queue.waitlist} == set(range(1, 7))
    assert [member.joined_sequence for member in queue.waitlist] == sorted(
        member.joined_sequence for member in queue.waitlist
    )


@pytest.mark.asyncio
async def test_resize_promotes_waitlist_and_rejects_active_underflow():
    service = PartyQueueService(InMemoryPartyQueueRepository())
    await service.create("resize", 1)
    await service.join("resize", 1, ("solo",))
    await service.join("resize", 2, ("mid",))

    queue, promoted = await service.resize("resize", 2)

    assert promoted == (2,)
    assert [member.user_id for member in queue.active] == [1, 2]
    with pytest.raises(QueueError, match="active roster"):
        await service.resize("resize", 1)


@pytest.mark.asyncio
async def test_leave_promotes_role_coverage_before_waitlist_order():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(repo)
    await service.create("lobby", 2)
    await service.join("lobby", 1, ("solo",))
    await service.join("lobby", 2, ("mid",))
    await service.join("lobby", 3, ("solo",))
    await service.join("lobby", 4, ("support",))

    queue, promoted = await service.leave("lobby", 2)

    assert promoted == 4
    assert [member.user_id for member in queue.active] == [1, 4]
    assert [member.user_id for member in queue.waitlist] == [3]


@pytest.mark.asyncio
async def test_role_score_ties_preserve_waitlist_order():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(repo)
    await service.create("lobby", 1)
    await service.join("lobby", 1, ("solo",))
    await service.join("lobby", 2, ("mid",))
    await service.join("lobby", 3, ("support",))

    _, promoted = await service.leave("lobby", 1)

    assert promoted == 2


@pytest.mark.asyncio
async def test_ready_drop_promotes_and_need_5_is_bounded():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(
        repo, extension=timedelta(minutes=5), max_extensions=1
    )
    await service.create("lobby", 2)
    await service.join("lobby", 1)
    await service.join("lobby", 2)
    await service.join("lobby", 3)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    started = await service.start_ready_check("lobby", now=now)

    extended, _ = await service.respond(
        "lobby", 1, ReadyStatus.NEED_5, now=now,
    )
    assert extended.ready_deadline == started.ready_deadline + timedelta(minutes=5)
    with pytest.raises(QueueError, match="extension limit"):
        await service.respond("lobby", 2, "need_5", now=now)

    dropped, promoted = await service.respond("lobby", 1, "drop", now=now)
    assert promoted == 3
    assert [member.user_id for member in dropped.active] == [2, 3]
    assert dropped.status is QueueStatus.OPEN
    assert dropped.ready_deadline is None


@pytest.mark.asyncio
async def test_response_after_deadline_is_rejected():
    service = PartyQueueService(
        InMemoryPartyQueueRepository(),
        ready_timeout=timedelta(seconds=1),
    )
    await service.create("late", 1)
    await service.join("late", 1)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.start_ready_check("late", now=now)

    with pytest.raises(QueueError, match="expired"):
        await service.respond(
            "late", 1, ReadyStatus.READY, now=now + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_ready_timeout_can_cancel_entire_queue():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(repo, ready_timeout=timedelta(seconds=30))
    await service.create("lobby", 2)
    await service.join("lobby", 1)
    await service.join("lobby", 2)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.start_ready_check("lobby", now=now)
    await service.respond("lobby", 1, "ready", now=now)

    queue, non_ready = await service.expire(
        "lobby", now=now + timedelta(seconds=31)
    )

    assert queue.status is QueueStatus.CANCELLED
    assert non_ready == (2,)


@pytest.mark.asyncio
async def test_ready_timeout_can_drop_non_ready_and_promote():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(
        repo, ready_timeout=timedelta(seconds=30), cancel_on_timeout=False
    )
    await service.create("lobby", 2)
    await service.join("lobby", 1)
    await service.join("lobby", 2)
    await service.join("lobby", 3)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await service.start_ready_check("lobby", now=now)
    await service.respond("lobby", 1, "ready", now=now)

    queue, non_ready = await service.expire(
        "lobby", now=now + timedelta(seconds=31)
    )

    assert non_ready == (2,)
    assert queue.status is QueueStatus.OPEN
    assert [member.user_id for member in queue.active] == [1, 3]


@pytest.mark.asyncio
async def test_join_is_idempotent_for_existing_member():
    repo = InMemoryPartyQueueRepository()
    service = PartyQueueService(repo)
    await service.create("lobby", 1)

    _, first = await service.join("lobby", 7, ("mid",))
    queue, second = await service.join("lobby", 7, ("support",))

    assert first == "active"
    assert second == "unchanged"
    assert len(queue.active) == 1
    assert queue.active[0].preferred_roles == ("mid",)
