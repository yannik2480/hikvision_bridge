"""Hikvision RTP encryption, including fragmented NALs and clear packets."""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from custom_components.hikvision_bridge.model.features.scrambling import cipher_key
from custom_components.hikvision_bridge.runtime.live import H264Decryptor, LiveError

CODE = "ABCDEF"


def encrypt(nal: bytes) -> bytes:
    count = min(4096, len(nal) // 16 * 16)
    aes = Cipher(algorithms.AES(cipher_key(CODE)), modes.ECB()).encryptor()
    return nal[:1] + aes.update(nal[:count]) + nal[count:]


def frame(payload: bytes, sequence: int = 1, *, flagged: bool = True, channel: int = 0) -> bytes:
    header = struct.pack("!BBHII", 0x90 if flagged else 0x80, 0xE0, sequence, 90000, 123)
    if flagged:
        header += b"\x40\x00\x00\x02\x80\x06\x00\x01\x11\x21\x02\x01"
    packet = header + payload
    return b"$" + bytes([channel]) + len(packet).to_bytes(2, "big") + packet


def payload(frame: bytes) -> bytes:
    packet = frame[4:]
    offset = 12 + (packet[0] & 15) * 4
    if packet[0] & 16:
        offset += 4 + int.from_bytes(packet[offset + 2 : offset + 4], "big") * 4
    return packet[offset:]


@pytest.mark.parametrize("size", [16, 17, 31, 32, 4000, 4096, 4097, 6000])
def test_encrypted_nal_boundaries(size: int) -> None:
    nal = b"\x65" + bytes(i % 251 for i in range(size - 1))
    original = frame(encrypt(nal))
    (result,) = H264Decryptor(CODE).feed(original)
    assert payload(result) == nal
    assert result[4:16] == original[4:16]  # timestamps, sequence, SSRC, marker
    assert len(result) == len(original) - 1


@pytest.mark.parametrize("sequence", [1, 65534])
def test_fragmented_nal_retains_timestamps_and_sequence(sequence: int) -> None:
    nal = b"\x65" + bytes(i % 251 for i in range(9000))
    encrypted = encrypt(nal)
    fragments = [encrypted[i : i + 700] for i in range(1, len(encrypted), 700)]
    decryptor = H264Decryptor(CODE)
    output = []
    originals = []
    for index, part in enumerate(fragments):
        header = bytes(
            [0x7C, 5 | (128 if index == 0 else 0) | (64 if index == len(fragments) - 1 else 0)]
        )
        original = frame(header + part, (sequence + index) % 65536, flagged=index == 0)
        originals.append(original)
        output += decryptor.feed(original)
    assert len(output) == len(originals)
    assert b"\x65" + b"".join(payload(f)[2:] for f in output) == nal
    assert [f[4:16] for f in output] == [f[4:16] for f in originals]


def test_clear_packets_remain_identical() -> None:
    decryptor = H264Decryptor(CODE)
    single = frame(b"\x67clear SPS", flagged=False)
    assert decryptor.feed(single) == [single]
    start = frame(b"\x7c\x85" + b"a" * 30, flagged=False)
    finish = frame(b"\x7c\x45" + b"b" * 30, 2, flagged=False)
    assert decryptor.feed(start) == []
    assert decryptor.feed(finish) == [start, finish]


def test_aggregation_and_wrong_code() -> None:
    nal = b"\x67" + b"a" * 45
    encrypted = encrypt(nal)
    packet = frame(b"\x78" + len(encrypted).to_bytes(2, "big") + encrypted)
    (result,) = H264Decryptor(CODE).feed(packet)
    assert payload(result) == b"\x78" + len(nal).to_bytes(2, "big") + nal
    with pytest.raises(LiveError, match="verification code does not fit"):
        H264Decryptor("WRONG1").feed(frame(encrypted))


def test_incomplete_fragments_are_discarded() -> None:
    decryptor = H264Decryptor(CODE)
    assert decryptor.feed(frame(b"\x7c\x85" + b"a" * 30)) == []
    assert decryptor.feed(frame(b"\x7c\x45" + b"b" * 30, 3)) == []
    assert decryptor.feed(frame(b"\x7c\x45" + b"b" * 30, 4)) == []
    fresh = frame(b"\x67clear SPS", flagged=False)
    assert decryptor.feed(fresh) == [fresh]


@pytest.mark.parametrize(
    "packet", [b"", b"abc", b"\x90" + bytes(11), b"\x90" + bytes(11) + b"\x40\x00\xff\xff"]
)
def test_malformed_rtp_is_rejected(packet: bytes) -> None:
    with pytest.raises(LiveError):
        H264Decryptor(CODE).feed(b"$\0" + len(packet).to_bytes(2, "big") + packet)
