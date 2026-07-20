from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.discord_match_rooms import DiscordMatchRoomOperations


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.default_role = object()
        self.members = {10: object(), 11: object()}
        self.channel = SimpleNamespace(set_permissions=AsyncMock())

    def get_channel(self, resource_id):
        return self.channel if resource_id == 100 else None

    def get_member(self, user_id):
        return self.members.get(user_id)


@pytest.mark.asyncio
async def test_lock_and_unlock_keep_everyone_private_and_toggle_participants():
    guild = FakeGuild()
    operations = DiscordMatchRoomOperations(guild, 50, 60)

    await operations.set_locked((100,), (10, 11), True)
    calls = guild.channel.set_permissions.await_args_list
    everyone = calls[0].kwargs["overwrite"]
    locked_member = calls[1].kwargs["overwrite"]
    assert everyone.view_channel is False
    assert everyone.connect is False
    assert locked_member.view_channel is True
    assert locked_member.send_messages is False
    assert locked_member.connect is False

    guild.channel.set_permissions.reset_mock()
    await operations.set_locked((100,), (10, 11), False)
    calls = guild.channel.set_permissions.await_args_list
    everyone = calls[0].kwargs["overwrite"]
    unlocked_member = calls[1].kwargs["overwrite"]
    assert everyone.view_channel is False
    assert everyone.connect is False
    assert unlocked_member.view_channel is True
    assert unlocked_member.send_messages is True
    assert unlocked_member.connect is True
