"""Masking, error classification and digest signing."""

import hashlib
import logging

from custom_components.hikvision_bridge.wire import faults
from custom_components.hikvision_bridge.wire.scrub import (
    MASK,
    ScrubbingFilter,
    scrub_text,
    scrub_url,
)
from custom_components.hikvision_bridge.wire.signer import (
    DigestSigner,
    parse_challenges,
    pick,
)


def test_scrub_masks_urls_tags_json_realm_and_mac() -> None:
    text = (
        "rtsp://user:p%40ss@10.0.0.1:554/x "
        "<serialNumber>ABC123</serialNumber><model>M</model> "
        '{"password": "x1", "name": "ok"} '
        'Digest realm="IP Camera(Z0000)" mac 0a:1b:2c:3d:4e:5f'
    )
    out = scrub_text(text, ["p%40ss"])
    assert "p%40ss" not in out
    assert "user:" not in out
    assert "ABC123" not in out
    assert "<model>M</model>" in out
    assert '"name": "ok"' in out
    assert "Z0000" not in out
    assert "0a:1b:2c:3d:4e:5f" not in out
    assert scrub_url("http://a:b@h/x") == f"http://{MASK}@h/x"


def test_log_filter_masks_message_and_exception() -> None:
    flt = ScrubbingFilter()
    flt.remember("hunter22")
    try:
        raise ValueError("rtsp://admin:hunter22@host/stream")
    except ValueError as err:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "failed %s", ("hunter22",), (type(err), err, None)
        )
    flt.filter(record)
    assert "hunter22" not in record.getMessage()
    assert record.exc_info is None


def test_classify_variants() -> None:
    not_support = "<ResponseStatus><statusCode>4</statusCode><subStatusCode>notSupport</subStatusCode></ResponseStatus>"
    assert isinstance(faults.classify(403, not_support), faults.Unsupported)
    invalid = "<ResponseStatus><statusCode>4</statusCode><subStatusCode>invalidOperation</subStatusCode></ResponseStatus>"
    assert isinstance(faults.classify(403, invalid), faults.Unsupported)
    low = "<ResponseStatus><subStatusCode>lowPrivilege</subStatusCode></ResponseStatus>"
    assert isinstance(faults.classify(403, low), faults.Forbidden)
    assert isinstance(faults.classify(404, ""), faults.Unsupported)
    assert isinstance(
        faults.classify(503, "<x><subStatusCode>serviceUnavailable</subStatusCode></x>"),
        faults.Unsupported,
    )
    bad = faults.classify(400, "<x><subStatusCode>badParameters</subStatusCode></x>")
    assert isinstance(bad, faults.DeviceRejected)
    assert bad.status == 400
    locked = faults.classify(
        403, '{"statusCode": 4, "subStatusCode": "userLocked", "unlockTime": 1800}'
    )
    assert isinstance(locked, faults.LockedOut)
    assert locked.retry_after == 1800
    assert (
        faults.classify(
            200,
            "<ResponseStatus><statusCode>1</statusCode><subStatusCode>ok</subStatusCode></ResponseStatus>",
        )
        is None
    )


def test_detail_names_what_the_device_said() -> None:
    assert (
        faults.detail(faults.DeviceRejected("Invalid Content", 400, "badParameters"))
        == "DeviceRejected (HTTP 400: Invalid Content badParameters)"
    )
    assert faults.detail(faults.DeviceRejected("HTTP 500", 500)) == "DeviceRejected (HTTP 500)"
    assert faults.detail(faults.Unreachable("TimeoutError: ")) == "Unreachable (TimeoutError: )"
    assert faults.detail(TimeoutError()) == "TimeoutError"


def test_challenge_parsing_both_device_styles() -> None:
    nvr = parse_challenges(
        [
            'Digest realm="abc", domain="/", qop="auth", nonce="n1:x", opaque="o", algorithm="MD5", stale="FALSE"'
        ]
    )
    cam = parse_challenges(['Digest qop="auth", realm="IP Camera(X)", nonce="n2", stale="FALSE"'])
    assert nvr[0].params["opaque"] == "o"
    assert nvr[0].params["nonce"] == "n1:x"
    assert "algorithm" not in cam[0].params
    assert not cam[0].stale
    both = parse_challenges(['Basic realm="r"', 'Digest realm="r", nonce="n", qop="auth"'])
    assert pick(both).scheme == "digest"
    assert pick(parse_challenges(['Basic realm="r"'])).scheme == "basic"


def test_digest_response_matches_rfc_formula() -> None:
    (challenge,) = parse_challenges(['Digest realm="r", nonce="n", qop="auth"'])
    signer = DigestSigner("u", "p", challenge)
    header = signer.authorization("GET", "/ISAPI/System/deviceInfo")
    fields = dict(part.strip().split("=", 1) for part in header[len("Digest ") :].split(", "))
    cnonce = fields["cnonce"].strip('"')
    ha1 = hashlib.md5(b"u:r:p").hexdigest()
    ha2 = hashlib.md5(b"GET:/ISAPI/System/deviceInfo").hexdigest()
    expected = hashlib.md5(f"{ha1}:n:00000001:{cnonce}:auth:{ha2}".encode()).hexdigest()
    assert fields["response"].strip('"') == expected
    assert fields["nc"] == "00000001"
    assert "algorithm" not in fields  # not offered, not sent
    second = signer.authorization("GET", "/x")
    assert "nc=00000002" in second
