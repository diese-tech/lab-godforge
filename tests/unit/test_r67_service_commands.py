"""Command routing/permission coverage for R67Service (Issue #47, Gate 3/4)."""

import random

import pytest

from utils.r67 import service as service_mod
from utils.r67.repository import SQLiteR67Repository
from utils.r67.service import R67Service


@pytest.fixture()
def service(tmp_path):
    repo = SQLiteR67Repository(tmp_path / "r67.db")
    return R67Service(repo, rng=random.Random(0))


def _all_responses():
    from utils.r67.responses import POOLS

    out = set()
    for pool in POOLS.values():
        out.update(pool)
    return out


def test_bare_r67_returns_an_approved_response(service):
    reply = service.handle_command(1, "", can_manage_guild=False)
    assert reply in _all_responses()


def test_bare_r67_works_even_when_reactions_disabled(service):
    # Reactions default off; direct command must still respond.
    reply = service.handle_command(1, "", can_manage_guild=False)
    assert reply in _all_responses()


def test_status_reflects_disabled_by_default(service):
    reply = service.handle_command(1, "status", can_manage_guild=False)
    assert reply == service_mod.REACTIONS_DISABLED_REPLY


def test_enable_requires_manage_guild(service):
    denied = service.handle_command(1, "reactions on", can_manage_guild=False)
    assert denied == service_mod.PERMISSION_DENIED
    # State unchanged.
    assert service.status(1).reactions_enabled is False


def test_enable_and_disable_flow_with_permission(service):
    on = service.handle_command(1, "reactions on", can_manage_guild=True)
    assert on == service_mod.REACTIONS_ENABLED_REPLY
    assert service.status(1).reactions_enabled is True

    status = service.handle_command(1, "status", can_manage_guild=False)
    assert status == service_mod.REACTIONS_ENABLED_REPLY

    off = service.handle_command(1, "reactions off", can_manage_guild=True)
    assert off == service_mod.REACTIONS_DISABLED_REPLY
    assert service.status(1).reactions_enabled is False


def test_unknown_subcommand_is_reported(service):
    reply = service.handle_command(1, "wat", can_manage_guild=True)
    assert reply == service_mod.UNKNOWN_SUBCOMMAND


def test_reactions_config_is_guild_only(service):
    reply = service.handle_command(None, "reactions on", can_manage_guild=True)
    assert reply == service_mod.GUILD_ONLY
    status = service.handle_command(None, "status", can_manage_guild=True)
    assert status == service_mod.GUILD_ONLY


def test_status_copy_does_not_leak_hidden_mechanics(service):
    service.handle_command(1, "reactions on", can_manage_guild=True)
    enabled = service.handle_command(1, "status", can_manage_guild=False)
    disabled = service_mod.REACTIONS_DISABLED_REPLY
    for text in (enabled, disabled):
        lowered = text.lower()
        for leak in ("7%", "cooldown", "probability", "survivor", "minute", "heat"):
            assert leak not in lowered
