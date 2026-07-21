"""Passive reaction behavior for R67Service (Issue #47, Gate 2/5)."""

import random
from datetime import timedelta

import pytest

from utils.party import utc_now
from utils.r67 import service as service_mod
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service


class FixedRandom(random.Random):
    """RNG whose ``random()`` always returns a fixed value."""

    def __init__(self, value):
        super().__init__()
        self._value = value

    def random(self):
        return self._value


def _service(tmp_path, rng):
    repo = SQLiteR67Repository(tmp_path / "r67.db")
    return R67Service(repo, rng=rng)


def _all_responses():
    from utils.r67.responses import POOLS

    out = set()
    for pool in POOLS.values():
        out.update(pool)
    return out


def _passive(svc, guild_id, text, *, channel_id=10, user_id=100, now=None):
    """Convenience wrapper returning just the passive response text."""
    return svc.process_passive(
        guild_id, channel_id, user_id, text, now=now
    ).response


def test_no_reaction_when_disabled(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.0))  # roll would pass if reached
    assert _passive(svc, 1, "67") is None


def test_no_reaction_for_non_qualifying_text(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.0))
    svc.enable_reactions(1)
    assert _passive(svc, 1, "hello world") is None


def test_reaction_on_successful_roll(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.0))  # 0.0 < 0.07 -> passes
    svc.enable_reactions(1)
    reply = _passive(svc, 1, "that's 67 right there")
    assert reply in _all_responses()


def test_failed_roll_returns_none_and_sets_no_cooldown(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.5))  # 0.5 >= 0.07 -> fails
    svc.enable_reactions(1)
    assert _passive(svc, 1, "67") is None
    # No cooldown persisted, so a subsequent passing roll can trigger.
    assert svc.repository.get_guild_state(1).passive_cooldown_until is None


def test_success_starts_guild_cooldown_that_blocks_next(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.0))
    svc.enable_reactions(1)
    first = _passive(svc, 1, "67")
    assert first is not None
    # Within cooldown window: no roll, no reaction.
    assert _passive(svc, 1, "67 again") is None


def test_cooldown_expires_after_five_minutes(tmp_path):
    svc = _service(tmp_path, FixedRandom(0.0))
    svc.enable_reactions(1)
    start = utc_now()
    assert _passive(svc, 1, "67", now=start) is not None
    # Just after the 5-minute window, another success is allowed.
    later = start + timedelta(minutes=5, seconds=1)
    assert _passive(svc, 1, "67", now=later) is not None


def test_command_and_passive_are_independent(tmp_path):
    # A passive cooldown must not block direct .r67 commands.
    svc = _service(tmp_path, FixedRandom(0.0))
    svc.enable_reactions(1)
    assert _passive(svc, 1, "67") is not None  # sets cooldown
    assert svc.direct_response(1) in _all_responses()  # still works


def test_trigger_chance_constant_is_seven_percent():
    assert service_mod.PASSIVE_TRIGGER_CHANCE == 0.07
    assert service_mod.PASSIVE_COOLDOWN == timedelta(minutes=5)
