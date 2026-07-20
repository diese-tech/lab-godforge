import pytest

from utils import settings


def test_managed_resource_ids_round_trip(tmp_settings):
    saved = settings.update_guild_settings(
        "123",
        {
            "managed": {
                "playChannelId": "1001",
                "playMessageId": 1002,
                "roomCategoryId": "1005",
                "rolePanelChannelId": "1003",
                "rolePanelMessageId": "1004",
                "roleIds": {
                    "jungle": "2001",
                    "mid": 2002,
                    "unknown": "9999",
                },
                "testMode": True,
            }
        },
        "setup:42",
    )

    assert saved["managed"]["playChannelId"] == "1001"
    assert saved["managed"]["playMessageId"] == "1002"
    assert saved["managed"]["roomCategoryId"] == "1005"
    assert saved["managed"]["testMode"] is True
    assert saved["managed"]["roleIds"]["jungle"] == "2001"
    assert saved["managed"]["roleIds"]["mid"] == "2002"
    assert "unknown" not in saved["managed"]["roleIds"]
    assert settings.get_guild_settings("123")["managed"] == saved["managed"]


@pytest.mark.parametrize("bad_id", ["abc", "-1", "1.5", "1" * 21])
def test_managed_resource_ids_reject_invalid_values(tmp_settings, bad_id):
    with pytest.raises(ValueError, match="Invalid playChannelId"):
        settings.update_guild_settings(
            "123",
            {"managed": {"playChannelId": bad_id}},
        )


def test_managed_resource_id_can_be_cleared(tmp_settings):
    settings.update_guild_settings(
        "123",
        {"managed": {"roleIds": {"solo": "2005"}}},
    )

    saved = settings.update_guild_settings(
        "123",
        {"managed": {"roleIds": {"solo": ""}}},
    )

    assert saved["managed"]["roleIds"]["solo"] == ""
