#!/usr/bin/env python3
"""The Company Brain — PERSONAL vertical mock source ("brief me for my next meeting").

PRIVACY-SAFE BY DESIGN. This service does NOT touch your real Gmail or Google
Calendar. It returns a realistic, hard-coded mock of "your next meeting" so the
demo runs offline and deterministically. The attendees' employers are REAL UK
firms ("Monzo Bank", "Revolut") so the downstream brain can enrich them —
type the attendee company into the company spine (GLEIF -> Companies House
number -> registered postcode -> coordinates -> crime / flood / weather, plus
Wikidata background) with no extra wiring.

    python3 demo/services/personal_service.py     # serves 127.0.0.1:8904
    curl -s http://127.0.0.1:8904/next-meeting | python3 -m json.tool

GOING LIVE (swap the mock for your real calendar, local-first, read-only):
  Replace _brief()/_attendees() with calls to a Google Calendar / Gmail MCP shim
  that returns the SAME JSON shapes (object for /next-meeting[/<sel>], bare array
  for /next-meeting/<sel>/attendees). Use read-only scopes
  (calendar.events.readonly, gmail.readonly), keep the shim on 127.0.0.1, and
  point build/gov-uk/taxi/personal.taxi's @HttpService baseUrl at it. The Taxi
  models (MeetingBrief, MeetingAttendee) and every downstream query stay
  byte-for-byte identical — only this one source flips from mock to real.

Pure Python stdlib — no dependencies.
"""
import json
import os
import socketserver
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

PORT = int(os.environ.get("PERSONAL_PORT", "8904"))
# Bind address. Defaults to 0.0.0.0 so the Orbital container can reach this mock
# via host.docker.internal:8904 (the @HttpService baseUrl in personal.taxi is
# called from INSIDE the container — a 127.0.0.1-only bind is refused from there).
# For a purely host-local test with no container, set PERSONAL_HOST=127.0.0.1.
HOST = os.environ.get("PERSONAL_HOST", "0.0.0.0")


def next_meeting() -> dict:
    """Return the user's next meeting as a brief.

    MOCK. The `company` of each attendee is a real UK firm so the brain can
    enrich it. `startsAt` is computed at request time (next top-of-hour, UTC)
    so the demo always shows a plausible *upcoming* meeting.

    LIVE SWAP: return the same dict shape from a Google Calendar/Gmail MCP shim
    (read-only scopes, 127.0.0.1). Keys: title, startsAt (ISO-8601),
    attendees[].{name,email,company}.
    """
    return {**_brief(), "attendees": _attendees()}


def _brief() -> dict:
    """Scalar header of the next meeting (title, startsAt, location)."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    starts_at = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    return {
        "title": "Q3 partnership review — embedded payments",
        "startsAt": starts_at,
        "location": "Google Meet",
    }


def _attendees() -> list:
    """Attendees of the next meeting. Each `company` is a REAL UK firm so the
    brain can enrich it (CompanyName -> GLEIF -> Companies House number -> ...)."""
    return [
        {"name": "Priya Sharma", "email": "priya.sharma@monzo.com", "company": "Monzo Bank"},
        {"name": "Tom Wright", "email": "tom.wright@revolut.com", "company": "Revolut"},
        {"name": "Aisha Khan", "email": "aisha.khan@wise.com", "company": "Wise"},
    ]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        # Routes (selector is accepted then ignored — there is only one "next"
        # meeting in this mock; a live MCP shim would honour next | today | ...):
        #   /next-meeting                       -> full object {title,startsAt,location,attendees}
        #                                          (task-specified contract; used by the curl demo
        #                                           and by the MeetingBrief scalar-binding operation)
        #   /next-meeting/<selector>            -> same full object
        #   /next-meeting/<selector>/attendees  -> the BARE attendees array [ {...}, ... ]
        #                                          (Orbital binds this as MeetingAttendee[]; nested
        #                                           model-arrays inside one returned model do not
        #                                           bind in this Orbital build, so attendees are a
        #                                           sibling collection operation instead)
        if path.startswith("/next-meeting/") and path.endswith("/attendees"):
            return self._send(200, json.dumps(_attendees()))
        if path == "/next-meeting" or path.startswith("/next-meeting/"):
            return self._send(200, json.dumps(next_meeting()))
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    shown = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else HOST
    print(f"Personal brain (MOCK) -> http://{shown}:{PORT}/next-meeting "
          f"(reachable from Orbital as host.docker.internal:{PORT})")
    Server((HOST, PORT), Handler).serve_forever()
