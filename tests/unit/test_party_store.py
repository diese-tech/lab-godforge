from datetime import datetime, timedelta, timezone

import pytest

from utils.party import DiscordDelivery, LobbyState, Participant
from utils.party_store import OperationConflictError, SQLitePartyRepository


def repository(tmp_path):
    return SQLitePartyRepository(tmp_path / "parties.sqlite3")


def test_persists_complete_lobby_and_recovers_after_restart(tmp_path):
    repo = repository(tmp_path)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    lobby = repo.create(
        guild_id=42, organizer_id=7, capacity=5, expires_at=expires,
        lobby_id="stable-id", operation_id="discord:create:1",
        delivery=DiscordDelivery(panel_channel_id=11, panel_message_id=12),
    )
    repo.save_participant(
        42, lobby.lobby_id,
        Participant(99, ("Jungle", "Mid"), ready=True),
        operation_id="discord:join:1",
    )
    repo.transition(42, lobby.lobby_id, LobbyState.FULL, operation_id="discord:full:1")

    recovered = SQLitePartyRepository(repo.path).recover_active(42)

    assert len(recovered) == 1
    restored = recovered[0].lobby
    assert restored.lobby_id == "stable-id"
    assert restored.delivery.panel_message_id == 12
    assert restored.participant(99).preferences == ("jungle", "mid")
    assert restored.participant(99).ready is True
    assert restored.expires_at == expires


def test_guild_scope_prevents_cross_guild_access(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="same",
        operation_id="create",
    )
    assert repo.get(2, "same") is None
    assert repo.recover_active(2) == []


def test_retried_transition_is_idempotent_and_audited_once(tmp_path):
    repo = repository(tmp_path)
    lobby = repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )
    first = repo.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="interaction-1")
    retried = repo.transition(1, lobby.lobby_id, LobbyState.FULL, operation_id="interaction-1")

    assert retried.version == first.version
    assert [event.event_type for event in repo.audit_events(1, "lobby")] == [
        "created",
        "state_transition",
    ]


def test_retried_create_without_caller_supplied_id_is_idempotent(tmp_path):
    repo = repository(tmp_path)
    first = repo.create(
        guild_id=1, organizer_id=2, capacity=5, operation_id="interaction-create",
    )
    retried = repo.create(
        guild_id=1, organizer_id=2, capacity=5, operation_id="interaction-create",
    )
    assert retried.lobby_id == first.lobby_id
    assert len(repo.audit_events(1, first.lobby_id)) == 1


def test_operation_id_cannot_be_reused_for_another_command(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="lobby",
        operation_id="create",
    )
    repo.transition(1, "lobby", LobbyState.FULL, operation_id="interaction")
    with pytest.raises(OperationConflictError):
        repo.transition(1, "lobby", LobbyState.OPEN, operation_id="interaction")


def test_delivery_references_can_be_reconciled_without_changing_identity(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="domain-id",
        operation_id="create",
    )
    changed = repo.set_delivery(
        1, "domain-id",
        DiscordDelivery(
            panel_channel_id=20, panel_message_id=21, voice_channel_id=22,
            team_channel_ids=(23, 24),
        ),
        operation_id="reconcile",
    )
    assert changed.lobby_id == "domain-id"
    assert changed.delivery.team_channel_ids == (23, 24)


def test_terminal_lobbies_are_not_returned_for_recovery(tmp_path):
    repo = repository(tmp_path)
    for index, terminal in enumerate((LobbyState.CANCELLED, LobbyState.EXPIRED)):
        lobby_id = f"lobby-{index}"
        repo.create(
            guild_id=1, organizer_id=2, capacity=5, lobby_id=lobby_id,
            operation_id=f"create-{index}",
        )
        repo.transition(
            1, lobby_id, terminal, operation_id=f"terminal-{index}",
            reason="test cleanup",
        )
    assert repo.recover_active(1) == []
    assert repo.audit_events(1, "lobby-0")[-1].metadata == {"reason": "test cleanup"}


def test_full_lobby_rejects_additional_participant(tmp_path):
    repo = repository(tmp_path)
    repo.create(
        guild_id=1, organizer_id=2, capacity=2, lobby_id="lobby",
        operation_id="create",
    )
    repo.save_participant(1, "lobby", Participant(2), operation_id="join-1")
    repo.save_participant(1, "lobby", Participant(3), operation_id="join-2")
    with pytest.raises(ValueError, match="full"):
        repo.save_participant(1, "lobby", Participant(4), operation_id="join-3")
