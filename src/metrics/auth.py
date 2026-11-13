"""Team access: login modes, the password hash and signed short-lived values.

`AUTH_MODE` selects how people log in (standard library only, no extra packages):

- `oidc`: single sign-on with an OpenID Connect provider such as Keycloak (see `metrics.oidc`).
- `password` (default): one shared team password, stored as a hash in `TEAM_PASSWORD_HASH`
  (create it with `metrics hash-password`). The hash uses colons, not `$`, so it can sit in
  a Docker Compose `.env` file without escaping. It works only with `ALLOW_PASSWORD_LOGIN=true`;
  without that or without a hash the team pages stay locked.
- `off`: no login at all, for local testing only. The page shows a warning.

After a successful login the app opens a server-side session (`metrics.logins`). It records
whether the person is an admin (only with `oidc`, as a member of `OIDC_ADMIN_GROUP`; `off`
counts as admin so the admin pages can be tried locally). `sign` and `unsign` protect the
short-lived OpenID Connect state cookie.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from functools import lru_cache

from metrics.config import get_settings

COOKIE_NAME = "metrics_session"
OFF_USER = "Test (Anmeldung ausgeschaltet)"


@dataclass(frozen=True)
class Login:
    user: str
    admin: bool = False
    subject: str = ""  # stable account ID ("sub" with OpenID Connect), for "Überall abmelden"
    # The login's verified e-mail, only right after an OpenID Connect login: it links the login to
    # its Person once (`metrics.lists.link_login`); not kept in the session.
    email: str | None = field(default=None, compare=False)


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


def password_login_allowed() -> bool:
    """The shared password works only when switched on explicitly (off on the server)."""
    return mode() == "password" and get_settings().allow_password_login


@lru_cache
def _secret() -> bytes:
    # Without SESSION_SECRET a restart only cancels logins in progress (the provider round trip).
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
