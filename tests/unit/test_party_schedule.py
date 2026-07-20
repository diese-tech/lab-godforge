from datetime import datetime, timezone

import pytest

from utils.party import PlayerPreferences
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService
from utils.party_schedule import (
    EventState,
    Recurrence,
    ScheduleError,
    ScheduleRepository,
    calendar_ics,
    convert_to_lobby,
    parse_local_start,
)
from utils.party_store import SQLitePartyRepository


NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def test_natural_time_requires_explicit_valid_timezone_and_normalizes_dst():
    assert parse_local_start(
        "Friday 8 PM", "America/New_York", now=NOW
    ) == datetime(2026, 7, 25, 0, tzinfo=timezone.utc)
    assert parse_local_start(
        "2026-12-04 8 PM", "America/New_York", now=NOW
    ) == datetime(2026, 12, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError, match="IANA"):
        parse_local_start("tomorrow 8 PM", "EST", now=NOW)


def _event(repository, *, recurrence=Recurrence.ONCE, capacity=2):
    return repository.create(
        guild_id=1,
        organizer_id=10,
        title="Friday Customs",
        starts_at=datetime(2026, 7, 25, 0, tzinfo=timezone.utc),
        timezone_name="America/New_York",
        recurrence=recurrence,
        capacity=capacity,
        role_slots=("solo", "jungle"),
        reminder_minutes=(60, 15),
        operation_id="interaction-1",
    )


def test_confirmation_rsvp_waitlist_and_promotion_survive_restart(tmp_path):
    path = tmp_path / "party.db"
    repository = ScheduleRepository(path)
    pending = _event(repository)
    assert pending.state is EventState.PENDING_CONFIRMATION
    confirmed = repository.confirm(pending.event_id, 10)
    assert confirmed.state is EventState.SCHEDULED
    repository.rsvp(
        pending.event_id, 1, PlayerPreferences("solo", "support", captain=True)
    )
    repository.rsvp(pending.event_id, 2, PlayerPreferences("jungle"))
    full = repository.rsvp(pending.event_id, 3, PlayerPreferences("mid", fill=True))
    assert [r.user_id for r in full.rsvps] == [1, 2]
    assert [r.user_id for r in full.waitlist] == [3]

    promoted = ScheduleRepository(path).cancel_rsvp(pending.event_id, 1)
    assert [r.user_id for r in promoted.rsvps] == [2, 3]
    assert promoted.rsvps[1].preferences.fill is True


@pytest.mark.asyncio
async def test_conversion_is_idempotent_and_preserves_preferences_and_waitlist(tmp_path):
    path = tmp_path / "party.db"
    schedules = ScheduleRepository(path)
    event = _event(schedules)
    schedules.confirm(event.event_id, 10)
    schedules.rsvp(event.event_id, 1, PlayerPreferences("solo", captain=True))
    schedules.rsvp(event.event_id, 2, PlayerPreferences("jungle"))
    schedules.rsvp(event.event_id, 3, PlayerPreferences("support", fill=True))
    parties = SQLitePartyRepository(path)
    queues = PartyQueueService(InMemoryPartyQueueRepository())

    first = await convert_to_lobby(schedules.get(event.event_id), schedules, parties, queues)
    second = await convert_to_lobby(schedules.get(event.event_id), schedules, parties, queues)

    assert first.lobby_id == second.lobby_id == f"scheduled-{event.event_id}"
    assert first.participant(1).primary_role == "solo"
    assert first.participant(1).captain is True
    queue = await queues.get(first.lobby_id)
    assert [member.user_id for member in queue.active] == [1, 2]
    assert [member.user_id for member in queue.waitlist] == [3]
    assert schedules.get(event.event_id).state is EventState.CONVERTED


def test_weekly_calendar_export_needs_no_calendar_account(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = _event(repository, recurrence=Recurrence.WEEKLY)
    payload = calendar_ics(event).decode()
    assert "BEGIN:VCALENDAR" in payload
    assert "RRULE:FREQ=WEEKLY" in payload
    assert "DTSTART:20260725T000000Z" in payload
    assert f"UID:{event.event_id}@godforge" in payload


def test_weekly_conversion_schedules_next_occurrence_once(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    event = _event(repository, recurrence=Recurrence.WEEKLY)
    repository.confirm(event.event_id, 10)
    repository.mark_converted(event.event_id, "lobby-one")
    repository.mark_converted(event.event_id, "lobby-one")
    upcoming = repository.list_upcoming(1, now=NOW)
    assert len(upcoming) == 1
    assert upcoming[0].starts_at == datetime(2026, 8, 1, 0, tzinfo=timezone.utc)


def test_create_retry_and_confirm_are_idempotent(tmp_path):
    repository = ScheduleRepository(tmp_path / "party.db")
    first = _event(repository)
    second = _event(repository)
    assert first.event_id == second.event_id
    repository.confirm(first.event_id, 10)
    assert repository.confirm(first.event_id, 10).state is EventState.SCHEDULED


def test_reminder_claim_is_restart_safe_and_only_due_before_start(tmp_path):
    path = tmp_path / "party.db"
    repository = ScheduleRepository(path)
    event = _event(repository)
    repository.confirm(event.event_id, 10)
    due_at = datetime(2026, 7, 24, 23, 1, tzinfo=timezone.utc)
    claimed = repository.claim_due_reminders(now=due_at)
    assert [(minutes, occurrence) for _, minutes, occurrence in claimed] == [
        (60, event.starts_at)
    ]
    assert ScheduleRepository(path).claim_due_reminders(now=due_at) == []
