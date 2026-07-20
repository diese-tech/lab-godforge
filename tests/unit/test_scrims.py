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
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 2),
        substitutes=(3,), region="NA East", availability="Weeknights",
        operation_id="team-alpha",
    )
    beta = repo.save_team(
        guild_id=1, captain_id=4, name="Beta", roster=(4, 5),
        substitutes=(6,), region="NA East", availability="Saturdays",
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
        alpha.team_id: (1, 2), beta.team_id: (4, 5)
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
    assert {member.user_id for member in lobby.participants} == {1, 2, 4, 5}
    assert schedules.get(launched.event_id).lobby_id == lobby.lobby_id


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
    assert locked.locked_rosters[alpha.team_id] == (1, 2)
