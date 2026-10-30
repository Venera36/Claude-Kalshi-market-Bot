"""Kalshi request signing. Signature = base64( sign( timestamp_ms + METHOD + path_without_query ) ), using RSA-PSS/SHA-256
for RSA keys or Ed25519 for Ed25519 keys (Kalshi's default key type). The private key never leaves this process."""
import base64
import binascii
import re
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from . import config as C


class Signer:
    def __init__(self, key_id, pem: bytes, der=False):
        if not key_id:
            raise ValueError("KALSHI_API_KEY_ID is empty")
        self.key_id = key_id.strip().strip("'\"")
        self.key = (serialization.load_der_private_key(pem, password=None) if der
                    else serialization.load_pem_private_key(pem, password=None))
        if not isinstance(self.key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise ValueError("unsupported key type (need RSA or Ed25519)")

    def headers(self, method, full_path):
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + full_path.split("?")[0]).encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def normalize_pem(text):
    """Rebuild a PEM key that lost its line breaks (Railway's one-line variable field turns them into spaces),
    or that still has literal "\\n" escapes or surrounding quotes. Returns bytes, or None if there is no PEM block."""
    t = text.strip().strip("'\"").replace("\\n", "\n").replace("\\r", "")
    m = PEM_RE.search(t)
    if not m:
        return None
    body = re.sub(r"[^A-Za-z0-9+/=]", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (m.group(1), "\n".join(lines), m.group(1))).encode()


def _key_from_text(text, var):
    """PEM text, base64 of a PEM file, or base64 of a DER key -> ('pem'|'der', bytes)."""
    pem = normalize_pem(text)
    if pem:
        return "pem", pem
    compact = re.sub(r"\s", "", text.strip().strip("'\""))
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{var} is neither a PEM key (-----BEGIN ... PRIVATE KEY-----) nor base64")
    pem = normalize_pem(raw.decode("latin-1"))
    if pem:
        return "pem", pem
    if raw[:1] == b"\x30":                                   # DER-encoded key (base64 without PEM headers)
        return "der", raw
    raise ValueError(f"{var} decodes from base64 but does not contain a private key")


def load_key_bytes():
    for var, val in (("KALSHI_PRIVATE_KEY_B64", C.KALSHI_PRIVATE_KEY_B64), ("KALSHI_PRIVATE_KEY", C.KALSHI_PRIVATE_KEY)):
        if val.strip():
            return _key_from_text(val, var)
    if C.KALSHI_PRIVATE_KEY_PATH:
        with open(C.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            return _key_from_text(f.read().decode("latin-1"), "KALSHI_PRIVATE_KEY_PATH")
    raise ValueError("no private key: set KALSHI_PRIVATE_KEY_B64, KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH")


def load_pem():
    """Kept for compatibility: the key as PEM bytes when it is PEM."""
    kind, data = load_key_bytes()
    return data


def load_signer():
    kind, data = load_key_bytes()
    return Signer(C.KALSHI_API_KEY_ID, data, der=(kind == "der"))


def base_path(base_url):
    return urlparse(base_url).path.rstrip("/")      # e.g. /trade-api/v2 (part of what gets signed)
