import pytest

from utils.team_formation import (
    FormationMode,
    FormationPlayer,
    SMITE_ROLES,
    TeamFormationError,
    form_smite_teams,
)


def _roster(*, captains=(1, 6)):
    roles = SMITE_ROLES * 2
    return tuple(
        FormationPlayer(
            user_id=index,
            primary_role=role,
            secondary_role=SMITE_ROLES[(SMITE_ROLES.index(role) + 1) % 5],
            captain=index in captains,
            skill_band="intermediate",
            experience=index,
        )
        for index, role in enumerate(roles, start=1)
    )


def test_role_fit_gives_common_roster_all_first_choices():
    result = form_smite_teams(_roster(), FormationMode.ROLE_FIT)

    assert result.first_choices == 10
    assert result.second_choices == 0
    assert result.fills == 0
    assert {a.role for a in result.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in result.red.assignments} == set(SMITE_ROLES)


def test_adversarial_roster_reports_unavoidable_fills_deterministically():
    roster = tuple(
        FormationPlayer(index, primary_role="mid", fill=True)
        for index in range(1, 11)
    )

    first = form_smite_teams(roster, FormationMode.ROLE_FIT)
    second = form_smite_teams(reversed(roster), FormationMode.ROLE_FIT)

    assert first == second
    assert first.first_choices == 2
    assert first.fills == 8
    assert "8 unavoidable fills" in first.explanation


def test_balanced_mode_minimizes_transparent_strength_gap():
    roster = tuple(
        FormationPlayer(
            index,
            primary_role=SMITE_ROLES[(index - 1) % 5],
            fill=True,
            skill_band="competitive" if index <= 5 else "beginner",
            experience=index,
        )
        for index in range(1, 11)
    )

    result = form_smite_teams(roster, FormationMode.BALANCED)

    # Five-player sides force a 3/2 split of the five high-band players. The
    # optimizer selects the experience arrangement with the smallest gap.
    assert result.strength_difference == 383
    assert "team strength difference" in result.explanation


def test_captain_mode_separates_volunteers_and_exposes_snake_order():
    result = form_smite_teams(_roster(), FormationMode.CAPTAINS)

    assert {result.blue.captain_id, result.red.captain_id} == {1, 6}
    assert len(result.draft_order) == 8
    assert set(result.draft_order) == set(range(1, 11)) - {1, 6}
    assert {a.role for a in result.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in result.red.assignments} == set(SMITE_ROLES)


def test_captain_mode_requires_two_volunteers():
    with pytest.raises(TeamFormationError, match="two captain"):
        form_smite_teams(_roster(captains=(1,)), FormationMode.CAPTAINS)


def test_roster_validation_rejects_non_ten_and_duplicate_players():
    with pytest.raises(TeamFormationError, match="exactly ten"):
        form_smite_teams(_roster()[:9])
    duplicated = _roster()[:-1] + (_roster()[0],)
    with pytest.raises(TeamFormationError, match="unique"):
        form_smite_teams(duplicated)


@pytest.mark.parametrize("seed", range(25))
def test_simulated_mixed_rosters_are_role_complete_and_repeatable(seed):
    roster = tuple(
        FormationPlayer(
            user_id=index + 1,
            primary_role=SMITE_ROLES[(index * 3 + seed) % 5],
            secondary_role=SMITE_ROLES[(index * 3 + seed + 1) % 5],
            fill=index % 3 == 0,
            captain=index in {seed % 10, (seed + 5) % 10},
            skill_band=("beginner", "intermediate", "competitive")[
                (index + seed) % 3
            ],
            experience=(index * 7 + seed) % 40,
            recent_adjustment=(index + seed) % 5 - 2,
        )
        for index in range(10)
    )
    first = form_smite_teams(roster, FormationMode.BALANCED)
    second = form_smite_teams(reversed(roster), FormationMode.BALANCED)

    assert first == second
    assert {a.role for a in first.blue.assignments} == set(SMITE_ROLES)
    assert {a.role for a in first.red.assignments} == set(SMITE_ROLES)
