"""Shared command-routing seam (Issue #48, Phase 2)."""

import pytest

from utils.routing import CommandRegistry


async def _noop(message):
    return None


def test_register_and_resolve_is_case_insensitive():
    reg = CommandRegistry()
    reg.register(("R67",), _noop)
    assert reg.resolve("r67") is _noop
    assert reg.resolve("R67") is _noop


def test_unknown_token_resolves_to_none():
    reg = CommandRegistry()
    assert reg.resolve("nope") is None


def test_duplicate_registration_is_rejected():
    reg = CommandRegistry()
    reg.register(("dup",), _noop)
    with pytest.raises(ValueError):
        reg.register(("dup",), _noop)


def test_tokens_lists_all_registered():
    reg = CommandRegistry()
    reg.register(("a", "b"), _noop)
    reg.register(("c",), _noop)
    assert set(reg.tokens) == {"a", "b", "c"}


def test_bot_registers_expected_tokens():
    import bot

    for token in ("match", "bet", "wallet", "ledger", "r67"):
        assert bot.command_registry.resolve(token) is not None
