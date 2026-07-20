"""Approved `.r67` response pools.

These constants are the owner-approved canonical pool locked in Issue #47
(Gate 1). They contain no Discord or persistence dependencies so they can be
imported by pure logic and tests alike.

Design rules baked into this data (do not violate without owner approval):

- No response explains what "67" means.
- GodForge lore (the Forge, the Gods, prophecy, containment) is preferred over
  generic Discord meme terminology.
- Council-themed responses and ``The pantheon has entered the chat.`` are
  explicitly excluded.
"""

from __future__ import annotations

from enum import Enum


class Tier(str, Enum):
    """Response rarity tiers used by the selector."""

    COMMON = "common"
    RARE = "rare"
    ULTRA_RARE = "ultra_rare"
    COMMAND_DISCOVERY = "command_discovery"


COMMON: tuple[str, ...] = (
    "SIX SEVEN.",
    "67 acknowledged.",
    "Unfortunately.",
    "There it is.",
    "You know exactly what you did.",
    "The prophecy continues.",
    "Not this again.",
    "I saw that.",
    "Certified 67 behavior.",
    "Lore accurate.",
    "Someone had to do it.",
    "This changes everything.",
    "The numbers have spoken.",
    "We are so back.",
    "It has begun.",
    "The voices approve.",
    "That’s enough out of you.",
    "Peak.",
    "Yep. 67.",
    "The gods have heard you.",
    "The gods will remember this.",
    "The forge remembers.",
)

RARE: tuple[str, ...] = (
    "My lawyer has advised me not to elaborate.",
    "Error 67: Explanation unavailable.",
    "The containment protocol has failed.",
    "The number chose you.",
    "Even the gods looked away.",
    "Olympus declined to comment.",
    "A deity just sighed.",
)

ULTRA_RARE: tuple[str, ...] = (
    "GLOBAL 67 DETECTED",
    "67/67.",
    "Achievement Unlocked: Brainrot",
    "THE NUMBERS, MASON.",
    "The bot has become self-aware. Confidence: 67%.",
    "The forge awakens.",
    "Divine intervention denied.",
    "The gods are typing...",
)

# Command-discovery responses only surface through passive reactions. They are
# the subtle discovery hook back toward the `.r67` command and must stay rare.
COMMAND_DISCOVERY: tuple[str, ...] = (
    "You know `.r67` exists, right?",
    "Fine. Try `.r67`.",
    "This incident has been logged. `.r67`",
    "There is a command for this. Unfortunately.",
    "`.r67` has entered the chat.",
)


POOLS: dict[Tier, tuple[str, ...]] = {
    Tier.COMMON: COMMON,
    Tier.RARE: RARE,
    Tier.ULTRA_RARE: ULTRA_RARE,
    Tier.COMMAND_DISCOVERY: COMMAND_DISCOVERY,
}
