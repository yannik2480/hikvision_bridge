"""Retention of saved recordings: only files the download action names are removed, by age."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path

from custom_components.hikvision_bridge.retention import prune_saved

NOW = datetime(2026, 11, 21, 12, 0, 0)


def _touch(path: Path, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_removes_only_saved_recordings_older_than_the_period(tmp_path: Path) -> None:
    old = _touch(tmp_path / "front_door_20261001_080000.mp4", age_days=51)
    recent = _touch(tmp_path / "front_door_20261120_080000.mp4", age_days=1)
    other = _touch(tmp_path / "notes.txt", age_days=400)
    unnamed = _touch(tmp_path / "front_door_bad.mp4", age_days=400)

    removed = prune_saved(tmp_path, days=30, now=NOW)

    assert removed == 1
    assert not old.exists()
    assert recent.exists()
    assert other.exists()
    assert unnamed.exists()


def test_zero_days_keeps_everything(tmp_path: Path) -> None:
    old = _touch(tmp_path / "cam_20250101_000000.ps", age_days=900)

    assert prune_saved(tmp_path, days=0, now=NOW) == 0
    assert old.exists()


def test_removes_emptied_subfolders_and_keeps_the_root(tmp_path: Path) -> None:
    _touch(tmp_path / "day1" / "cam_20250101_000000.ts", age_days=400)
    (tmp_path / "empty").mkdir()

    prune_saved(tmp_path, days=30, now=NOW)

    assert tmp_path.is_dir()
    assert not (tmp_path / "day1").exists()
    assert not (tmp_path / "empty").exists()


def test_missing_folder_is_not_an_error(tmp_path: Path) -> None:
    assert prune_saved(tmp_path / "nothing_here", days=30, now=NOW) == 0
