from datetime import datetime, timezone

import pytest

from utils.match_history import (
    MatchHistoryRepository,
    MatchOutcome,
    MatchPlayer,
    MatchTeam,
    SeriesScore,
)


def team(name, captain, *players):
    return MatchTeam(
        name, captain,
        tuple(MatchPlayer(user_id, role) for user_id, role in players),
    )


@pytest.fixture
def history(tmp_path):
    return MatchHistoryRepository(tmp_path / "parties.db")


def create(history, suffix="", at=None):
    return history.create(
        guild_id=10, organizer_id=99, match_id=f"GF-{suffix or '1'}",
        operation_id=f"create-{suffix or '1'}", draft_reference="DRAFT-8",
        team_one=team("Blue", 1, (1, "solo"), (2, "jungle")),
        team_two=team("Red", 3, (3, "mid"), (4, "support")), at=at,
    )


def test_captains_confirm_same_winner_idempotently(history):
    create(history)
    first = history.report_winner(
        10, "GF-1", captain_id=1, winner=MatchOutcome.TEAM_ONE,
        operation_id="report-1",
    )
    assert first.outcome == MatchOutcome.PENDING
    final = history.report_winner(
        10, "GF-1", captain_id=3, winner=MatchOutcome.TEAM_ONE,
        operation_id="report-2", score=SeriesScore(2, 0),
    )
    assert final.outcome == MatchOutcome.TEAM_ONE
    assert final.series_score == SeriesScore(2, 0)
    assert history.report_winner(
        10, "GF-1", captain_id=3, winner=MatchOutcome.TEAM_ONE,
        operation_id="report-2", score=SeriesScore(2, 0),
    ) == final


def test_conflict_requires_organizer_resolution(history):
    create(history)
    history.report_winner(
        10, "GF-1", captain_id=1, winner=MatchOutcome.TEAM_ONE,
        operation_id="one",
    )
    disputed = history.report_winner(
        10, "GF-1", captain_id=3, winner=MatchOutcome.TEAM_TWO,
        operation_id="two",
    )
    assert disputed.outcome == MatchOutcome.DISPUTED
    with pytest.raises(PermissionError):
        history.resolve(
            10, "GF-1", organizer_id=1, outcome=MatchOutcome.TEAM_ONE,
            operation_id="bad",
        )
    resolved = history.resolve(
        10, "GF-1", organizer_id=99, outcome=MatchOutcome.TEAM_TWO,
        operation_id="resolve", score=SeriesScore(1, 2),
    )
    assert resolved.outcome == MatchOutcome.TEAM_TWO
    assert resolved.resolved_by == 99


@pytest.mark.parametrize("outcome", [MatchOutcome.CANCELLED, MatchOutcome.NO_CONTEST])
def test_explicit_terminal_non_results(history, outcome):
    create(history)
    record = history.resolve(
        10, "GF-1", organizer_id=99, outcome=outcome,
        operation_id=f"resolve-{outcome}",
    )
    assert record.outcome == outcome
    assert record.series_score is None


def test_rejects_non_captain_invalid_score_and_terminal_changes(history):
    create(history)
    with pytest.raises(PermissionError):
        history.report_winner(
            10, "GF-1", captain_id=2, winner=MatchOutcome.TEAM_ONE,
            operation_id="not-captain",
        )
    with pytest.raises(ValueError):
        history.resolve(
            10, "GF-1", organizer_id=99, outcome=MatchOutcome.TEAM_ONE,
            operation_id="wrong-score", score=SeriesScore(0, 2),
        )
    history.resolve(
        10, "GF-1", organizer_id=99, outcome=MatchOutcome.NO_CONTEST,
        operation_id="done",
    )
    with pytest.raises(ValueError):
        history.resolve(
            10, "GF-1", organizer_id=99, outcome=MatchOutcome.CANCELLED,
            operation_id="change",
        )


def test_guild_team_player_history_and_stats(history):
    create(history, "old", datetime(2026, 1, 1, tzinfo=timezone.utc))
    history.resolve(
        10, "GF-old", organizer_id=99, outcome=MatchOutcome.TEAM_ONE,
        operation_id="old-result",
    )
    create(history, "new", datetime(2026, 1, 2, tzinfo=timezone.utc))
    history.resolve(
        10, "GF-new", organizer_id=99, outcome=MatchOutcome.TEAM_TWO,
        operation_id="new-result",
    )
    assert [m.match_id for m in history.recent_for_guild(10)] == ["GF-new", "GF-old"]
    assert len(history.recent_for_team(10, "blue")) == 2
    assert len(history.recent_for_player(10, 1)) == 2
    stats = history.player_stats(10, 1)
    assert (stats.appearances, stats.wins, stats.current_streak) == (2, 1, 0)
    assert stats.role_frequency == {"solo": 2}
    assert stats.teammate_frequency == {2: 2}


def test_team_contract_rejects_duplicate_cross_team_players(history):
    with pytest.raises(ValueError):
        history.create(
            guild_id=10, organizer_id=99, operation_id="bad",
            team_one=team("Blue", 1, (1, "solo")),
            team_two=team("Red", 1, (1, "mid")),
        )


def test_operation_id_cannot_be_reused_for_different_input(history):
    create(history)
    with pytest.raises(ValueError, match="operation ID"):
        history.resolve(
            10, "GF-1", organizer_id=99, outcome=MatchOutcome.CANCELLED,
            operation_id="create-1",
        )


def test_same_external_match_id_is_isolated_between_guilds(history):
    create(history)
    other = history.create(
        guild_id=20, organizer_id=199, match_id="GF-1",
        operation_id="other-create",
        team_one=team("Other Blue", 11, (11, "solo"), (12, "jungle")),
        team_two=team("Other Red", 13, (13, "mid"), (14, "support")),
    )
    history.report_winner(
        20, "GF-1", captain_id=11, winner=MatchOutcome.TEAM_TWO,
        operation_id="other-report-one",
    )
    other = history.report_winner(
        20, "GF-1", captain_id=13, winner=MatchOutcome.TEAM_TWO,
        operation_id="other-report-two",
    )

    assert other.guild_id == 20
    assert other.outcome == MatchOutcome.TEAM_TWO
    assert history.get(10, "GF-1").outcome == MatchOutcome.PENDING
