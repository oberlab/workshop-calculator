"""A fake Kimai Cloud for tests and local tries (WM-142): invented users, no real Kimai.

`metrics fake-kimai --port 8765` serves the endpoints the app reads, in the shape `metrics.kimai_map`
assumes, at `http://127.0.0.1:8765/api` (set `KIMAI_URL` to it and `KIMAI_TOKEN` to the token it
prints). The users are the people of `metrics demo-data` (`example.org` e-mails), except that Mira
Testfeld has another e-mail in Kimai (to be assigned by hand) and Kimai has an extra user nobody in
the app matches. Their contracts give the demo's weekly hours in each contract type; Ben Vorlage has
no contract. A few absences lie around the start day (one of them only requested).

- `GET /api/users`, `/api/users/{id}`, `/api/users/me`, `/api/absences?user=&begin=&end=`,
  `/api/absences/calendar`, `/api/absences/types`, `/api/version`: as Kimai answers them, with
  `Authorization: Bearer <token>`. The token's own user is "Buchhaltung" (`ME`). The list carries
  the e-mail as Kimai 2.72 does (`list_email=False` leaves it out); only the single user has
  `preferences` and `roles`.
- Two switches play a token with too few permissions (`--eingeschraenkt`): `others_without_contract`
  hides the contract preferences of everyone but the token's own user, `absences_own_only` answers
  403 to absences of other users.
- Anything but GET on `/api/…` is refused with 405 and counted: the app never writes to Kimai.
- For trying it out (no token): `POST /_abwesenheit?email=…&date=2026-11-20&type=holiday&days=1`
  adds an approved absence (one per day, weekends skipped); `POST /_entfernen?id=…` removes one
  (as a cancelled vacation would); `POST /_wochenstunden?email=…&hours=32` sets a week contract;
  `GET /_state` shows everything as JSON.
"""

import argparse
import datetime as dt
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from metrics import kimai_map as km

DEFAULT_TOKEN = "fake-kimai-token"
ME = 7  # the token's own user ("Buchhaltung")


def _contract(kind: str, hours: int) -> list[dict]:
    if kind == "day":
        per_day = hours * 3600 // 4  # four working days
        days = [{"name": n, "value": str(per_day)} for n in km.DAY_HOURS[:4]]
        return [{"name": km.CONTRACT_TYPE, "value": "day"}, *days, {"name": km.DAY_HOURS[4], "value": "0"}]
    if kind == "month":
        monthly = int(hours * 3600 * km.WEEKS_PER_MONTH)
        return [
            {"name": km.CONTRACT_TYPE, "value": "month"},
            {"name": km.HOURS_PER_MONTH, "value": str(monthly)},
        ]
    return [
        {"name": km.CONTRACT_TYPE, "value": "week"},
        {"name": km.HOURS_PER_WEEK, "value": str(hours * 3600)},
        {"name": "work_days_week", "value": "1,2,3,4,5"},
    ]


# username, alias, e-mail, enabled, contract type and weekly hours (None: no contract)
USERS = (
    ("kim.beispiel", "Kim Beispiel", "kim.beispiel@example.org", True, ("day", 20)),
    ("lea.muster", "Lea Muster", " Lea.Muster@Example.org ", True, ("week", 30)),
    ("sam.demo", "Sam Demo", "sam.demo@example.org", True, ("week", 25)),
    ("jo.probe", "Jo Probe", "jo.probe@example.org", True, ("month", 20)),
    ("mira", "Mira Testfeld", "mira@privat.example.net", True, ("week", 15)),
    ("ben.vorlage", "Ben Vorlage", "ben.vorlage@example.org", True, None),
    ("buchhaltung", "Buchhaltung", "buchhaltung@example.org", True, ("week", 40)),
    ("ute.ehemalig", "Ute Ehemalig", "ute.ehemalig@example.org", False, None),
)


def _workday(day: dt.date, offset: int) -> dt.date:
    """The `offset`-th working day from `day` (0: `day` itself or the next working day)."""
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    for _ in range(offset):
        day += dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
    return day


@dataclass
class FakeKimai:
    token: str = DEFAULT_TOKEN
    users: dict[int, dict] = field(default_factory=dict)
    absences: dict[int, dict] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    list_email: bool = True  # the user list carries the e-mail (Kimai 2.72)
    others_without_contract: bool = False  # a token that may not see other users' contracts
    absences_own_only: bool = False  # a token that may not read other users' absences (403)
    _next: int = 100

    @classmethod
    def with_demo_users(cls, today: dt.date, token: str = DEFAULT_TOKEN) -> "FakeKimai":
        fake = cls(token=token)
        for i, (username, alias, email, enabled, contract) in enumerate(USERS, start=1):
            fake.users[i] = {
                "id": i,
                "username": username,
                "alias": alias,
                "email": email,
                "enabled": enabled,
                "preferences": [{"name": "timezone", "value": "Europe/Berlin"}]
                + (_contract(*contract) if contract else []),
                "roles": ["ROLE_TEAMLEAD", "ROLE_USER"] if i == ME else ["ROLE_USER"],
            }
        lea, sam, jo, kim = 2, 3, 4, 1
        next_monday = today + dt.timedelta(days=7 - today.weekday())
        fake.add(lea, next_monday, "holiday", days=3)
        fake.add(sam, _workday(today, 1), "sickness")
        fake.add(kim, _workday(today, 3), "holiday", half_day=True)
        fake.add(jo, _workday(next_monday, 1), "holiday", status="requested")
        return fake

    def user_by_email(self, email: str) -> dict | None:
        key = km.email_key(email)
        return next((u for u in self.users.values() if km.email_key(u["email"]) == key), None)

    def add(
        self, user_id: int, day: dt.date, kind: str, days: int = 1, status: str = "approved", half_day=False
    ) -> list[int]:
        """An absence of `days` working days from `day`: one entry per day, as Kimai stores them."""
        ids = []
        for n in range(days):
            self._next += 1
            self.absences[self._next] = {
                "id": self._next,
                "user": user_id,
                "date": _workday(day, n).isoformat(),
                "duration": 14400 if half_day else 28800,
                "type": kind,
                "status": status,
                "halfDay": half_day,
            }
            ids.append(self._next)
        return ids

    def set_hours(self, user_id: int, hours: float) -> None:
        user = self.users[user_id]
        keep = [p for p in user["preferences"] if p["name"] not in km.CONTRACT_PREFERENCES]
        user["preferences"] = keep + [
            {"name": km.CONTRACT_TYPE, "value": "week"},
            {"name": km.HOURS_PER_WEEK, "value": str(int(hours * 3600))},
        ]

    # What the API answers ------------------------------------------------------------------------

    def _user_json(self, user: dict, full: bool) -> dict:
        found = {k: user[k] for k in ("id", "username", "alias", "enabled")}
        found |= {"title": None, "color": None, "accountNumber": None, "initials": user["alias"][:2].upper()}
        if full or self.list_email:
            found["email"] = user["email"]
        if full:
            prefs = user["preferences"]
            if self.others_without_contract and user["id"] != ME:
                prefs = [p for p in prefs if p["name"] not in km.CONTRACT_PREFERENCES]
            found |= {"preferences": prefs, "teams": [], "roles": user.get("roles", ["ROLE_USER"])}
        return found

    def _event_json(self, absence: dict) -> dict:
        """[assumed] Kimai's CalendarEvent: title, start, end, allDay, colors."""
        return {
            "title": absence["type"],
            "start": absence["date"],
            "end": absence["date"],
            "allDay": not absence["halfDay"],
            "color": "#aaaaaa",
            "textColor": "#000000",
        }

    def _absence_json(self, absence: dict) -> dict:
        user = self.users[absence["user"]]
        return {**absence, "user": self._user_json(user, False)}

    def answer(self, path: str, query: dict[str, str]):
        """(status, body) of a GET on the API."""
        path = path.removeprefix(km.API)
        if path == km.VERSION:
            return 200, {"version": "2.40.0", "versionId": 24000, "copyright": "fake"}
        if path == km.USERS:
            visible = query.get("visible", "1")
            users = [u for u in self.users.values() if visible == "3" or u["enabled"] == (visible == "1")]
            return 200, [self._user_json(u, False) for u in users]
        if path == km.ME:
            return 200, self._user_json(self.users[ME], True)
        if path.startswith("/users/"):
            user = self.users.get(int(path.rsplit("/", 1)[1])) if path.rsplit("/", 1)[1].isdigit() else None
            return (200, self._user_json(user, True)) if user else (404, {"message": "Not found"})
        if path == km.ABSENCE_TYPES:
            return 200, {k: k.replace("_", " ").title() for k in km.KINDS}
        if path in (km.ABSENCES, km.ABSENCES_CALENDAR):
            user = int(query["user"]) if query.get("user", "").isdigit() else ME
            if self.absences_own_only and user != ME:
                return 403, {"message": "Access denied."}
            begin = dt.date.fromisoformat(query["begin"][:10]) if query.get("begin") else dt.date.min
            end = dt.date.fromisoformat(query["end"][:10]) if query.get("end") else dt.date.max
            found = [
                a
                for a in sorted(self.absences.values(), key=lambda a: (a["date"], a["id"]))
                if a["user"] == user and begin <= dt.date.fromisoformat(a["date"]) <= end
            ]
            if path == km.ABSENCES_CALENDAR:
                return 200, [self._event_json(a) for a in found]
            return 200, [self._absence_json(a) for a in found]
        return 404, {"message": "Not found"}

    def state(self) -> dict:
        return {
            "users": list(self.users.values()),
            "absences": list(self.absences.values()),
            "writes": self.writes,
        }


def _handler(fake: FakeKimai):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _send(self, status: int, body=None) -> None:
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            with fake.lock:
                if url.path == "/_state":
                    return self._send(200, fake.state())
                fake.requests.append(f"GET {url.path}")
                if not url.path.startswith(km.API + "/"):
                    return self._send(404, {"message": "Not found"})
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send(401, {"message": "Unauthorized"})
                try:
                    status, body = fake.answer(url.path, query)
                except (KeyError, ValueError):
                    status, body = 400, {"message": "Bad request"}
            self._send(status, body)

        def _write(self) -> None:
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            with fake.lock:
                if url.path.startswith(km.API):
                    fake.writes.append(f"{self.command} {url.path}")
                    return self._send(405, {"message": "The app never writes to Kimai."})
                if self.command != "POST":
                    return self._send(405)
                try:
                    if url.path == "/_abwesenheit":
                        user = fake.user_by_email(query["email"])
                        if user is None:
                            return self._send(404, {"message": "unknown e-mail"})
                        ids = fake.add(
                            user["id"],
                            dt.date.fromisoformat(query["date"]),
                            query.get("type", "holiday"),
                            int(query.get("days", "1")),
                            query.get("status", "approved"),
                            query.get("half") in ("1", "true"),
                        )
                        return self._send(201, {"ids": ids})
                    if url.path == "/_entfernen":
                        removed = fake.absences.pop(int(query["id"]), None)
                        return self._send(204 if removed else 404)
                    if url.path == "/_wochenstunden":
                        user = fake.user_by_email(query["email"])
                        if user is None:
                            return self._send(404, {"message": "unknown e-mail"})
                        fake.set_hours(user["id"], float(query["hours"].replace(",", ".")))
                        return self._send(204)
                except (KeyError, ValueError):
                    return self._send(400, {"message": "Bad request"})
            self._send(404, {"message": "Not found"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _write  # noqa: N815

    return Handler


def serve(
    host: str = "127.0.0.1", port: int = 0, today: dt.date | None = None, token: str = DEFAULT_TOKEN
) -> tuple[ThreadingHTTPServer, FakeKimai, str]:
    """Start the fake in a thread; returns (server, fake, KIMAI_URL)."""
    fake = FakeKimai.with_demo_users(today or dt.date.today(), token)
    server = ThreadingHTTPServer((host, port), _handler(fake))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, fake, f"http://{host}:{server.server_address[1]}{km.API}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--today", type=dt.date.fromisoformat, help="day the absences lie around")
    parser.add_argument("--token", help="the token to expect (default: a fixed one for tries)")
    parser.add_argument(
        "--eingeschraenkt",
        action="store_true",
        help="play a token without the permissions for other users' contracts and absences",
    )
    args = parser.parse_args(argv)
    token = args.token or DEFAULT_TOKEN
    server, fake, url = serve(args.host, args.port, args.today, token)
    fake.others_without_contract = fake.absences_own_only = args.eingeschraenkt
    print(f"Fake Kimai at {url}: KIMAI_URL={url} KIMAI_TOKEN={token}", flush=True)
    print(f"State: http://{args.host}:{args.port}/_state; Ctrl+C stops it.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


__all__ = ["DEFAULT_TOKEN", "FakeKimai", "main", "serve"]
