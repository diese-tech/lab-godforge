"""SessionCommandHandler behavior (Issue #48, Phase 3)."""

from unittest.mock import MagicMock

import pytest

from utils import formatter
from utils.session import SessionManager
from utils.session_commands import SessionCommandHandler


def _handler(tracked=None, active=None):
    return SessionCommandHandler(
        sessions=SessionManager(),
        formatter=formatter,
        tracked_messages=tracked if tracked is not None else {},
        channel_has_active=lambda cid: active,
    )


async def test_start_blocked_by_active_draft():
    handler = _handler(active="draft")
    result = await handler.handle({"action": "start"}, 1)
    # format_error returns an embed; just assert we did not start a session.
    assert handler._sessions.get(1) is None


async def test_start_then_duplicate_start():
    handler = _handler()
    first = await handler.handle({"action": "start"}, 1)
    assert "started" in first
    assert handler._sessions.get(1) is not None
    # Second start returns an error notice (session already active).
    second = await handler.handle({"action": "start"}, 1)
    assert "already active" in second


async def test_end_clears_tracked_messages_for_channel():
    tracked = {10: {"channel_id": 1}, 11: {"channel_id": 2}}
    handler = _handler(tracked=tracked)
    await handler.handle({"action": "start"}, 1)
    await handler.handle({"action": "end"}, 1)
    # Only the channel-1 tracked message is removed.
    assert 10 not in tracked
    assert 11 in tracked


async def test_reset_keeps_session_but_clears_tracked():
    tracked = {10: {"channel_id": 1}}
    handler = _handler(tracked=tracked)
    await handler.handle({"action": "start"}, 1)
    msg = await handler.handle({"action": "reset"}, 1)
    assert "cleared" in msg
    assert handler._sessions.get(1) is not None
    assert 10 not in tracked


async def test_show_without_session_returns_error():
    handler = _handler()
    result = await handler.handle({"action": "show"}, 99)
    assert "No active session" in result
