"""Match-result rendering and history-record helpers.

Feature-support module for the Issue #48 refactor. It holds the small, mostly
pure pieces of the match-results surface that previously lived in ``bot.py``:
building the result-card embed, parsing a card's stable identity, and creating
the authoritative history record for a launched party draft. The repository is
injected for ``ensure_match_history``.
"""

from __future__ import annotations

import discord

from utils.match_history import MatchOutcome, MatchPlayer, MatchTeam


def ensure_match_history(repository, lobby, launch):
    """Idempotently create the authoritative record once a draft is active."""
    roles = {
        participant.user_id: participant.primary_role or ""
        for participant in lobby.participants
    }

    def team(name, draft_team):
        return MatchTeam(
            name,
            draft_team.captain_id,
            tuple(
                MatchPlayer(user_id, roles.get(user_id, ""))
                for user_id in draft_team.participant_ids
            ),
        )

    return repository.create(
        guild_id=lobby.guild_id,
        organizer_id=lobby.organizer_id,
        team_one=team("Blue", launch.blue),
        team_two=team("Red", launch.red),
        operation_id=(
            f"party-draft-history:{lobby.guild_id}:{lobby.lobby_id}:"
            f"{launch.match_id}"
        ),
        draft_reference=launch.match_id,
        match_id=launch.match_id,
    )


def build_result_embed(record) -> discord.Embed:
    """Render the match-result card for a history record."""
    labels = {
        MatchOutcome.PENDING: "Waiting for both captains",
        MatchOutcome.DISPUTED: "Reports conflict - organizer must resolve",
        MatchOutcome.TEAM_ONE: f"{record.team_one.name} won",
        MatchOutcome.TEAM_TWO: f"{record.team_two.name} won",
        MatchOutcome.CANCELLED: "Cancelled",
        MatchOutcome.NO_CONTEST: "No contest",
    }
    embed = discord.Embed(
        title=f"Match Result - {record.match_id}",
        description=labels[record.outcome],
        color=0x2ECC71
        if record.outcome in {MatchOutcome.TEAM_ONE, MatchOutcome.TEAM_TWO}
        else 0xF1C40F,
    )
    embed.add_field(
        name=record.team_one.name,
        value=f"Captain <@{record.team_one.captain_id}>",
    )
    embed.add_field(
        name=record.team_two.name,
        value=f"Captain <@{record.team_two.captain_id}>",
    )
    embed.set_footer(text=f"match_id={record.match_id}")
    return embed


def match_id_from_interaction(interaction: discord.Interaction) -> str:
    """Recover the stable match id from a result card's footer."""
    embeds = getattr(interaction.message, "embeds", ())
    footer = embeds[0].footer.text if embeds and embeds[0].footer else ""
    if not footer.startswith("match_id="):
        raise ValueError("This result card is missing its stable identity.")
    return footer.removeprefix("match_id=")
