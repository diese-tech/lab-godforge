from unittest.mock import AsyncMock, Mock

import pytest

from utils.lobby_views import (
    CREATE_MODAL_CUSTOM_ID,
    JOIN_MODAL_CUSTOM_ID,
    LOBBY_CARD_ACTIONS,
    LOBBY_CARD_CUSTOM_ID_PREFIX,
    READY_CHECK_ACTIONS,
    READY_CHECK_CUSTOM_ID_PREFIX,
    CreateLobbyModal,
    JoinPreferencesModal,
    LobbyCardView,
    ReadyCheckView,
)


def _interaction(response_done=False):
    interaction = Mock()
    interaction.response.is_done.return_value = response_done
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _set(input_item, value):
    input_item._value = value


def test_create_modal_is_persistent_and_respects_five_component_limit():
    modal = CreateLobbyModal(AsyncMock())

    assert modal.timeout is None
    assert modal.custom_id == CREATE_MODAL_CUSTOM_ID
    assert len(modal.children) == 5
    assert [item.custom_id for item in modal.children] == [
        "mode",
        "region",
        "format",
        "party_requirements",
        "optional_details",
    ]


@pytest.mark.asyncio
async def test_create_modal_delegates_seven_explicit_fields():
    handler = AsyncMock()
    modal = CreateLobbyModal(handler)
    _set(modal.mode, "Conquest")
    _set(modal.region, "NA East")
    _set(modal.format, "PUG")
    _set(modal.party_requirements, "10 / yes")
    _set(modal.optional_details, "Skill: mixed | Notes: chill games")
    interaction = _interaction()

    await modal.on_submit(interaction)

    handler.assert_awaited_once_with(
        interaction,
        {
            "mode": "Conquest",
            "region": "NA East",
            "format": "PUG",
            "party_size": 10,
            "voice_required": True,
            "skill_band": "mixed",
            "notes": "chill games",
        },
    )


@pytest.mark.asyncio
async def test_create_modal_validation_is_safe_and_actionable():
    handler = AsyncMock()
    modal = CreateLobbyModal(handler)
    _set(modal.mode, "Conquest")
    _set(modal.region, "EU")
    _set(modal.format, "PUG")
    _set(modal.party_requirements, "many")
    interaction = _interaction()

    await modal.on_submit(interaction)

    handler.assert_not_awaited()
    message = interaction.response.send_message.await_args.args[0]
    assert "10 / yes" in message
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}


def test_join_modal_has_stable_id_and_four_fields():
    modal = JoinPreferencesModal(AsyncMock())

    assert modal.timeout is None
    assert modal.custom_id == JOIN_MODAL_CUSTOM_ID
    assert [item.custom_id for item in modal.children] == [
        "primary_role",
        "secondary_role",
        "fill",
        "captain",
    ]


@pytest.mark.asyncio
async def test_join_preferences_are_normalized_and_delegated():
    handler = AsyncMock()
    modal = JoinPreferencesModal(handler)
    _set(modal.primary_role, "Jungle")
    _set(modal.secondary_role, "Mid")
    _set(modal.fill, "yes")
    _set(modal.captain, "no")
    interaction = _interaction()

    await modal.on_submit(interaction)

    handler.assert_awaited_once_with(
        interaction,
        {
            "primary_role": "jungle",
            "secondary_role": "mid",
            "fill": True,
            "captain": False,
        },
    )


def test_lobby_card_is_persistent_with_stable_action_ids():
    view = LobbyCardView(AsyncMock())

    assert view.timeout is None
    assert len(view.children) == 6
    assert [item.custom_id for item in view.children] == [
        f"{LOBBY_CARD_CUSTOM_ID_PREFIX}:{action}:v1"
        for action, _label, _style in LOBBY_CARD_ACTIONS
    ]
    assert [item.label for item in view.children] == [
        "Join",
        "Leave",
        "Edit",
        "Cancel",
        "Share",
        "Ready Check",
    ]
    assert [item.row for item in view.children] == [0, 0, 0, 0, 0, 1]


@pytest.mark.asyncio
async def test_lobby_card_delegates_action_and_hides_handler_errors():
    handler = AsyncMock(side_effect=RuntimeError("private failure"))
    view = LobbyCardView(handler)
    interaction = _interaction(response_done=True)

    await view.children[4].callback(interaction)

    handler.assert_awaited_once_with(interaction, "share")
    message = interaction.followup.send.await_args.args[0]
    assert "private failure" not in message
    assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_lobby_ready_check_action_is_delegated():
    handler = AsyncMock()
    view = LobbyCardView(handler)
    interaction = _interaction()

    await view.children[5].callback(interaction)

    handler.assert_awaited_once_with(interaction, "ready_check")


def test_ready_check_view_is_persistent_with_stable_actions():
    view = ReadyCheckView(AsyncMock())

    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        f"{READY_CHECK_CUSTOM_ID_PREFIX}:{action}:v1"
        for action, _label, _style in READY_CHECK_ACTIONS
    ]
    assert [item.label for item in view.children] == [
        "Ready",
        "Need 5 Minutes",
        "Drop",
    ]
    assert all(item.row == 0 for item in view.children)


@pytest.mark.asyncio
async def test_ready_check_delegates_and_safely_reports_failures():
    handler = AsyncMock(side_effect=RuntimeError("private detail"))
    view = ReadyCheckView(handler)
    interaction = _interaction()

    await view.children[1].callback(interaction)

    handler.assert_awaited_once_with(interaction, "need_five")
    message = interaction.response.send_message.await_args.args[0]
    assert "private detail" not in message
    assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}
