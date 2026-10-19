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
    """Publish the cached upcoming shifts to the live hub app.

    Transport: commit shifts.json to the `data` branch of the yuki-hub repo
    via the GitHub API (direct POSTs to *.onrender.com hang from this VM,
    but api.github.com works). The Render app fetches shifts.json from the
    GitHub contents API at request time (5-min cache), so no redeploy is
    needed when shifts change. Prints a one-line summary.
    """
    import base64

    GH = os.path.expanduser("~/workspace/custom-connectors/custom.github/bin/gh.py")
    REPO = "ccaamm2113/yuki-hub"
    PATH = "shifts.json"
    BRANCH = "data"

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        "SELECT cal_event_id, title, start_utc, end_utc FROM shifts"
        " WHERE end_utc > ? ORDER BY start_utc",
        (now,),
    ).fetchall()
    db.close()
    payload_shifts = [dict(r) for r in rows]

    def gh(method, api_path, body=None):
        cmd = ["python3", GH, method, api_path]
        if body is not None:
            with open("/tmp/yukihub_gh_body.json", "w") as f:
                json.dump(body, f)
            cmd.append("/tmp/yukihub_gh_body.json")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            raise SyncError(f"github api call failed: {e}")
        if out.returncode != 0:
            raise SyncError(f"github api {method} {api_path} failed: {(out.stderr or out.stdout)[:160]}")
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            raise SyncError("github returned an unreadable response")

    # Get current SHA on the data branch (if the file exists yet).
    sha = None
    try:
        existing = gh("GET", f"/repos/{REPO}/contents/{PATH}?ref={BRANCH}")
        if isinstance(existing, dict):
            sha = existing.get("sha")
    except SyncError as e:
        if "404" not in str(e):
            print(f"push skipped: {e}")
            return 1

    content = base64.b64encode(
        json.dumps({"shifts": payload_shifts, "updated_at": now}).encode()
    ).decode()
    body = {"message": f"shifts sync {now[:16]}", "content": content, "branch": BRANCH}
    if sha:
        body["sha"] = sha
    try:
        gh("PUT", f"/repos/{REPO}/contents/{PATH}", body)
    except SyncError as e:
        print(f"push failed: {e}")
        return 1
    print(f"pushed {len(payload_shifts)} shifts to {REPO}@{BRANCH}/{PATH}")
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
