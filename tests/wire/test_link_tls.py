"""TLS settings for the devices' HTTPS endpoints."""

from __future__ import annotations

import ssl
from unittest.mock import MagicMock

import aiohttp
from multidict import CIMultiDict

from custom_components.hikvision_bridge.runtime.tls import device_context
from custom_components.hikvision_bridge.wire.link import Endpoint, IsapiLink


def _names(context: ssl.SSLContext) -> list[str]:
    return [cipher["name"] for cipher in context.get_ciphers()]


def test_contexts_leave_out_finite_field_dh() -> None:
    """DHE made the DS-KD8003 door station answer valid logins with 401 most of the time."""
    for verify in (True, False):
        names = _names(device_context(verify))
        assert not [n for n in names if n.startswith(("DHE-", "EDH-"))]
        assert any(n.startswith("ECDHE-") for n in names)  # everything else stays on offer


def test_contexts_verify_as_asked() -> None:
    assert device_context(True).verify_mode == ssl.CERT_REQUIRED
    assert device_context(False).verify_mode == ssl.CERT_NONE
    assert device_context(False).check_hostname is False
    assert device_context(True) is device_context(True)  # built once


class _Response:
    status = 200
    headers = CIMultiDict({"Content-Type": "application/xml"})

    async def read(self) -> bytes:
        return b"<ok/>"

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def _sent_kwargs(scheme: str, context: ssl.SSLContext | None) -> dict:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = _Response()
    link = IsapiLink(
        session, Endpoint("10.0.0.1", 443, scheme), "u", "p", trusted=True, ssl_context=context
    )
    link._auth_checked = True  # no challenge round trip needed to see the request
    await link.call("GET", "/ISAPI/System/deviceInfo")
    return session.request.call_args.kwargs


async def test_https_requests_use_the_given_context() -> None:
    context = device_context(False)
    assert (await _sent_kwargs("https", context))["ssl"] is context


async def test_plain_http_and_no_context_leave_the_session_alone() -> None:
    assert "ssl" not in await _sent_kwargs("http", device_context(False))
    assert "ssl" not in await _sent_kwargs("https", None)
