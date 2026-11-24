"""Retention of recordings saved to the Home Assistant disk by the download action.

Only files named the way the download action names them are touched:
`<channel>_<YYYYmmdd_HHMMSS>.<ext>` inside the entry's folder. Anything else in that folder
is left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import DOMAIN

SAVED_NAME = re.compile(r"^.+_\d{8}_\d{6}\.(mp4|ts|ps)$")
MEDIA_FOLDER = DOMAIN


def saved_folder(hass: HomeAssistant, entry_title: str) -> Path:
    """The folder the download action saves an entry's recordings to."""
    root = Path(hass.config.media_dirs.get("local") or hass.config.path("media"))
    return root / MEDIA_FOLDER / slugify(entry_title)


def prune_saved(folder: Path, days: int, now: datetime) -> int:
    """Delete saved recordings older than `days` days; return how many were removed.

    `days` of 0 keeps everything. Empty subfolders left behind are removed too.
    """
    if days <= 0 or not folder.is_dir():
        return 0
    cutoff = (now - timedelta(days=days)).timestamp()
    removed = 0
    for path in folder.rglob("*"):
        if not path.is_file() or not SAVED_NAME.match(path.name):
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    # Deepest folders first, so a folder emptied by this run goes too.
    for path in sorted(
        (p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        if not any(path.iterdir()):
            path.rmdir()
    return removed
