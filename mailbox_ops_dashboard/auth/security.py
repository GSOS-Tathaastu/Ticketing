"""Password hashing using stdlib pbkdf2 (no external crypto dependency).

Format stored in User.password_hash:  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
The auth layer is deliberately isolated so it can be swapped for LDAP/AD/SSO
later without touching the rest of the app.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from config.settings import settings

_ALGO = "pbkdf2_sha256"


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or settings.pbkdf2_iterations
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False
