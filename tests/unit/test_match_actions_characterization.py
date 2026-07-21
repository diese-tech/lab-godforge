"""Characterization tests for match-result/continuity interaction handlers.

Written before extracting bot._handle_match_result_action and
bot._handle_match_continuity_action into a feature module (Issue #48, Phase 6b).
These pin down current behavior, which previously had no direct test coverage —
only the underlying utils.match_history / utils.match_continuity services were
tested. Discord interactions are mocked.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.match_continuity import MatchContinuityRepository
from utils.match_history import MatchHistoryRepository, MatchOutcome
from utils.party import LobbyState, Participant, PartyLobby
from utils.party_draft import PartyDraftLaunchRepository
from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService

ROLES = ("solo", "jungle", "mid", "support", "adc")


@pytest.fixture()
def match_repos(tmp_path, monkeypatch):
    history = MatchHistoryRepository(tmp_path / "party.db")
    continuity = MatchContinuityRepository(tmp_path / "party.db")
    party_draft = PartyDraftLaunchRepository(tmp_path / "party.db")
    queue_service = PartyQueueService(InMemoryPartyQueueRepository())
    monkeypatch.setattr(bot, "match_history_repository", history)
    monkeypatch.setattr(bot, "match_continuity_repository", continuity)
    monkeypatch.setattr(bot, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot, "party_queue_service", queue_service)
    monkeypatch.setattr(bot._match_action_deps, "match_history_repository", history)
    monkeypatch.setattr(bot._match_action_deps, "match_continuity_repository", continuity)
    monkeypatch.setattr(bot._match_action_deps, "party_draft_repository", party_draft)
    monkeypatch.setattr(bot._match_action_deps, "party_queue_service", queue_service)
    return history, continuity, party_draft, queue_service


def _record(history, *, match_id="GF-1", guild_id=1, organizer_id=50):
    from utils.match_history import MatchPlayer, MatchTeam

    blue = MatchTeam(
        "Blue", 1, tuple(MatchPlayer(i + 1, role) for i, role in enumerate(ROLES))
    )
    red = MatchTeam(
        "Red", 6, tuple(MatchPlayer(i + 6, role) for i, role in enumerate(ROLES))
    )
    return history.create(
        guild_id=guild_id,
        organizer_id=organizer_id,
        team_one=blue,
        team_two=red,
        operation_id=f"op-{match_id}",
        draft_reference=match_id,
        match_id=match_id,
    )


def _interaction(*, guild_id=1, user_id=50, match_id="GF-1"):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    interaction.guild = MagicMock(id=guild_id) if guild_id is not None else None
    interaction.user = MagicMock(id=user_id)
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    footer = MagicMock(text=f"match_id={match_id}")
    embed = MagicMock(footer=footer)
    interaction.message = MagicMock(embeds=[embed])
    channel = MagicMock()
    channel.send = AsyncMock()

    async def _empty_history(*args, **kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    channel.history = _empty_history
    interaction.channel = channel
    return interaction


async def test_organizer_can_report_no_contest(match_repos):
    history, *_ = match_repos
    _record(history)
    interaction = _interaction(user_id=50)
    await bot._handle_match_result_action(interaction, MatchOutcome.NO_CONTEST.value)
    interaction.response.edit_message.assert_awaited_once()
    updated = history.get(1, "GF-1")
    assert updated.outcome is MatchOutcome.NO_CONTEST


async def test_captain_cannot_cancel_only_report_winner(match_repos):
    history, *_ = match_repos
    _record(history)
    interaction = _interaction(user_id=1)  # blue captain, not organizer
    await bot._handle_match_result_action(interaction, MatchOutcome.CANCELLED.value)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_captain_reports_winner(match_repos):
    history, *_ = match_repos
    _record(history)
    interaction = _interaction(user_id=1)  # blue captain
    await bot._handle_match_result_action(interaction, MatchOutcome.TEAM_ONE.value)
    interaction.response.edit_message.assert_awaited_once()


async def test_missing_match_reports_not_found(match_repos):
    interaction = _interaction(match_id="missing")
    await bot._handle_match_result_action(interaction, MatchOutcome.TEAM_ONE.value)
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" in reply.lower()


async def test_result_action_requires_guild(match_repos):
    interaction = _interaction(guild_id=None)
    await bot._handle_match_result_action(interaction, MatchOutcome.TEAM_ONE.value)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Server-only" in reply


def _ready_lobby(guild_id=1, organizer_id=50):
    players = tuple(
        Participant(user_id, primary_role=role, captain=user_id in {1, 6})
        for user_id, role in enumerate(ROLES * 2, start=1)
    )
    return PartyLobby(
        "lobby-1",
        guild_id=guild_id,
        organizer_id=organizer_id,
        capacity=10,
        state=LobbyState.FORMING,
        participants=players,
    )


def _begin_launch(party_draft, *, match_id="GF-1", guild_id=1, organizer_id=50):
    launch, _ = party_draft.begin(
        _ready_lobby(guild_id=guild_id, organizer_id=organizer_id),
        operation_id=f"launch-{match_id}",
        channel_id=555,
        match_id_factory=lambda: match_id,
    )
    return launch


async def test_continuity_requires_organizer(match_repos):
    history, continuity, party_draft, queue_service = match_repos
    _record(history)
    _begin_launch(party_draft)
    interaction = _interaction(user_id=999)  # not organizer
    await bot._handle_match_continuity_action(interaction, "run_it_back")
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_continuity_missing_launch_reports_not_found(match_repos):
    history, *_ = match_repos
    _record(history)
    interaction = _interaction(user_id=50)
    await bot._handle_match_continuity_action(interaction, "run_it_back")
    reply = interaction.response.send_message.call_args.args[0]
    assert "could not be found" in reply.lower()


async def test_continuity_run_it_back_edits_message(match_repos):
    history, continuity, party_draft, queue_service = match_repos
    _record(history)
    history.report_winner(
        1, "GF-1", captain_id=1, winner=MatchOutcome.TEAM_ONE,
        operation_id="resolve-GF-1-blue",
    )
    history.report_winner(
        1, "GF-1", captain_id=6, winner=MatchOutcome.TEAM_ONE,
        operation_id="resolve-GF-1-red",
    )
    _begin_launch(party_draft)
    await queue_service.create("lobby-1", 10)
    for user_id, role in zip(range(1, 11), ROLES * 2):
        await queue_service.join("lobby-1", user_id, (role,))
    interaction = _interaction(user_id=50)
    await bot._handle_match_continuity_action(interaction, "run_it_back")
    interaction.response.edit_message.assert_awaited_once()
    kwargs = interaction.response.edit_message.call_args.kwargs
    assert "Run It Back" in kwargs["content"]
