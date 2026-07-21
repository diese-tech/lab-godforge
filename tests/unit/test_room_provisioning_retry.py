from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
from utils.party import LobbyState, Participant
from utils.party_queue import QueueMember, ReadyStatus
from utils.party_store import SQLitePartyRepository


@pytest.mark.asyncio
async def test_failed_room_provisioning_keeps_ready_check_retryable(
    tmp_path, monkeypatch
):
    repository = SQLitePartyRepository(tmp_path / "party.db")
    repository.create(
        guild_id=1, organizer_id=10, capacity=2, lobby_id="lobby",
        operation_id="create",
    )
    repository.save_participant(
        1, "lobby", Participant(10), operation_id="join-owner"
    )
    repository.save_participant(
        1, "lobby", Participant(11), operation_id="join-player"
    )
    repository.transition(1, "lobby", LobbyState.FULL, operation_id="full")
    repository.transition(
        1, "lobby", LobbyState.READY_CHECK, operation_id="ready-check"
    )

    queue = SimpleNamespace(
        active=[QueueMember(10), QueueMember(11)],
        ready={10: ReadyStatus.READY, 11: ReadyStatus.READY},
        ready_deadline=None,
    )
    queue_service = SimpleNamespace(
        respond=AsyncMock(return_value=(queue, None))
    )
    room_service = SimpleNamespace(
        provision=AsyncMock(side_effect=RuntimeError("missing permission"))
    )
    monkeypatch.setattr(bot, "party_repository", repository)
    monkeypatch.setattr(bot, "party_queue_service", queue_service)
    monkeypatch.setattr(
        bot, "_match_room_service_for_guild", lambda guild: room_service
    )
    monkeypatch.setattr(bot._party_lobby_deps, "party_repository", repository)
    monkeypatch.setattr(bot._party_lobby_deps, "party_queue_service", queue_service)
    monkeypatch.setattr(
        bot._party_lobby_deps, "match_room_service_for_guild", lambda guild: room_service
    )

    response = SimpleNamespace(edit_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=1,
        guild=SimpleNamespace(get_channel=lambda channel_id: None),
        user=SimpleNamespace(id=10),
        message=SimpleNamespace(
            embeds=[
                SimpleNamespace(
                    footer=SimpleNamespace(text="lobby_id=lobby")
                )
            ]
        ),
        response=response,
    )

    await bot._handle_ready_check_action(interaction, "ready")

    assert repository.get(1, "lobby").state is LobbyState.READY_CHECK
    assert response.edit_message.await_args.kwargs["view"] is not None
    assert "press Ready again" in response.edit_message.await_args.kwargs["content"]
