"""The link never sends the password readable: no basic over HTTP, no downgrade after digest."""

from __future__ import annotations

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.wire import faults, link as link_module
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink

pytestmark = pytest.mark.usefixtures("socket_enabled")

INFO = b'<?xml version="1.0"?><DeviceInfo><deviceName>x</deviceName></DeviceInfo>'
DIGEST = 'Digest qop="auth", realm="r", nonce="n1"'


class Impostor:
    """Accepts any digest header; can switch to offering basic only."""

    def __init__(self, challenge: str) -> None:
        self.challenge = challenge
        self.accept_digest = True
        self.seen: list[str] = []

    async def handle(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        self.seen.append(auth)
        if auth.startswith("Digest") and self.accept_digest:
            return web.Response(body=INFO, content_type="application/xml")
        return web.Response(status=401, headers={"WWW-Authenticate": self.challenge})


@pytest.fixture
async def rig():
    opened = []

    async def make(device: Impostor) -> IsapiLink:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", device.handle)
        server = TestServer(app)
        await server.start_server()
        session = aiohttp.ClientSession()
        opened.append((server, session))
        return IsapiLink(session, Endpoint(server.host, server.port, "http"), "admin", "pw-123")

    yield make
    for server, session in opened:
        await session.close()
        await server.close()


async def test_basic_over_http_is_refused(rig) -> None:
    device = Impostor('Basic realm="r"')
    link = await rig(device)
    with pytest.raises(faults.InsecureAuth):
        await link.call("GET", "/ISAPI/System/deviceInfo")
    assert not any(h.startswith("Basic") for h in device.seen)


async def test_link_that_proved_digest_never_sends_basic(rig, monkeypatch) -> None:
    monkeypatch.setattr(link_module, "HICCUP_PAUSE", 0.0)
    device = Impostor(DIGEST)
    link = await rig(device)
    await link.call("GET", "/ISAPI/System/deviceInfo")
    device.accept_digest = False
    device.challenge = 'Basic realm="r"'
    for _ in range(3):
        with pytest.raises(faults.BridgeFault):
            await link.call("GET", "/ISAPI/System/deviceInfo")
    assert not any(h.startswith("Basic") for h in device.seen)
