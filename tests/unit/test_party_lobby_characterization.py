"""Characterization tests for the play-panel/lobby-card/queue/ready-check/
draft-launch handlers in bot.py.

Written before extracting these handlers into feature modules (Issue #48,
Phase 5d). This is the largest and most tightly-coupled remaining surface —
pins down current behavior for _handle_play_panel_action,
_handle_create_lobby_submission, _join_lobby_from_preferences,
_ensure_party_queue, _handle_lobby_card_action, _launch_party_draft, and
_handle_ready_check_action using real repositories (SQLitePartyRepository,
PartyQueueService) and mocked Discord interactions/channels/guilds.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.party import LobbyState, Participant
from utils.party_draft import PartyDraftLaunchRepository
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService
from utils.party_store import SQLitePartyRepository
from utils.match_history import MatchHistoryRepository
from utils.scrims import ScrimRepository


@pytest.fixture()
def party_repos(tmp_path, monkeypatch):
    party = SQLitePartyRepository(tmp_path / "party.db")
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    party_draft = PartyDraftLaunchRepository(tmp_path / "party.db")
    match_history = MatchHistoryRepository(tmp_path / "party.db")
    scrims = ScrimRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "party_repository", party)
    monkeypatch.setattr(bot, "party_queue_service", queue_service)
    monkeypatch.setattr(bot, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot, "match_history_repository", match_history)
    monkeypatch.setattr(bot, "scrim_repository", scrims)
    monkeypatch.setattr(bot._match_action_deps, "match_history_repository", match_history)
    monkeypatch.setattr(bot._match_action_deps, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot._match_action_deps, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._party_lobby_deps, "party_repository", party)
    monkeypatch.setattr(bot._party_lobby_deps, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._party_lobby_deps, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot._party_lobby_deps, "scrim_repository", scrims)
    return party, queue_service, party_draft, match_history, scrims


def _guild(guild_id=1, *, members=None):
    guild = MagicMock()
    guild.id = guild_id
    members = members or {}
    guild.get_member = lambda uid: members.get(uid)
    guild.get_channel = lambda cid: None
    return guild


def _interaction(*, guild_id=1, user_id=100, guild=None, channel=None, message=None):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    interaction.guild = guild if guild is not None else (_guild(guild_id) if guild_id else None)
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    channel = channel if channel is not None else MagicMock(id=54321)
    channel.send = AsyncMock(return_value=MagicMock(id=12345))
    interaction.channel = channel
    interaction.message = message
    return interaction


def _lobby_footer_message(lobby_id):
    footer = MagicMock(text=f"lobby_id={lobby_id}")
    embed = MagicMock(footer=footer)
    message = MagicMock(embeds=[embed])
    message.edit = AsyncMock()
    return message


# -- Play panel ---------------------------------------------------------

async def test_play_panel_preferences_shows_current_roles(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "preferences")
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "view" in kwargs


async def test_play_panel_browse_empty_reports_none(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "browse")
    reply = interaction.response.send_message.call_args.args[0]
    assert "No party lobbies" in reply


async def test_play_panel_create_opens_modal(party_repos):
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "create")
    interaction.response.send_modal.assert_awaited_once()


async def test_play_panel_browse_shows_open_lobby(party_repos):
    party, *_ = party_repos
    party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction()
    await bot._handle_play_panel_action(interaction, "browse")
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "embed" in kwargs


# -- Create lobby + join -------------------------------------------------

async def test_create_lobby_submission_creates_and_seats_organizer(party_repos, tmp_settings):
    party, queue_service, *_ = party_repos
    interaction = _interaction(user_id=100)
    payload = {
        "party_size": 10, "mode": "conquest", "region": "na", "format": "5v5",
        "voice_required": False, "skill_band": "", "notes": "",
    }
    await bot._handle_create_lobby_submission(interaction, payload)
    interaction.response.send_message.assert_awaited_once()
    lobbies = [r.lobby for r in party.recover_active(1)]
    assert len(lobbies) == 1
    assert lobbies[0].participants[0].user_id == 100
    queue = await queue_service.get(lobbies[0].lobby_id)
    assert queue is not None


async def test_join_lobby_from_preferences_adds_participant(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(100), operation_id="join-organizer")
    interaction = _interaction(user_id=200, message=_lobby_footer_message(lobby.lobby_id))
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False, "captain": False}
    await bot._join_lobby_from_preferences(interaction, lobby.lobby_id, payload)
    updated = party.get(1, lobby.lobby_id)
    assert any(p.user_id == 200 for p in updated.participants)


async def test_join_full_lobby_starts_ready_check(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))
    payload = {"primary_role": "mid", "secondary_role": "", "fill": False, "captain": False}
    await bot._join_lobby_from_preferences(interaction, lobby.lobby_id, payload)
    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.READY_CHECK
    interaction.channel.send.assert_awaited_once()


# -- Lobby card -----------------------------------------------------------

async def test_lobby_card_leave_removes_participant(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(100), operation_id="join-organizer")
    party.save_participant(1, lobby.lobby_id, Participant(200), operation_id="join-other")
    await bot._ensure_party_queue(party.get(1, lobby.lobby_id))
    interaction = _interaction(user_id=200, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "leave")
    updated = party.get(1, lobby.lobby_id)
    assert not any(p.user_id == 200 for p in updated.participants)


async def test_lobby_card_inactive_lobby_reports_error(party_repos):
    interaction = _interaction(message=_lobby_footer_message("missing-lobby"))
    await bot._handle_lobby_card_action(interaction, "leave")
    reply = interaction.response.send_message.call_args.args[0]
    assert "no longer active" in reply


async def test_lobby_card_cancel_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "cancel")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_lobby_card_cancel_by_organizer_transitions(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=100, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "cancel")
    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.CANCELLED


async def test_lobby_card_ready_check_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_lobby_card_action(interaction, "ready_check")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


# -- Ready check ------------------------------------------------------------

async def test_ready_check_all_ready_transitions_to_forming(party_repos, monkeypatch):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="join-2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    room_service = MagicMock()
    room_service.provision = AsyncMock(
        return_value=MagicMock(text_room_id=555, team_voice_ids=())
    )
    monkeypatch.setattr(bot, "_match_room_service_for_guild", lambda guild: room_service)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )

    guild = _guild(1)
    interaction1 = _interaction(
        user_id=1, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )
    await bot._handle_ready_check_action(interaction1, "ready")

    interaction2 = _interaction(
        user_id=2, guild=guild, message=_lobby_footer_message(lobby.lobby_id)
    )
    await bot._handle_ready_check_action(interaction2, "ready")

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.FORMING
    reply = interaction2.response.edit_message.call_args.kwargs["content"]
    assert "forming the match" in reply


async def test_ready_check_drop_reopens_lobby(party_repos):
    party, queue_service, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=1, capacity=2, operation_id="create-1")
    party.save_participant(1, lobby.lobby_id, Participant(1), operation_id="join-1")
    party.save_participant(1, lobby.lobby_id, Participant(2), operation_id="join-2")
    await queue_service.create(lobby.lobby_id, 2)
    await queue_service.join(lobby.lobby_id, 1, ())
    await queue_service.join(lobby.lobby_id, 2, ())
    await queue_service.start_ready_check(lobby.lobby_id)
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="ready")

    interaction = _interaction(user_id=2, message=_lobby_footer_message(lobby.lobby_id))
    await bot._handle_ready_check_action(interaction, "drop")

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.OPEN
    assert not any(p.user_id == 2 for p in updated.participants)


# -- Draft launch -------------------------------------------------------

async def test_launch_party_draft_requires_organizer(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=999)
    await bot._launch_party_draft(interaction, lobby)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_launch_party_draft_requires_forming_state(party_repos):
    party, *_ = party_repos
    lobby = party.create(guild_id=1, organizer_id=100, capacity=10, operation_id="create-1")
    interaction = _interaction(user_id=100)
    await bot._launch_party_draft(interaction, lobby)
    reply = interaction.response.send_message.call_args.args[0]
    assert "ready check" in reply.lower()


async def test_launch_party_draft_local_success(party_repos, monkeypatch):
    from utils.draft import DraftManager

    party, queue_service, party_draft, match_history, scrims = party_repos
    ROLES = ("solo", "jungle", "mid", "support", "adc")
    lobby = party.create(guild_id=1, organizer_id=1, capacity=10, operation_id="create-1")
    members = {}
    for user_id, role in enumerate(ROLES * 2, start=1):
        lobby = party.save_participant(
            1, lobby.lobby_id,
            Participant(user_id, primary_role=role, captain=user_id in {1, 6}),
            operation_id=f"j{user_id}",
        )
        members[user_id] = MagicMock(id=user_id, display_name=f"Player{user_id}")
    party.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="full")
    party.transition(1, lobby.lobby_id, LobbyState.READY_CHECK, operation_id="rc")
    lobby = party.transition(1, lobby.lobby_id, LobbyState.FORMING, operation_id="forming")

    drafts = DraftManager()
    monkeypatch.setattr(bot, "drafts", drafts)
    monkeypatch.setattr(bot._party_lobby_deps, "drafts", drafts)
    monkeypatch.setattr(bot.activity_client, "base_url", "")

    guild = _guild(1, members=members)
    interaction = _interaction(user_id=1, guild=guild)
    interaction.channel.name = "party-draft"

    await bot._launch_party_draft(interaction, lobby)

    updated = party.get(1, lobby.lobby_id)
    assert updated.state is LobbyState.ACTIVE
    launch = party_draft.get(lobby.lobby_id)
    assert launch.status == "active"
    assert drafts.get(interaction.channel.id) is not None
    interaction.followup.send.assert_awaited()
