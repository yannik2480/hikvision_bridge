"""Alarm IO and the read-modify-write-verify helper."""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.model.features import VerifyMismatch, io, patch_and_verify
from custom_components.hikvision_bridge.model.inventory import IoPort
from custom_components.hikvision_bridge.wire.faults import Unsupported
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from tests.fakedevice import FakeDevice

pytestmark = pytest.mark.usefixtures("socket_enabled")

DOC = '<?xml version="1.0"?><Thing xmlns="urn:x"><enabled>false</enabled><keep>1</keep></Thing>'
IO_STATE = "<IOPortStatus><ioPortID>1</ioPortID><ioState>{}</ioState></IOPortStatus>"


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


@pytest.mark.parametrize(("raw", "expected"), [("active", True), ("low", False), ("odd", None)])
async def test_io_state(rig, raw: str, expected: bool | None) -> None:
    device = FakeDevice(overrides={"/ISAPI/System/IO/inputs/1/status": (200, IO_STATE.format(raw))})
    link = await rig(device)
    port = IoPort(port_id="1", output=False, name=None, channel_id=None, proxied=False)
    assert await io.read_state(link, port) is expected


async def test_io_output_command(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    port = IoPort(port_id="2", output=True, name=None, channel_id=None, proxied=False)
    await io.set_output(link, port, True)
    method, path, body = device.writes[0]
    assert (method, path) == ("PUT", "/ISAPI/System/IO/outputs/2/trigger")
    assert "<outputState>high</outputState>" in body
    assert io.parse_ports(None, output=True) == {}


async def test_patch_keeps_document_and_verifies(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Thing": DOC})
    link = await rig(device)
    await patch_and_verify(link, "/ISAPI/Thing", {"enabled": "true"})
    assert device.documents["/ISAPI/Thing"] == DOC.replace("false", "true")


async def test_patch_detects_value_not_taken(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Thing": DOC})
    original_handle = device.handle

    async def ignore_writes(request):
        response = await original_handle(request)
        device.documents["/ISAPI/Thing"] = DOC  # device silently keeps the old value
        return response

    device.handle = ignore_writes
    link = await rig(device)
    with pytest.raises(VerifyMismatch) as caught:
        await patch_and_verify(link, "/ISAPI/Thing", {"enabled": "true"})
    assert caught.value.element == "enabled"


HOST = "<HttpHostNotification><url>/</url><portNo>80</portNo></HttpHostNotification>"


async def test_patch_accepts_path_read_back_as_full_address(rig) -> None:
    # Door stations read the notification path back with scheme and host in front.
    device = FakeDevice(documents={"/ISAPI/Host": HOST})
    original_handle = device.handle

    async def expand_url(request):
        response = await original_handle(request)
        device.documents["/ISAPI/Host"] = device.documents["/ISAPI/Host"].replace(
            "<url>/api/", "<url>http://192.0.2.5:8123/api/"
        )
        return response

    device.handle = expand_url
    link = await rig(device)
    await patch_and_verify(link, "/ISAPI/Host", {"url": "/api/webhook/abc", "portNo": "8123"})


async def test_patch_detects_path_not_taken(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Host": HOST})
    original_handle = device.handle

    async def keep_old(request):
        response = await original_handle(request)
        device.documents["/ISAPI/Host"] = HOST.replace("<url>/<", "<url>http://192.0.2.5/<")
        return response

    device.handle = keep_old
    link = await rig(device)
    with pytest.raises(VerifyMismatch) as caught:
        await patch_and_verify(link, "/ISAPI/Host", {"url": "/api/webhook/abc"})
    assert caught.value.element == "url"


async def test_patch_falls_back_when_element_missing(rig) -> None:
    device = FakeDevice(
        documents={"/ISAPI/Thing": "<Thing><other>1</other></Thing>", "/ISAPI/Thing/alt": DOC}
    )
    link = await rig(device)
    with pytest.raises(Unsupported):
        await patch_and_verify(link, "/ISAPI/Thing", {"enabled": "true"})
    await patch_and_verify(
        link, "/ISAPI/Thing", {"enabled": "true"}, fallback_path="/ISAPI/Thing/alt"
    )
    assert "<enabled>true</enabled>" in device.documents["/ISAPI/Thing/alt"]


HOST_WITH_PROTOCOL = (
    "<HttpHostNotification><url>/</url><protocolType>HTTP</protocolType>"
    "<portNo>80</portNo></HttpHostNotification>"
)


def _device_keeps(device: FakeDevice, path: str, keep) -> None:
    """Store something other than what is written to ``path``, as some devices do."""
    original = device.handle

    async def answer(request):
        response = await original(request)
        if request.method == "PUT" and request.path == path:
            device.documents[path] = keep(device.documents[path])
        return response

    device.handle = answer


async def test_patch_accepts_protocol_in_other_case(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Host": HOST_WITH_PROTOCOL})
    _device_keeps(device, "/ISAPI/Host", lambda text: text.replace("HTTP<", "http<"))
    link = await rig(device)
    await patch_and_verify(link, "/ISAPI/Host", {"protocolType": "HTTP", "portNo": "8123"})


async def test_patch_skips_element_the_device_does_not_echo(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Host": HOST_WITH_PROTOCOL})
    _device_keeps(
        device,
        "/ISAPI/Host",
        lambda text: text.replace("<protocolType>HTTP</protocolType>", ""),
    )
    link = await rig(device)
    await patch_and_verify(link, "/ISAPI/Host", {"protocolType": "HTTP", "portNo": "8123"})
    assert "<portNo>8123</portNo>" in device.documents["/ISAPI/Host"]


async def test_patch_names_protocol_the_device_kept(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Host": HOST_WITH_PROTOCOL})
    _device_keeps(device, "/ISAPI/Host", lambda text: text.replace("HTTP<", "HTTPS<"))
    link = await rig(device)
    with pytest.raises(VerifyMismatch) as caught:
        await patch_and_verify(link, "/ISAPI/Host", {"protocolType": "HTTP"})
    assert caught.value.element == "protocolType"
    assert caught.value.actual == "HTTPS"
    assert "HTTPS" in str(caught.value)


async def test_patch_hides_value_of_webhook_address(rig) -> None:
    device = FakeDevice(documents={"/ISAPI/Host": HOST_WITH_PROTOCOL})
    _device_keeps(device, "/ISAPI/Host", lambda _text: HOST_WITH_PROTOCOL)
    link = await rig(device)
    with pytest.raises(VerifyMismatch) as caught:
        await patch_and_verify(link, "/ISAPI/Host", {"url": "/api/webhook/secret-id"})
    assert caught.value.element == "url"
    assert caught.value.actual is None
    assert "secret-id" not in str(caught.value)
