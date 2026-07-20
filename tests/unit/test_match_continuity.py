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
    SeriesScore,
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


@pytest.mark.asyncio
async def test_retry_replays_failed_queue_projection(tmp_path, monkeypatch):
    continuity, queue = await service(tmp_path)
    original = queue.reset_roster
    calls = 0

    async def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("queue unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(queue, "reset_roster", flaky)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await continuity.continue_match(
            completed_match(), lobby_id="lobby-1",
            action="run_it_back", operation_id="queue-fail",
        )
    reserved = continuity.repository.get(77, "GF-1")
    assert not reserved.queue_projected
    retried = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="run_it_back", operation_id="queue-retry",
    )
    assert retried.queue_projected
    assert retried.rooms_projected
    assert retried.draft_projected
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_replays_failed_room_projection_without_repeating_queue(tmp_path):
    calls = 0

    async def flaky_rooms(_lobby_id, _participant_ids):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("discord unavailable")
        return True

    continuity, queue = await service(tmp_path, room_reconciler=flaky_rooms)
    queue.reset_roster = AsyncMock(wraps=queue.reset_roster)
    with pytest.raises(RuntimeError, match="discord unavailable"):
        await continuity.continue_match(
            completed_match(), lobby_id="lobby-1",
            action="run_it_back", operation_id="room-fail",
        )
    reserved = continuity.repository.get(77, "GF-1")
    assert reserved.queue_projected and not reserved.rooms_projected
    retried = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="run_it_back", operation_id="room-retry",
    )
    assert retried.reused_rooms and retried.draft_projected
    assert queue.reset_roster.await_count == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_replays_failed_history_draft_projection_only(tmp_path):
    draft = AsyncMock(side_effect=[RuntimeError("history unavailable"), None])
    rooms = AsyncMock(return_value=True)
    continuity, queue = await service(
        tmp_path, room_reconciler=rooms, draft_starter=draft
    )
    queue.reset_roster = AsyncMock(wraps=queue.reset_roster)
    with pytest.raises(RuntimeError, match="history unavailable"):
        await continuity.continue_match(
            completed_match(), lobby_id="lobby-1",
            action="run_it_back", operation_id="draft-fail",
        )
    reserved = continuity.repository.get(77, "GF-1")
    assert reserved.queue_projected and reserved.rooms_projected
    assert not reserved.draft_projected
    retried = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="run_it_back", operation_id="draft-retry",
    )
    assert retried.draft_projected
    assert queue.reset_roster.await_count == 1
    rooms.assert_awaited_once()
    assert draft.await_count == 2


@pytest.mark.asyncio
async def test_continue_series_requires_series_context(tmp_path):
    continuity, _ = await service(tmp_path)
    with pytest.raises(ContinuityError, match="existing series score"):
        await continuity.continue_match(
            completed_match(), lobby_id="lobby-1",
            action="continue_series", operation_id="not-series",
        )

    original = completed_match()
    series = MatchRecord(
        original.match_id, original.guild_id, original.organizer_id,
        original.team_one, original.team_two,
        outcome=original.outcome, series_score=SeriesScore(2, 1),
    )
    result = await continuity.continue_match(
        series, lobby_id="lobby-1",
        action="continue_series", operation_id="series",
    )
    assert result.status is ContinuityStatus.READY


def test_series_control_can_be_omitted_from_specific_result_card():
    async def handler(_interaction, _action):
        pass

    specific = MatchContinuityView(handler, allow_continue_series=False)
    assert "Continue Series" not in [item.label for item in specific.children]
    # Startup registers the full persistent routing table.
    persistent_router = MatchContinuityView(handler)
    assert "Continue Series" in [item.label for item in persistent_router.children]


@pytest.mark.asyncio
async def test_invite_substitutes_reaches_ready_when_roster_is_complete(tmp_path):
    continuity, _ = await service(tmp_path)
    result = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="invite_substitutes", operation_id="invite-complete",
    )
    assert result.status is ContinuityStatus.READY
    assert result.next_match_id


@pytest.mark.asyncio
async def test_invite_substitutes_resumes_after_more_players_join(tmp_path):
    continuity, queue = await service(tmp_path)
    await queue.join("lobby-1", 11, ("solo",))
    waiting = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="invite_substitutes", operation_id="invite-wait",
        departing_ids=(1, 2),
    )
    assert waiting.status is ContinuityStatus.AWAITING_SUBSTITUTES
    assert not waiting.queue_projected

    await queue.join("lobby-1", 12, ("jungle",))
    ready = await continuity.continue_match(
        completed_match(), lobby_id="lobby-1",
        action="invite_substitutes", operation_id="invite-retry",
    )
    assert ready.status is ContinuityStatus.READY
    assert ready.next_match_id
    assert ready.queue_projected and ready.rooms_projected and ready.draft_projected


@pytest.mark.asyncio
async def test_run_it_back_preserves_captains_not_tuple_position(tmp_path):
    continuity, _ = await service(tmp_path)
    original = completed_match()
    record = MatchRecord(
        original.match_id, original.guild_id, original.organizer_id,
        MatchTeam("Blue", 5, original.team_one.players),
        MatchTeam("Red", 10, original.team_two.players),
        outcome=original.outcome,
    )
    result = await continuity.continue_match(
        record, lobby_id="lobby-1",
        action="run_it_back", operation_id="captains",
    )
    assert result.team_one.captain_id == 5
    assert result.team_two.captain_id == 10
