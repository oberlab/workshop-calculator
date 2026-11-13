"""A fake CalDAV calendar for tests and local tries (WM-110): no real calendar, no e-mail.

`metrics fake-calendar --port 5232` serves one calendar at `http://127.0.0.1:5232/cal/` (set
`CALDAV_URL` to it). It keeps events in memory and plays Nextcloud's part in scheduling: when an
event with an `ORGANIZER` is created or changed, it writes the messages Nextcloud would send into
an outbox instead of mailing them:

- `REQUEST` to every attendee of a new event ("Einladung"), and to the remaining attendees when
  time, place, title or status change ("Aktualisierung");
- `CANCEL` to an attendee taken out of the event, and to everyone when the event is cancelled.

`GET /_outbox` shows the outbox as JSON; `POST /_antwort?uid=..&email=..&partstat=DECLINED` plays
an attendee's answer (the organizer's copy gets the PARTSTAT, as with Nextcloud). `DELETE` is
refused and counted: the app never deletes events. PUT honours `If-None-Match: *` and `If-Match`.
"""

import argparse
import datetime as dt
import json
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from metrics.planning.caldav import Event, _parse_time, parse_ics, to_ics

PREFIX = "/cal/"


@dataclass
class Message:
    method: str  # REQUEST or CANCEL
    to: str
    uid: str
    summary: str
    start: str
    sequence: int
    kind: str  # Einladung, Aktualisierung, Absage


@dataclass
class FakeCalendar:
    objects: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (ics, etag)
    outbox: list[Message] = field(default_factory=list)
    deletes: int = 0
    requests: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _version: int = 0

    def _etag(self) -> str:
        self._version += 1
        return f'"{self._version}"'

    def event(self, uid: str) -> Event | None:
        found = self.objects.get(f"{uid}.ics")
        return parse_ics(found[0])[0] if found else None

    def store(self, name: str, ics: str) -> str:
        etag = self._etag()
        self.objects[name] = (ics, etag)
        return etag

    def schedule(self, old: Event | None, new: Event) -> None:
        """What Nextcloud would send for this change (implicit scheduling)."""
        if not new.organizer:
            return
        before = {a.email.lower() for a in old.attendees} if old else set()
        after = {a.email.lower() for a in new.attendees}

        def send(method: str, to: str, kind: str) -> None:
            self.outbox.append(
                Message(method, to, new.uid, new.summary, f"{new.start:%Y-%m-%d %H:%M}", new.sequence, kind)
            )

        if new.status == "CANCELLED":
            if old is None or old.status != "CANCELLED":
                for email in sorted(after | before):
                    send("CANCEL", email, "Absage")
            return
        changed = old is not None and (
            (old.start, old.end, old.location, old.summary, old.status)
            != (new.start, new.end, new.location, new.summary, new.status)
        )
        for email in sorted(after):
            if email not in before:
                send("REQUEST", email, "Einladung")
            elif changed:
                send("REQUEST", email, "Aktualisierung")
        for email in sorted(before - after):
            send("CANCEL", email, "Absage")

    def answer(self, uid: str, email: str, partstat: str) -> None:
        """An attendee's answer arrives: the organizer's copy gets the PARTSTAT."""
        event = self.event(uid)
        for a in event.attendees:
            if a.email.lower() == email.lower():
                a.partstat = partstat
        self.store(f"{uid}.ics", to_ics(event))


def _handler(calendar: FakeCalendar, auth: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _send(self, status: int, body: str = "", headers: dict | None = None, kind="text/plain") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            calendar.requests.append(f"{self.command} {self.path}")
            if auth and self.headers.get("Authorization") != auth:
                self._send(401)
                return False
            return True

        def _body(self) -> str:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/_outbox":
                with calendar.lock:
                    body = json.dumps([asdict(m) for m in calendar.outbox], ensure_ascii=False, indent=1)
                return self._send(200, body, kind="application/json")
            if not self._allowed():
                return
            with calendar.lock:
                found = calendar.objects.get(self.path.removeprefix(PREFIX))
            if not self.path.startswith(PREFIX) or found is None:
                return self._send(404)
            self._send(200, found[0], {"ETag": found[1]}, "text/calendar")

        def do_PUT(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            name = self.path.removeprefix(PREFIX)
            body = self._body()
            events = parse_ics(body)
            if not self.path.startswith(PREFIX) or not events:
                return self._send(400)
            with calendar.lock:
                existing = calendar.objects.get(name)
                if self.headers.get("If-None-Match") == "*" and existing:
                    return self._send(412)
                if (match := self.headers.get("If-Match")) and (not existing or existing[1] != match):
                    return self._send(412)
                old = parse_ics(existing[0])[0] if existing else None
                etag = calendar.store(name, body)
                calendar.schedule(old, events[0])
            self._send(204 if existing else 201, headers={"ETag": etag})

        def do_DELETE(self) -> None:  # noqa: N802
            if self._allowed():
                calendar.deletes += 1
                self._send(403, "The app never deletes events.")

        def do_REPORT(self) -> None:  # noqa: N802
            if not self._allowed():
                return
            body = self._body()

            def bound(name: str) -> dt.datetime | None:
                marker = f'{name}="'
                if marker not in body:
                    return None
                return _parse_time({}, body.split(marker, 1)[1].split('"', 1)[0])

            start, end = bound("start"), bound("end")
            parts = []
            with calendar.lock:
                for name, (ics, etag) in calendar.objects.items():
                    e = parse_ics(ics)[0]
                    if (start and e.end <= start) or (end and e.start >= end):
                        continue
                    data = ics.replace("&", "&amp;").replace("<", "&lt;")
                    parts.append(
                        f"<d:response><d:href>{PREFIX}{name}</d:href><d:propstat><d:prop>"
                        f"<d:getetag>{etag}</d:getetag><c:calendar-data>{data}</c:calendar-data>"
                        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                    )
            xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                + "".join(parts)
                + "</d:multistatus>"
            )
            self._send(207, xml, kind="application/xml")

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if url.path != "/_antwort":
                return self._send(404)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            with calendar.lock:
                if f"{q.get('uid')}.ics" not in calendar.objects:
                    return self._send(404)
                calendar.answer(q["uid"], q.get("email", ""), q.get("partstat", "ACCEPTED"))
            self._send(204)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 0, auth: str | None = None):
    """Start the fake in a thread; returns (server, calendar, url)."""
    calendar = FakeCalendar()
    server = ThreadingHTTPServer((host, port), _handler(calendar, auth))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, calendar, f"http://{host}:{server.server_address[1]}{PREFIX}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5232)
    args = parser.parse_args(argv)
    server, _, url = serve(args.host, args.port)
    print(f"Fake calendar at {url} (outbox: http://{args.host}:{args.port}/_outbox); Ctrl+C stops it.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


__all__ = ["FakeCalendar", "Message", "main", "serve", "to_ics"]
