"""ScheduleLifecycle cleanup hook (Issue #48, Phase 8d)."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from utils.lifecycle import LifecycleContext
from utils.schedule_lifecycle import ScheduleLifecycle


def _event(organizer_id=1, rsvp_ids=(), waitlist_ids=(), title="Friday Night"):
    event = MagicMock()
    event.organizer_id = organizer_id
    event.title = title
    event.rsvps = [MagicMock(user_id=uid) for uid in rsvp_ids]
    event.waitlist = [MagicMock(user_id=uid) for uid in waitlist_ids]
    return event


def test_name_is_schedule():
    lifecycle = ScheduleLifecycle(MagicMock())
    assert lifecycle.name == "schedule"


async def test_cleanup_dms_all_recipients():
    repo = MagicMock()
    occurrence = MagicMock()
    occurrence.timestamp.return_value = 1700000000
    event = _event(organizer_id=1, rsvp_ids=(2,), waitlist_ids=(3,))
    repo.claim_due_reminders.return_value = [(event, 15, occurrence)]
    lifecycle = ScheduleLifecycle(repo)

    users = {}
    for uid in (1, 2, 3):
        user = MagicMock()
        user.send = AsyncMock()
        users[uid] = user

    ctx = LifecycleContext(get_guild=lambda gid: None, get_user=lambda uid: users.get(uid))
    await lifecycle.on_cleanup(ctx)

    for user in users.values():
        user.send.assert_awaited_once()
        reply = user.send.call_args.args[0]
        assert "Friday Night" in reply


async def test_cleanup_falls_back_to_fetch_user_when_uncached():
    repo = MagicMock()
    occurrence = MagicMock()
    occurrence.timestamp.return_value = 1700000000
    event = _event(organizer_id=1)
    repo.claim_due_reminders.return_value = [(event, 60, occurrence)]
    lifecycle = ScheduleLifecycle(repo)

    fetched_user = MagicMock()
    fetched_user.send = AsyncMock()

    ctx = LifecycleContext(
        get_guild=lambda gid: None,
        get_user=lambda uid: None,
        fetch_user=AsyncMock(return_value=fetched_user),
    )
    await lifecycle.on_cleanup(ctx)

    fetched_user.send.assert_awaited_once()


async def test_cleanup_swallows_forbidden_and_continues():
    repo = MagicMock()
    occurrence = MagicMock()
    occurrence.timestamp.return_value = 1700000000
    event = _event(organizer_id=1, rsvp_ids=(2,))
    repo.claim_due_reminders.return_value = [(event, 15, occurrence)]
    lifecycle = ScheduleLifecycle(repo)

    blocked_user = MagicMock()
    blocked_user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "blocked"))
    ok_user = MagicMock()
    ok_user.send = AsyncMock()
    users = {1: blocked_user, 2: ok_user}

    ctx = LifecycleContext(get_guild=lambda gid: None, get_user=lambda uid: users.get(uid))
    # Must not raise even though one DM fails.
    await lifecycle.on_cleanup(ctx)
    ok_user.send.assert_awaited_once()


async def test_cleanup_noop_when_nothing_due():
    repo = MagicMock()
    repo.claim_due_reminders.return_value = []
    lifecycle = ScheduleLifecycle(repo)
    ctx = LifecycleContext(get_guild=lambda gid: None)
    await lifecycle.on_cleanup(ctx)  # no error
