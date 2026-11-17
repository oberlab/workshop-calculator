"""Two addresses (WM-154): the public front page on FRONT_URL, the team part on PUBLIC_URL.

On the front address only the public front page, its data, the privacy notice, the imprint and the
files they need answer (`metrics.web.access.FRONT_ROUTES`); everything else is 404 there. On the team
address "/" leads to Mein Bereich or the login page instead of the front page. Without FRONT_URL the
app serves everything on one address, as before. Cookies are host-only, so the login cookie never
reaches the front address.
"""

from urllib.parse import urlsplit

from fastapi import Request

from metrics import auth


def front_url() -> str | None:
    return auth.get_settings().front_url


def is_split() -> bool:
    return front_url() is not None


def on_front_host(request: Request) -> bool:
    front = front_url()
    return front is not None and request.url.hostname == urlsplit(front).hostname


def front_href(request: Request) -> str:
    """Where the public front page is, seen from this page."""
    front = front_url()
    return "/" if front is None or on_front_host(request) else f"{front}/"


def login_href(request: Request) -> str:
    """Where the login page is, seen from this page: the team address from the front address."""
    if on_front_host(request):
        return f"{auth.get_settings().public_url}/login"
    return "/login"
