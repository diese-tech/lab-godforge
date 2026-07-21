"""DraftRenderer orchestration (Issue #48, Phase 3c)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.draft_render import DraftRenderer


def _renderer(tracked=None):
    formatter = MagicMock()
    formatter.format_draft_board.return_value = "BOARD"
    formatter.format_claim_embed.return_value = "CLAIM"
    return (
        DraftRenderer(
            formatter=formatter,
            tracked_messages=tracked if tracked is not None else {},
            number_emojis=["1️⃣", "2️⃣"],
        ),
        formatter,
    )


def _game():
    return SimpleNamespace(
        picks={"blue": ["A"], "red": ["B"]},
        claims={"blue": [], "red": []},
        game_number=1,
    )


def _draft():
    return SimpleNamespace(
        board_message_id=None,
        claim_message_ids={},
        draft_id="d1",
        draft_sequence=1,
        forgelens_match_id="",
        current_game=_game(),
    )


async def test_update_board_posts_new_when_no_message():
    renderer, _ = _renderer()
    draft = _draft()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=555))

    await renderer.update_board(draft, channel)

    channel.send.assert_awaited_once()
    assert draft.board_message_id == 555


async def test_update_board_edits_existing_message():
    renderer, _ = _renderer()
    draft = _draft()
    draft.board_message_id = 777
    edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=SimpleNamespace(edit=edit))
    channel.send = AsyncMock()

    await renderer.update_board(draft, channel)

    edit.assert_awaited_once()
    channel.send.assert_not_awaited()


async def test_post_claim_embeds_tracks_and_reacts():
    tracked = {}
    renderer, _ = _renderer(tracked=tracked)
    draft = _draft()
    sent = SimpleNamespace(id=42, add_reaction=AsyncMock())
    channel = MagicMock()
    channel.id = 9
    channel.send = AsyncMock(return_value=sent)

    await renderer.post_claim_embeds(draft, channel)

    # Both teams posted; last one recorded under its message id.
    assert channel.send.await_count == 2
    assert tracked[42]["kind"] == "claim"
    assert draft.claim_message_ids["red"] == 42
    # Each embed got a reaction per number emoji (2 teams x 2 emojis).
    assert sent.add_reaction.await_count == 4
