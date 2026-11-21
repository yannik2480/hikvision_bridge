"""Session folders an earlier run left behind are removed; this run's folders are kept."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from custom_components.hikvision_bridge.runtime import sessions


def test_only_folders_older_than_this_run_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sessions, "_RUN_STARTED", 1_000_000.0)

    old = tmp_path / "hikvision_old"
    old.mkdir()
    (old / "seg00001.m4s").write_bytes(b"x")
    os.utime(old, (900_000.0, 900_000.0))

    current = tmp_path / "hikvision_current"
    current.mkdir()
    os.utime(current, (1_000_100.0, 1_000_100.0))

    unrelated = tmp_path / "other_old"
    unrelated.mkdir()
    os.utime(unrelated, (900_000.0, 900_000.0))

    sessions._remove_stale_folders()

    assert not old.exists()
    assert current.exists()
    assert unrelated.exists()
