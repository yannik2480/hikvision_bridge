"""Less common answers: open devices, redirects, web pages, optional resources, streams."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.wire import faults, feed
from custom_components.hikvision_bridge.wire.feed import stream_missing
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from tests.fakedevice import FakeDevice

pytestmark = pytest.mark.usefixtures("socket_enabled")

NOT_SUPPORTED = (
    "<ResponseStatus><statusCode>4</statusCode><subStatusCode>notSupport</subStatusCode>"
    "</ResponseStatus>"
)
BUSY = "<ResponseStatus><subStatusCode>serviceUnavailable</subStatusCode></ResponseStatus>"
BAD_REQUEST = (
    "<ResponseStatus><statusCode>6</statusCode><subStatusCode>badXmlContent</subStatusCode>"
    "</ResponseStatus>"
)


@pytest.fixture
async def open_link():
    opened: list[tuple[TestServer, aiohttp.ClientSession]] = []

    async def make(app: web.Application, username: str = "operator", password: str = "pw"):
        server = TestServer(app)
        await server.start_server()
        session = aiohttp.ClientSession()
        opened.append((server, session))
        return IsapiLink(session, Endpoint(server.host, server.port), username, password)

    yield make
    for server, session in opened:
        await session.close()
        await server.close()


def _single(status: int, body: str = "", ctype: str = "text/html", headers=None) -> web.Application:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=status, text=body, content_type=ctype, headers=headers)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def test_device_without_authentication_still_works(open_link) -> None:
    device = FakeDevice(require_auth=False)
    link = await open_link(device.app())
    info = await link.read_xml("/ISAPI/System/deviceInfo")
    assert info is not None
    assert link.auth_scheme is None


@pytest.mark.parametrize(
    ("app", "fault"),
    [
        (_single(302, headers={"Location": "https://x/"}), faults.NotIsapi),
        (_single(404), faults.NotIsapi),
        (_single(200, "<html></html>"), faults.NotIsapi),
        (_single(500, "", "application/xml"), faults.Unreachable),
        (_single(401, "nothing"), faults.NotIsapi),
    ],
)
async def test_probe_answers_that_are_not_isapi(open_link, app, fault) -> None:
    link = await open_link(app)
    with pytest.raises(fault):
        await link.read_xml("/ISAPI/System/deviceInfo")


async def test_optional_reads_remember_unsupported_paths(open_link) -> None:
    path = "/ISAPI/Thing/one"
    device = FakeDevice(overrides={path: (403, NOT_SUPPORTED)})
    link = await open_link(device.app(), device.username, device.password)
    assert await link.read_xml(path, optional=True) is None
    assert path in link.unsupported
    before = len(device.requests)
    assert await link.read_xml(path, optional=True) is None
    assert await link.read_text(path, optional=True) is None
    assert len(device.requests) == before  # not asked again
    with pytest.raises(faults.Unsupported):
        await link.read_text(path)


async def test_optional_reads_skip_forbidden_paths(open_link) -> None:
    # DS-7116HGHI-M1 answers a bare 403 for the event capabilities (issue #22).
    path, text = "/ISAPI/Event/channels/capabilities", "/ISAPI/Thing/text"
    device = FakeDevice(overrides={path: (403, ""), text: (403, "")})
    link = await open_link(device.app(), device.username, device.password)
    assert await link.read_xml(path, optional=True) is None
    assert path in link.unsupported
    before = len(device.requests)
    assert await link.read_xml(path, optional=True) is None
    assert len(device.requests) == before  # not asked again
    assert await link.read_json(text, optional=True) is None
    with pytest.raises(faults.Forbidden):
        await link.read_text(text)


async def test_optional_read_of_rejected_and_broken_documents(open_link) -> None:
    rejected, broken, js = "/ISAPI/Thing/bad", "/ISAPI/Thing/broken", "/ISAPI/Thing/json"
    device = FakeDevice(
        overrides={
            rejected: (400, BAD_REQUEST),
            broken: (200, "<a><b></a>"),
            js: (200, "{not json"),
        }
    )
    link = await open_link(device.app(), device.username, device.password)
    assert await link.read_xml(rejected, optional=True) is None
    assert rejected in link.unsupported
    assert await link.read_xml(broken, optional=True) is None
    with pytest.raises(faults.NotIsapi):
        await link.read_xml(broken)
    assert await link.read_json(js, optional=True) is None
    with pytest.raises(faults.NotIsapi):
        await link.read_json(js)


async def test_json_and_bytes_and_write(open_link) -> None:
    device = FakeDevice()
    link = await open_link(device.app(), device.username, device.password)
    status = await link.read_json("/ISAPI/System/workingstatus?format=json")
    assert isinstance(status, dict)
    picture = await link.read_bytes("/ISAPI/Streaming/channels/101/picture")
    assert picture.body.startswith(b"\xff\xd8")
    await link.write_xml("/ISAPI/Thing/doc", "<doc/>")
    assert device.writes == [("PUT", "/ISAPI/Thing/doc", "<doc/>")]


async def test_stream_with_wrong_password_blocks_link(open_link) -> None:
    device = FakeDevice()
    link = await open_link(device.app(), device.username, "wrong")
    with pytest.raises(faults.BadCredentials):
        async with link.open_stream("/ISAPI/Event/notification/alertStream"):
            pass
    assert device.failed_logins == 1
    assert isinstance(link.blocked, faults.BadCredentials)
    with pytest.raises(faults.BadCredentials):
        async with link.open_stream("/ISAPI/Event/notification/alertStream"):
            pass
    assert device.failed_logins == 1


async def test_stream_refused_by_device(open_link) -> None:
    device = FakeDevice()
    link = await open_link(device.app(), device.username, device.password)
    device.overrides["/ISAPI/Other/stream"] = (403, NOT_SUPPORTED)
    with pytest.raises(faults.Unsupported):
        async with link.open_stream("/ISAPI/Other/stream"):
            pass


@pytest.mark.parametrize(
    ("answer", "missing"),
    [
        # T1-STVI16/S on firmware V3.1.8 has no event stream at all.
        ((404, "Can't locate the url"), True),
        # A busy stream (no free connection slot) is there; the feed retries it.
        ((503, BUSY), False),
        (None, False),
    ],
)
async def test_stream_missing_only_on_404(open_link, answer, missing) -> None:
    path = "/ISAPI/Event/notification/alertStream"
    device = FakeDevice(overrides={path: answer} if answer else {})
    link = await open_link(device.app(), device.username, device.password)
    assert await stream_missing(link) is missing


async def test_silent_stream_counts_as_present(open_link, monkeypatch) -> None:
    monkeypatch.setattr(feed, "PROBE_TIMEOUT", 0.2)

    # Accepts the connection but never sends headers; setup must not wait for it forever.
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/ISAPI/Event/notification/alertStream":
            await asyncio.Event().wait()
        return web.Response(text="<ok/>", content_type="application/xml")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    link = await open_link(app)
    assert await stream_missing(link) is False


async def test_lock_reported_mid_session_stops_everything(open_link) -> None:
    device = FakeDevice()
    link = await open_link(device.app(), device.username, device.password)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.failed_logins = device.lock_after  # device locks for whatever reason
    device.expire_nonces()
    with pytest.raises(faults.LockedOut):
        await link.read_xml("/ISAPI/System/deviceInfo")
    before = len(device.requests)
    with pytest.raises(faults.LockedOut):
        await link.read_xml("/ISAPI/System/deviceInfo")
    assert len(device.requests) == before
