from datetime import datetime, timedelta, timezone

import pytest

from utils.party_queue import InMemoryPartyQueueRepository, PartyQueueService
from utils.party_schedule import ScheduleRepository
from utils.party_store import SQLitePartyRepository
from utils.scrims import (
    ChallengeState,
    ScrimError,
    ScrimRepository,
    launch_scrim,
)


FUTURE = datetime.now(timezone.utc) + timedelta(days=7)


def _teams(repo):
    alpha = repo.save_team(
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 2, 3, 7, 8),
        substitutes=(9,), region="NA East", availability="Weeknights",
        operation_id="team-alpha",
    )
    beta = repo.save_team(
        guild_id=1, captain_id=4, name="Beta", roster=(4, 5, 6, 10, 11),
        substitutes=(12,), region="NA East", availability="Saturdays",
        operation_id="team-beta",
    )
    return alpha, beta


def test_team_rosters_are_guild_scoped_durable_and_validated(tmp_path):
    path = tmp_path / "party.db"
    repo = ScrimRepository(path)
    alpha, _ = _teams(repo)
    assert ScrimRepository(path).get_team(alpha.team_id) == alpha
    assert [team.name for team in repo.list_teams(1)] == ["Alpha", "Beta"]
    with pytest.raises(ScrimError, match="captain"):
        repo.save_team(
            guild_id=1, captain_id=99, name="Oops", roster=(7, 8),
            region="EU", availability="Any", operation_id="invalid",
        )
    with pytest.raises(ScrimError, match="both active"):
        repo.save_team(
            guild_id=1, captain_id=7, name="Overlap", roster=(7, 8),
            substitutes=(8,), region="EU", availability="Any", operation_id="overlap",
        )


def test_team_name_cannot_be_taken_over_without_manager_override(tmp_path):
    repo = ScrimRepository(tmp_path / "party.db")
    alpha, _ = _teams(repo)
    with pytest.raises(ScrimError, match="stored captain"):
        repo.save_team(
            guild_id=1, captain_id=99, name="Alpha",
            roster=(99, 20, 21, 22, 23), region="EU", availability="Any",
            operation_id="takeover",
        )
    assert repo.get_team(alpha.team_id).captain_id == 1
    transferred = repo.save_team(
        guild_id=1, captain_id=99, name="Alpha",
        roster=(99, 20, 21, 22, 23), region="EU", availability="Any",
        operation_id="managed-transfer", manager_override=True,
    )
    assert transferred.captain_id == 99


def test_operation_ids_reject_changed_team_and_challenge_payloads(tmp_path):
    repo = ScrimRepository(tmp_path / "party.db")
    alpha, beta = _teams(repo)
    with pytest.raises(ScrimError, match="different team data"):
        repo.save_team(
            guild_id=1, captain_id=1, name="Alpha", roster=alpha.roster,
            substitutes=alpha.substitutes, region=alpha.region,
            availability="Changed", operation_id="team-alpha",
        )
    challenge = repo.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="UTC",
        operation_id="stable-challenge",
    )
    with pytest.raises(ScrimError, match="different mutation data"):
        repo.challenge(
            challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
            actor_id=1, starts_at=FUTURE + timedelta(hours=1), timezone_name="UTC",
            operation_id="stable-challenge",
        )
    repo.respond(
        challenge.challenge_id, actor_id=4, response="accept",
        operation_id="stable-response",
    )
    with pytest.raises(ScrimError, match="different mutation data"):
        repo.respond(
            challenge.challenge_id, actor_id=4, response="reject",
            operation_id="stable-response",
        )


def test_challenge_accept_reject_and_counterproposal_permissions(tmp_path):
    repo = ScrimRepository(tmp_path / "party.db")
    alpha, beta = _teams(repo)
    challenge = repo.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="America/New_York",
        operation_id="challenge-1",
    )
    with pytest.raises(ScrimError, match="challenged captain"):
        repo.respond(
            challenge.challenge_id, actor_id=1, response="accept",
            operation_id="bad-response",
        )
    counter = repo.respond(
        challenge.challenge_id, actor_id=4, response="propose",
        proposed_at=FUTURE + timedelta(hours=1), operation_id="counter",
    )
    assert counter.state is ChallengeState.PROPOSED
    assert counter.challenger_team_id == beta.team_id
    accepted = repo.respond(
        challenge.challenge_id, actor_id=1, response="accept",
        operation_id="accept-counter",
    )
    assert accepted.state is ChallengeState.ACCEPTED

    other = repo.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="UTC",
        operation_id="challenge-2",
    )
    rejected = repo.respond(
        other.challenge_id, actor_id=4, response="reject",
        operation_id="reject",
    )
    assert rejected.state is ChallengeState.REJECTED


@pytest.mark.asyncio
async def test_checkin_lock_and_launch_reuse_canonical_pipeline(tmp_path):
    path = tmp_path / "party.db"
    scrims = ScrimRepository(path)
    alpha, beta = _teams(scrims)
    challenge = scrims.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="America/New_York",
        operation_id="challenge",
    )
    challenge = scrims.respond(
        challenge.challenge_id, actor_id=4, response="accept", operation_id="accept"
    )
    challenge = scrims.check_in(
        challenge.challenge_id, actor_id=1, operation_id="check-alpha"
    )
    assert challenge.state is ChallengeState.ACCEPTED
    challenge = scrims.check_in(
        challenge.challenge_id, actor_id=4, operation_id="check-beta"
    )
    assert challenge.state is ChallengeState.CHECKED_IN
    with pytest.raises(ScrimError, match="organizer"):
        scrims.lock_rosters(
            challenge.challenge_id, actor_id=4, operation_id="bad-lock"
        )
    challenge = scrims.lock_rosters(
        challenge.challenge_id, actor_id=1, operation_id="lock"
    )
    assert challenge.locked_rosters == {
        alpha.team_id: (1, 2, 3, 7, 8), beta.team_id: (4, 5, 6, 10, 11)
    }

    schedules = ScheduleRepository(path)
    parties = SQLitePartyRepository(path)
    queues = PartyQueueService(InMemoryPartyQueueRepository())
    lobby = await launch_scrim(
        challenge, scrims, schedules, parties, queues, operation_id="launch"
    )
    launched = scrims.get_challenge(challenge.challenge_id)
    assert launched.state is ChallengeState.LAUNCHED
    assert launched.lobby_id == lobby.lobby_id
    assert {member.user_id for member in lobby.participants} == {
        1, 2, 3, 4, 5, 6, 7, 8, 10, 11
    }
    assert schedules.get(launched.event_id).lobby_id == lobby.lobby_id
    blue, red = scrims.fixed_draft_teams(launched)
    assert blue.participant_ids == alpha.roster
    assert red.participant_ids == beta.roster
    assert blue.captain_id == alpha.captain_id
    assert red.captain_id == beta.captain_id


@pytest.mark.asyncio
async def test_launch_requires_five_unique_active_players_per_team(tmp_path):
    path = tmp_path / "party.db"
    scrims = ScrimRepository(path)
    alpha, beta = _teams(scrims)
    scrims.save_team(
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 2),
        substitutes=(3, 7, 8), region="NA East", availability="Weeknights",
        operation_id="shorten-alpha",
    )
    challenge = scrims.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="UTC", operation_id="short-c",
    )
    scrims.respond(
        challenge.challenge_id, actor_id=4, response="accept", operation_id="short-a"
    )
    scrims.check_in(challenge.challenge_id, actor_id=1, operation_id="short-i1")
    scrims.check_in(challenge.challenge_id, actor_id=4, operation_id="short-i2")
    locked = scrims.lock_rosters(
        challenge.challenge_id, actor_id=1, operation_id="short-lock"
    )
    with pytest.raises(ScrimError, match="exactly five"):
        await launch_scrim(
            locked, scrims, ScheduleRepository(path), SQLitePartyRepository(path),
            PartyQueueService(InMemoryPartyQueueRepository()), operation_id="short-launch",
        )


def test_roster_lock_is_a_snapshot_and_organizer_override_is_explicit(tmp_path):
    repo = ScrimRepository(tmp_path / "party.db")
    alpha, beta = _teams(repo)
    challenge = repo.challenge(
        challenger_team_id=alpha.team_id, recipient_team_id=beta.team_id,
        actor_id=1, starts_at=FUTURE, timezone_name="UTC", operation_id="c",
    )
    repo.respond(challenge.challenge_id, actor_id=4, response="accept", operation_id="a")
    repo.check_in(challenge.challenge_id, actor_id=1, operation_id="i1")
    repo.check_in(challenge.challenge_id, actor_id=4, operation_id="i2")
    locked = repo.lock_rosters(
        challenge.challenge_id, actor_id=99, operation_id="admin-lock",
        organizer_override=True,
    )
    repo.save_team(
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 3),
        substitutes=(2,), region="NA East", availability="Weeknights",
        operation_id="team-alpha-edit",
    )
    assert locked.locked_rosters[alpha.team_id] == (1, 2, 3, 7, 8)
