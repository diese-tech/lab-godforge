import logging

import pytest

from utils.forgelens_adapter import ForgeLensAdapter, map_draft_record, map_match_record


def _draft_record():
    return {
        "draft_id": "GF-0042",
        "status": "draft_complete",
        "guild_id": 303,
        "guild_name": "Test Guild",
        "channel_id": 404,
        "channel_name": "draft-room",
        "game_number": 2,
        "teams": {
            "blue": {"captain": {"user_id": 101, "name": "BlueCap"}},
            "red": {"captain": {"user_id": 202, "name": "RedCap"}},
        },
        "started_at": "2026-07-20T01:00:00+00:00",
        "ended_at": "2026-07-20T01:30:00+00:00",
        "draft_order": [
            {"step": 0, "team": "blue", "action": "ban", "phase": "Bans 1"}
        ],
        "games": [
            {
                "game_number": 2,
                "bans": {"blue": ["Athena"], "red": []},
                "picks": {"blue": ["Bellona"], "red": []},
                "claims": {
                    "blue": {
                        "Bellona": {"user_id": 505, "name": "SoloMain"}
                    },
                    "red": {},
                },
            }
        ],
        "fearless_pool": {"Ullr", "Anhur"},
    }


def test_maps_generic_draft_to_portable_contract():
    payload = map_draft_record(_draft_record(), forgelens_match_id="FL-123")

    assert payload["schema_version"] == 2
    assert payload["producer"] == "GodForge"
    assert payload["event_type"] == "draft_export"
    assert payload["draft_id"] == payload["match_id"] == "GF-0042"
    assert payload["forgelens_match_id"] == "FL-123"
    assert payload["teams"]["blue"]["captain"]["user_id"] == 101
    assert payload["timestamps"]["ended_at"] == payload["ended_at"]
    assert payload["games"][0]["bans"]["blue"] == ["Athena"]
    assert payload["picks"][0] == {
        "game_number": 2,
        "team": "blue",
        "gods": ["Bellona"],
    }
    assert payload["selected_gods"] == [
        {
            "game_number": 2,
            "team": "blue",
            "god": "Bellona",
            "claimed_by": {"user_id": 505, "name": "SoloMain"},
        }
    ]
    assert payload["fearless_pool"] == ["Anhur", "Ullr"]


def test_maps_match_wrapper_without_companion_fields_in_core_record():
    match = {
        "match_id": "GF-0042",
        "guild_id": 303,
        "draft": _draft_record() | {"draft_id": None, "guild_id": None},
    }

    payload = map_match_record(match, forgelens_match_id="FL-999")

    assert payload["draft_id"] == "GF-0042"
    assert payload["guild_id"] == 303
    assert payload["forgelens_match_id"] == "FL-999"
    assert "forgelens_match_id" not in match
    assert "forgelens_match_id" not in match["draft"]


@pytest.mark.asyncio
async def test_disabled_adapter_is_a_safe_noop():
    calls = []
    adapter = ForgeLensAdapter(calls.append)

    result = await adapter.deliver_draft(_draft_record())

    assert result.attempted is False
    assert result.delivered is False
    assert result.error is None
    assert calls == []


@pytest.mark.asyncio
async def test_enabled_adapter_delivers_mapped_copy_without_mutating_record():
    record = _draft_record()
    delivered = []
    adapter = ForgeLensAdapter(delivered.append, enabled=True)

    result = await adapter.deliver_draft(record, forgelens_match_id="FL-123")

    assert result.attempted is True
    assert result.delivered is True
    assert delivered[0]["forgelens_match_id"] == "FL-123"
    delivered[0]["games"][0]["picks"]["blue"].append("Changed")
    assert record["games"][0]["picks"]["blue"] == ["Bellona"]


@pytest.mark.asyncio
async def test_delivery_failure_is_observable_and_does_not_escape(caplog):
    async def fail(_payload):
        raise RuntimeError("ForgeLens unavailable")

    adapter = ForgeLensAdapter(fail, enabled=True)

    with caplog.at_level(logging.ERROR):
        result = await adapter.deliver_draft(_draft_record())

    assert result.attempted is True
    assert result.delivered is False
    assert result.error == "ForgeLens unavailable"
    assert "ForgeLens delivery failed" in caplog.text
