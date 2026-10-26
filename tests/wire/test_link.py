"""Authentication negotiation, lockout protection and error mapping of IsapiLink."""

import asyncio

import aiohttp
from aiohttp.test_utils import TestServer
import pytest

from custom_components.hikvision_bridge.wire import faults, link as link_module
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink
from tests.fakedevice import FakeDevice

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _serve(device: FakeDevice):
    server = TestServer(device.app())
    await server.start_server()
    return server


@pytest.fixture
async def rig():
    started = []

    async def make(device: FakeDevice, *, username=None, password=None, trusted=False):
        server = await _serve(device)
        session = aiohttp.ClientSession()
        started.append((server, session))
        link = IsapiLink(
            session,
            Endpoint(server.host, server.port, "http"),
            username if username is not None else device.username,
            password if password is not None else device.password,
            trusted=trusted,
        )
        return link

    yield make
    for server, session in started:
        await session.close()
        await server.close()


async def test_first_request_is_unauthenticated_and_digest_follows(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    info = await link.read_xml("/ISAPI/System/deviceInfo")
    assert info.text("deviceType") == "NVR"
    assert link.auth_scheme == "digest"
    assert device.failed_logins == 0
    assert device.requests[0] == ("GET", "/ISAPI/System/deviceInfo")


async def test_wrong_password_stops_after_one_attempt(rig) -> None:
    device = FakeDevice()
    link = await rig(device, password="wrong")
    with pytest.raises(faults.BadCredentials):
        await link.read_xml("/ISAPI/System/deviceInfo")
    assert device.failed_logins == 1
    # Further calls never reach the device.
    before = len(device.requests)
    for _ in range(3):
        with pytest.raises(faults.BadCredentials):
            await link.read_xml("/ISAPI/Event/triggers")
    assert len(device.requests) == before
    assert device.failed_logins == 1

    link.replace_credentials(device.username, device.password)
    assert (await link.read_xml("/ISAPI/System/deviceInfo")) is not None


async def test_expired_nonce_is_renewed_once_without_counting_as_failure(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.expire_nonces()
    assert (await link.read_xml("/ISAPI/Event/triggers")) is not None
    assert device.failed_logins == 0


async def test_parallel_requests_survive_an_expired_nonce(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    for _ in range(3):
        device.expire_nonces()
        results = await asyncio.gather(
            link.read_xml("/ISAPI/Event/triggers"),
            link.read_xml("/ISAPI/System/deviceInfo"),
        )
        assert all(result is not None for result in results)
    assert link.blocked is None
    assert device.failed_logins == 0


async def test_nonce_counts_reach_the_device_in_order(rig) -> None:
    device = FakeDevice(strict_count=True)
    link = await rig(device)
    paths = ["/ISAPI/System/deviceInfo", "/ISAPI/Event/triggers"] * 6
    results = await asyncio.gather(*(link.read_xml(path) for path in paths))
    assert all(result is not None for result in results)
    assert device.failed_logins == 0


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_module, "HICCUP_PAUSE", 0.0)


async def test_one_refusal_of_working_credentials_does_not_block(rig, no_pause) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.refuse_next = 2  # the request and its retry with a fresh nonce
    with pytest.raises(faults.AuthHiccup):
        await link.read_xml("/ISAPI/Event/triggers")
    assert link.blocked is None
    assert (await link.read_xml("/ISAPI/Event/triggers")) is not None
    assert device.failed_logins == 2


async def test_link_pauses_after_a_refusal(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.refuse_next = 2
    with pytest.raises(faults.AuthHiccup):
        await link.read_xml("/ISAPI/Event/triggers")
    before = len(device.requests)
    with pytest.raises(faults.AuthHiccup):
        await link.read_xml("/ISAPI/Event/triggers")
    assert len(device.requests) == before
    assert link.blocked is None


async def test_stored_credentials_survive_a_replaced_nonce_on_first_use(rig) -> None:
    # Another client of the same account made the device issue a new nonce.
    device = FakeDevice(refuse_next=1)
    link = await rig(device, trusted=True)
    assert (await link.read_xml("/ISAPI/System/deviceInfo")) is not None
    assert link.blocked is None
    assert device.failed_logins == 1


async def test_new_credentials_get_no_second_attempt(rig) -> None:
    device = FakeDevice(refuse_next=1)
    link = await rig(device)
    with pytest.raises(faults.BadCredentials):
        await link.read_xml("/ISAPI/System/deviceInfo")
    assert device.failed_logins == 1


async def test_changed_password_blocks_on_the_second_refusal(rig, no_pause) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.password = "changed-on-device"
    with pytest.raises(faults.AuthHiccup):
        await link.read_xml("/ISAPI/Event/triggers")
    with pytest.raises(faults.BadCredentials):
        await link.read_xml("/ISAPI/Event/triggers")
    assert isinstance(link.blocked, faults.BadCredentials)
    before = len(device.requests)
    with pytest.raises(faults.BadCredentials):
        await link.read_xml("/ISAPI/Event/triggers")
    assert len(device.requests) == before
    # Well below the device's failed-login lock.
    assert device.failed_logins == 3


async def test_changed_stored_password_costs_at_most_three_logins(rig, no_pause) -> None:
    device = FakeDevice(password="changed-on-device")
    link = await rig(device, password="stored-before", trusted=True)
    with pytest.raises(faults.AuthHiccup):
        await link.read_xml("/ISAPI/System/deviceInfo")
    with pytest.raises(faults.BadCredentials):
        await link.read_xml("/ISAPI/System/deviceInfo")
    assert device.failed_logins == 3
    assert device.failed_logins < device.lock_after


async def test_lock_is_reported_and_blocks(rig) -> None:
    device = FakeDevice(lock_after=0)
    link = await rig(device)
    with pytest.raises(faults.LockedOut) as caught:
        await link.read_xml("/ISAPI/System/deviceInfo")
    assert caught.value.retry_after == 1800
    assert isinstance(link.blocked, faults.LockedOut)


async def test_optional_unsupported_is_remembered(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    assert await link.read_xml("/ISAPI/System/Holidays", optional=True) is None
    count = len(device.requests)
    assert await link.read_xml("/ISAPI/System/Holidays", optional=True) is None
    assert len(device.requests) == count
    with pytest.raises(faults.Unsupported):
        await link.read_xml("/ISAPI/ContentMgmt/storage/hdd")
    # 503 on the host capability endpoint is "unsupported", not an outage.
    assert (
        await link.read_xml("/ISAPI/Event/notification/httpHosts/capabilities", optional=True)
        is None
    )


async def test_optional_unsupported_with_query_is_remembered(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    path = "/ISAPI/System/Holidays?format=json"
    assert await link.read_json(path, optional=True) is None
    count = len(device.requests)
    assert await link.read_json(path, optional=True) is None
    assert len(device.requests) == count


async def test_unreachable() -> None:
    async with aiohttp.ClientSession() as session:
        link = IsapiLink(session, Endpoint("127.0.0.1", 1, "http"), "u", "p", connect_timeout=1)
        with pytest.raises(faults.Unreachable):
            await link.read_xml("/ISAPI/System/deviceInfo")


async def test_not_isapi(rig) -> None:
    device = FakeDevice(require_auth=False)
    device._index["/ISAPI/System/deviceInfo"] = (404, "<html>nope</html>")
    link = await rig(device)
    with pytest.raises(faults.NotIsapi):
        await link.read_xml("/ISAPI/System/deviceInfo")


async def test_write_round_trip(rig) -> None:
    device = FakeDevice(profile="camera_g3")
    link = await rig(device)
    reply = await link.write_xml(
        "/ISAPI/Smart/LineDetection/1", "<LineDetection><enabled>true</enabled></LineDetection>"
    )
    assert reply.status == 200
    assert device.writes[0][1] == "/ISAPI/Smart/LineDetection/1"


async def test_refusal_without_challenge_reprobes_and_recovers(rig) -> None:
    device = FakeDevice()
    link = await rig(device)
    await link.read_xml("/ISAPI/System/deviceInfo")
    device.refuse_next = 1
    device.refuse_without_challenge = 1
    assert (await link.read_xml("/ISAPI/Event/triggers")) is not None
    assert link.blocked is None
