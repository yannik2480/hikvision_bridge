"""Region host handling of the Hik-Connect account: foreign hosts are never adopted."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hikvision_bridge.cloud.account import (
    DEFAULT_HOST,
    CloudAccount,
    CloudError,
    trusted_region_host,
)


def _account(responses: list[dict], region: str | None = None) -> tuple[CloudAccount, AsyncMock]:
    acct = CloudAccount(None, "a@example.invalid", "pw", region)  # type: ignore[arg-type]
    send = AsyncMock(side_effect=responses)
    acct._exchange = send
    return acct, send


@pytest.mark.parametrize(
    "domain",
    [
        "evil.example.invalid",
        "hik-connect.com.evil.invalid",
        "evil-hik-connect.com",
        "a.hik-connect.com:8443",
        "x.hik-connect.com/y",
        "u@hik-connect.com",
    ],
)
@pytest.mark.parametrize("code", [1100, 200])
async def test_foreign_api_domain_is_not_adopted(domain: str, code: int) -> None:
    acct, send = _account([{"meta": {"code": code}, "loginArea": {"apiDomain": domain}}])
    with pytest.raises(CloudError):
        await acct.login()
    assert acct.region_host == DEFAULT_HOST
    assert send.await_count == 1


@pytest.mark.parametrize("stored", ["evil.example.invalid", "127.0.0.1", "", None, 5])
def test_stored_foreign_region_host_is_ignored(stored: object) -> None:
    acct, _ = _account([], stored)  # type: ignore[arg-type]
    assert acct.region_host == DEFAULT_HOST


def test_legitimate_region_host_is_kept() -> None:
    acct, _ = _account([], "apiieu.hik-connect.com")
    assert acct.region_host == "apiieu.hik-connect.com"
    assert trusted_region_host("API.Hik-Connect.com") == "api.hik-connect.com"


async def test_legitimate_redirect_still_works() -> None:
    acct, _send = _account(
        [
            {"meta": {"code": 1100}, "loginArea": {"apiDomain": "apiieu.hik-connect.com"}},
            {
                "meta": {"code": 200},
                "loginSession": {"sessionId": "s1", "rfSessionId": "r1"},
            },
        ]
    )
    await acct.login()
    assert acct.region_host == "apiieu.hik-connect.com"
