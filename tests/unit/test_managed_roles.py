from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from utils import managed_roles


class FakeRole:
    def __init__(self, role_id, name, position=1, *, default=False):
        self.id = role_id
        self.name = name
        self.position = position
        self._default = default

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, roles=(), *, manage_roles=True, top_position=100):
        self.roles_by_id = {role.id: role for role in roles}
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=FakeRole(999, "GodForge", top_position),
        )
        self.create_role = AsyncMock(side_effect=self._create)

    def get_role(self, role_id):
        return self.roles_by_id.get(role_id)

    async def _create(self, **kwargs):
        role = FakeRole(1000 + len(self.roles_by_id), kwargs["name"], 1)
        self.roles_by_id[role.id] = role
        return role


def test_preflight_requires_manage_roles():
    with pytest.raises(managed_roles.MissingManageRoles, match="Manage Roles"):
        managed_roles.preflight(FakeGuild(manage_roles=False))


def test_preflight_rejects_unmanageable_hierarchy():
    role = FakeRole(42, "Too High", 100)
    with pytest.raises(managed_roles.InvalidRoleHierarchy, match="above"):
        managed_roles.preflight(FakeGuild((role,), top_position=50), (role,))


@pytest.mark.asyncio
async def test_reconcile_reuses_stored_id_after_rename_and_never_searches_by_name():
    renamed = FakeRole(42, "Renamed By Admin", 2)
    same_name_unowned = FakeRole(43, "GodForge • Jungle", 2)
    guild = FakeGuild((renamed, same_name_unowned))

    result = await managed_roles.reconcile(
        guild,
        {"jungle": 42},
        enabled_keys=("jungle",),
    )

    assert result.role_ids == {"jungle": 42}
    assert result.created_keys == ()
    guild.create_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_creates_missing_role_with_zero_permissions():
    guild = FakeGuild((FakeRole(43, "GodForge • Solo", 2),))

    result = await managed_roles.reconcile(
        guild,
        {},
        enabled_keys=("solo",),
    )

    assert result.created_keys == ("solo",)
    assert result.role_ids["solo"] != 43
    kwargs = guild.create_role.await_args.kwargs
    assert kwargs["permissions"] == discord.Permissions.none()
    assert kwargs["mentionable"] is False
    assert kwargs["hoist"] is False


@pytest.mark.asyncio
async def test_reconcile_is_idempotent_when_returned_ids_are_persisted():
    guild = FakeGuild()
    first = await managed_roles.reconcile(guild, {}, enabled_keys=("solo", "mid"))
    second = await managed_roles.reconcile(
        guild, first.role_ids, enabled_keys=("solo", "mid")
    )

    assert second.role_ids == first.role_ids
    assert second.created_keys == ()
    assert guild.create_role.await_count == 2


@pytest.mark.asyncio
async def test_toggle_add_remove_and_noop_are_idempotent():
    role = FakeRole(42, "Any Name", 2)
    guild = FakeGuild((role,))
    member = SimpleNamespace(roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())

    assert await managed_roles.set_member_role(
        guild, member, "solo", True, {"solo": 42}
    )
    member.roles = [role]
    assert not await managed_roles.set_member_role(
        guild, member, "solo", True, {"solo": 42}
    )
    assert await managed_roles.set_member_role(
        guild, member, "solo", False, {"solo": 42}
    )
    member.roles = []
    assert not await managed_roles.set_member_role(
        guild, member, "solo", False, {"solo": 42}
    )

    member.add_roles.assert_awaited_once_with(
        role, reason="GodForge self-assigned cosmetic role"
    )
    member.remove_roles.assert_awaited_once_with(
        role, reason="GodForge self-assigned cosmetic role"
    )


@pytest.mark.asyncio
async def test_toggle_rejects_deleted_stored_role_without_name_adoption():
    same_name = FakeRole(99, "GodForge • Solo", 2)
    guild = FakeGuild((same_name,))
    member = SimpleNamespace(roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())

    with pytest.raises(managed_roles.ManagedRoleMissing, match="deleted"):
        await managed_roles.set_member_role(
            guild, member, "solo", True, {"solo": 42}
        )

    member.add_roles.assert_not_awaited()
