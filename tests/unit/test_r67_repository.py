"""Durable state coverage for the r67 SQLite repository (Issue #47, Gate 4/5)."""

from datetime import timedelta

import pytest

from utils.party import utc_now
from utils.r67.repository import RoleGrant, SQLiteR67Repository


@pytest.fixture()
def repo(tmp_path):
    return SQLiteR67Repository(tmp_path / "r67.db")


def test_unknown_guild_defaults_to_disabled_no_cooldowns(repo):
    state = repo.get_guild_state(4242)
    assert state.reactions_enabled is False
    assert state.passive_cooldown_until is None
    assert state.survivor_cooldown_until is None


def test_enable_and_disable_persist(repo):
    repo.set_reactions_enabled(1, True)
    assert repo.get_guild_state(1).reactions_enabled is True
    repo.set_reactions_enabled(1, False)
    assert repo.get_guild_state(1).reactions_enabled is False


def test_toggle_resets_passive_cooldown_but_keeps_survivor(repo):
    now = utc_now()
    repo.set_reactions_enabled(1, True)
    repo.set_passive_cooldown(1, now + timedelta(minutes=5))
    repo.set_survivor_cooldown(1, now + timedelta(hours=67))
    # Disabling clears the passive cooldown, preserves the Survivor cooldown.
    repo.set_reactions_enabled(1, False)
    state = repo.get_guild_state(1)
    assert state.passive_cooldown_until is None
    assert state.survivor_cooldown_until is not None


def test_cooldown_roundtrip_is_timezone_aware(repo):
    until = utc_now() + timedelta(minutes=5)
    repo.set_passive_cooldown(9, until)
    stored = repo.get_guild_state(9).passive_cooldown_until
    assert stored is not None
    assert stored.tzinfo is not None
    assert abs((stored - until).total_seconds()) < 1


def test_state_survives_new_repository_instance(repo, tmp_path):
    repo.set_reactions_enabled(5, True)
    reopened = SQLiteR67Repository(tmp_path / "r67.db")
    assert reopened.get_guild_state(5).reactions_enabled is True


def test_role_grant_lifecycle(repo):
    now = utc_now()
    grant = RoleGrant(
        guild_id=1,
        user_id=100,
        role_id=999,
        expires_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=68),
    )
    repo.add_role_grants([grant])
    assert len(repo.all_role_grants()) == 1

    due = repo.due_role_grants(now)
    assert len(due) == 1

    repo.remove_role_grant(1, 100, 999)
    assert repo.all_role_grants() == []


def test_future_grant_is_not_due(repo):
    now = utc_now()
    repo.add_role_grants(
        [
            RoleGrant(
                guild_id=1,
                user_id=100,
                role_id=999,
                expires_at=now + timedelta(minutes=67),
                created_at=now,
            )
        ]
    )
    assert repo.due_role_grants(now) == []
    assert len(repo.all_role_grants()) == 1


def test_removal_failure_is_recorded(repo):
    now = utc_now()
    repo.add_role_grants(
        [RoleGrant(guild_id=1, user_id=2, role_id=3, expires_at=now, created_at=now)]
    )
    repo.record_removal_failure(1, 2, 3, "missing permissions")
    grant = repo.all_role_grants()[0]
    assert grant.removal_attempts == 1
    assert grant.last_error == "missing permissions"
