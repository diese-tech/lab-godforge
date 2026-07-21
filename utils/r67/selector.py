"""Weighted response selection for `.r67`.

Two selection modes are locked in Issue #47 (Gate 1 / Gate 5):

Direct ``.r67`` command::

    Common 88%   Rare 10%   Ultra-rare 2%

Passive reaction::

    Common 84%   Rare 10%   Ultra-rare 1%   Command-discovery 5%

Randomness is dependency-injected (a ``random.Random`` instance) so tests can
run deterministically. Selection never returns the same response twice in a
row for a given guild when an ``exclude`` value is supplied.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from utils.r67.responses import POOLS, Tier


@dataclass(frozen=True, slots=True)
class Selection:
    tier: Tier
    text: str


# Ordered (tier, weight) tables. Order matters for the cumulative roll.
COMMAND_WEIGHTS: tuple[tuple[Tier, float], ...] = (
    (Tier.COMMON, 0.88),
    (Tier.RARE, 0.10),
    (Tier.ULTRA_RARE, 0.02),
)

PASSIVE_WEIGHTS: tuple[tuple[Tier, float], ...] = (
    (Tier.COMMON, 0.84),
    (Tier.RARE, 0.10),
    (Tier.ULTRA_RARE, 0.01),
    (Tier.COMMAND_DISCOVERY, 0.05),
)


def _roll_tier(weights: tuple[tuple[Tier, float], ...], rng: random.Random) -> Tier:
    roll = rng.random()
    cumulative = 0.0
    for tier, weight in weights:
        cumulative += weight
        if roll < cumulative:
            return tier
    # Floating-point guard: fall back to the final tier.
    return weights[-1][0]


def _pick_text(tier: Tier, rng: random.Random, exclude: str | None) -> str:
    pool = POOLS[tier]
    candidates = [text for text in pool if text != exclude]
    if not candidates:
        # Only possible when the pool has a single entry equal to ``exclude``.
        candidates = list(pool)
    return rng.choice(candidates)


def select_command(rng: random.Random, exclude: str | None = None) -> Selection:
    """Select a response for a direct ``.r67`` invocation."""
    tier = _roll_tier(COMMAND_WEIGHTS, rng)
    return Selection(tier=tier, text=_pick_text(tier, rng, exclude))


def select_passive(rng: random.Random, exclude: str | None = None) -> Selection:
    """Select a response for a passive 67 reaction."""
    tier = _roll_tier(PASSIVE_WEIGHTS, rng)
    return Selection(tier=tier, text=_pick_text(tier, rng, exclude))
