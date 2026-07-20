from unittest.mock import AsyncMock

import pytest

from utils.match_continuity import (
    ContinuityAction,
    ContinuityError,
    ContinuityStatus,
    MatchContinuityRepository,
    MatchContinuityService,
)
from utils.match_history import (
    MatchOutcome,
    MatchPlayer,
    MatchRecord,
    MatchTeam,
)
from utils.party_queue import (
    InMemoryPartyQueueRepository,
    PartyQueueService,
    QueueStatus,
)
from utils.lobby_views import MatchContinuityView


ROLES = ("solo", "jungle", "mid", "support", "adc")


def completed_match():
    blue = MatchTeam(
        "Blue", 1, tuple(MatchPlayer(index + 1, role) for index, role in enumerate(ROLES))
    )
    red = MatchTeam(
        "Red", 6, tuple(MatchPlayer(index + 6, role) for index, role in enumerate(ROLES))
    )
    return MatchRecord(
        "GF-1", 77, 1, blue, red, outcome=MatchOutcome.TEAM_ONE
    )


async def service(tmp_path, *, room_reconciler=None, draft_starter=None):
    queue = PartyQueueService(InMemoryPartyQueueRepository())
    await queue.create("lobby-1", 10)
    for user_id, role in zip(range(1, 11), ROLES * 2):
        await queue.join("lobby-1", user_id, (role,))
    return MatchContinuityService(
        MatchContinuityRepository(tmp_path / "party.db"),
        queue,
        room_reconciler=room_reconciler,
        draft_starter=draft_starter,
    ), queue


@pytest.mark.asyncio
async def test_run_it_back_is_idempotent_and_reconciles_rooms_and_draft(tmp_path):
    rooms = AsyncMock(return_value=True)
    draft = AsyncMock()
    continuity, _ = await service(
        tmp_path, room_reconciler=rooms, draft_starter=draft
    )
    first = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action=ContinuityAction.RUN_IT_BACK, operation_id="click-1",
    )
    retry = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action=ContinuityAction.RUN_IT_BACK, operation_id="click-2",
    )
    assert first == retry
    assert first.status is ContinuityStatus.READY
    assert first.next_match_id
    assert first.reused_rooms is True
    assert first.changes == ()
    rooms.assert_awaited_once()
    draft.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_one_next_state_can_be_selected(tmp_path):
    continuity, _ = await service(tmp_path)
    await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="run_it_back", operation_id="click-1",
    )
    with pytest.raises(ContinuityError, match="run it back already selected"):
        await continuity.continue_match(
            completed_match(), lobby_id="lobby-1",
            action="shuffle_teams", operation_id="click-2",
        )


@pytest.mark.asyncio
async def test_shuffle_reports_team_or_role_assignment_changes(tmp_path):
    continuity, _ = await service(tmp_path)
    result = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="shuffle_teams", operation_id="shuffle",
    )
    assert result.changes
    assert {p.user_id for p in result.team_one.players + result.team_two.players} == set(range(1, 11))
    assert {p.role for p in result.team_one.players} == set(ROLES)
    assert {p.role for p in result.team_two.players} == set(ROLES)


@pytest.mark.asyncio
async def test_departure_uses_role_aware_waitlist_substitute(tmp_path):
    continuity, queue = await service(tmp_path)
    await queue.join("lobby-1", 11, ("support",))
    result = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="run_it_back", operation_id="sub",
        departing_ids=(4,),
    )
    assert result.promoted_ids == (11,)
    assert 11 in {p.user_id for p in result.team_one.players + result.team_two.players}
    assert any(change.user_id == 4 and change.next_team is None for change in result.changes)
    assert any(change.user_id == 11 and change.previous_team is None for change in result.changes)
    updated = await queue.get("lobby-1")
    assert 4 not in {member.user_id for member in updated.active + updated.waitlist}
    assert 11 in {member.user_id for member in updated.active}


@pytest.mark.asyncio
async def test_return_to_queue_does_not_create_match_or_draft(tmp_path):
    draft = AsyncMock()
    continuity, _ = await service(tmp_path, draft_starter=draft)
    result = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="return_to_queue", operation_id="queue",
    )
    assert result.status is ContinuityStatus.QUEUED
    assert result.next_match_id is None
    draft.assert_not_awaited()
    queue = await continuity.queue_service.get("lobby-1")
    assert queue.status is QueueStatus.OPEN
    assert queue.ready == {}


@pytest.mark.asyncio
async def test_unresolved_match_cannot_continue(tmp_path):
    continuity, _ = await service(tmp_path)
    pending = completed_match()
    pending = MatchRecord(
        pending.match_id, pending.guild_id, pending.organizer_id,
        pending.team_one, pending.team_two,
    )
    with pytest.raises(ContinuityError, match="confirmed winner"):
        await continuity.continue_match(
            pending, lobby_id="lobby-1",
            action="run_it_back", operation_id="bad",
        )


def test_continuity_controls_are_persistent_and_stably_addressed():
    async def handler(_interaction, _action):
        pass

    view = MatchContinuityView(handler)
    assert view.timeout is None
    assert [item.label for item in view.children] == [
        "Run It Back", "Shuffle Teams", "Return to Queue",
        "Invite Substitutes", "Continue Series",
    ]
    assert all(
        item.custom_id.startswith("godforge:match:continuity:")
        for item in view.children
    )
