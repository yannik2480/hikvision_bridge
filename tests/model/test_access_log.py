"""Reading new entries of the access event log, against a log that answers like a DS-KD8003."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.hikvision_bridge.model.features import acslog


class FakeLink:
    """Answers AcsEvent searches from a list of entries, oldest first."""

    def __init__(self, log: list[dict[str, Any]], *, reverse: bool = True) -> None:
        self.log = log
        self.reverse = reverse

    async def call(self, method: str, path: str, *, body: str, content_type: str) -> Any:
        cond = json.loads(body)["AcsEventCond"]
        ordered = list(reversed(self.log)) if self.reverse else list(self.log)
        start = cond["searchResultPosition"]
        page = ordered[start : start + acslog.PAGE]
        payload = {
            "AcsEvent": {
                "searchID": cond["searchID"],
                "numOfMatches": len(page),
                "totalMatches": len(self.log),
                "InfoList": page,
            }
        }
        return SimpleNamespace(text=json.dumps(payload))


def _entry(serial: int) -> dict[str, Any]:
    return {
        "major": 5,
        "minor": 75,
        "time": f"2026-11-14T08:{serial // 60:02d}:{serial % 60:02d}+01:00",
        "serialNo": serial,
        "doorNo": 1,
    }


@pytest.mark.parametrize("reverse", [True, False])
async def test_poll_reads_every_entry_that_came_since_the_last_poll(reverse: bool) -> None:
    log = [_entry(n) for n in range(1, 41)]
    access = acslog.AccessLog(FakeLink(log, reverse=reverse))
    assert await access.poll() == []  # the first poll only marks where the log stands

    log.extend(_entry(n) for n in range(41, 111))  # more than three pages since the last poll
    frames = await access.poll()

    assert [f.extra["serial"] for f in frames] == list(range(41, 111))
    assert await access.poll() == []
    assert access.stats.entries == 70


async def test_poll_after_a_cleared_log_continues_from_the_new_entries() -> None:
    log = [_entry(n) for n in range(1, 11)]
    access = acslog.AccessLog(FakeLink(log))
    await access.poll()

    log[:] = [_entry(n) for n in range(1, 4)]  # the log was cleared and started over
    assert await access.poll() == []

    log.append(_entry(4))
    assert [f.extra["serial"] for f in await access.poll()] == [4]
