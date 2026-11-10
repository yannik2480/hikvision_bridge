"""TLS contexts for the devices' HTTPS endpoints.

Some firmware (found on the DS-KD8003 door station) cannot be relied on for finite-field
Diffie-Hellman: with a DHE suite most requests arrive damaged and are answered with a bare 401,
which looks exactly like a rejected password. With plain RSA key exchange (all it offers apart
from DHE) the same requests succeed every time. Devices that speak ECDHE or TLS 1.3 are
unaffected, so DHE is simply left out of the offer.

The contexts are built when this module is imported (in an executor thread), because creating
one loads the CA certificates from disk, which must not happen in the event loop.
"""

from __future__ import annotations

import ssl

from homeassistant.util import ssl as ha_ssl

# Everything the default list offers, except finite-field Diffie-Hellman (TLS 1.2 and older;
# TLS 1.3 suites are not affected by this setting).
CIPHERS = "DEFAULT:!DHE:!kDHE:!kDHEPSK"


def _verified() -> ssl.SSLContext:
    context = ha_ssl.create_client_context()
    context.set_ciphers(CIPHERS)
    return context


def _unverified() -> ssl.SSLContext:
    # Same as Home Assistant's own no-verify context; that one is shared, so it is not reused.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.options |= ssl.OP_NO_COMPRESSION
    context.set_default_verify_paths()
    context.set_ciphers(CIPHERS)
    return context


_VERIFIED = _verified()
_UNVERIFIED = _unverified()


def device_context(verify: bool) -> ssl.SSLContext:
    """The client context for a device's HTTPS endpoint."""
    return _VERIFIED if verify else _UNVERIFIED
