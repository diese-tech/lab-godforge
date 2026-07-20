import bot
from utils import ledger as ledger_utils
from web_api import server as web_server


def test_draft_start_options_support_optional_match_and_game(monkeypatch):
    monkeypatch.setenv("GODFORGE_ENABLE_FORGELENS", "true")
    options = bot._draft_start_options(".draft start @blue @red --match FL-123 --game 2")
    assert options == {"forgelens_match_id": "FL-123", "game_number": 2}

    options = bot._draft_start_options(".draft start @blue @red")
    assert options == {"forgelens_match_id": "", "game_number": 1}


def test_draft_start_options_ignore_forgelens_match_by_default(monkeypatch):
    monkeypatch.delenv("GODFORGE_ENABLE_FORGELENS", raising=False)

    options = bot._draft_start_options(".draft start @blue @red --match FL-123 --game 2")

    assert options == {"forgelens_match_id": "", "game_number": 2}


def test_legacy_economy_guard_blocks_web_mutations_by_default(tmp_ledger, monkeypatch):
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", False)

    try:
        web_server._ensure_legacy_economy_enabled()
    except ValueError as exc:
        assert "disabled" in str(exc).lower()
    else:
        raise AssertionError("legacy guard should block disabled economy mutations")

    assert ledger_utils.load_ledger()["matches"] == []


def test_legacy_economy_guard_can_be_explicitly_enabled(tmp_ledger, monkeypatch):
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", True)

    web_server._ensure_legacy_economy_enabled()
    match = ledger_utils.create_match("Solaris", "Onyx")

    assert match["match_id"] == "GF-0001"
