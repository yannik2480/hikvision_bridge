"""Loopback relay authentication, SDP handling and connection lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

import pytest

from custom_components.hikvision_bridge.runtime.live import LiveRelay, Message, read_message
from tests.model.test_live import CODE, encrypt, frame, payload


@asynccontextmanager
async def fake_recorder(
    *,
    reject: bool = False,
    codec: str = "H264",
    resource_path=None,
    advertised_origin=None,
    stale=False,
    repeat_stale=False,
    basic=False,
    on_failure=None,
):
    requests = []
    writers = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.append(writer)
        staled = set()
        try:
            while True:
                msg = await read_message(reader)
                requests.append(msg)
                headers = [("CSeq", msg.get("CSeq"))]
                method = msg.line.split()[0]
                expired = (
                    stale
                    and method == "GET_PARAMETER"
                    and bool(msg.get("Authorization"))
                    and (msg.get("CSeq") not in staled or repeat_stale)
                )
                if reject or not msg.get("Authorization") or expired:
                    challenge = (
                        'Basic realm="DVR"'
                        if basic
                        else 'Digest realm="DVR", nonce="abc", qop="auth"'
                    )
                    if expired:
                        staled.add(msg.get("CSeq"))
                        challenge = 'Digest realm="DVR", nonce="fresh", qop="auth", stale=true'
                    headers.append(("WWW-Authenticate", challenge))
                    reply = Message("RTSP/1.0 401 Unauthorized", headers)
                else:
                    method = msg.line.split()[0]
                    # The signer must use the upstream URI, not the loopback token URL.
                    assert f'uri="{msg.line.split()[1]}"' in msg.get("Authorization")
                    if stale and method == "GET_PARAMETER":
                        assert 'nonce="fresh"' in msg.get("Authorization")
                    configured = urlsplit(relay.upstream)
                    origin = (
                        advertised_origin
                        if advertised_origin is not None
                        else f"rtsp://{configured.netloc}"
                    )
                    base = origin + configured.path.rstrip("/") + "/"
                    headers.append(("Session", "test;timeout=60"))
                    body = b""
                    if method == "DESCRIBE":
                        headers += [
                            ("Content-Type", "application/sdp"),
                            ("Content-Base", base),
                        ]
                        body = (
                            f"v=0\r\ns=Video\r\nm=video 0 RTP/AVP 96\r\n"
                            f"a=rtpmap:96 {codec}/90000\r\n"
                            f"a=control:{base}trackID=1\r\n"
                            "m=audio 0 RTP/AVP 0\r\na=control:trackID=2\r\n"
                        ).encode()
                    if method == "SETUP":
                        headers.append(("Transport", msg.get("Transport")))
                    if method == "PLAY":
                        headers.append(("RTP-Info", f"url={base}trackID=1;seq=1;rtptime=90000"))
                    reply = Message("RTSP/1.0 200 OK", headers, body)
                writer.write(reply.encode())
                if msg.line.startswith("PLAY ") and msg.get("Authorization") and not reject:
                    if codec == "H264":
                        writer.write(frame(encrypt(b"\x65" + b"x" * 100)))
                    else:
                        writer.write(frame(b"\x40\x01clear HEVC", flagged=False))
                    writer.write(frame(b"rtcp", channel=1, flagged=False))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    relay = LiveRelay(
        "127.0.0.1",
        server.sockets[0].getsockname()[1],
        "101",
        "user",
        "pass",
        CODE,
        resource_path=resource_path,
        on_failure=on_failure,
    )
    try:
        url = await relay.start()
        yield relay, url, requests
    finally:
        await asyncio.wait_for(relay.close(), 2)
        server.close()
        for writer in writers:
            writer.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def connect(relay):
    return await asyncio.open_connection("127.0.0.1", relay.server.sockets[0].getsockname()[1])


async def test_token_is_checked_before_upstream_connection(socket_enabled) -> None:
    async with fake_recorder() as (relay, _, requests):
        assert relay.server.sockets[0].getsockname()[0] == "127.0.0.1"
        reader, writer = await connect(relay)
        writer.write(
            Message("DESCRIBE rtsp://127.0.0.1/invalid RTSP/1.0", [("CSeq", "1")]).encode()
        )
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert not requests
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize(
    "resource_path",
    [None, "/Streaming/tracks/101/?starttime=20260101T120000Z&endtime=20260101T121000Z"],
)
async def test_digest_sdp_and_encrypted_video(socket_enabled, resource_path) -> None:
    async with fake_recorder(resource_path=resource_path) as (relay, url, requests):
        reader, writer = await connect(relay)
        try:
            for number, method, suffix, headers in [
                (1, "OPTIONS", "", []),
                (2, "DESCRIBE", "", []),
                (3, "SETUP", "/trackID=1", [("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1")]),
                (4, "PLAY", "", []),
            ]:
                writer.write(
                    Message(
                        f"{method} {url}{suffix} RTSP/1.0", [("CSeq", str(number)), *headers]
                    ).encode()
                )
                await writer.drain()
                reply = await read_message(reader)
                assert reply.line == "RTSP/1.0 200 OK"
                if method == "DESCRIBE":
                    assert url.encode() in reply.body
                    assert b"m=audio" not in reply.body
                    assert reply.get("Content-Base") == url + "/"
            assert payload(await read_message(reader)) == b"\x65" + b"x" * 100
            assert (await read_message(reader))[1] == 1  # RTCP unchanged.
            assert len(requests) == 5  # Only the first request needs an auth challenge.
            await asyncio.wait_for(relay.close(), 1)
            assert not relay.tasks
            assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            writer.close()
            await writer.wait_closed()


async def test_failed_login_blocks_retries(socket_enabled) -> None:
    reject = True
    failures = []
    async with fake_recorder(reject=reject, on_failure=lambda: failures.append(1)) as (
        relay,
        url,
        requests,
    ):
        reader, writer = await connect(relay)
        writer.write(Message(f"DESCRIBE {url} RTSP/1.0", [("CSeq", "1")]).encode())
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert len(requests) == 2
        writer.close()
        await writer.wait_closed()
        if reject:
            assert relay.failure
            reader, writer = await connect(relay)
            with suppress(ConnectionResetError):
                assert await asyncio.wait_for(reader.read(), 1) == b""
            assert len(requests) == 2
            writer.close()
            await writer.wait_closed()
    assert failures == [1]  # Reported once, not on every refused reconnect.


async def test_basic_only_challenge_is_refused(socket_enabled) -> None:
    # RTSP is plain text; Basic would put the password readable on the network.
    failures = []
    async with fake_recorder(basic=True, on_failure=lambda: failures.append(1)) as (
        relay,
        url,
        requests,
    ):
        reader, writer = await connect(relay)
        writer.write(Message(f"DESCRIBE {url} RTSP/1.0", [("CSeq", "1")]).encode())
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        assert len(requests) == 1
        assert not requests[0].get("Authorization")
        assert relay.failure
    assert failures == [1]


async def request(reader, writer, method, uri, number=1, headers=()):
    writer.write(Message(f"{method} {uri} RTSP/1.0", [("CSeq", str(number)), *headers]).encode())
    await writer.drain()
    return await read_message(reader)


@pytest.mark.parametrize("advertised_origin", ["rtsp://10.0.0.7", "rtsp://dvr.internal:554", ""])
@pytest.mark.parametrize(
    "resource_path",
    [None, "/Streaming/tracks/101/?starttime=20260101T120000Z&endtime=20260101T121000Z"],
)
async def test_advertised_urls_use_configured_endpoint(
    socket_enabled, advertised_origin, resource_path
):
    async with fake_recorder(advertised_origin=advertised_origin, resource_path=resource_path) as (
        relay,
        url,
        requests,
    ):
        reader, writer = await connect(relay)
        try:
            reply = await request(reader, writer, "DESCRIBE", url)
            assert reply.get("Content-Base") == url + "/"
            track = next(
                line.removeprefix("a=control:")
                for line in reply.body.decode().splitlines()
                if line.startswith("a=control:")
            )
            assert track == url + "/trackID=1"
            reply = await request(
                reader,
                writer,
                "SETUP",
                track,
                2,
                [("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1")],
            )
            assert reply.line == "RTSP/1.0 200 OK"
            setup = next(msg for msg in requests if msg.line.startswith("SETUP "))
            configured = urlsplit(relay.upstream)
            target = urlsplit(setup.line.split()[1])
            assert target.netloc == configured.netloc
            assert target.path == configured.path.rstrip("/") + "/trackID=1"
            assert target.query == configured.query
            reply = await request(reader, writer, "PLAY", url, 3)
            assert reply.get("RTP-Info") == f"url={url}/trackID=1;seq=1;rtptime=90000"
            assert payload(await read_message(reader)) == b"\x65" + b"x" * 100
        finally:
            writer.close()
            await writer.wait_closed()


async def test_non_h264_sdp_passes_through_without_reconnect_error(socket_enabled, caplog):
    async with fake_recorder(codec="H265") as (relay, url, _):
        reader, writer = await connect(relay)
        try:
            reply = await request(reader, writer, "DESCRIBE", url)
            assert reply.line == "RTSP/1.0 200 OK"
            assert b"H265/90000" in reply.body
            assert b"m=audio" in reply.body
            await request(
                reader,
                writer,
                "SETUP",
                url + "/trackID=1",
                2,
                [("Transport", "RTP/AVP/TCP;unicast;interleaved=0-1")],
            )
            await request(reader, writer, "PLAY", url, 3)
            original = frame(b"\x40\x01clear HEVC", flagged=False)
            assert await read_message(reader) == original
            assert relay.failure is None
            assert "requires H.264" not in caplog.text
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize("repeat_stale", [False, True])
async def test_stale_nonce_retries_once_per_request(socket_enabled, repeat_stale):
    async with fake_recorder(stale=True, repeat_stale=repeat_stale) as (relay, url, requests):
        reader, writer = await connect(relay)
        try:
            assert (await request(reader, writer, "OPTIONS", url)).line == "RTSP/1.0 200 OK"
            writer.write(Message(f"GET_PARAMETER {url} RTSP/1.0", [("CSeq", "2")]).encode())
            await writer.drain()
            if repeat_stale:
                assert await asyncio.wait_for(reader.read(), 1) == b""
                assert relay.failure
            else:
                assert (await read_message(reader)).line == "RTSP/1.0 200 OK"
                assert relay.failure is None
            signed = [msg for msg in requests if msg.line.startswith("GET_PARAMETER ")]
            assert len(signed) == 2
            assert 'nonce="abc"' in signed[0].get("Authorization")
            assert 'nonce="fresh"' in signed[1].get("Authorization")
            assert "nc=00000001" in signed[1].get("Authorization")
            if not repeat_stale:
                assert (
                    await request(reader, writer, "GET_PARAMETER", url, 3)
                ).line == "RTSP/1.0 200 OK"
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    "foreign",
    [
        "rtsp://10.0.0.7/Streaming/Channels/102/trackID=1",
        "rtsp://10.0.0.7/Streaming/Channels/1010/trackID=1",
        "rtsp://10.0.0.7/Other/Channels/101/trackID=1",
    ],
)
def test_advertised_url_rewrite_does_not_capture_other_streams(foreign):
    relay = LiveRelay("configured.example", 1554, "101", "user", "pass", CODE)
    relay.url = "rtsp://127.0.0.1:1234/test-token"
    assert relay.rewrite_urls(foreign) == foreign
