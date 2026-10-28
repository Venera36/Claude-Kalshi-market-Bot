"""Kalshi request signing. Signature = base64( sign( timestamp_ms + METHOD + path_without_query ) ), using RSA-PSS/SHA-256
for RSA keys or Ed25519 for Ed25519 keys (Kalshi's default key type). The private key never leaves this process."""
import base64
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from . import config as C


class Signer:
    def __init__(self, key_id, pem: bytes):
        if not key_id:
            raise ValueError("KALSHI_API_KEY_ID is empty")
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(pem, password=None)
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


def load_pem():
    if C.KALSHI_PRIVATE_KEY_B64.strip():
        return base64.b64decode(C.KALSHI_PRIVATE_KEY_B64.strip())
    if C.KALSHI_PRIVATE_KEY.strip():
        return C.KALSHI_PRIVATE_KEY.replace("\\n", "\n").strip().encode()
    if C.KALSHI_PRIVATE_KEY_PATH:
        with open(C.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            return f.read()
    raise ValueError("no private key: set KALSHI_PRIVATE_KEY_B64, KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH")


def load_signer():
    return Signer(C.KALSHI_API_KEY_ID, load_pem())


def base_path(base_url):
    return urlparse(base_url).path.rstrip("/")      # e.g. /trade-api/v2 (part of what gets signed)
