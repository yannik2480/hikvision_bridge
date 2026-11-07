"""Hik-Connect account handling, device list parsing and the cloud entry."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.hikvision_bridge.cloud import account as account_module
from custom_components.hikvision_bridge.cloud.account import (
    CaptchaRequired,
    CloudAccount,
    CloudAuthFailed,
)
from custom_components.hikvision_bridge.cloud.catalog import CloudDevice, parse_page
from custom_components.hikvision_bridge.const import (
    CLOUD_INTERVAL,
    CONF_ACCOUNT,
    CONF_ENTRY_KIND,
    CONF_REGION_HOST,
    DOMAIN,
    KIND_CLOUD,
)

# Shape mirrors the live response (keys only); every value is invented.
PAGE = {
    "meta": {"code": 200},
    "page": {"offset": 0, "limit": 50, "totalCount": 2},
    "deviceInfos": [
        {
            "deviceSerial": "Q00000001",
            "fullSerial": "DS-0000TEST-X0020260101AAWRQ00000001",
            "name": "Garage NVR",
            "deviceType": "DS-0000NVR-T8",
            "version": "V4.0.0",
            "deviceCategory": "COMMON",
            "status": 1,
            "channelNumber": 8,
        },
        {
            "deviceSerial": "Q00000002",
            "fullSerial": "DS-KV0000-TEST20260101Q00000002",
            "name": "Gate",
            "deviceType": "DS-KV6113-WPE1",
            "deviceCategory": "VIS",
            "status": 0,
            "channelNumber": 1,
        },
    ],
    "statusInfos": {
        "Q00000001": {"isEncrypt": 1, "encryptPwd": "never-copied"},
        "Q00000002": {"isEncrypt": 0},
    },
    "connectionInfos": {"Q00000001": {"localIp": "10.0.0.1", "wanIp": "198.51.100.1"}},
}


def test_parse_page_keeps_only_what_is_shown() -> None:
    devices, more = parse_page(PAGE)
    assert not more
    nvr, gate = devices
    assert nvr.online
    assert nvr.stream_encrypted is True
    assert not nvr.is_intercom
    assert gate.is_intercom
    assert gate.online is False
    text = repr(devices)
    for hidden in ("never-copied", "10.0.0.1", "198.51.100.1"):
        assert hidden not in text


def test_parse_page_tolerates_unknown_shapes() -> None:
    assert parse_page({}) == ([], False)
    assert parse_page({"deviceInfos": "x", "statusInfos": []}) == ([], False)
    devices, more = parse_page({"deviceInfos": [{"name": "no serial"}], "page": {"hasNext": True}})
    assert devices == []
    assert more


def _account(responses: list[dict]) -> tuple[CloudAccount, AsyncMock]:
    acct = CloudAccount(session=None, account="a@example.invalid", password="pw")  # type: ignore[arg-type]
    send = AsyncMock(side_effect=responses)
    acct._exchange = send
    return acct, send


async def test_login_follows_one_region_redirect() -> None:
    acct, send = _account(
        [
            {"meta": {"code": 1100}, "loginArea": {"apiDomain": "region.hik-connect.com"}},
            {
                "meta": {"code": 200},
                "loginArea": {"apiDomain": "region.hik-connect.com"},
                "loginSession": {"sessionId": "s1", "rfSessionId": "r1"},
            },
        ]
    )
    await acct.login()
    assert acct.region_host == "region.hik-connect.com"
    assert send.await_count == 2


@pytest.mark.parametrize(
    ("code", "error"),
    [(1013, CloudAuthFailed), (1015, CaptchaRequired)],
)
async def test_login_rejection_is_not_retried(code: int, error: type[Exception]) -> None:
    acct, send = _account([{"meta": {"code": code}}])
    with pytest.raises(error):
        await acct.login()
    assert send.await_count == 1


def test_account_secrets_are_masked_in_logs() -> None:
    CloudAccount(session=None, account="masked@example.invalid", password="hidden-pass")  # type: ignore[arg-type]
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x %s y", ("hidden-pass",), None)
    assert account_module.LOG_SCRUBBER.filter(record)
    assert "hidden-pass" not in record.getMessage()


def _cloud_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENTRY_KIND: KIND_CLOUD,
            CONF_ACCOUNT: "a@example.invalid",
            CONF_PASSWORD: "pw",
            CONF_REGION_HOST: "region.hik-connect.com",
        },
        unique_id="cloud_test",
        title="a@example.invalid",
    )


FETCH = "custom_components.hikvision_bridge.cloud_entry.fetch_devices"


def _devices(online: bool = True) -> dict[str, CloudDevice]:
    devices, _ = parse_page(PAGE)
    nvr, gate = devices
    if not online:
        nvr = replace(nvr, online=False)
    return {nvr.key: nvr, gate.key: gate}


async def test_cloud_entry_entities_and_transitions(hass: HomeAssistant) -> None:
    # A locally configured recorder whose serial contains the cloud short serial.
    # Its entry is disabled so that it is not set up here.
    local = MockConfigEntry(
        domain=DOMAIN, data={}, title="local", disabled_by=ConfigEntryDisabler.USER
    )
    local.add_to_hass(hass)
    local_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=local.entry_id,
        identifiers={(DOMAIN, "ds0000testx0020260101aawrq00000001xyz")},
        name="Garage NVR",
    )

    entry = _cloud_entry()
    entry.add_to_hass(hass)
    with patch(FETCH, AsyncMock(return_value=_devices())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    entities = {e.unique_id: e for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    nvr_key = next(k for k in _devices() if "q00000001" in k)
    online_id = entities[f"cloud_{nvr_key}_online"].entity_id
    encryption_id = entities[f"cloud_{nvr_key}_stream_encryption"].entity_id
    assert hass.states.get(online_id).state == STATE_ON
    assert hass.states.get(encryption_id).state == STATE_ON
    # door button only for the intercom
    buttons = [u for u in entities if u.endswith("_unlock")]
    assert len(buttons) == 1
    assert "q00000002" in buttons[0]

    cloud_device = dr.async_get(hass).async_get(entities[f"cloud_{nvr_key}_online"].device_id)
    assert cloud_device.via_device_id == local_device.id

    event_id = entities[f"cloud_{nvr_key}_connection"].entity_id
    with patch(FETCH, AsyncMock(return_value=_devices(online=False))):
        async_fire_time_changed(hass, dt_util.utcnow() + CLOUD_INTERVAL + timedelta(seconds=1))
        await hass.async_block_till_done()
    assert hass.states.get(online_id).state == STATE_OFF
    assert hass.states.get(event_id).attributes["event_type"] == "went_offline"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cloud_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    entry = _cloud_entry()
    entry.add_to_hass(hass)
    with patch(FETCH, AsyncMock(side_effect=CloudAuthFailed())):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
