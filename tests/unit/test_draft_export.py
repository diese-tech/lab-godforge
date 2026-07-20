from utils import draft as draft_utils
from utils import formatter
from utils.draft import DraftState


def _make_draft(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(draft_utils.match_ids, "MATCH_ID_STATE_PATH", tmp_path / "match_ids.json")
    params = {
        "blue_captain_id": 101,
        "blue_captain_name": "BlueCap",
        "red_captain_id": 202,
        "red_captain_name": "RedCap",
        "guild_id": 303,
        "guild_name": "Test Guild",
        "channel_id": 404,
        "channel_name": "draft-room",
    }
    params.update(overrides)
    return DraftState(**params)


def test_draft_export_contains_forgelens_handoff_context(tmp_path, monkeypatch):
    draft = _make_draft(
        tmp_path,
        monkeypatch,
        forgelens_match_id="FL-123",
        game_number=2,
    )

    team, action = draft.execute_step("Athena")
    assert (team, action) == ("blue", "ban")
    draft.current_game.picks["blue"].append("Bellona")
    draft.current_game.claim("blue", "Bellona", 505, "SoloMain")

    export = draft.to_export_dict()

    assert export["schema_version"] == draft_utils.DRAFT_EXPORT_SCHEMA_VERSION
    assert export["producer"] == "GodForge"
    assert export["event_type"] == "draft_export"
    assert export["status"] == "drafting"
    assert export["draft_id"] == "GF-0001"
    assert export["match_id"] == export["draft_id"]
    assert export["forgelens_match_id"] == "FL-123"
    assert export["guild_id"] == 303
    assert export["channel_id"] == 404
    assert export["game_number"] == 2
    assert export["draft_sequence"] == 1
    assert export["teams"]["blue"]["label"] == "blue"
    assert export["teams"]["blue"]["captain"]["user_id"] == 101
    assert export["teams"]["red"]["captain"]["user_id"] == 202
    assert export["timestamps"]["started_at"] == export["started_at"]
    assert export["timestamps"]["ended_at"] == export["ended_at"]
    assert export["draft_order"][0] == {"step": 0, "team": "blue", "action": "ban", "phase": "Bans 1"}
    assert export["games"][0]["game_number"] == 2
    assert export["games"][0]["bans"]["blue"] == ["Athena"]
    assert export["games"][0]["picks"]["blue"] == ["Bellona"]
    assert export["selected_gods"][0] == {
        "game_number": 2,
        "team": "blue",
        "god": "Bellona",
        "claimed_by": {"user_id": 505, "name": "SoloMain"},
    }
    assert "stats" not in export["games"][0]["claims"]["blue"]["Bellona"]


def test_fearless_draft_uses_exact_twenty_step_phased_sequence(tmp_path, monkeypatch):
    draft = _make_draft(tmp_path, monkeypatch)
    expected_sequence = [
        ("blue", "ban", "Bans 1"), ("red", "ban", "Bans 1"), ("blue", "ban", "Bans 1"),
        ("red", "ban", "Bans 1"), ("blue", "ban", "Bans 1"), ("red", "ban", "Bans 1"),
        ("blue", "pick", "Picks 1"), ("red", "pick", "Picks 1"), ("red", "pick", "Picks 1"),
        ("blue", "pick", "Picks 1"), ("blue", "pick", "Picks 1"), ("red", "pick", "Picks 1"),
        ("red", "ban", "Bans 2"), ("blue", "ban", "Bans 2"), ("red", "ban", "Bans 2"),
        ("blue", "ban", "Bans 2"),
        ("red", "pick", "Picks 2"), ("blue", "pick", "Picks 2"), ("blue", "pick", "Picks 2"),
        ("red", "pick", "Picks 2"),
    ]

    exported_order = [
        (step["team"], step["action"], step["phase"])
        for step in draft.to_export_dict()["draft_order"]
    ]

    assert draft_utils.STEPS_PER_GAME == 20
    assert exported_order == expected_sequence

    for index, (expected_team, expected_action, expected_phase) in enumerate(expected_sequence):
        assert draft_utils.get_phase_label(draft.current_game.step) == expected_phase
        assert draft.get_current_team_and_action() == (expected_team, expected_action)

        team, action = draft.execute_step(f"God {index + 1}")
        assert (team, action) == (expected_team, expected_action)
        assert draft.current_game.is_complete() is (index == 19)

    assert draft.get_current_team_and_action() is None
    assert draft_utils.get_phase_label(draft.current_game.step) == "Complete"
    assert len(draft.current_game.bans["blue"]) == 5
    assert len(draft.current_game.bans["red"]) == 5
    assert len(draft.current_game.picks["blue"]) == 5
    assert len(draft.current_game.picks["red"]) == 5
    assert sum(len(gods) for gods in draft.current_game.bans.values()) == 10
    assert sum(len(gods) for gods in draft.current_game.picks.values()) == 10


def test_draft_complete_status_only_after_claims(tmp_path, monkeypatch):
    draft = _make_draft(tmp_path, monkeypatch)

    for god in [
        "Athena", "Bacchus", "Cabrakan", "Discordia", "Eset",
        "Fenrir", "Geb", "Hades", "Izanami", "Janus",
        "Khepri", "Loki", "Mercury", "Neith", "Odin",
        "Poseidon", "Ra", "Sobek", "Thor", "Ullr",
    ]:
        draft.execute_step(god)

    assert draft.current_game.is_complete()
    assert draft.current_status() == "picks_bans_complete"

    draft.claim_god("blue", draft.current_game.picks["blue"][0], 1, "Blue One")
    assert draft.current_status() == "claiming"

    for team in ("blue", "red"):
        for index, god in enumerate(draft.current_game.picks[team]):
            draft.claim_god(team, god, 1000 + index + (0 if team == "blue" else 100), f"{team}-{index}")

    assert draft.current_status() == "draft_complete"
    assert draft.to_export_dict()["status"] == "draft_complete"


def test_draft_board_contains_forgelens_status_field(tmp_path, monkeypatch):
    monkeypatch.setenv("GODFORGE_ENABLE_FORGELENS", "true")
    draft = _make_draft(tmp_path, monkeypatch, forgelens_match_id="FL-999", game_number=3)

    embed = formatter.format_draft_board(draft)
    status_field = next(field for field in embed.fields if field.name == "ForgeLens Status")

    assert "draft_status=drafting" in status_field.value
    assert "draft_id=GF-0001" in status_field.value
    assert "forgelens_match_id=FL-999" in status_field.value
    assert "game_number=3" in status_field.value
    assert "draft_sequence=1" in status_field.value


def test_draft_board_hides_forgelens_status_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GODFORGE_ENABLE_FORGELENS", raising=False)
    draft = _make_draft(tmp_path, monkeypatch, forgelens_match_id="FL-999")

    embed = formatter.format_draft_board(draft)

    assert all(field.name != "ForgeLens Status" for field in embed.fields)
