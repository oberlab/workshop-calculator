"""Single sign-on with OpenID Connect (tested with Keycloak), standard library only.

Authorization code flow with PKCE for a confidential client:

1. `/login` redirects to the provider with a random `state`, `nonce` and PKCE challenge,
   kept in a short-lived signed cookie.
2. The provider sends the browser back to `/login/callback` with a code. The app exchanges
   it directly at the token endpoint (server to server over TLS, with the client secret),
   which is why the ID token's signature does not need to be checked separately
   (OpenID Connect Core 3.1.3.7); issuer, audience, nonce and expiry are still checked.
3. Optionally only members of one group may log in (`OIDC_ALLOWED_GROUP`). Keycloak puts
   groups into the token only with a "Group Membership" mapper on the client; the claim name
   is `groups` unless `OIDC_GROUPS_CLAIM` says otherwise (useful when a shared client scope
   already fills `groups` with something else). Members of `OIDC_ADMIN_GROUP` are admins
   and may always log in.
"""

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
from functools import lru_cache

from metrics.auth import Login
from metrics.config import get_settings

STATE_COOKIE = "metrics_oidc"
STATE_SECONDS = 600
TIMEOUT = 10


class OidcError(Exception):
    """Login failed; the message is shown to the user (German)."""


def configured() -> bool:
    s = get_settings()
    return bool(s.oidc_issuer and s.oidc_client_id and s.oidc_client_secret)


def _get_json(url: str, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


@lru_cache
def discovery() -> dict:
    issuer = get_settings().oidc_issuer.rstrip("/")
    return _get_json(f"{issuer}/.well-known/openid-configuration")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def start(redirect_uri: str) -> tuple[str, dict]:
    """The provider's login URL and the values to keep until the callback."""
    s = get_settings()
    flow = {
        "state": secrets.token_urlsafe(24),
        "nonce": secrets.token_urlsafe(24),
        "verifier": secrets.token_urlsafe(48),
    }
    query = {
        "response_type": "code",
        "client_id": s.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": flow["state"],
        "nonce": flow["nonce"],
        "code_challenge": _b64url(hashlib.sha256(flow["verifier"].encode()).digest()),
        "code_challenge_method": "S256",
    }
    return f"{discovery()['authorization_endpoint']}?{urllib.parse.urlencode(query)}", flow


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as error:
        raise OidcError("Ungültige Antwort des Anmeldedienstes.") from error


def check_claims(claims: dict, nonce: str, now: float | None = None) -> None:
    s = get_settings()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") != discovery()["issuer"] or s.oidc_client_id not in audiences:
        raise OidcError("Die Anmeldung stammt nicht vom erwarteten Anmeldedienst.")
    if claims.get("nonce") != nonce:
        raise OidcError("Die Anmeldung ist ungültig. Bitte noch einmal versuchen.")
    if int(claims.get("exp", 0)) < (now or time.time()):
        raise OidcError("Die Anmeldung ist abgelaufen. Bitte noch einmal versuchen.")


def in_allowed_group(claims: dict, group: str | None, claim: str = "groups") -> bool:
    if not group:
        return True
    # Keycloak sends "/team" with "full group path" on, "team" without; accept both.
    wanted = group.strip("/")
    values = claims.get(claim) or []
    if isinstance(values, str):  # a mapper with "multivalued" off sends a single string
        values = [values]
    return any(str(g).strip("/") == wanted for g in values)


def display_name(claims: dict) -> str:
    return claims.get("name") or claims.get("preferred_username") or claims.get("email") or "Team"


def finish(params: dict, flow: dict | None, redirect_uri: str) -> Login:
    """Check the callback, exchange the code and return who logged in."""
    if params.get("error"):
        raise OidcError("Die Anmeldung wurde abgebrochen.")
    if not flow or not params.get("state") or params["state"] != flow.get("state"):
        raise OidcError("Die Anmeldung ist abgelaufen oder ungültig. Bitte noch einmal versuchen.")
    s = get_settings()
    try:
        tokens = _post_form(
            discovery()["token_endpoint"],
            {
                "grant_type": "authorization_code",
                "code": params.get("code", ""),
                "redirect_uri": redirect_uri,
                "client_id": s.oidc_client_id,
                "client_secret": s.oidc_client_secret,
                "code_verifier": flow["verifier"],
            },
        )
    except OSError as error:  # includes HTTPError and timeouts
        raise OidcError("Der Anmeldedienst ist nicht erreichbar oder hat die Anmeldung abgelehnt.") from error
    claims = _claims(tokens.get("id_token", ""))
    check_claims(claims, flow["nonce"])
    admin = bool(s.oidc_admin_group) and in_allowed_group(claims, s.oidc_admin_group, s.oidc_groups_claim)
    if not admin and not in_allowed_group(claims, s.oidc_allowed_group, s.oidc_groups_claim):
        raise OidcError("Dein Konto hat keinen Zugriff auf die Analyse.")
    return Login(display_name(claims), admin)
