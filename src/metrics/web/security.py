"""Request and response protection (`PROPOSAL.md` section 7): CSRF and Origin checks on every POST,
security headers with a strict Content-Security-Policy, and simple in-process rate limits.

CSRF uses a double-submit token: a random value in an HttpOnly cookie that every form repeats in the
hidden field `csrf_token` (htmx requests send it in the `X-CSRF-Token` header instead). Together with
the `Origin` check and `SameSite=Lax` cookies a request from another site cannot pass.
"""

import hmac
import secrets
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request

from metrics import auth

CSRF_COOKIE = "metrics_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def is_secure(request: Request) -> bool:
    """Served over https (directly, through the trusted proxy, or as configured in PUBLIC_URL)."""
    public = auth.get_settings().public_url or ""
    return request.url.scheme == "https" or public.startswith("https://")


def set_cookie(request: Request, response, name: str, value: str, max_age: int | None, path: str = "/"):
    """Every cookie of the app: HttpOnly, SameSite=Lax, Secure when served over https."""
    response.set_cookie(
        name, value, max_age=max_age, path=path, httponly=True, samesite="lax", secure=is_secure(request)
    )


# --- CSRF and Origin ---------------------------------------------------------------------------


def csrf_token(request: Request) -> str:
    """The token for forms on this page; a new one if the browser has none yet."""
    token = request.cookies.get(CSRF_COOKIE) or getattr(request.state, "new_csrf_token", None)
    if not token:
        token = request.state.new_csrf_token = secrets.token_urlsafe(32)
    return token


def remember_csrf_token(request: Request, response) -> None:
    """Set the cookie if `csrf_token` made a new token for this response."""
    token = getattr(request.state, "new_csrf_token", None)
    if token:
        set_cookie(request, response, CSRF_COOKIE, token, max_age=None)


def _allowed_origins(request: Request) -> set[str]:
    origins = {f"{request.url.scheme}://{request.url.netloc}"}
    public = auth.get_settings().public_url
    if public:
        parts = urlsplit(public)
        origins.add(f"{parts.scheme}://{parts.netloc}")
    return origins


async def check_csrf(request: Request) -> None:
    """App-wide dependency: a POST must come from this site and carry the form's token."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("origin") not in _allowed_origins(request):
        raise HTTPException(403, "Anfrage abgelehnt")
    expected = request.cookies.get(CSRF_COOKIE) or ""
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        # Read and keep the body: the handlers parse it again.
        form = parse_qs((await request.body()).decode(errors="replace"))
        sent = (form.get(CSRF_FIELD) or [""])[0]
    if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
        raise HTTPException(403, "Anfrage abgelehnt")


# --- headers -----------------------------------------------------------------------------------


class SecurityHeaders:
    """ASGI middleware adding the security headers to every response, static files included."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(n, v) for n, v in SECURITY_HEADERS if n not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --- rate limits -------------------------------------------------------------------------------


class RateLimit:
    """At most `limit` requests per client address within `window` seconds (in this process)."""

    MAX_CLIENTS = 10_000  # bounds the memory; the oldest entries are dropped first

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, request: Request) -> None:
        """Raise 429 when the client is over the limit; otherwise count the request."""
        key = request.client.host if request.client else "-"
        now = time.monotonic()
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            over = len(hits) >= self.limit
            if not over:
                hits.append(now)
            self._hits[key] = hits  # re-inserted: the dict stays ordered by last use
            while len(self._hits) > self.MAX_CLIENTS:
                self._hits.pop(next(iter(self._hits)))
        if over:
            raise HTTPException(429, "Zu viele Anfragen. Bitte später noch einmal versuchen.")

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
