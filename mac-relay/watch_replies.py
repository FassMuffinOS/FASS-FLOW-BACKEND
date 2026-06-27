#!/usr/bin/env python3
"""Inbound relay: tails this Mac's local Messages database for new replies
and posts each one to FASS Flow's /comms/inbound endpoint.

macOS stores all Messages.app history in a SQLite database at
~/Library/Messages/chat.db. There is no public API for "new message
arrived" notifications, so this script polls that file directly — the
same approach every third-party iMessage tool on macOS uses, since Apple
doesn't expose one.

Required setup:
  1. System Settings -> Privacy & Security -> Full Disk Access -> enable
     it for Terminal (or whichever app/binary actually runs this script).
     Without this, sqlite3 will fail to open chat.db with "unable to open
     database file" even though the path looks correct.
  2. pip3 install requests
  3. export FASS_API_BASE="https://your-backend.example.com/api/v1"
  4. export FASS_BUSINESS_USER_ID="<the relay's business_user_id>"
     One Mac + one Apple ID = one business's relay in this design; if you
     need multiple businesses on multiple numbers, run one of these per
     Mac/Apple ID, each with its own FASS_BUSINESS_USER_ID.
  5. python3 watch_replies.py

State (last-seen ROWID) is kept in ~/.fass_relay_state so a restart
doesn't replay the whole history — though /comms/inbound also de-dupes on
the message's GUID via a unique index, so a replay is harmless, just
wasteful.
"""
import os
import sqlite3
import time
from pathlib import Path

import requests

API_BASE = os.environ.get("FASS_API_BASE", "http://localhost:8000/api/v1")
BUSINESS_USER_ID = os.environ.get("FASS_BUSINESS_USER_ID")
POLL_SECONDS = int(os.environ.get("FASS_RELAY_POLL_SECONDS", "5"))
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
STATE_FILE = Path.home() / ".fass_relay_state"

# Apple's message.date is nanoseconds since 2001-01-01, not the Unix epoch —
# not needed here since we key off ROWID, but worth flagging for anyone
# extending this to show real timestamps.

QUERY = """
select message.ROWID, message.guid, message.text, handle.id as phone, message.service
from message
join handle on message.handle_id = handle.ROWID
where message.is_from_me = 0
  and message.text is not null
  and message.ROWID > ?
order by message.ROWID
"""


def load_last_seen() -> int:
    if STATE_FILE.exists():
        try:
            return int(STATE_FILE.read_text().strip())
        except ValueError:
            return 0
    return 0


def save_last_seen(rowid: int):
    STATE_FILE.write_text(str(rowid))


def run_once(last_seen: int) -> int:
    conn = sqlite3.connect(str(CHAT_DB))
    try:
        rows = conn.execute(QUERY, (last_seen,)).fetchall()
    finally:
        conn.close()

    for rowid, guid, text, phone, service in rows:
        payload = {
            "business_user_id": BUSINESS_USER_ID,
            "phone": phone,
            "body": text,
            "channel": "imessage" if service == "iMessage" else "sms",
            "external_id": guid,
        }
        resp = requests.post(f"{API_BASE}/comms/inbound", json=payload, timeout=15)
        status = "ok" if resp.ok else f"HTTP {resp.status_code}"
        print(f"[{status}] reply from {phone}: {text[:60]!r}")
        last_seen = max(last_seen, rowid)
    return last_seen


def main():
    if not BUSINESS_USER_ID:
        raise SystemExit("FASS_BUSINESS_USER_ID env var is required.")
    if not CHAT_DB.exists():
        raise SystemExit(f"{CHAT_DB} not found — is this running on the Mac signed into Messages.app?")

    print(f"FASS reply watcher starting. Watching {CHAT_DB}, polling every {POLL_SECONDS}s.")
    last_seen = load_last_seen()
    while True:
        try:
            last_seen = run_once(last_seen)
            save_last_seen(last_seen)
        except Exception as exc:
            print(f"watcher error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
