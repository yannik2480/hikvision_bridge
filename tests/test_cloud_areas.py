"""Tests for Hik-Connect cloud security area discovery, control and services."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelState,
)
from homeassistant.const import ATTR_DEVICE_ID, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_bridge.cloud.account import (
    CloudAccount,
    CloudAuthFailed,
    CloudError,
)
from custom_components.hikvision_bridge.cloud.actions import (
    create_area,
    delete_area,
    fetch_areas,
    fetch_devices,
    set_area_defence_mode,
    update_area,
)
from custom_components.hikvision_bridge.cloud.catalog import CloudArea, CloudDevice, parse_areas
from custom_components.hikvision_bridge.cloud_entry import CloudBridge
from custom_components.hikvision_bridge.const import (
    CONF_ACCOUNT,
    CONF_ENTRY_KIND,
    CONF_REGION_HOST,
    DOMAIN,
    KIND_CLOUD,
)

FETCH = "custom_components.hikvision_bridge.cloud_entry.fetch_devices"


def _cloud_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_KIND: KIND_CLOUD,
            CONF_ACCOUNT: "user@example.invalid",
            CONF_PASSWORD: "secret",
            CONF_REGION_HOST: "api.hik-connect.com",
        },
        title="Hik-Connect",
    )


def _sample_device(serial: str = "Q00000001", areas: tuple[CloudArea, ...] = ()) -> CloudDevice:
    return CloudDevice(
        serial=serial,
        full_serial=f"DS-0000TEST-X0020260101AAWR{serial}",
        name="Test NVR",
        model="DS-0000NVR-T8",
        firmware="V4.0.0",
        category="COMMON",
        online=True,
        channels=8,
        stream_encrypted=False,
        areas=areas,
    )


def test_parse_areas_valid() -> None:
    payload = {
        "meta": {"code": 200},
        "list": [
            {
                "groupId": 101,
                "groupDevSerial": "Q00000001",
                "groupName": "Area 1",
                "groupType": 2,
                "mode": 1,
            },
            {
                "groupId": 102,
                "groupDevSerial": "Q00000001",
                "groupName": "Area 2",
                "groupType": 2,
                "mode": 0,
            },
        ],
    }
    areas = parse_areas(payload, "Q00000001")
    assert len(areas) == 2
    assert areas[0].group_id == 101
    assert areas[0].name == "Area 1"
    assert areas[0].mode == 1
    assert areas[1].group_id == 102
    assert areas[1].name == "Area 2"
    assert areas[1].mode == 0


def test_parse_areas_malformed() -> None:
    assert parse_areas({}, "Q00000001") == []
    assert parse_areas({"list": "not a list"}, "Q00000001") == []
    assert parse_areas({"list": [{"no_group_id": 1}]}, "Q00000001") == []


async def test_fetch_areas_enriches_members() -> None:
    acct = CloudAccount(session=None, account="u", password="p")  # type: ignore[arg-type]
    list_reply = {
        "meta": {"code": 200},
        "list": [{"groupId": 100, "groupDevSerial": "DEV1", "groupName": "Area 1", "mode": 1}],
    }
    mem_reply = {
        "meta": {"code": 200},
        "list": [{"groupId": 100, "memberId": "cam1"}, {"groupId": 100, "memberId": "cam2"}],
    }
    req = AsyncMock(side_effect=[list_reply, mem_reply])
    acct.request = req  # type: ignore[method-assign]

    areas = await fetch_areas(acct, "DEV1")
    assert len(areas) == 1
    assert areas[0].group_id == 100
    assert areas[0].camera_ids == ("cam1", "cam2")


async def test_fetch_areas_keeps_members_and_previous() -> None:
    acct = CloudAccount(session=None, account="u", password="p")  # type: ignore[arg-type]
    old = CloudArea(group_id=100, device_serial="DEV1", name="Area 1", mode=0, camera_ids=("c",))
    list_reply = {
        "meta": {"code": 200},
        "list": [{"groupId": 100, "groupDevSerial": "DEV1", "groupName": "Area 1", "mode": 1}],
    }
    acct.request = AsyncMock(return_value=list_reply)  # type: ignore[method-assign]
    areas = await fetch_areas(acct, "DEV1", (old,), members=False)
    assert areas[0].mode == 1
    assert areas[0].camera_ids == ("c",)
    assert acct.request.call_count == 1  # no member request

    acct.request = AsyncMock(side_effect=CloudError("cloud_rejected"))  # type: ignore[method-assign]
    assert await fetch_areas(acct, "DEV1", (old,)) == [old]

    acct.request = AsyncMock(side_effect=CloudAuthFailed())  # type: ignore[method-assign]
    with pytest.raises(CloudAuthFailed):
        await fetch_areas(acct, "DEV1", (old,))


async def test_fetch_devices_reads_areas_of_recorders_only() -> None:
    acct = CloudAccount(session=None, account="u", password="p")  # type: ignore[arg-type]
    nvr = _sample_device("Q00000001")
    camera = replace(_sample_device("Q00000002"), channels=1)
    with (
        patch(
            "custom_components.hikvision_bridge.cloud.actions.parse_page",
            return_value=([nvr, camera], False),
        ),
        patch(
            "custom_components.hikvision_bridge.cloud.actions.fetch_areas",
            AsyncMock(return_value=[]),
        ) as areas,
    ):
        acct.request = AsyncMock(return_value={})  # type: ignore[method-assign]
        await fetch_devices(acct)
    areas.assert_called_once()
    assert areas.call_args.args[1] == "Q00000001"


async def test_update_area_restores_previous_on_failure() -> None:
    acct = CloudAccount(session=None, account="u", password="p")  # type: ignore[arg-type]
    old = CloudArea(group_id=100, device_serial="DEV1", name="Old", mode=0, camera_ids=("c1",))
    acct.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{}, CloudError("cloud_rejected"), {"groupInfo": {"groupId": 101}}]
    )
    with pytest.raises(CloudError):
        await update_area(acct, "DEV1", 100, "New", ["c2"], old)
    assert acct.request.call_args_list[-1].kwargs["json_data"] == {
        "groupName": "Old",
        "resourceIds": ["c1"],
    }


async def test_area_actions() -> None:
    acct = CloudAccount(session=None, account="u", password="p")  # type: ignore[arg-type]
    req = AsyncMock()
    acct.request = req  # type: ignore[method-assign]

    # set mode
    await set_area_defence_mode(acct, "DEV1", 100, 2)
    req.assert_called_with(
        "POST",
        "/v3/devices/group/DEV1/switchDefenceMode",
        json_data={"groupId": 100, "mode": 2},
    )

    # create area
    req.reset_mock()
    req.return_value = {"groupInfo": {"groupId": 105}}
    new_id = await create_area(acct, "DEV1", "New Area", ["c1", "c2"])
    assert new_id == 105
    req.assert_called_with(
        "POST",
        "/v3/devices/group/DEV1",
        json_data={"groupName": "New Area", "resourceIds": ["c1", "c2"]},
    )

    # delete area
    req.reset_mock()
    await delete_area(acct, "DEV1", 100)
    req.assert_called_with("DELETE", "/v3/devices/group/DEV1/100")

    # update area (delete then recreate)
    req.reset_mock()
    req.return_value = {"groupInfo": {"groupId": 106}}
    updated_id = await update_area(acct, "DEV1", 100, "New Name", ["c1"])
    assert updated_id == 106
    assert req.call_count == 2


async def test_alarm_control_panel_entity_and_modes(hass: HomeAssistant) -> None:
    area1 = CloudArea(
        group_id=101,
        device_serial="Q00000001",
        name="Area 1",
        mode=1,
        camera_ids=("cam1",),
    )
    area2 = CloudArea(
        group_id=102,
        device_serial="Q00000001",
        name="Area 2",
        mode=2,
        camera_ids=("cam2", "cam3"),
    )
    device = _sample_device("Q00000001", areas=(area1, area2))

    entry = _cloud_entry()
    entry.add_to_hass(hass)
    with patch(FETCH, AsyncMock(return_value={device.key: device})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        entities = {
            e.unique_id: e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        }

        area1_uid = f"cloud_{device.key}_area_101"
        area2_uid = f"cloud_{device.key}_area_102"
        assert area1_uid in entities
        assert area2_uid in entities

        area1_entity_id = entities[area1_uid].entity_id
        area2_entity_id = entities[area2_uid].entity_id

        # Check initial states
        s1 = hass.states.get(area1_entity_id)
        assert s1.state == AlarmControlPanelState.ARMED_AWAY
        assert s1.attributes["group_id"] == 101
        assert s1.attributes["camera_ids"] == ["cam1"]
        assert s1.attributes["camera_count"] == 1

        s2 = hass.states.get(area2_entity_id)
        assert s2.state == AlarmControlPanelState.ARMED_HOME
        assert s2.attributes["camera_count"] == 2

        # Test actions
        bridge: CloudBridge = entry.runtime_data
        with patch(
            "custom_components.hikvision_bridge.cloud_entry.set_area_defence_mode",
            AsyncMock(),
        ) as mock_set:
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_disarm",
                {"entity_id": area1_entity_id},
                blocking=True,
            )
            mock_set.assert_called_once_with(bridge.account, "Q00000001", 101, 0)

        with patch(
            "custom_components.hikvision_bridge.cloud_entry.set_area_defence_mode",
            AsyncMock(),
        ) as mock_set:
            await hass.services.async_call(
                "alarm_control_panel",
                "alarm_arm_home",
                {"entity_id": area1_entity_id},
                blocking=True,
            )
            mock_set.assert_called_once_with(bridge.account, "Q00000001", 101, 2)

    # An area deleted in the cloud drops its entity.
    with patch(FETCH, AsyncMock(return_value={device.key: replace(device, areas=(area1,))})):
        await bridge.refresher.async_refresh()
        await hass.async_block_till_done()
    assert registry.async_get(area2_entity_id) is None
    assert registry.async_get(area1_entity_id) is not None


async def test_cloud_area_services(hass: HomeAssistant) -> None:
    device = _sample_device("Q00000001")
    entry = _cloud_entry()
    entry.add_to_hass(hass)
    with patch(FETCH, AsyncMock(return_value={device.key: device})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Get device registry ID from registered entities
        registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        cloud_device_id = entries[0].device_id

        # Test cloud_create_area service
        with patch(
            "custom_components.hikvision_bridge.cloud.actions.create_area",
            AsyncMock(return_value=999),
        ) as mock_create:
            res = await hass.services.async_call(
                DOMAIN,
                "cloud_create_area",
                {
                    ATTR_DEVICE_ID: cloud_device_id,
                    "group_name": "Test Area",
                    "resource_ids": ["c1", "c2"],
                },
                blocking=True,
                return_response=True,
            )
            assert res == {"group_id": 999}
            mock_create.assert_called_once()

        # Test cloud_update_area service
        with patch(
            "custom_components.hikvision_bridge.cloud.actions.update_area",
            AsyncMock(return_value=1000),
        ) as mock_update:
            res = await hass.services.async_call(
                DOMAIN,
                "cloud_update_area",
                {
                    "device_serial": "Q00000001",
                    "group_id": 999,
                    "group_name": "Test Area Updated",
                    "resource_ids": ["c1"],
                },
                blocking=True,
                return_response=True,
            )
            assert res == {"group_id": 1000}
            mock_update.assert_called_once()

        # Test cloud_delete_area service
        with patch(
            "custom_components.hikvision_bridge.cloud.actions.delete_area",
            AsyncMock(),
        ) as mock_delete:
            await hass.services.async_call(
                DOMAIN,
                "cloud_delete_area",
                {
                    "device_serial": "Q00000001",
                    "group_id": 1000,
                },
                blocking=True,
            )
            mock_delete.assert_called_once()
