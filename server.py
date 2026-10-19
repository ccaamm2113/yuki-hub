#!/usr/bin/env python3
"""Yuki Hub v1 — Cam's personal dashboard.

Display hub only: shifts are cached in SQLite (synced by sync_shifts.py),
alarms are computed from shifts and delivered as messages by the assistant.
No login, no paywall — single user, local/personal use.
"""
import os
import sqlite3
import json as _json
import urllib.request as _urlreq
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, g, render_template, request, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "instance", "hub.db")
LOCAL_TZ = ZoneInfo("America/Chicago")
VERSION = "v1"
# Shared secret for the /api/sync push endpoint. Set as env var on Render;
# the local sync script reads it from .sync_secret (gitignored).
SYNC_SECRET = os.environ.get("SYNC_SECRET", "")
# Live shift feed: the sync job commits shifts.json to the `data` branch of
# the yuki-hub repo; the app reads it here (no redeploy needed on changes).
# raw.githubusercontent.com is used instead of the GitHub API to avoid the
# 60/hr unauthenticated rate limit on shared Render IPs.
SHIFTS_FEED_URL = (
    "https://raw.githubusercontent.com/ccaamm2113/yuki-hub/data/shifts.json"
)
_remote_cache = {"at": None, "shifts": []}


def fetch_remote_shifts():
    """Shifts from the GitHub data branch, 5-min in-memory cache.
    Returns a list of dicts (cal_event_id/title/start_utc/end_utc) or []."""
    now = datetime.now(timezone.utc)
    if _remote_cache["at"] and (now - _remote_cache["at"]).total_seconds() < 300:
        return _remote_cache["shifts"]
    try:
        req = _urlreq.Request(
            SHIFTS_FEED_URL,
            headers={"User-Agent": "yuki-hub"},
        )
        with _urlreq.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())
        shifts = data.get("shifts", []) if isinstance(data, dict) else []
        _remote_cache["at"] = now
        _remote_cache["shifts"] = shifts
        return shifts
    except Exception:
        # Fall back to the last cached copy (may be stale or empty).
        return _remote_cache["shifts"]

app = Flask(__name__)

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


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def parse_utc(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_shift(row):
    start = parse_utc(row["start_utc"]).astimezone(LOCAL_TZ)
    end = parse_utc(row["end_utc"]).astimezone(LOCAL_TZ)
    return {
        "title": row["title"],
        "day": start.strftime("%a %b %d"),
        "time_range": f"{start.strftime('%-I:%M %p')} – {end.strftime('%-I:%M %p')}",
        "start_local": start,
        "alarm_local": start - timedelta(hours=1),
    }


def countdown_text(target_utc, now_utc):
    delta = target_utc - now_utc
    if delta.total_seconds() <= 0:
        return "now"
    days = delta.days
    hours = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    if days > 0:
        return f"in {days}d {hours}h"
    if hours > 0:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


@app.route("/api/version")
def api_version():
    return jsonify({"version": VERSION})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Push shifts from the assistant's sync job. Bearer auth with SYNC_SECRET."""
    if not SYNC_SECRET or request.headers.get("Authorization") != f"Bearer {SYNC_SECRET}":
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    shifts = data.get("shifts", [])
    init_db()
    db = get_db()
    synced_at = datetime.now(timezone.utc).isoformat()
    db.execute("DELETE FROM shifts")
    for s in shifts:
        if not s.get("cal_event_id") or not s.get("start_utc"):
            continue
        db.execute(
            "INSERT INTO shifts (cal_event_id, title, start_utc, end_utc, synced_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (s["cal_event_id"], s.get("title", "(no title)"),
             s["start_utc"], s.get("end_utc"), synced_at),
        )
    db.commit()
    n = db.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"]
    return jsonify({"ok": True, "shifts": n})


@app.route("/")
def index():
    init_db()
    now_utc = datetime.now(timezone.utc)

    # Live feed first (GitHub data branch), local SQLite as fallback.
    remote = fetch_remote_shifts()
    if remote:
        upcoming = [fmt_shift(s) for s in remote]
    else:
        db = get_db()
        rows = db.execute(
            "SELECT * FROM shifts WHERE end_utc > ? ORDER BY start_utc",
            (now_utc.isoformat(),),
        ).fetchall()
        upcoming = [fmt_shift(r) for r in rows]
    next_shift = None
    for s in upcoming:
        if s["start_local"].astimezone(timezone.utc) > now_utc:
            next_shift = s
            break

    # This week (Chicago time): Monday 00:00 -> Sunday 23:59:59
    now_local = now_utc.astimezone(LOCAL_TZ)
    week_start = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)
    week_shifts = [
        s for s in upcoming if week_start <= s["start_local"] < week_end
    ]

    next_alarm = next_shift["alarm_local"] if next_shift else None
    week_alarms = [
        {"title": s["title"], "alarm": s["alarm_local"].strftime("%a %b %d · %-I:%M %p")}
        for s in week_shifts[:5]
    ]

    countdown = (
        countdown_text(next_shift["start_local"].astimezone(timezone.utc), now_utc)
        if next_shift
        else None
    )

    return render_template(
        "index.html",
        version=VERSION,
        next_shift=next_shift,
        countdown=countdown,
        week_shifts=week_shifts,
        next_alarm=next_alarm.strftime("%a %b %d · %-I:%M %p") if next_alarm else None,
        week_alarms=week_alarms,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
