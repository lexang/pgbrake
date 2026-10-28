"""Offline Pro license keys.

A key is ``PGB1.<payload>.<signature>``: base64url JSON payload signed with
Ed25519. Verification needs only the public key below, so it works offline
and never phones home.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PUBLIC_KEY = "FoDihWYHnDjt-jZNoxKDxuVBRkBemd7yiqy5Tcq95xk="
PREFIX = "PGB1"
BUY_URL = "https://github.com/lexang/pgbrake#pro"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def license_path() -> Path:
    return Path(os.environ.get("PGBRAKE_HOME", Path.home() / ".pgbrake")) / "license"


@dataclass
class License:
    valid: bool
    email: str = ""
    plan: str = ""
    order: str = ""
    error: str = ""


def verify(key: str, public_key: str = PUBLIC_KEY) -> License:
    try:
        prefix, payload, sig = key.strip().split(".")
        if prefix != PREFIX:
            return License(False, error="not a pgbrake key")
        Ed25519PublicKey.from_public_bytes(_b64d(public_key)).verify(_b64d(sig), f"{prefix}.{payload}".encode())
        data = json.loads(_b64d(payload))
    except InvalidSignature:
        return License(False, error="signature does not match")
    except Exception:
        return License(False, error="malformed key")
    return License(True, email=data.get("email", ""), plan=data.get("plan", "pro"), order=data.get("order", ""))


def sign(payload: dict, private_key) -> str:
    """Issue a key (used by the seller's webhook, not by users)."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = private_key.sign(f"{PREFIX}.{body}".encode())
    return f"{PREFIX}.{body}.{_b64e(sig)}"


def load_key() -> str | None:
    if os.environ.get("PGBRAKE_LICENSE"):
        return os.environ["PGBRAKE_LICENSE"]
    p = license_path()
    return p.read_text().strip() if p.exists() else None


def current() -> License:
    key = load_key()
    return verify(key) if key else License(False, error="no license")


def is_pro() -> bool:
    return current().valid


def activate(key: str) -> License:
    lic = verify(key)
    if lic.valid:
        p = license_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key.strip() + "\n")
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return lic
