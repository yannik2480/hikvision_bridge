"""A small simulated ISAPI device for tests, serving the fixtures.

It enforces digest authentication (MD5, qop=auth), can expire nonces, counts failed
logins and locks like a real device, records writes, and streams alert parts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import secrets

from aiohttp import web

FIXTURES = Path(__file__).parent / "fixtures"
_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


@dataclass
class FakeDevice:
    profile: str = "nvr"
    username: str = "operator"
    password: str = "s3cret-pass"
    realm: str = "IP Camera(TEST1)"
    lock_after: int = 5
    require_auth: bool = True
    overrides: dict[str, tuple[int, str]] = field(default_factory=dict)
    stream_parts: list[bytes] = field(default_factory=list)
    stream_hold: float = 0.0
    # Pause between parts, so each arrives in its own read (as from a device encoding pictures).
    stream_gap: float = 0.0
    failed_logins: int = 0
    challenges_sent: int = 0
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    documents: dict[str, str] = field(default_factory=dict)
    nonces: set[str] = field(default_factory=set)
    expired: set[str] = field(default_factory=set)
    # Refuse this many correctly signed requests (a device that briefly rejects logins).
    refuse_next: int = 0
    refuse_without_challenge: int = 0
    # Refuse a request whose nonce count is not above the last one seen for its nonce.
    strict_count: bool = True
    counts: dict[str, int] = field(default_factory=dict)
    # Answers computed from the request body, by path (search, download).
    responders: dict[str, Callable[[str, str], web.StreamResponse]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        folder = FIXTURES / self.profile
        index = json.loads((folder / "_index.json").read_text())
        self._index = {
            path: (meta["status"], (folder / meta["file"]).read_text())
            for path, meta in index.items()
        }

    # --------------------------------------------------------------- auth

    def _challenge(self, stale: bool = False) -> str:
        nonce = secrets.token_hex(12)
        self.nonces.add(nonce)
        self.challenges_sent += 1
        return f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", stale="{"TRUE" if stale else "FALSE"}"'

    def expire_nonces(self) -> None:
        self.expired |= self.nonces

    def _check(self, request: web.Request) -> web.Response | None:
        if not self.require_auth:
            return None
        if self.failed_logins >= self.lock_after:
            body = "<userCheck><statusValue>401</statusValue><lockStatus>lock</lockStatus><unlockTime>1800</unlockTime><retryLoginTime>0</retryLoginTime></userCheck>"
            return web.Response(status=401, text=body, content_type="application/xml")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return web.Response(status=401, headers={"WWW-Authenticate": self._challenge()})
        params = {m[0]: m[1] if m[1] else m[2] for m in _PARAM.findall(header[7:])}
        nonce = params.get("nonce", "")
        if nonce in self.expired or nonce not in self.nonces:
            return web.Response(
                status=401, headers={"WWW-Authenticate": self._challenge(stale=False)}
            )
        ha1 = _md5(f"{params.get('username')}:{self.realm}:{self.password}")
        ha2 = _md5(f"{request.method}:{params.get('uri')}")
        expected = _md5(f"{ha1}:{nonce}:{params.get('nc')}:{params.get('cnonce')}:auth:{ha2}")
        count = int(params.get("nc", "0"), 16)
        out_of_order = self.strict_count and count <= self.counts.get(nonce, 0)
        refused = self.refuse_next > 0
        if (
            params.get("username") != self.username
            or params.get("response") != expected
            or out_of_order
            or refused
        ):
            self.refuse_next = max(0, self.refuse_next - 1)
            self.failed_logins += 1
            if self.refuse_without_challenge > 0:
                self.refuse_without_challenge -= 1
                return web.Response(status=401)
            return web.Response(status=401, headers={"WWW-Authenticate": self._challenge()})
        self.counts[nonce] = count
        if params.get("uri") != request.path_qs:
            return web.Response(status=400, text="uri mismatch")
        return None

    # ------------------------------------------------------------ handlers

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append((request.method, request.path_qs))
        denied = self._check(request)
        if denied is not None:
            return denied
        path = request.path_qs
        if request.path == "/ISAPI/Event/notification/alertStream":
            return await self._stream(request)
        if request.path in self.responders:
            body = await request.text()
            self.writes.append((request.method, path, body))
            return self.responders[request.path](request.method, body)
        if request.method in ("PUT", "POST"):
            body = await request.text()
            self.writes.append((request.method, path, body))
            self.documents[path] = body
            return web.Response(
                text="<ResponseStatus><statusCode>1</statusCode><statusString>OK</statusString><subStatusCode>ok</subStatusCode></ResponseStatus>",
                content_type="application/xml",
            )
        if path in self.overrides:
            status, text = self.overrides[path]
            return web.Response(status=status, text=text, content_type="application/xml")
        if path in self.documents:
            return web.Response(text=self.documents[path], content_type="application/xml")
        if path in self._index:
            status, text = self._index[path]
            ctype = "application/json" if text.lstrip().startswith("{") else "application/xml"
            return web.Response(status=status, text=text, content_type=ctype)
        trigger = self._single_trigger(path)
        if trigger is not None:
            return web.Response(text=trigger, content_type="application/xml")
        if request.path.endswith("/picture"):
            return web.Response(body=b"\xff\xd8\xff\xe0fakejpeg\xff\xd9", content_type="image/jpeg")
        return web.Response(
            status=403,
            text="<ResponseStatus><statusCode>4</statusCode><subStatusCode>notSupport</subStatusCode></ResponseStatus>",
            content_type="application/xml",
        )

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "multipart/mixed; boundary=boundary"}
        )
        await response.prepare(request)
        for part in self.stream_parts:
            await response.write(part)
            await asyncio.sleep(self.stream_gap)
        if self.stream_hold:
            await asyncio.sleep(self.stream_hold)
        return response

    def _single_trigger(self, path: str) -> str | None:
        """Serve /ISAPI/Event/triggers/<id> from the trigger list, as real devices do."""
        prefix = "/ISAPI/Event/triggers/"
        listing = self.documents.get(prefix[:-1]) or self._index.get(prefix[:-1], (0, ""))[1]
        if not path.startswith(prefix) or not listing:
            return None
        wanted = path[len(prefix) :]
        for block in re.findall(r"<EventTrigger>.*?</EventTrigger>", listing, flags=re.S):
            if f"<id>{wanted}</id>" in block:
                namespace = re.search(r'xmlns="([^"]+)"', listing)
                xmlns = f' xmlns="{namespace[1]}"' if namespace else ""
                return block.replace("<EventTrigger>", f'<EventTrigger version="2.0"{xmlns}>', 1)
        return None

    def app(self) -> web.Application:
        application = web.Application()
        application.router.add_route("*", "/{tail:.*}", self.handle)
        return application


def xml_part(xml: str, content_type: str = 'application/xml; charset="UTF-8"') -> bytes:
    body = xml.encode()
    return (
        b"--boundary\r\n"
        + f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        + body
        + b"\r\n"
    )


def event_xml(
    event_type: str,
    state: str = "active",
    channel: str | None = "1",
    when: str = "2026-10-21T11:18:01+02:00",
    namespace: str = "http://www.hikvision.com/ver20/XMLSchema",
    extra: str = "",
) -> str:
    channel_xml = f"<channelID>{channel}</channelID>" if channel is not None else "<channelID/>"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<EventNotificationAlert version="2.0" xmlns="{namespace}">\n'
        f"<ipAddress>10.0.0.92</ipAddress>\n<macAddress>02:00:00:00:01:01</macAddress>\n{channel_xml}\n"
        f"<dateTime>{when}</dateTime>\n<activePostCount>1</activePostCount>\n"
        f"<eventType>{event_type}</eventType>\n<eventState>{state}</eventState>\n"
        f"<eventDescription>test</eventDescription>\n{extra}</EventNotificationAlert>\n"
    )


def picture_part(data: bytes, name: str | None = None) -> bytes:
    disposition = (
        f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\n' if name else ""
    )
    return (
        b"--boundary\r\n"
        + f"Content-Type: image/jpeg\r\n{disposition}Content-Length: {len(data)}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )


def anpr_xml(
    plate: str = "B AB 1234",
    channel: str = "1",
    when: str = "2026-10-21T11:18:01+02:00",
    pictures: tuple[str, ...] = ("licensePlatePicture.jpg", "detectionPicture.jpg"),
) -> str:
    """Plate notification as ANPR cameras send it (pictures follow as separate parts)."""
    infos = "".join(
        f"<pictureInfo><fileName>{name}</fileName><type>{name.split('.')[0]}</type></pictureInfo>"
        for name in pictures
    )
    return event_xml(
        "ANPR",
        channel=channel,
        when=when,
        extra=(
            "<ANPR>\n<country>DE</country>\n"
            f"<licensePlate>{plate}</licensePlate>\n<line>1</line>\n<direction>forward</direction>\n"
            "<confidenceLevel>97</confidenceLevel>\n<plateType>unknown</plateType>\n"
            "<plateColor>white</plateColor>\n<vehicleType>vehicle</vehicleType>\n"
            "<vehicleInfo><index>1</index><vehicleType>0</vehicleType><color>blue</color>"
            "</vehicleInfo>\n<vehicleListName>whiteList</vehicleListName>\n"
            f"<pictureInfoList>{infos}</pictureInfoList>\n</ANPR>\n"
        ),
    )
