"""Decrypting recordings made with stream encryption."""

from __future__ import annotations

import random

from aiohikvision.isapi.model.features import scrambling
from aiohikvision.isapi.model.features.scrambling import ProgramStream
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CODE = "ABCDEF"
VPS = b"\x40\x01" + b"\x0c\x01\xff\xff\x01\x60" + bytes(range(1, 16))  # 21 byte body
SPS = b"\x42\x01" + bytes(range(1, 60))


def _body(rnd: random.Random, size: int) -> bytes:
    """Slice data without zero bytes, so it holds no start codes."""
    return bytes(rnd.randint(1, 255) for _ in range(size))


def _encrypt(nal: bytes, header: int = 2) -> bytes:
    body = nal[header:]
    count = min(len(body), scrambling.ENCRYPTED_PREFIX) // 16 * 16
    aes = Cipher(algorithms.AES(scrambling.cipher_key(CODE)), modes.ECB()).encryptor()
    return nal[:header] + aes.update(body[:count]) + body[count:]


def _pts(value: int) -> bytes:
    return bytes(
        [
            0x21 | ((value >> 29) & 0x0E),
            (value >> 22) & 0xFF,
            ((value >> 14) & 0xFE) | 1,
            (value >> 7) & 0xFF,
            ((value << 1) & 0xFE) | 1,
        ]
    )


def _packets(frame: bytes, scrambled: bool, pts: int, size: int = 3000) -> bytes:
    """One frame in video PES packets of at most ``size`` payload bytes."""
    out = b""
    for at in range(0, len(frame), size):
        part = frame[at : at + size]
        header = _pts(pts) if at == 0 else b""
        flags = 0x80 | (0x20 if scrambled else 0)
        length = 3 + len(header) + len(part)
        out += (
            b"\x00\x00\x01\xe0"
            + length.to_bytes(2, "big")
            + bytes([flags, 0x80 if header else 0, len(header)])
            + header
            + part
        )
    return out


PACK = b"\x00\x00\x01\xba\x44\x00\x04\x00\x04\x01\x01\x89\xc3\xf8"


def recording(*, encrypted: bool, frames: int = 6) -> bytes:
    """Key frame (parameter sets and a long slice), then P-frames; every 3rd encrypted."""
    rnd = random.Random(7)
    out = b"IMKH" + bytes(36) + PACK
    for number in range(frames):
        if number == 0:
            nals = [VPS, SPS, b"\x26\x01" + _body(rnd, 9000)]
        else:
            nals = [b"\x02\x01" + _body(rnd, 1500 + 37 * number)]
        scrambled = number % 3 == 0
        if encrypted and scrambled:
            nals = [_encrypt(n) for n in nals]
        frame = b"".join(b"\x00\x00\x00\x01" + n for n in nals)
        out += PACK + _packets(frame, encrypted and scrambled, 3600 * number)
    return out


def _through(stream: ProgramStream, data: bytes, seed: int) -> bytes:
    rnd = random.Random(seed)
    out = b""
    at = 0
    while at < len(data):
        size = rnd.randint(1, 5000)
        out += stream.feed(data[at : at + size])
        at += size
    return out + stream.flush()


def test_decrypts_in_any_pieces() -> None:
    plain = recording(encrypted=False)
    encrypted = recording(encrypted=True)
    assert encrypted != plain
    for seed in range(5):
        stream = ProgramStream(scrambling.cipher_key(CODE), "hevc")
        assert _through(stream, encrypted, seed) == plain
        assert stream.key_fits is True
        assert stream.seconds == 5 * 3600 / 90000


def test_wrong_key_is_noticed() -> None:
    stream = ProgramStream(scrambling.cipher_key("WRONG1"), "hevc")
    _through(stream, recording(encrypted=True), 0)
    assert stream.key_fits is False


def test_passes_clear_streams_unchanged() -> None:
    plain = recording(encrypted=False)
    stream = ProgramStream()
    assert _through(stream, plain, 1) == plain
    assert stream.key_fits is None
    # Without a key, encrypted streams also pass unchanged (scrambling bits kept).
    encrypted = recording(encrypted=True)
    assert _through(ProgramStream(), encrypted, 2) == encrypted


def test_codec_is_detected() -> None:
    stream = ProgramStream(scrambling.cipher_key(CODE))
    assert _through(stream, recording(encrypted=True), 3) == recording(encrypted=False)
    assert stream.codec == "hevc"


def test_short_unit_keeps_partial_block() -> None:
    # 21 byte VPS body: one block encrypted, five bytes clear.
    encrypted = _encrypt(VPS)
    assert encrypted[2:18] != VPS[2:18]
    assert encrypted[18:] == VPS[18:]


def test_cipher_key() -> None:
    assert scrambling.cipher_key("ABCDEF") == b"ABCDEF" + bytes(10)
    assert scrambling.cipher_key("x" * 20) == b"x" * 16
