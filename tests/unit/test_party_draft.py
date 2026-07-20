from dataclasses import replace

import pytest

from utils.party import LobbyState, Participant, PartyLobby
from utils.party_draft import PartyDraftError, PartyDraftLaunchRepository, form_teams
from utils.draft import DraftManager


def _ready_lobby():
    players = tuple(
        Participant(
            user_id,
            primary_role=role,
            captain=user_id in {1, 2},
        )
        for user_id, role in enumerate(
            ("solo", "jungle", "mid", "support", "adc", "solo"), start=1
        )
    )
    return PartyLobby(
        "lobby-1",
        guild_id=10,
        organizer_id=99,
        capacity=6,
        state=LobbyState.FORMING,
        participants=players,
        mode="conquest",
        region="na east",
        format="pug",
        notes="fearless",
    )


def test_form_teams_spreads_volunteer_captains_deterministically():
    blue, red = form_teams(_ready_lobby())

    assert blue.captain_id == 1
    assert red.captain_id == 2
    assert set(blue.participant_ids + red.participant_ids) == set(range(1, 7))


def test_form_teams_requires_ready_lobby():
    with pytest.raises(PartyDraftError, match="ready check"):
        form_teams(replace(_ready_lobby(), state=LobbyState.OPEN))


def test_launch_is_idempotent_and_retains_party_context(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    ids = iter(("GF-0042", "GF-0043"))

    first, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: next(ids),
    )
    retry, retry_should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: next(ids),
    )

    assert should_start is True
    assert retry_should_start is False
    assert retry.match_id == first.match_id == "GF-0042"
    assert first.snapshot["lobby_id"] == "lobby-1"
    assert first.snapshot["rules"]["mode"] == "conquest"
    assert first.snapshot["participants"][0]["assigned_roles"] == ["solo"]


def test_failed_launch_can_be_retried_without_reusing_match_id(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    first, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_failed(first.lobby_id, "backend unavailable")

    retried, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-2",
        channel_id=500,
        match_id_factory=lambda: "GF-0043",
    )

    assert should_start is True
    assert retried.status == "pending"
    assert retried.match_id == "GF-0043"
    assert retried.error == ""


def test_successful_launch_blocks_new_interaction_duplicates(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    launch, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_active(launch.lobby_id)

    duplicate, should_start = repo.begin(
        _ready_lobby(),
        operation_id="interaction-2",
        channel_id=500,
        match_id_factory=lambda: "GF-9999",
    )

    assert should_start is False
    assert duplicate.status == "active"
    assert duplicate.match_id == "GF-0042"


def test_operation_id_reuse_by_another_lobby_is_rejected(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    other = replace(_ready_lobby(), lobby_id="lobby-2")

    with pytest.raises(PartyDraftError, match="another lobby"):
        repo.begin(
            other,
            operation_id="interaction-1",
            channel_id=501,
            match_id_factory=lambda: "GF-0043",
        )


def test_active_launch_cannot_be_downgraded_to_failed(tmp_path):
    repo = PartyDraftLaunchRepository(tmp_path / "party.db")
    launch, _ = repo.begin(
        _ready_lobby(),
        operation_id="interaction-1",
        channel_id=500,
        match_id_factory=lambda: "GF-0042",
    )
    repo.mark_active(launch.lobby_id)

    unchanged = repo.mark_failed(launch.lobby_id, "notification failed")

    assert unchanged.status == "active"
    assert unchanged.error == ""


def test_local_draft_export_retains_party_context():
    lobby = _ready_lobby()
    draft = DraftManager().start(
        500, 1, "Blue", 2, "Red", 10, "Guild", "draft",
        match_id="GF-0042",
        party_context={"lobby_id": lobby.lobby_id, "guild_id": lobby.guild_id},
    )

    assert draft.to_export_dict()["party"] == {
        "lobby_id": "lobby-1",
        "guild_id": 10,
    }
