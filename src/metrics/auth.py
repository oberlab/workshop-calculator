"""Team access for the analysis page: login modes and the signed session cookie.

`AUTH_MODE` selects how people log in (standard library only, no extra packages):

- `password` (default): one shared team password, stored as a hash in `TEAM_PASSWORD_HASH`
  (create it with `metrics hash-password`). The hash uses colons, not `$`, so it can sit in
  a Docker Compose `.env` file without escaping. Without a hash the page stays locked.
- `oidc`: single sign-on with an OpenID Connect provider such as Keycloak (see `metrics.oidc`).
- `off`: no login at all, for local testing only. The page shows a warning.

After a successful login the app sets its own signed cookie, valid for 14 days.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from functools import lru_cache

from metrics.config import get_settings

COOKIE_NAME = "metrics_team"
SESSION_SECONDS = 14 * 24 * 3600
OFF_USER = "Test (Anmeldung ausgeschaltet)"

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def mode() -> str:
    return get_settings().auth_mode


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt:{_SCRYPT['n']}:{_SCRYPT['r']}:{_SCRYPT['p']}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(digest))


def check_team_password(password: str) -> bool:
    return verify_password(password, get_settings().team_password_hash)


def password_configured() -> bool:
    return bool(get_settings().team_password_hash)


@lru_cache
def _secret() -> bytes:
    # Without SESSION_SECRET every restart logs everyone out, which is acceptable.
    configured = get_settings().session_secret
    return configured.encode() if configured else secrets.token_bytes(32)


def _sign(payload: str, secret: bytes) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def sign(data: dict, max_age: int, now: float | None = None, secret: bytes | None = None) -> str:
    """A tamper-proof cookie value holding `data` until `max_age` seconds from now."""
    body = json.dumps({**data, "exp": int((now or time.time()) + max_age)}, separators=(",", ":"))
    payload = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload, secret or _secret())}"


def unsign(value: str | None, now: float | None = None, secret: bytes | None = None) -> dict | None:
    """The data of a value made by `sign`, or None if it is forged, malformed or expired."""
    if not value:
        return None
    payload, _, signature = value.rpartition(".")
    if not hmac.compare_digest(signature, _sign(payload, secret or _secret())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data if int(data["exp"]) > (now or time.time()) else None
    except (ValueError, KeyError, TypeError):
        return None


def make_token(user: str, now: float | None = None, secret: bytes | None = None) -> str:
    return sign({"tier": "team", "user": user}, SESSION_SECONDS, now, secret)


def token_user(token: str | None, now: float | None = None, secret: bytes | None = None) -> str | None:
    """The logged-in user's display name, or None if the cookie is missing or invalid."""
    data = unsign(token, now, secret)
    return str(data.get("user") or "") if data and data.get("tier") == "team" else None


def current_user(token: str | None) -> str | None:
    if mode() == "off":
        return OFF_USER
    return token_user(token)
