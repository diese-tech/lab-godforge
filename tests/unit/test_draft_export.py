from utils import draft as draft_utils
from utils.draft import DraftState


def test_draft_export_contains_forgelens_handoff_context(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_utils.match_ids, "MATCH_ID_STATE_PATH", tmp_path / "match_ids.json")
    draft = DraftState(
        blue_captain_id=101,
        blue_captain_name="BlueCap",
        red_captain_id=202,
        red_captain_name="RedCap",
        guild_id=303,
        guild_name="Test Guild",
        channel_id=404,
        channel_name="draft-room",
    )

    team, action = draft.execute_step("Athena")
    assert (team, action) == ("blue", "ban")
    draft.current_game.picks["blue"].append("Bellona")
    draft.current_game.claim("blue", "Bellona", 505, "SoloMain")

    export = draft.to_export_dict()

    assert export["schema_version"] == draft_utils.DRAFT_EXPORT_SCHEMA_VERSION
    assert export["producer"] == "GodForge"
    assert export["match_id"] == "GF-0001"
    assert export["draft_id"] == export["match_id"]
    assert export["guild_id"] == 303
    assert export["channel_id"] == 404
    assert export["teams"]["blue"]["captain"]["user_id"] == 101
    assert export["teams"]["red"]["captain"]["user_id"] == 202
    assert export["timestamps"]["started_at"] == export["started_at"]
    assert export["timestamps"]["ended_at"] == export["ended_at"]
    assert export["draft_order"][0] == {"step": 0, "team": "blue", "action": "ban", "phase": "Bans 1"}
    assert export["games"][0]["game_number"] == 1
    assert export["games"][0]["bans"]["blue"] == ["Athena"]
    assert export["games"][0]["picks"]["blue"] == ["Bellona"]
    assert export["games"][0]["claims"]["blue"]["Bellona"] == {
        "user_id": 505,
        "name": "SoloMain",
    }
    assert "stats" not in export["games"][0]["claims"]["blue"]["Bellona"]
