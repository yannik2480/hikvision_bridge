"""Clip pictures: authenticated, cached within bounds, and not cached when a download fails."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

from aiohttp import ClientSession
from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hikvision_bridge import views
from custom_components.hikvision_bridge.runtime.playback import NoRecording
from custom_components.hikvision_bridge.views import ThumbnailView, encode_token
from custom_components.hikvision_bridge.wire.faults import BridgeFault
from tests.test_panel import _setup

URI = "rtsp://h/Streaming/tracks/101/?starttime=x"


class Downloads:
    """Stands in for the device download and for ffmpeg; counts what was asked of them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, outcome) -> None:
        self.calls = 0
        self.outcome = outcome
        monkeypatch.setattr(views, "open_recording", self._open)
        monkeypatch.setattr(views, "run_ffmpeg", self._ffmpeg)

    @asynccontextmanager
    async def _open(self, _playback, _uri) -> AsyncIterator[tuple[None, None]]:
        yield None, None

    async def _ffmpeg(self, _binary, _args, _chunks, _sink=None) -> bytes:
        self.calls += 1
        return await self.outcome()


def _picture(data: bytes):
    async def outcome() -> bytes:
        return data

    return outcome


def _raises(error: Exception):
    async def outcome() -> bytes:
        raise error

    return outcome


async def _get(hass: HomeAssistant, view: ThumbnailView, entry_id: str, token: str):
    request = SimpleNamespace(app={KEY_HASS: hass})
    return await view.get(request, entry_id, "1", "card", token)


async def test_picture_is_cached(
    hass: HomeAssistant, serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)
    downloads = Downloads(monkeypatch, _picture(b"jpeg"))
    view = ThumbnailView()
    token = encode_token(URI)
    first = await _get(hass, view, entry.entry_id, token)
    second = await _get(hass, view, entry.entry_id, token)
    assert first.status == second.status == 200
    assert first.body == second.body == b"jpeg"
    assert first.content_type == "image/jpeg"
    assert downloads.calls == 1


async def test_file_without_picture_is_remembered(
    hass: HomeAssistant, serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)
    downloads = Downloads(monkeypatch, _raises(NoRecording()))
    view = ThumbnailView()
    token = encode_token(URI)
    for _ in range(2):
        assert (await _get(hass, view, entry.entry_id, token)).status == 404
    assert downloads.calls == 1


async def test_failed_download_is_not_remembered(
    hass: HomeAssistant, serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)
    downloads = Downloads(monkeypatch, _raises(BridgeFault()))
    view = ThumbnailView()
    token = encode_token(URI)
    assert (await _get(hass, view, entry.entry_id, token)).status == 404
    assert not view._cache
    downloads.outcome = _picture(b"jpeg")
    assert (await _get(hass, view, entry.entry_id, token)).status == 200
    assert downloads.calls == 2


async def test_slow_download_gives_up(
    hass: HomeAssistant, serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)

    async def stuck() -> bytes:
        await asyncio.sleep(10)
        return b"never"

    Downloads(monkeypatch, stuck)
    monkeypatch.setattr(views, "THUMBNAIL_TIMEOUT", 0.05)
    view = ThumbnailView()
    assert (await _get(hass, view, entry.entry_id, encode_token(URI))).status == 404
    assert not view._cache


async def test_cache_is_bounded_by_count_and_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(views, "THUMBNAIL_CACHE", 3)
    view = ThumbnailView()
    for index in range(5):
        view._remember(f"key{index}", b"x" * 10)
    assert list(view._cache) == ["key2", "key3", "key4"]
    monkeypatch.setattr(views, "THUMBNAIL_CACHE", 100)
    monkeypatch.setattr(views, "THUMBNAIL_CACHE_BYTES", 25)
    view = ThumbnailView()
    for index in range(4):
        view._remember(f"key{index}", b"y" * 10)
    assert list(view._cache) == ["key2", "key3"]


async def test_pictures_need_a_login(hass: HomeAssistant, serve, hass_client_no_auth) -> None:
    _, entry = await _setup(hass, serve)
    client: ClientSession = await hass_client_no_auth()
    path = views.THUMBNAIL_URL.format(
        entry_id=entry.entry_id, channel_id="1", source="card", token=encode_token(URI)
    )
    response = await client.get(path)
    assert response.status == 401
