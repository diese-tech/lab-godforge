"""Discord adapter for the cosmetic `67 Survivor` role.

This module is the only place in the r67 feature that touches Discord role
objects. It finds or creates the reusable ``67 Survivor`` role, validates that
GodForge can actually manage it, and assigns/removes it. It contains no
event-detection logic (that lives in ``tracker.py`` / ``service.py``).

Behavior locked in Issue #47 (Gate 3/5):

- Reuse an existing ``67 Survivor`` role; create it only when absent and
  permissions allow. The role is cosmetic: no permissions, not hoisted, not
  mentionable.
- Assignment requires ``Manage Roles`` and correct role hierarchy; when either
  is missing the caller still announces the event but reports it unmarked.
- Removal treats a already-deleted role or departed member as success so a
  stored grant can be cleared.
"""

from __future__ import annotations

import discord

ROLE_NAME = "67 Survivor"
CREATE_REASON = "GodForge 67 Survivor event"
REMOVE_REASON = "GodForge 67 Survivor role expired"


class SurvivorRoleError(RuntimeError):
    """A recoverable Survivor-role failure (missing permission or hierarchy)."""


def find_role(guild) -> discord.Role | None:
    """Return the existing ``67 Survivor`` role for *guild*, if any."""
    for role in getattr(guild, "roles", ()):
        if getattr(role, "name", None) == ROLE_NAME:
            return role
    return None


def _bot_member(guild):
    member = getattr(guild, "me", None)
    if member is None:
        raise SurvivorRoleError("GodForge could not resolve its guild member.")
    return member


def can_manage_roles(guild) -> bool:
    member = _bot_member(guild)
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and getattr(perms, "manage_roles", False))


def can_assign(guild, role) -> bool:
    """True if GodForge has permission and hierarchy to assign *role*."""
    if role is None or not can_manage_roles(guild):
        return False
    if getattr(role, "is_default", lambda: False)():
        return False
    top_role = getattr(_bot_member(guild), "top_role", None)
    if top_role is None:
        return False
    return getattr(role, "position", 0) < getattr(top_role, "position", -1)


async def ensure_role(guild) -> discord.Role:
    """Return the reusable ``67 Survivor`` role, creating it when absent.

    Raises :class:`SurvivorRoleError` when the role is missing and GodForge
    cannot create it.
    """
    existing = find_role(guild)
    if existing is not None:
        return existing
    if not can_manage_roles(guild):
        raise SurvivorRoleError("GodForge needs the Manage Roles permission.")
    return await guild.create_role(
        name=ROLE_NAME,
        permissions=discord.Permissions.none(),
        mentionable=False,
        hoist=False,
        reason=CREATE_REASON,
    )


async def assign(guild, role, user_ids) -> list[int]:
    """Assign *role* to each resolvable member; return the ids actually granted."""
    granted: list[int] = []
    for user_id in user_ids:
        member = guild.get_member(user_id)
        if member is None:
            continue
        try:
            await member.add_roles(role, reason=CREATE_REASON)
        except (discord.Forbidden, discord.HTTPException):
            continue
        granted.append(user_id)
    return granted


async def remove_member_role(guild, role_id: int, user_id: int) -> None:
    """Remove the granted role from a member.

    A deleted role or departed member is treated as success (nothing to undo).
    Transient Discord failures raise :class:`SurvivorRoleError` so the caller can
    retry later without losing the grant record.
    """
    role = guild.get_role(role_id)
    member = guild.get_member(user_id)
    if role is None or member is None:
        return
    try:
        await member.remove_roles(role, reason=REMOVE_REASON)
    except discord.Forbidden as exc:
        raise SurvivorRoleError("GodForge lost permission to remove the role.") from exc
    except discord.HTTPException as exc:
        raise SurvivorRoleError("Discord failed to remove the role.") from exc
