"""Direct tests for the extracted CustomCommandRuntime (Issue #48, Phase 4)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import custom_commands
from utils.custom_command_runtime import CustomCommandRuntime, parse_cooldown_seconds


def _message(content, *, guild_id="123", channel_name="captains", admin=False, roles=None):
    channel = MagicMock()
    channel.id = 555
    channel.name = channel_name
    channel.send = AsyncMock()
    author = MagicMock()
    author.id = 999
    author.bot = False
    author.display_name = "User"
    author.roles = roles or []
    author.guild_permissions = MagicMock(administrator=admin)
    msg = MagicMock()
    msg.content = content
    msg.channel = channel
    msg.author = author
    msg.guild = MagicMock(id=guild_id)
    return msg


@pytest.mark.parametrize(
    "value,expected",
    [("0s", 0), ("30s", 30), ("5m", 300), ("1h", 3600), ("99h", 3600), ("x", 0)],
)
def test_parse_cooldown_seconds(value, expected):
    assert parse_cooldown_seconds(value) == expected


def _runtime(admin=False, clock=None):
    return CustomCommandRuntime(is_admin=lambda m: admin, clock=clock)


async def test_runtime_executes_matching_command(tmp_custom_commands):
    custom_commands.upsert_command(
        "123",
        {"trigger": ".hi", "response": "Hello.", "channel": "", "role_gate": "Everyone",
         "cooldown": "0s", "enabled": True},
    )
    rt = _runtime()
    msg = _message(".hi")
    assert await rt.handle(msg, "hi") is True
    assert msg.channel.send.await_args.args[0] == "Hello."


async def test_runtime_unknown_trigger_not_handled(tmp_custom_commands):
    rt = _runtime()
    msg = _message(".nope")
    assert await rt.handle(msg, "nope") is False
    msg.channel.send.assert_not_awaited()


async def test_runtime_injected_clock_drives_cooldown(tmp_custom_commands):
    custom_commands.upsert_command(
        "123",
        {"trigger": ".tick", "response": "tick", "channel": "", "role_gate": "Everyone",
         "cooldown": "5s", "enabled": True},
    )
    now = [1000.0]
    rt = _runtime(clock=lambda: now[0])
    msg = _message(".tick")

    assert await rt.handle(msg, "tick") is True
    assert msg.channel.send.await_args.args[0] == "tick"

    # Still within cooldown -> cooldown notice.
    now[0] = 1002.0
    await rt.handle(msg, "tick")
    assert "cooldown" in msg.channel.send.await_args.args[0]

    # After cooldown -> executes again.
    now[0] = 1006.0
    await rt.handle(msg, "tick")
    assert msg.channel.send.await_args.args[0] == "tick"


async def test_runtime_admin_gate_uses_injected_check(tmp_custom_commands):
    custom_commands.upsert_command(
        "123",
        {"trigger": ".admincmd", "response": "secret", "channel": "", "role_gate": "Admins",
         "cooldown": "0s", "enabled": True},
    )
    msg = _message(".admincmd")
    assert await _runtime(admin=False).handle(msg, "admincmd") is True
    assert "do not have permission" in msg.channel.send.await_args.args[0]

    msg2 = _message(".admincmd")
    assert await _runtime(admin=True).handle(msg2, "admincmd") is True
    assert msg2.channel.send.await_args.args[0] == "secret"
