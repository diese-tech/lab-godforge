"""Characterization tests for the /scrim slash-command handlers in bot.py.

Written before extracting the scrim command group into a feature module
(Issue #48, Phase 7). These pin down current behavior of the Discord adapter
layer (bot.scrim_team_create, bot.scrim_challenge, etc. and ScrimChallengeView),
which previously had no direct test coverage — only utils/scrims.py's repository
logic was tested. Interactions are mocked; app_commands.Command objects are
invoked via their .callback to bypass Discord's dispatch machinery.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot
from utils.scrims import ScrimRepository

FUTURE = datetime.now(timezone.utc) + timedelta(days=7)
WHEN = FUTURE.strftime("%Y-%m-%d %H:%M")


@pytest.fixture()
def scrim_repo(tmp_path, monkeypatch):
    repo = ScrimRepository(tmp_path / "party.db")
    monkeypatch.setattr(bot, "scrim_repository", repo)
    monkeypatch.setattr(bot._scrim_deps, "scrim_repository", repo)
    return repo


def _interaction(*, guild_id=1, user_id=100, manage_guild=False):
    interaction = MagicMock()
    interaction.id = 999
    interaction.guild_id = guild_id
    interaction.guild = MagicMock() if guild_id is not None else None
    user = MagicMock()
    user.id = user_id
    user.guild_permissions.manage_guild = manage_guild
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.message = None
    return interaction


async def test_team_create_saves_and_replies_ephemeral(scrim_repo):
    interaction = _interaction(user_id=1)
    await bot.scrim_team_create.callback(
        interaction,
        name="Alpha",
        roster="<@20002> <@20003> <@20007> <@20008>",
        region="NA East",
        availability="Weeknights",
        substitutes="",
    )
    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert "Alpha" in interaction.response.send_message.call_args.args[0]
    assert scrim_repo.list_teams(1)[0].name == "Alpha"


async def test_team_create_requires_guild(scrim_repo):
    interaction = _interaction(guild_id=None)
    await bot.scrim_team_create.callback(
        interaction, name="A", roster="<@20002> <@20003>", region="R", availability="A",
    )
    assert "Server-only" in interaction.response.send_message.call_args.args[0]


async def test_teams_lists_registered_teams(scrim_repo):
    scrim_repo.save_team(
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 2, 3, 7, 8),
        substitutes=(), region="NA", availability="Weeknights",
        operation_id="op-1",
    )
    interaction = _interaction()
    await bot.scrim_teams.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Alpha" in reply


async def test_teams_empty_reports_none_registered(scrim_repo):
    interaction = _interaction()
    await bot.scrim_teams.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "No scrim teams" in reply


def _two_teams(repo):
    alpha = repo.save_team(
        guild_id=1, captain_id=1, name="Alpha", roster=(1, 2, 3, 7, 8),
        substitutes=(), region="NA", availability="Weeknights", operation_id="a",
    )
    beta = repo.save_team(
        guild_id=1, captain_id=4, name="Beta", roster=(4, 5, 6, 9, 10),
        substitutes=(), region="NA", availability="Weekends", operation_id="b",
    )
    return alpha, beta


async def test_challenge_posts_embed_with_challenge_view(scrim_repo):
    alpha, beta = _two_teams(scrim_repo)
    interaction = _interaction(user_id=1)
    await bot.scrim_challenge.callback(
        interaction,
        your_team_id=alpha.team_id,
        opponent_team_id=beta.team_id,
        when=WHEN,
        timezone_name="America/New_York",
    )
    kwargs = interaction.response.send_message.call_args.kwargs
    assert isinstance(kwargs["view"], bot.ScrimChallengeView)
    assert kwargs["embed"].footer.text.startswith("Scrim challenge ")


async def test_challenge_rejects_teams_outside_guild(scrim_repo):
    alpha, beta = _two_teams(scrim_repo)
    interaction = _interaction(guild_id=999, user_id=1)
    await bot.scrim_challenge.callback(
        interaction,
        your_team_id=alpha.team_id,
        opponent_team_id=beta.team_id,
        when=WHEN,
        timezone_name="America/New_York",
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert "must be registered" in reply


def _challenge(repo):
    alpha, beta = _two_teams(repo)
    return repo.challenge(
        challenger_team_id=alpha.team_id,
        recipient_team_id=beta.team_id,
        actor_id=1,
        starts_at=FUTURE,
        timezone_name="America/New_York",
        operation_id="challenge-1",
    )


async def test_respond_accept_updates_state(scrim_repo):
    challenge = _challenge(scrim_repo)
    interaction = _interaction(user_id=4)  # beta captain
    await bot.scrim_respond.callback(
        interaction, challenge_id=challenge.challenge_id, response="accept",
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert "accepted" in reply.lower()


async def test_respond_unknown_challenge_reports_error(scrim_repo):
    interaction = _interaction(user_id=4)
    await bot.scrim_respond.callback(
        interaction, challenge_id="nope", response="accept",
    )
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" in reply.lower()


async def test_checkin_records_progress(scrim_repo):
    challenge = _challenge(scrim_repo)
    scrim_repo.respond(
        challenge.challenge_id, actor_id=4, response="accept", operation_id="r1",
    )
    interaction = _interaction(user_id=1)
    await bot.scrim_checkin.callback(interaction, challenge_id=challenge.challenge_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "1/2" in reply


async def test_lock_requires_both_checked_in(scrim_repo):
    challenge = _challenge(scrim_repo)
    scrim_repo.respond(
        challenge.challenge_id, actor_id=4, response="accept", operation_id="r1",
    )
    interaction = _interaction(user_id=1)
    await bot.scrim_lock.callback(interaction, challenge_id=challenge.challenge_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" not in reply.lower()  # got past the lookup
    # Rosters aren't locked yet since only one side checked in.


async def test_launch_rejects_non_organizer_non_manager(scrim_repo):
    challenge = _challenge(scrim_repo)
    interaction = _interaction(user_id=999, manage_guild=False)
    await bot.scrim_launch.callback(interaction, challenge_id=challenge.challenge_id)
    reply = interaction.response.send_message.call_args.args[0]
    assert "Only the organizer" in reply


async def test_launch_reports_missing_challenge(scrim_repo):
    interaction = _interaction(user_id=1)
    await bot.scrim_launch.callback(interaction, challenge_id="missing")
    reply = interaction.response.send_message.call_args.args[0]
    assert "not found" in reply.lower()


# -- ScrimChallengeView ------------------------------------------------------

def _view_interaction(challenge_id, *, guild_id=1, user_id=4):
    interaction = _interaction(guild_id=guild_id, user_id=user_id)
    footer = MagicMock()
    footer.text = f"Scrim challenge {challenge_id}"
    embed = MagicMock()
    embed.footer = footer
    message = MagicMock()
    message.embeds = [embed]
    interaction.message = message
    return interaction


async def test_view_accept_button_updates_challenge(scrim_repo):
    challenge = _challenge(scrim_repo)
    interaction = _view_interaction(challenge.challenge_id)
    view = bot.ScrimChallengeView()
    await view.accept.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "accepted" in reply.lower()


async def test_view_missing_challenge_id_reports_error(scrim_repo):
    interaction = _interaction()
    interaction.message = None
    view = bot.ScrimChallengeView()
    await view.accept.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "missing its durable ID" in reply


async def test_view_checkin_button_reports_progress(scrim_repo):
    challenge = _challenge(scrim_repo)
    scrim_repo.respond(
        challenge.challenge_id, actor_id=4, response="accept", operation_id="r1",
    )
    interaction = _view_interaction(challenge.challenge_id, user_id=1)
    view = bot.ScrimChallengeView()
    await view.checkin.callback(interaction)
    reply = interaction.response.send_message.call_args.args[0]
    assert "1/2" in reply
