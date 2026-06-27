#!/usr/bin/env python3
"""Outbound relay: polls the FASS Flow comms outbox and sends each queued
message through Messages.app via AppleScript.

This is the piece that actually produces blue-bubble iMessages — it only
works because it's driving a real Messages.app session signed into a real
Apple ID on this Mac. There is no API call to Apple anywhere in this
script; AppleScript is just operating the app as if a person were
clicking "send." That's also exactly why this carries real risk: Apple's
ToS doesn't sanction automated/bulk sending through Messages.app, and an
Apple ID used this way can be rate-limited or banned with no warning and
no appeal path. Use a dedicated Apple ID for this relay, not a personal
one, and keep volume low and conversational rather than blast-style.

Setup:
  1. Sign this Mac into Messages.app with the relay's dedicated Apple ID.
  2. pip3 install requests
  3. export FASS_API_BASE="https://your-backend.example.com/api/v1"
  4. python3 send_relay.py
  5. For production, run this under launchd (see README.md) so it survives
     reboots and restarts itself if it crashes.
"""
import os
import subprocess
import time

import requests

API_BASE = os.environ.get("FASS_API_BASE", "http://localhost:8000/api/v1")
POLL_SECONDS = int(os.environ.get("FASS_RELAY_POLL_SECONDS", "5"))


def send_via_messages(phone: str, body: str, prefer_imessage: bool = True) -> tuple[bool, str | None]:
    """Returns (success, error_message)."""
    service_type = "iMessage" if prefer_imessage else "SMS"
    # AppleScript string literals: escape backslashes first, then quotes.
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_phone = phone.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = {service_type}
        set targetBuddy to buddy "{safe_phone}" of targetService
        send "{safe_body}" to targetBuddy
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode == 0:
        return True, None
    return False, result.stderr.strip()


def run_once():
    resp = requests.get(f"{API_BASE}/comms/outbox", timeout=15)
    resp.raise_for_status()
    messages = resp.json().get("messages", [])
    for msg in messages:
        prefer_imessage = msg.get("channel", "imessage") == "imessage"
        ok, err = send_via_messages(msg["phone"], msg["body"], prefer_imessage)
        # If iMessage delivery fails (recipient isn't reachable on iMessage,
        # most commonly), fall back to SMS once before giving up — this is
        # the same fallback behavior real "blue bubble" relays advertise.
        if not ok and prefer_imessage:
            ok, err = send_via_messages(msg["phone"], msg["body"], prefer_imessage=False)

        ack = {"status": "sent" if ok else "failed"}
        if err:
            ack["error"] = err[:500]
        requests.post(f"{API_BASE}/comms/outbox/{msg['id']}/ack", json=ack, timeout=15)
        print(f"[{'sent' if ok else 'FAILED'}] -> {msg['phone']}: {msg['body'][:60]!r}" + (f" ({err})" if err else ""))


def main():
    print(f"FASS comms relay starting. Polling {API_BASE} every {POLL_SECONDS}s.")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"relay error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
