"""Match-result rendering/parse helpers (Issue #48, Phase 6)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from utils import match_results
from utils.match_history import MatchOutcome


def _record(outcome=MatchOutcome.PENDING):
    return SimpleNamespace(
        match_id="GF-1",
        outcome=outcome,
        team_one=SimpleNamespace(name="Blue", captain_id=1),
        team_two=SimpleNamespace(name="Red", captain_id=2),
    )


def test_build_result_embed_sets_footer_identity():
    embed = match_results.build_result_embed(_record())
    assert embed.footer.text == "match_id=GF-1"
    assert "Blue" in embed.fields[0].name


def test_build_result_embed_win_color():
    embed = match_results.build_result_embed(_record(MatchOutcome.TEAM_ONE))
    assert embed.color.value == 0x2ECC71


def test_match_id_from_interaction_reads_footer():
    footer = SimpleNamespace(text="match_id=GF-9")
    embed = SimpleNamespace(footer=footer)
    interaction = SimpleNamespace(message=SimpleNamespace(embeds=[embed]))
    assert match_results.match_id_from_interaction(interaction) == "GF-9"


def test_match_id_from_interaction_rejects_missing_identity():
    embed = SimpleNamespace(footer=SimpleNamespace(text="nope"))
    interaction = SimpleNamespace(message=SimpleNamespace(embeds=[embed]))
    with pytest.raises(ValueError, match="stable identity"):
        match_results.match_id_from_interaction(interaction)


def test_ensure_match_history_delegates_to_repository():
    repo = MagicMock()
    lobby = SimpleNamespace(
        guild_id=7,
        lobby_id="L1",
        organizer_id=3,
        participants=[SimpleNamespace(user_id=1, primary_role="mid")],
    )
    launch = SimpleNamespace(
        match_id="GF-5",
        blue=SimpleNamespace(captain_id=1, participant_ids=(1,)),
        red=SimpleNamespace(captain_id=2, participant_ids=(2,)),
    )

    match_results.ensure_match_history(repo, lobby, launch)

    repo.create.assert_called_once()
    kwargs = repo.create.call_args.kwargs
    assert kwargs["match_id"] == "GF-5"
    assert kwargs["team_one"].players[0].role == "mid"
