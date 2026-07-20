"""Shared feature lifecycle registry (Issue #48)."""

import random
from unittest.mock import MagicMock

import pytest

from utils.lifecycle import FeatureModule, FeatureRegistry, LifecycleContext
from utils.r67.feature import R67Feature
from utils.r67.repository import RoleGrant, SQLiteR67Repository
from utils.r67.service import R67Service
from utils.party import utc_now
from datetime import timedelta
from unittest.mock import AsyncMock


class _RecordingFeature:
    def __init__(self, name):
        self.name = name
        self.started = 0
        self.cleaned = 0

    async def on_startup(self, ctx):
        self.started += 1

    async def on_cleanup(self, ctx):
        self.cleaned += 1


class _BoomFeature:
    name = "boom"

    async def on_startup(self, ctx):
        raise RuntimeError("startup boom")

    async def on_cleanup(self, ctx):
        raise RuntimeError("cleanup boom")


def _ctx():
    return LifecycleContext(get_guild=lambda gid: None)


async def test_registry_runs_all_features():
    reg = FeatureRegistry()
    a, b = _RecordingFeature("a"), _RecordingFeature("b")
    reg.register(a)
    reg.register(b)
    await reg.run_startup(_ctx())
    await reg.run_cleanup(_ctx())
    assert (a.started, a.cleaned) == (1, 1)
    assert (b.started, b.cleaned) == (1, 1)


async def test_one_feature_failure_does_not_block_others():
    reg = FeatureRegistry()
    boom = _BoomFeature()
    ok = _RecordingFeature("ok")
    reg.register(boom)
    reg.register(ok)
    # Must not raise; the healthy feature still runs.
    await reg.run_startup(_ctx())
    await reg.run_cleanup(_ctx())
    assert ok.started == 1
    assert ok.cleaned == 1


def test_registry_exposes_registered_features():
    reg = FeatureRegistry()
    f = _RecordingFeature("x")
    reg.register(f)
    assert reg.features == (f,)


def test_r67_feature_satisfies_protocol(tmp_path):
    service = R67Service(SQLiteR67Repository(tmp_path / "r67.db"))
    feature = R67Feature(service)
    assert isinstance(feature, FeatureModule)
    assert feature.name == "r67"


async def test_r67_feature_cleans_expired_grants(tmp_path):
    repo = SQLiteR67Repository(tmp_path / "r67.db")
    service = R67Service(repo, rng=random.Random(0))
    now = utc_now()
    repo.add_role_grants(
        [
            RoleGrant(
                guild_id=1,
                user_id=5,
                role_id=999,
                expires_at=now - timedelta(minutes=1),
                created_at=now - timedelta(minutes=68),
            )
        ]
    )
    role = MagicMock()
    role.id = 999
    guild = MagicMock()
    guild.get_role = lambda rid: role
    guild.get_member = lambda uid: MagicMock(remove_roles=AsyncMock())

    feature = R67Feature(service)
    await feature.on_cleanup(LifecycleContext(get_guild=lambda gid: guild))

    assert repo.all_role_grants() == []
