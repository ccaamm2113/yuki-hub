#!/usr/bin/env python3
"""sync_shifts.py — pull upcoming shifts from Cam's Google Calendar
("My schedule") into the Yuki Hub SQLite cache.

The assistant runs this on a schedule (read-only calendar list).
Upserts by calendar event id. Prints a one-line summary.
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "instance", "hub.db")
CLI = "/opt/hatch/bin/hatch_gws_cli"
CAL_NAME = "My schedule"
WINDOW_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    cal_event_id TEXT UNIQUE,
    title TEXT,
    start_utc TEXT,
    end_utc TEXT,
    synced_at TEXT
);
"""


class SyncError(Exception):
    pass


def cli(*args):
    """Run the GWS CLI, return parsed JSON. Raise SyncError with a clean
    message (no stack trace) on auth/connection/API failures."""
    try:
        out = subprocess.run(
            [CLI, *args], capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        raise SyncError("calendar CLI not found on this machine")
    except subprocess.TimeoutExpired:
        raise SyncError("calendar request timed out")
    if out.returncode != 0:
        err = (out.stderr or out.stdout or "").strip()
        low = err.lower()
        if any(k in low for k in ("auth", "credential", "token", "unauthorized", "login")):
            raise SyncError("Google Calendar is not connected — run the connector setup first")
        raise SyncError(f"calendar request failed: {err[:160]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        raise SyncError("calendar returned an unreadable response")


def resolve_calendar_id():
    data = cli("calendar", "calendarList", "list")
    for item in data.get("items", []):
        if item.get("summary") == CAL_NAME:
            return item["id"]
    raise SyncError(f"no calendar named '{CAL_NAME}' found in the calendar list")


def fetch_events(cal_id):
    now = datetime.now(timezone.utc)
    params = {
        "calendarId": cal_id,
        "timeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeMax": (now + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 100,
    }
    data = cli("calendar", "events", "list", "--params", json.dumps(params))
    return data.get("items", [])


def to_utc_iso(value):
    """Calendar API dateTime -> UTC ISO string. All-day 'date' events become
    midnight UTC (they are not shifts; kept for completeness)."""
    if "dateTime" in value:
        dt = datetime.fromisoformat(value["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return value.get("date", "") + "T00:00:00+00:00"


def push_to_hub():
    """POST the cached upcoming shifts to the live hub app (/api/sync).

    Needs HUB_URL env (e.g. https://yuki-hub.onrender.com) and the shared
    secret in .sync_secret (gitignored). Prints a one-line summary.
    """
    import urllib.request

    hub_url = os.environ.get("HUB_URL", "").rstrip("/")
    if not hub_url:
        print("push skipped: HUB_URL not set")
        return 1
    secret_path = os.path.join(BASE, ".sync_secret")
    if not os.path.exists(secret_path):
        print("push skipped: .sync_secret missing")
        return 1
    secret = open(secret_path).read().strip()
    if not secret:
        print("push skipped: .sync_secret empty")
        return 1

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        "SELECT cal_event_id, title, start_utc, end_utc FROM shifts"
        " WHERE end_utc > ? ORDER BY start_utc",
        (now,),
    ).fetchall()
    db.close()
    payload = json.dumps({"shifts": [dict(r) for r in rows]}).encode()
    req = urllib.request.Request(
        hub_url + "/api/sync",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {secret}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        print(f"push failed: {e}")
        return 1
    if not body.get("ok"):
        print(f"push failed: {body}")
        return 1
    print(f"pushed {body.get('shifts')} shifts to {hub_url}")
    return 0


def main():
    push = "--push" in sys.argv
    try:
        cal_id = resolve_calendar_id()
        events = fetch_events(cal_id)
    except SyncError as e:
        print(f"sync failed: {e}")
        return 1

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    synced_at = datetime.now(timezone.utc).isoformat()

    n = 0
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start = ev.get("start", {})
        end = ev.get("end", {})
        if "dateTime" not in start:  # skip all-day entries
            continue
        db.execute(
            """INSERT INTO shifts (cal_event_id, title, start_utc, end_utc, synced_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(cal_event_id) DO UPDATE SET
                 title=excluded.title, start_utc=excluded.start_utc,
                 end_utc=excluded.end_utc, synced_at=excluded.synced_at""",
            (
                ev.get("id"),
                ev.get("summary", "(no title)"),
                to_utc_iso(start),
                to_utc_iso(end),
                synced_at,
            ),
        )
        n += 1

    # Prune shifts that ended more than a week ago.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    db.execute("DELETE FROM shifts WHERE end_utc < ?", (cutoff,))
    db.commit()

    nxt = db.execute(
        "SELECT title, start_utc FROM shifts WHERE start_utc > ? ORDER BY start_utc LIMIT 1",
        (datetime.now(timezone.utc).isoformat(),),
    ).fetchone()
    db.close()

    if nxt:
        print(f"synced {n} shifts from '{CAL_NAME}'; next: {nxt[0]} at {nxt[1]}")
    else:
        print(f"synced {n} shifts from '{CAL_NAME}'; no upcoming shifts")
    if push:
        return push_to_hub()
    return 0


if __name__ == "__main__":
    sys.exit(main())
