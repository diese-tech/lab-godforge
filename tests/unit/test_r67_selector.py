"""Weighted response selection coverage (Issue #47, Gate 5)."""

import random

import pytest

from utils.r67 import responses, selector
from utils.r67.responses import Tier


class SequenceRandom(random.Random):
    """Deterministic RNG: ``random()`` returns queued values in order."""

    def __init__(self, floats):
        super().__init__()
        self._floats = list(floats)

    def random(self):
        return self._floats.pop(0)


def test_command_weights_sum_to_one():
    assert sum(w for _, w in selector.COMMAND_WEIGHTS) == pytest.approx(1.0)


def test_passive_weights_sum_to_one():
    assert sum(w for _, w in selector.PASSIVE_WEIGHTS) == pytest.approx(1.0)


def test_command_never_selects_command_discovery():
    tiers = {t for t, _ in selector.COMMAND_WEIGHTS}
    assert Tier.COMMAND_DISCOVERY not in tiers


def test_command_tier_boundaries():
    # roll < 0.88 -> common; < 0.98 -> rare; else ultra-rare.
    assert selector.select_command(SequenceRandom([0.0, 0.0])).tier is Tier.COMMON
    assert selector.select_command(SequenceRandom([0.879, 0.0])).tier is Tier.COMMON
    assert selector.select_command(SequenceRandom([0.88, 0.0])).tier is Tier.RARE
    assert selector.select_command(SequenceRandom([0.979, 0.0])).tier is Tier.RARE
    assert selector.select_command(SequenceRandom([0.98, 0.0])).tier is Tier.ULTRA_RARE
    assert selector.select_command(SequenceRandom([0.999, 0.0])).tier is Tier.ULTRA_RARE


def test_passive_tier_boundaries():
    assert selector.select_passive(SequenceRandom([0.0, 0.0])).tier is Tier.COMMON
    assert selector.select_passive(SequenceRandom([0.839, 0.0])).tier is Tier.COMMON
    assert selector.select_passive(SequenceRandom([0.84, 0.0])).tier is Tier.RARE
    assert selector.select_passive(SequenceRandom([0.939, 0.0])).tier is Tier.RARE
    assert selector.select_passive(SequenceRandom([0.94, 0.0])).tier is Tier.ULTRA_RARE
    assert selector.select_passive(SequenceRandom([0.949, 0.0])).tier is Tier.ULTRA_RARE
    assert (
        selector.select_passive(SequenceRandom([0.95, 0.0])).tier
        is Tier.COMMAND_DISCOVERY
    )
    assert (
        selector.select_passive(SequenceRandom([0.999, 0.0])).tier
        is Tier.COMMAND_DISCOVERY
    )


def test_selected_text_belongs_to_its_tier():
    sel = selector.select_command(SequenceRandom([0.0, 0.0]))
    assert sel.text in responses.POOLS[sel.tier]


def test_no_consecutive_repeat_within_pool():
    rng = random.Random(1234)
    last = responses.COMMON[0]
    for _ in range(200):
        sel = selector.select_command(rng, exclude=last)
        if sel.tier is Tier.COMMON:
            assert sel.text != last
        last = sel.text


def test_every_approved_response_is_reachable():
    rng = random.Random(0)
    seen = set()
    for _ in range(20000):
        seen.add(selector.select_passive(rng).text)
    all_responses = set()
    for pool in responses.POOLS.values():
        all_responses.update(pool)
    assert seen == all_responses


def test_no_unexpected_responses_are_produced():
    rng = random.Random(7)
    all_responses = set()
    for pool in responses.POOLS.values():
        all_responses.update(pool)
    for _ in range(5000):
        assert selector.select_passive(rng).text in all_responses
        assert selector.select_command(rng).text in all_responses
