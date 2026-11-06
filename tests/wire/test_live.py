"""Loopback relay authentication, SDP handling and connection lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest

from custom_components.hikvision_bridge.runtime.live import LiveRelay, Message, read_message
from tests.model.test_live import CODE, encrypt, frame, payload


@asynccontextmanager
async def fake_recorder(*, reject: bool = False, codec: str = "H264"):
    requests = []
    writers = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.append(writer)
        try:
            while True:
                msg = await read_message(reader)
                requests.append(msg)
                headers = [("CSeq", msg.get("CSeq"))]
                if reject or not msg.get("Authorization"):
                    headers.append(
                        ("WWW-Authenticate", 'Digest realm="DVR", nonce="abc", qop="auth"')
                    )
                    reply = Message("RTSP/1.0 401 Unauthorized", headers)
                else:
                    method = msg.line.split()[0]
                    # The signer must use the upstream URI, not the loopback token URL.
                    assert f'uri="{relay.upstream}' in msg.get("Authorization")
                    headers.append(("Session", "test;timeout=60"))
                    body = b""
                    if method == "DESCRIBE":
                        headers += [
                            ("Content-Type", "application/sdp"),
                            ("Content-Base", relay.upstream + "/"),
                        ]
                        body = (
                            f"v=0\r\ns=Video\r\nm=video 0 RTP/AVP 96\r\n"
                            f"a=rtpmap:96 {codec}/90000\r\n"
                            f"a=control:{relay.upstream}/trackID=1\r\n"
                            "m=audio 0 RTP/AVP 0\r\na=control:trackID=2\r\n"
                        ).encode()
                    if method == "SETUP":
                        headers.append(("Transport", msg.get("Transport")))
                    reply = Message("RTSP/1.0 200 OK", headers, body)
                writer.write(reply.encode())
                if msg.line.startswith("PLAY ") and msg.get("Authorization") and not reject:
                    writer.write(frame(encrypt(b"\x65" + b"x" * 100)))
                    writer.write(frame(b"rtcp", channel=1, flagged=False))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    relay = LiveRelay("127.0.0.1", server.sockets[0].getsockname()[1], "101", "user", "pass", CODE)
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


async def test_digest_sdp_and_encrypted_video(socket_enabled) -> None:
    async with fake_recorder() as (relay, url, requests):
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


@pytest.mark.parametrize(("reject", "codec"), [(True, "H264"), (False, "H265")])
async def test_failed_login_blocks_retries_and_h265_is_rejected(
    socket_enabled, reject, codec
) -> None:
    async with fake_recorder(reject=reject, codec=codec) as (relay, url, requests):
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
