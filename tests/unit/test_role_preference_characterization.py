"""Characterization tests for bot._handle_role_preference (Issue #48, Phase 8f).

Written before extracting this handler into PartyLobbyService. Pins down
current behavior: toggling a managed role on/off and syncing party role
preferences, using a real SQLitePartyRepository and mocked Discord objects.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
import discord
from utils.party_store import SQLitePartyRepository


@pytest.fixture()
def party_repo(tmp_path, monkeypatch):
    repo = SQLitePartyRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "party_repository", repo)
    monkeypatch.setattr(bot._party_lobby_deps, "party_repository", repo)
    return repo


def _interaction(*, guild_id=1, user_id=100, current_role_ids=(), managed_role_id=None):
    interaction = MagicMock()
    interaction.guild = MagicMock(id=guild_id)
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [MagicMock(id=rid) for rid in current_role_ids]
    interaction.user = member
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.fixture(autouse=True)
def stub_settings_and_role(monkeypatch, tmp_settings):
    from utils import settings as settings_mod

    settings_mod.update_guild_settings(
        "1", {"managed": {"roleIds": {"solo": "9999"}}}
    )
    monkeypatch.setattr(bot, "set_member_role", AsyncMock())
    yield


async def test_requires_guild():
    interaction = MagicMock()
    interaction.guild = None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    await bot._handle_role_preference(interaction, "solo")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Server-only" in reply


async def test_enabling_role_adds_to_preferences(party_repo):
    interaction = _interaction(current_role_ids=())  # role not yet held -> enabling
    await bot._handle_role_preference(interaction, "solo")
    reply = interaction.response.send_message.call_args.args[0]
    assert "added" in reply
    profile = party_repo.get_player_preferences(1, 100)
    assert "solo" in profile.roles


async def test_disabling_role_removes_from_preferences(party_repo):
    # Member already holds the managed role id 9999 -> toggling disables it.
    interaction = _interaction(current_role_ids=(9999,))
    await bot._handle_role_preference(interaction, "solo")
    reply = interaction.response.send_message.call_args.args[0]
    assert "removed" in reply
    profile = party_repo.get_player_preferences(1, 100)
    assert "solo" not in profile.roles


async def test_captain_toggle_updates_captain_flag(party_repo):
    interaction = _interaction(current_role_ids=())
    await bot._handle_role_preference(interaction, "captain")
    profile = party_repo.get_player_preferences(1, 100)
    assert profile.captain is True
