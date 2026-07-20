"""Pure draft-command helpers (Issue #48, Phase 3c)."""

from types import SimpleNamespace

from utils import draft_support


def test_start_options_defaults():
    opts = draft_support.draft_start_options("start", forgelens=lambda: True)
    assert opts == {"forgelens_match_id": "", "game_number": 1}


def test_start_options_parses_match_and_game():
    opts = draft_support.draft_start_options(
        "start --match ABC123 --game 3", forgelens=lambda: True
    )
    assert opts == {"forgelens_match_id": "ABC123", "game_number": 3}


def test_match_option_ignored_when_forgelens_disabled():
    opts = draft_support.draft_start_options(
        "start --match ABC123 --game 2", forgelens=lambda: False
    )
    assert opts == {"forgelens_match_id": "", "game_number": 2}


def _draft():
    return SimpleNamespace(
        draft_id="d9",
        forgelens_match_id="M1",
        current_game=SimpleNamespace(game_number=2),
    )


def test_completion_marker_without_forgelens():
    marker = draft_support.draft_completion_marker(_draft(), forgelens=lambda: False)
    assert marker.splitlines() == ["Draft complete", "draft_id=d9", "game_number=2"]


def test_completion_marker_with_forgelens_inserts_match_id():
    marker = draft_support.draft_completion_marker(_draft(), forgelens=lambda: True)
    assert marker.splitlines() == [
        "Draft complete",
        "draft_id=d9",
        "forgelens_match_id=M1",
        "game_number=2",
    ]
