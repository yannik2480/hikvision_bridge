"""Tests for intercom capabilities, call status, and remote door control."""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.model.features import intercom
from custom_components.hikvision_bridge.model.vocabulary import Kind, canonical
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from custom_components.hikvision_bridge.wire.xmlview import parse
from tests.fakedevice import FakeDevice

pytestmark = pytest.mark.usefixtures("socket_enabled")

INTERCOM_CAPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<VideoIntercomCap version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<isSupportCallStatus>true</isSupportCallStatus>"
    "<isSupportRemoteOpenDoor>true</isSupportRemoteOpenDoor>"
    "</VideoIntercomCap>"
)

DOOR_CAPS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    '<doorNo min="1" max="1"/>'
    '<cmd opt="open,close,alwaysOpen,resume"/>'
    "</RemoteControlDoor>"
)

DOOR_CAPS_MULTI = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    '<doorNo min="1" max="2"/>'
    '<cmd opt="open,close"/>'
    "</RemoteControlDoor>"
)


@pytest.fixture
async def rig():
    opened = []

    async def make(device: FakeDevice) -> IsapiLink:
        server = TestServer(device.app())
        await server.start_server()
        session = aiohttp.ClientSession()
        opened.append((server, session))
        return IsapiLink(
            session, Endpoint(server.host, server.port), device.username, device.password
        )

    yield make
    for server, session in opened:
        await session.close()
        await server.close()


def test_parse_capabilities_none():
    assert intercom.parse_capabilities(None, None) is None


def test_parse_capabilities():
    intercom_doc = parse(INTERCOM_CAPS)
    door_doc = parse(DOOR_CAPS)
    caps = intercom.parse_capabilities(intercom_doc, door_doc)
    assert caps is not None
    assert caps.has_call_status is True
    assert caps.doors == (1,)


def test_parse_capabilities_multi_door():
    door_doc = parse(DOOR_CAPS_MULTI)
    caps = intercom.parse_capabilities(None, door_doc)
    assert caps is not None
    assert caps.has_call_status is False
    assert caps.doors == (1, 2)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "idle"),
        ("", "idle"),
        ("idle", "idle"),
        ("ring", "ringing"),
        ("ringing", "ringing"),
        ("RING", "ringing"),
        ("oncall", "on_call"),
        ("on_call", "on_call"),
        ("other", "idle"),
    ],
)
def test_canonical_call_status(raw: str | None, expected: str):
    assert intercom.canonical_call_status(raw) == expected


def test_vocabulary_doorbell():
    assert canonical("doorbell") == Kind.DOORBELL
    assert canonical("ring") == Kind.DOORBELL
    assert canonical("ringcall") == Kind.DOORBELL
    assert canonical("bell") == Kind.DOORBELL
    assert canonical("callstatus") == Kind.OTHER
    assert canonical("videointercom") == Kind.OTHER
    assert canonical("intercom") == Kind.OTHER


async def test_read_capabilities(rig):
    device = FakeDevice(
        overrides={
            "/ISAPI/VideoIntercom/capabilities": (200, INTERCOM_CAPS),
            "/ISAPI/AccessControl/RemoteControl/door/capabilities": (200, DOOR_CAPS),
        }
    )
    link = await rig(device)
    caps = await intercom.read_capabilities(link)
    assert caps is not None
    assert caps.has_call_status is True
    assert caps.doors == (1,)


INTERCOM_CAPS_NO_CALL_FLAG = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<VideoIntercomCap version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<isSupportRemoteOpenDoor>true</isSupportRemoteOpenDoor>"
    "<isSupportWorkStatus>true</isSupportWorkStatus>"
    "</VideoIntercomCap>"
)

CALL_STATUS_PATH = "/ISAPI/VideoIntercom/callStatus?format=json"
CALL_STATUS_IDLE = (200, '{"CallStatus": {"status": "idle"}}')


async def test_read_capabilities_probes_call_status_without_flag(rig):
    device = FakeDevice(
        overrides={
            "/ISAPI/VideoIntercom/capabilities": (200, INTERCOM_CAPS_NO_CALL_FLAG),
            CALL_STATUS_PATH: CALL_STATUS_IDLE,
        }
    )
    caps = await intercom.read_capabilities(await rig(device))
    assert caps is not None
    assert caps.has_call_status is True
    assert caps.doors == (1,)


async def test_read_capabilities_without_flag_or_endpoint(rig):
    device = FakeDevice(
        overrides={
            "/ISAPI/VideoIntercom/capabilities": (200, INTERCOM_CAPS_NO_CALL_FLAG),
            CALL_STATUS_PATH: (404, ""),
        }
    )
    caps = await intercom.read_capabilities(await rig(device))
    assert caps is not None
    assert caps.has_call_status is False


async def test_read_capabilities_respects_explicit_false(rig):
    device = FakeDevice(
        overrides={
            "/ISAPI/VideoIntercom/capabilities": (
                200,
                INTERCOM_CAPS.replace("<isSupportCallStatus>true", "<isSupportCallStatus>false"),
            ),
            CALL_STATUS_PATH: CALL_STATUS_IDLE,
        }
    )
    caps = await intercom.read_capabilities(await rig(device))
    assert caps is not None
    assert caps.has_call_status is False


async def test_read_call_status(rig):
    device = FakeDevice(
        overrides={
            "/ISAPI/VideoIntercom/callStatus?format=json": (
                200,
                '{"CallStatus": {"status": "ring"}}',
            ),
        }
    )
    link = await rig(device)
    status = await intercom.read_call_status(link)
    assert status == "ringing"


async def test_remote_open_door(rig):
    device = FakeDevice()
    link = await rig(device)
    await intercom.remote_open_door(link, door_no=1)
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", "/ISAPI/AccessControl/RemoteControl/door/1")
    assert "<cmd>open</cmd>" in body
