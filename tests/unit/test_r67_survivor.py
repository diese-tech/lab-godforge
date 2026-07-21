"""67 Survivor tracker, role lifecycle, and orchestration (Issue #47, Gate 3/5)."""

import random
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.party import utc_now
from utils.r67 import roles as roles_adapter
from utils.r67 import service as service_mod
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service
from utils.r67.tracker import SurvivorTracker


# ---------------------------------------------------------------------------
# Tracker (pure)
# ---------------------------------------------------------------------------

def test_sixth_unique_user_triggers():
    tracker = SurvivorTracker()
    now = utc_now()
    for uid in range(1, 6):
        assert tracker.record(1, 10, uid, now) is None
    winners = tracker.record(1, 10, 6, now)
    assert winners == [1, 2, 3, 4, 5, 6]


def test_duplicate_user_does_not_add_participant():
    tracker = SurvivorTracker()
    now = utc_now()
    for uid in [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]:
        assert tracker.record(1, 10, uid, now) is None
    # Only five unique users so far; a sixth unique triggers.
    assert tracker.record(1, 10, 6, now) == [1, 2, 3, 4, 5, 6]


def test_sixth_message_at_6999ms_succeeds():
    tracker = SurvivorTracker()
    start = utc_now()
    for uid in range(1, 6):
        tracker.record(1, 10, uid, start)
    sixth = start + timedelta(seconds=6, milliseconds=999)
    assert tracker.record(1, 10, 6, sixth) == [1, 2, 3, 4, 5, 6]


def test_sixth_message_after_seven_seconds_fails():
    tracker = SurvivorTracker()
    start = utc_now()
    for uid in range(1, 6):
        tracker.record(1, 10, uid, start)
    late = start + timedelta(seconds=7, milliseconds=1)
    # The first five have aged out; only the sixth remains -> no trigger.
    assert tracker.record(1, 10, 6, late) is None


def test_five_in_one_channel_and_one_in_another_fails():
    tracker = SurvivorTracker()
    now = utc_now()
    for uid in range(1, 6):
        tracker.record(1, 10, uid, now)
    assert tracker.record(1, 20, 6, now) is None


def test_seventh_near_simultaneous_user_does_not_start_second_event():
    tracker = SurvivorTracker()
    now = utc_now()
    for uid in range(1, 6):
        tracker.record(1, 10, uid, now)
    assert tracker.record(1, 10, 6, now) == [1, 2, 3, 4, 5, 6]
    # Channel window was cleared; a seventh message starts fresh, no trigger.
    assert tracker.record(1, 10, 7, now) is None


def test_clear_guild_resets_windows():
    tracker = SurvivorTracker()
    now = utc_now()
    for uid in range(1, 6):
        tracker.record(1, 10, uid, now)
    tracker.clear_guild(1)
    assert tracker.record(1, 10, 6, now) is None


# ---------------------------------------------------------------------------
# Service orchestration
# ---------------------------------------------------------------------------

@pytest.fixture()
def service(tmp_path):
    repo = SQLiteR67Repository(tmp_path / "r67.db")
    return R67Service(repo, rng=random.Random(0))


def _drive_event(service, guild_id=1, channel_id=10, base=None):
    now = base or utc_now()
    winners = None
    for uid in range(1, 7):
        winners = service.process_passive(
            guild_id, channel_id, uid, "67", now=now
        ).survivor_winners
    return winners, now


def test_survivor_counts_even_during_passive_cooldown(service):
    service.enable_reactions(1)
    now = utc_now()
    # Force a passive success to set the 5-minute cooldown (user 1).
    service.rng = _AlwaysPass()
    service.process_passive(1, 10, 1, "67", now=now)
    # Remaining five users within 7s still complete the Survivor event.
    winners = None
    for uid in range(2, 7):
        winners = service.process_passive(1, 10, uid, "67", now=now).survivor_winners
    assert winners == [1, 2, 3, 4, 5, 6]


def test_survivor_requires_optin(service):
    now = utc_now()
    winners = None
    for uid in range(1, 7):
        winners = service.process_passive(1, 10, uid, "67", now=now).survivor_winners
    assert winners is None


def test_triggering_sets_67_hour_cooldown_and_blocks_retrigger(service):
    service.enable_reactions(1)
    winners, now = _drive_event(service)
    assert winners == [1, 2, 3, 4, 5, 6]
    state = service.repository.get_guild_state(1)
    assert state.survivor_cooldown_until is not None
    assert abs(
        (state.survivor_cooldown_until - (now + timedelta(hours=67))).total_seconds()
    ) < 1
    # Within cooldown, another six unique users do not retrigger.
    within = now + timedelta(hours=1)
    winners2 = None
    for uid in range(10, 16):
        winners2 = service.process_passive(1, 10, uid, "67", now=within).survivor_winners
    assert winners2 is None


class _AlwaysPass(random.Random):
    def random(self):
        return 0.0


# ---------------------------------------------------------------------------
# Role adapter + grant/cleanup
# ---------------------------------------------------------------------------

def _mock_guild(*, manage_roles=True, existing_role=None, bot_top_position=100):
    guild = MagicMock()
    guild.id = 1
    me = MagicMock()
    me.guild_permissions.manage_roles = manage_roles
    me.top_role.position = bot_top_position
    guild.me = me
    guild.roles = [existing_role] if existing_role else []
    return guild


def _mock_role(name=roles_adapter.ROLE_NAME, position=1, role_id=999):
    role = MagicMock()
    role.name = name
    role.position = position
    role.id = role_id
    role.is_default = lambda: False
    return role


@pytest.mark.asyncio
async def test_grant_reuses_existing_role(service):
    role = _mock_role()
    guild = _mock_guild(existing_role=role)
    members = {uid: MagicMock(add_roles=AsyncMock()) for uid in range(1, 7)}
    guild.get_member = lambda uid: members.get(uid)
    guild.create_role = AsyncMock()

    result = await service.grant_survivor_roles(guild, [1, 2, 3, 4, 5, 6])

    assert result.marked is True
    assert result.role_id == 999
    guild.create_role.assert_not_called()
    assert len(service.repository.all_role_grants()) == 6


@pytest.mark.asyncio
async def test_grant_creates_role_when_absent(service):
    guild = _mock_guild(existing_role=None)
    created = _mock_role(role_id=555)
    guild.create_role = AsyncMock(return_value=created)
    members = {uid: MagicMock(add_roles=AsyncMock()) for uid in range(1, 7)}
    guild.get_member = lambda uid: members.get(uid)

    result = await service.grant_survivor_roles(guild, [1, 2, 3, 4, 5, 6])

    guild.create_role.assert_awaited_once()
    assert result.marked is True
    assert result.role_id == 555


@pytest.mark.asyncio
async def test_grant_unmarked_when_missing_manage_roles(service):
    guild = _mock_guild(manage_roles=False, existing_role=None)
    guild.create_role = AsyncMock()

    result = await service.grant_survivor_roles(guild, [1, 2, 3, 4, 5, 6])

    assert result.marked is False
    assert result.role_id is None
    guild.create_role.assert_not_called()
    assert service.repository.all_role_grants() == []


@pytest.mark.asyncio
async def test_grant_unmarked_when_role_above_bot(service):
    # Existing role sits above the bot's top role -> cannot assign.
    role = _mock_role(position=200)
    guild = _mock_guild(existing_role=role, bot_top_position=100)

    result = await service.grant_survivor_roles(guild, [1, 2, 3, 4, 5, 6])

    assert result.marked is False
    assert result.role_id == 999
    assert service.repository.all_role_grants() == []


def test_announcement_includes_mentions_and_unmarked_note():
    marked = service_mod.build_survivor_announcement([1, 2, 3], marked=True)
    assert "THE SIX HAVE SPOKEN" in marked
    assert "<@1>" in marked and "<@3>" in marked
    assert service_mod.SURVIVOR_UNMARKED_NOTE not in marked

    unmarked = service_mod.build_survivor_announcement([1], marked=False)
    assert service_mod.SURVIVOR_UNMARKED_NOTE in unmarked


@pytest.mark.asyncio
async def test_cleanup_removes_expired_grants(service):
    from utils.r67.repository import RoleGrant

    now = utc_now()
    service.repository.add_role_grants(
        [
            RoleGrant(
                guild_id=1,
                user_id=5,
                role_id=999,
                expires_at=now - timedelta(minutes=1),
                created_at=now - timedelta(minutes=68),
            )
        ]
    )
    guild = MagicMock()
    guild.get_role = lambda rid: _mock_role(role_id=rid)
    member = MagicMock(remove_roles=AsyncMock())
    guild.get_member = lambda uid: member

    removed = await service.cleanup_expired_role_grants(lambda gid: guild, now=now)

    assert removed == 1
    assert service.repository.all_role_grants() == []
    member.remove_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_records_failure_and_keeps_grant(service):
    from utils.r67.repository import RoleGrant

    now = utc_now()
    service.repository.add_role_grants(
        [
            RoleGrant(
                guild_id=1,
                user_id=5,
                role_id=999,
                expires_at=now - timedelta(minutes=1),
                created_at=now - timedelta(minutes=68),
            )
        ]
    )
    guild = MagicMock()
    guild.get_role = lambda rid: _mock_role(role_id=rid)
    member = MagicMock(
        remove_roles=AsyncMock(side_effect=roles_adapter.discord.Forbidden.__new__(
            roles_adapter.discord.Forbidden
        ))
    )
    guild.get_member = lambda uid: member

    removed = await service.cleanup_expired_role_grants(lambda gid: guild, now=now)

    assert removed == 0
    grants = service.repository.all_role_grants()
    assert len(grants) == 1
    assert grants[0].removal_attempts == 1


@pytest.mark.asyncio
async def test_cleanup_skips_unavailable_guild(service):
    from utils.r67.repository import RoleGrant

    now = utc_now()
    service.repository.add_role_grants(
        [
            RoleGrant(
                guild_id=1,
                user_id=5,
                role_id=999,
                expires_at=now - timedelta(minutes=1),
                created_at=now,
            )
        ]
    )
    removed = await service.cleanup_expired_role_grants(lambda gid: None, now=now)
    assert removed == 0
    assert len(service.repository.all_role_grants()) == 1
