"""End-to-end r67 tests driving bot.on_message with mocked Discord objects.

These exercise the real routing in ``bot.on_message`` — command parsing,
permission gating, passive reactions, migration defaults, and the hidden
67 Survivor event — with Discord fully mocked (Issue #47, Gate 5/6).
"""

import random
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service


class _Rng(random.Random):
    """Deterministic RNG whose random() returns a fixed value."""

    def __init__(self, value):
        super().__init__()
        self._value = value

    def random(self):
        return self._value


@pytest.fixture()
def r67(tmp_path, monkeypatch, tmp_settings, tmp_dashboard_db):
    """Fresh r67 service on an isolated DB, with a roll that always succeeds."""
    repo = SQLiteR67Repository(tmp_path / "r67.db")
    service = R67Service(repo, rng=_Rng(0.0))  # 0.0 < 0.07 -> passive roll passes
    monkeypatch.setattr(bot, "r67_service", service)
    return service


def _make_message(content, *, user_id=100, manage_guild=False, guild=None):
    msg = MagicMock()
    msg.content = content
    author = MagicMock()
    author.id = user_id
    author.bot = False
    author.guild_permissions.administrator = False
    author.guild_permissions.manage_guild = manage_guild
    msg.author = author
    channel = MagicMock()
    channel.id = 555
    channel.send = AsyncMock()
    msg.channel = channel
    msg.guild = guild if guild is not None else _make_guild()
    return msg


def _make_guild(guild_id=1):
    guild = MagicMock()
    guild.id = guild_id
    return guild


def _all_responses():
    from utils.r67.responses import POOLS

    out = set()
    for pool in POOLS.values():
        out.update(pool)
    return out


async def test_dot_r67_returns_approved_response(r67):
    msg = _make_message(".r67")
    await bot.on_message(msg)
    msg.channel.send.assert_called_once()
    assert msg.channel.send.call_args[0][0] in _all_responses()


async def test_reactions_on_requires_manage_guild(r67):
    denied = _make_message(".r67 reactions on", manage_guild=False)
    await bot.on_message(denied)
    reply = denied.channel.send.call_args[0][0]
    assert "Manage Server" in reply
    assert r67.status(1).reactions_enabled is False


async def test_reactions_on_with_permission_enables_and_status_reflects(r67):
    enable = _make_message(".r67 reactions on", manage_guild=True)
    await bot.on_message(enable)
    assert r67.status(1).reactions_enabled is True

    status = _make_message(".r67 status")
    await bot.on_message(status)
    assert "Enabled" in status.channel.send.call_args[0][0]


async def test_r67_newline_status_parses_as_status(r67):
    # Regression: whitespace-tolerant argument split.
    msg = _make_message(".r67\nstatus")
    await bot.on_message(msg)
    assert "Disabled" in msg.channel.send.call_args[0][0]


async def test_passive_reaction_when_enabled(r67):
    r67.enable_reactions(1)
    msg = _make_message("that's 67 for sure")
    await bot.on_message(msg)
    msg.channel.send.assert_called_once()
    assert msg.channel.send.call_args[0][0] in _all_responses()


async def test_no_passive_reaction_when_disabled_by_default(r67):
    # Migration default: existing guild is opt-out; a plain 67 does nothing.
    msg = _make_message("look, 67")
    await bot.on_message(msg)
    msg.channel.send.assert_not_called()


async def test_passive_ignores_non_qualifying_message(r67):
    r67.enable_reactions(1)
    msg = _make_message("just a normal sentence")
    await bot.on_message(msg)
    msg.channel.send.assert_not_called()


async def test_dot_r67_command_is_not_treated_as_passive(r67):
    # A direct command must never count as passive/Survivor activity.
    r67.enable_reactions(1)
    msg = _make_message(".r67")
    await bot.on_message(msg)
    # Exactly one send: the direct response, not an extra passive reply.
    msg.channel.send.assert_called_once()


async def test_survivor_event_fires_and_announces(r67):
    r67.enable_reactions(1)
    # Shared guild with a role the bot can assign.
    role = MagicMock()
    role.name = "67 Survivor"
    role.id = 999
    role.position = 1
    role.is_default = lambda: False
    guild = _make_guild()
    guild.roles = [role]
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role.position = 100
    members = {uid: MagicMock(add_roles=AsyncMock()) for uid in range(1, 7)}
    guild.get_member = lambda uid: members.get(uid)
    guild.create_role = AsyncMock()

    last_msg = None
    for uid in range(1, 7):
        last_msg = _make_message("67", user_id=uid, guild=guild)
        await bot.on_message(last_msg)

    # The sixth message posts the announcement (plus possibly a passive reply).
    sent_texts = [c.args[0] for c in last_msg.channel.send.call_args_list]
    assert any("THE SIX HAVE SPOKEN" in text for text in sent_texts)
    guild.create_role.assert_not_called()  # existing role reused
    assert len(r67.repository.all_role_grants()) == 6
