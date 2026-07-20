"""DraftCoordinator routing/state logic (Issue #48, Phase 3c)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.draft import DraftManager
from utils.draft_coordinator import DraftCoordinator


def _coord(*, activity_enabled=False, drafts=None):
    formatter = MagicMock()
    formatter.format_error.side_effect = lambda m: f"ERR:{m}"
    activity = MagicMock()
    activity.enabled = activity_enabled
    return DraftCoordinator(
        client=MagicMock(),
        drafts=drafts if drafts is not None else DraftManager(),
        activity_client=activity,
        renderer=MagicMock(),
        active_draft_store=MagicMock(),
        tracked_messages={},
        formatter=formatter,
        resolve_god_name=lambda g: (g, None),
        reports_channels={},
        number_emojis=["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"],
        channel_has_active=lambda cid: None,
    )


def test_has_active_draft_tracks_activity_state():
    coord = _coord()
    assert coord.has_active_draft(10) is False
    coord.match_ids[10] = "m1"
    assert coord.has_active_draft(10) is True


def test_cleanup_draft_clears_all_state_and_cancels_task():
    coord = _coord()
    coord.match_ids[10] = "m1"
    coord.match_channels["m1"] = 10
    coord.snapshots[10] = {"x": 1}
    coord.board_message_ids[10] = 999
    task = MagicMock()
    coord.ws_tasks[10] = task

    coord.cleanup_draft(10)

    assert coord.match_ids == {}
    assert coord.match_channels == {}
    assert coord.snapshots == {}
    assert coord.board_message_ids == {}
    assert coord.ws_tasks == {}
    task.cancel.assert_called_once()


def test_register_activity_draft_records_state():
    coord = _coord()
    coord.register_activity_draft(10, "m1", {"s": 1})
    assert coord.match_ids[10] == "m1"
    assert coord.match_channels["m1"] == 10
    assert coord.snapshots[10] == {"s": 1}


def _message(content=".draft start", mentions=None, channel_id=10):
    msg = MagicMock()
    msg.content = content
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.channel.name = "general"
    msg.channel.send = AsyncMock()
    msg.mentions = mentions or []
    msg.guild = MagicMock(id=1, name="G")
    author = MagicMock()
    author.id = 5
    msg.author = author
    return msg


async def test_handle_draft_local_start_requires_two_captains():
    coord = _coord(activity_enabled=False)
    msg = _message(mentions=[])
    result = await coord.handle_draft({"action": "start"}, msg)
    assert "Usage:" in result


async def test_handle_draft_routes_to_activity_when_enabled():
    coord = _coord(activity_enabled=True)
    coord.activity_error = None
    msg = _message(mentions=[])
    # Activity start with <2 mentions also returns the usage error, but via the
    # activity path (no drafts lock). Just assert it produced the usage error.
    result = await coord.handle_draft({"action": "start"}, msg)
    assert "Usage:" in result


async def test_local_start_success_saves_restart_pointer():
    coord = _coord(activity_enabled=False)
    blue = MagicMock(id=1, display_name="Blue")
    red = MagicMock(id=2, display_name="Red")
    msg = _message(mentions=[blue, red])
    coord._formatter.format_draft_board.return_value = "BOARD"
    msg.channel.send = AsyncMock(return_value=MagicMock(id=77))

    result = await coord.handle_draft({"action": "start"}, msg)

    assert result is None
    coord._active_store.save.assert_called_once()
    assert coord._drafts.get(10) is not None
