"""Pure qualifying-text detection for passive `67` references.

This module has no Discord or persistence dependencies. It answers a single
question: does a message body contain a *standalone* ``67`` reference that
should be eligible for a passive reaction?

The accepted forms and exclusions are locked in Issue #47 (Gate 2 / Gate 5):

Accepted::

    67  6 7  6-7  6/7  6.7
    six seven  six-seven  sixty seven  sixty-seven

Rejected::

    167  670           (digits touching 67 / longer numeric strings)
    abc67  67abc  67th (letters touching 67)
    6.7.8  6-7-8       (longer separated numeric strings, e.g. versions/IDs)
    URLs, inline code, and fenced code blocks (stripped before matching)

Dates and scores such as ``6/7`` or ``6-7`` are intentionally eligible: the
accidental appearance of 67 in ordinary conversation is part of the joke.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Stripping: remove regions that must never contribute a match.
# ---------------------------------------------------------------------------

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
# URLs: scheme-based or bare www. hosts. Consume the whole non-space run.
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def strip_ignored(text: str) -> str:
    """Return *text* with fenced code, inline code, and URLs blanked out.

    Ignored regions are replaced with a single space so tokens on either side
    do not accidentally fuse into a false match.
    """
    text = _FENCED_CODE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    return text


# ---------------------------------------------------------------------------
# Matching: standalone 67 references with strict boundaries.
# ---------------------------------------------------------------------------

# Bare "67" — no letter/digit/underscore may touch either side (rejects 167,
# 670, abc67, 67abc, 67th). Additionally reject a "67" embedded in a longer
# dotted/dashed/slashed numeric run such as 1.67.0, 10.67.0.1, or 6-67-8: a
# separator flanked by another digit means it is a version/IP/ID, not a
# standalone reference. A trailing sentence period ("it's 67.") is still fine
# because the period is not followed by a digit.
_BARE = re.compile(r"(?<![\w])(?<!\d[.\-/])67(?![\w])(?![.\-/]\d)")

# Separated numeric forms "6<sep>7" with sep in { space, '.', '-', '/' }.
# Neighboring letters, digits, underscores, or *separators* are rejected, so a
# longer run such as 6.7.8, 6-7-8, 16.7, or 6.78 never qualifies while a lone
# 6/7 or 6-7 does.
_SEPARATED = re.compile(r"(?<![\w.\-/])6[ .\-/]7(?![\w.\-/])")

# Written forms: six seven, six-seven, sixty seven, sixty-seven (case-insensitive).
_WRITTEN = re.compile(r"\bsix(?:ty)?[ -]seven\b", re.IGNORECASE)


def is_qualifying(text: str) -> bool:
    """True if *text* contains at least one standalone qualifying 67 reference.

    URLs and code are stripped first; the remaining text is scanned for any of
    the accepted numeric or written forms.
    """
    if not text:
        return False
    cleaned = strip_ignored(text)
    return bool(
        _BARE.search(cleaned)
        or _SEPARATED.search(cleaned)
        or _WRITTEN.search(cleaned)
    )
