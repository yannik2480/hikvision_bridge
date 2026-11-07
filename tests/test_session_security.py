"""Playback sessions are bound to their entry and owner; thumbnails check their source."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant
import pytest

from custom_components.hikvision_bridge.const import DOMAIN
from custom_components.hikvision_bridge.runtime import sessions
from custom_components.hikvision_bridge.views import ThumbnailView, encode_token
from tests.test_panel import HLS, START, _script, _setup


async def _start(client, entry, **extra) -> dict:
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(minutes=5)).isoformat(),
            **extra,
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    return answer["result"]


async def test_unload_stops_sessions(
    hass: HomeAssistant, serve, hass_ws_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, entry = await _setup(hass, serve)
    _script(tmp_path, monkeypatch, HLS)
    client = await hass_ws_client(hass)
    started = await _start(client, entry)
    session = sessions.manager(hass).sessions[started["session_id"]]
    assert session.entry_id == entry.entry_id
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not sessions.manager(hass).sessions
    assert not await hass.async_add_executor_job(session.folder.exists)


async def test_owner_and_lifetime(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    hass_client_no_auth,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, entry = await _setup(hass, serve)
    _script(tmp_path, monkeypatch, HLS)
    client = await hass_ws_client(hass)
    started = await _start(client, entry)
    session = sessions.manager(hass).sessions[started["session_id"]]
    web = await hass_client_no_auth()
    assert (await web.get(started["url"])).status == 200

    # Past its absolute lifetime the session is refused and ended.
    session.created -= sessions.MAX_LIFETIME + 1
    assert (await web.get(started["url"])).status == 404
    await hass.async_block_till_done()
    assert not sessions.manager(hass).sessions

    # A deactivated owner no longer gets the stream.
    started = await _start(client, entry)
    session = sessions.manager(hass).sessions[started["session_id"]]
    assert session.owner
    assert (await web.get(started["url"])).status == 200
    owner = await hass.auth.async_create_user("Viewer")
    session.owner = owner.id
    await hass.auth.async_deactivate_user(owner)
    assert (await web.get(started["url"])).status == 404
    await hass.async_block_till_done()
    assert not sessions.manager(hass).sessions


async def test_only_owner_or_admin_stops(
    hass: HomeAssistant,
    serve,
    hass_ws_client,
    hass_read_only_access_token,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, entry = await _setup(hass, serve)
    _script(tmp_path, monkeypatch, HLS)
    admin = await hass_ws_client(hass)
    other = await hass_ws_client(hass, hass_read_only_access_token)
    started = await _start(admin, entry)

    await other.send_json_auto_id(
        {"type": f"{DOMAIN}/playback/stop", "session_id": started["session_id"]}
    )
    answer = await other.receive_json()
    assert not answer["success"]
    assert answer["error"]["code"] == "unauthorized"
    assert started["session_id"] in sessions.manager(hass).sessions

    # Nor may it replace the session of another user.
    await other.send_json_auto_id(
        {
            "type": f"{DOMAIN}/playback/start",
            "entry_id": entry.entry_id,
            "channel_id": "1",
            "source": "auto",
            "time": (START + timedelta(minutes=5)).isoformat(),
            "replace": started["session_id"],
        }
    )
    answer = await other.receive_json()
    assert not answer["success"]
    assert answer["error"]["code"] == "unauthorized"
    assert started["session_id"] in sessions.manager(hass).sessions

    await admin.send_json_auto_id(
        {"type": f"{DOMAIN}/playback/stop", "session_id": started["session_id"]}
    )
    assert (await admin.receive_json())["success"]
    assert not sessions.manager(hass).sessions


async def test_thumbnail_cache_needs_valid_source(hass: HomeAssistant, serve) -> None:
    _, entry = await _setup(hass, serve)
    view = ThumbnailView()
    request = SimpleNamespace(app={KEY_HASS: hass})
    token = encode_token("rtsp://h/Streaming/tracks/101/?starttime=x")
    view._cache[f"{entry.entry_id}/1/card/{token}"] = b"jpeg"
    view._cache[f"{entry.entry_id}/9/card/{token}"] = b"jpeg"
    view._cache[f"gone/1/card/{token}"] = b"jpeg"
    assert (await view.get(request, entry.entry_id, "1", "card", token)).body == b"jpeg"
    assert (await view.get(request, entry.entry_id, "9", "card", token)).status == 404
    assert (await view.get(request, "gone", "1", "card", token)).status == 404
    assert (await view.get(request, entry.entry_id, "1", "card", "!!")).status == 404
