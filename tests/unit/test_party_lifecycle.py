from datetime import datetime, timezone

import pytest

from utils.party import InvalidLobbyTransition, LobbyState, PartyLobby


def test_lifecycle_has_explicit_happy_path_and_terminal_states():
    lobby = PartyLobby("lobby-1", guild_id=1, organizer_id=2, capacity=10)
    for state in (
        LobbyState.FULL,
        LobbyState.READY_CHECK,
        LobbyState.FORMING,
        LobbyState.ACTIVE,
        LobbyState.COMPLETED,
    ):
        lobby = lobby.transitioned(state)
    assert lobby.is_terminal
    with pytest.raises(InvalidLobbyTransition):
        lobby.transitioned(LobbyState.OPEN)


@pytest.mark.parametrize("terminal", [LobbyState.CANCELLED, LobbyState.EXPIRED])
def test_open_lobby_can_end_without_becoming_active(terminal):
    lobby = PartyLobby("lobby-1", 1, 2, 10).transitioned(terminal)
    assert lobby.is_terminal


def test_same_state_transition_is_idempotent():
    lobby = PartyLobby("lobby-1", 1, 2, 10)
    assert lobby.transitioned(LobbyState.OPEN) is lobby


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        PartyLobby("lobby-1", 1, 2, 10, created_at=datetime(2026, 1, 1))

