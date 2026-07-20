import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from utils.party_queue import (
    PartyQueueService,
    QueueStatus,
    ReadyStatus,
    SQLitePartyQueueRepository,
)


@pytest.mark.asyncio
async def test_restart_preserves_member_order_and_role_preferences(tmp_path):
    path = tmp_path / "godforge.sqlite3"
    service = PartyQueueService(SQLitePartyQueueRepository(path))
    await service.create("lobby", 2)
    await service.join("lobby", 10, ("solo",))
    await service.join("lobby", 11, ("mid",))
    await service.join("lobby", 12, ("support",))
    await service.join("lobby", 13, ("jungle",))

    restored = await SQLitePartyQueueRepository(path).load("lobby")

    assert [member.user_id for member in restored.active] == [10, 11]
    assert [member.user_id for member in restored.waitlist] == [12, 13]
    assert restored.waitlist[0].preferred_roles == ("support",)
    assert restored.next_sequence == 5


@pytest.mark.asyncio
async def test_restart_preserves_ready_deadline_responses_extensions_and_status(tmp_path):
    path = tmp_path / "godforge.sqlite3"
    service = PartyQueueService(
        SQLitePartyQueueRepository(path),
        ready_timeout=timedelta(seconds=30),
        extension=timedelta(minutes=5),
    )
    await service.create("lobby", 2)
    await service.join("lobby", 10)
    await service.join("lobby", 11)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    started = await service.start_ready_check("lobby", now=now)
    await service.respond("lobby", 10, ReadyStatus.READY)
    extended, _ = await service.respond("lobby", 11, ReadyStatus.NEED_5)

    restored = await SQLitePartyQueueRepository(path).load("lobby")

    assert restored.status is QueueStatus.READY_CHECK
    assert restored.ready == {10: ReadyStatus.READY, 11: ReadyStatus.NEED_5}
    assert restored.ready_deadline == started.ready_deadline + timedelta(minutes=5)
    assert restored.ready_deadline == extended.ready_deadline
    assert restored.extensions_used == 1


@pytest.mark.asyncio
async def test_promotion_order_survives_restart(tmp_path):
    path = tmp_path / "godforge.sqlite3"
    repository = SQLitePartyQueueRepository(path)
    service = PartyQueueService(repository)
    await service.create("lobby", 2)
    await service.join("lobby", 1, ("solo",))
    await service.join("lobby", 2, ("mid",))
    await service.join("lobby", 3, ("solo",))
    await service.join("lobby", 4, ("support",))
    await service.leave("lobby", 2)

    restored = await SQLitePartyQueueRepository(path).load("lobby")

    assert [member.user_id for member in restored.active] == [1, 4]
    assert [member.user_id for member in restored.waitlist] == [3]


def test_schema_creation_is_additive_to_supplied_database(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO existing_data(value) VALUES ('preserved')")

    SQLitePartyQueueRepository(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM existing_data").fetchone()[0] == "preserved"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"party_queue_state", "party_queue_members"} <= tables
