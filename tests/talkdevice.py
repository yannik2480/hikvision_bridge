"""A simulated two-way audio endpoint, on a plain socket.

Hikvision devices take the sound as one ``PUT .../audioData`` with ``Content-Length: 0`` and then
read the rest of the connection as its body, which an aiohttp test server cannot serve: this
speaks just enough HTTP/1.1 for that, with digest authentication as the devices have it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import re
import secrets

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')
OK = (
    "<ResponseStatus><statusCode>1</statusCode><statusString>OK</statusString>"
    "<subStatusCode>ok</subStatusCode></ResponseStatus>"
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


@dataclass
class TalkDevice:
    username: str = "operator"
    password: str = "s3cret-pass"
    realm: str = "IP Camera(TALK1)"
    # The reply to open, when the firmware hands out a session.
    open_reply: str = OK
    open_status: int = 200
    # Refuse the next audioData with a fresh challenge, as after the nonce expired.
    refuse_upload: int = 0
    # Close the upload after this many bytes of sound (the device ending the talk).
    hang_up_after: int | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    audio: bytearray = field(default_factory=bytearray)
    uploads_closed: asyncio.Event = field(default_factory=asyncio.Event)
    nonces: set[str] = field(default_factory=set)
    _server: asyncio.Server | None = None

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _challenge(self) -> str:
        nonce = secrets.token_hex(12)
        self.nonces.add(nonce)
        return f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", stale="FALSE"'

    def _signed(self, method: str, path: str, header: str) -> bool:
        if not header.startswith("Digest "):
            return False
        params = {m[0]: m[1] or m[2] for m in _PARAM.findall(header[7:])}
        if params.get("nonce") not in self.nonces or params.get("uri") != path:
            return False
        ha1 = _md5(f"{self.username}:{self.realm}:{self.password}")
        ha2 = _md5(f"{method}:{path}")
        expected = _md5(
            f"{ha1}:{params['nonce']}:{params.get('nc')}:{params.get('cnonce')}:auth:{ha2}"
        )
        return params.get("username") == self.username and params.get("response") == expected

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        request_line, *lines = head.decode("latin-1").split("\r\n")
        method, path, _ = request_line.split(" ", 2)
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines if h)
        }
        self.requests.append((method, path))
        length = int(headers.get("content-length", "0"))
        if length:
            await reader.readexactly(length)
        upload = path.split("?", 1)[0].endswith("/audioData")
        if not self._signed(method, path, headers.get("authorization", "")) or (
            upload and self.refuse_upload > 0
        ):
            if upload and self.refuse_upload > 0 and "authorization" in headers:
                self.refuse_upload -= 1
            self._reply(writer, 401, "", challenge=self._challenge())
            await writer.drain()
            writer.close()
            return
        if upload:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n\r\n")
            await writer.drain()
            await self._receive(reader)
            writer.close()
            self.uploads_closed.set()
            return
        if path.endswith("/open"):
            self._reply(writer, self.open_status, self.open_reply)
        else:
            self._reply(writer, 200, OK)
        await writer.drain()
        writer.close()

    async def _receive(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(4096):
            self.audio += chunk
            if self.hang_up_after is not None and len(self.audio) >= self.hang_up_after:
                return

    @staticmethod
    def _reply(
        writer: asyncio.StreamWriter, status: int, body: str, challenge: str | None = None
    ) -> None:
        data = body.encode()
        lines = [
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}",
            "Content-Type: application/xml",
            f"Content-Length: {len(data)}",
            "Connection: close",
        ]
        if challenge:
            lines.append(f"WWW-Authenticate: {challenge}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
