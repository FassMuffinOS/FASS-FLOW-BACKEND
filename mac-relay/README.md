# FASS Comms Relay — self-hosted iMessage/SMS

This is "our own Sendblue": two small scripts that run on a real Mac, drive
Messages.app via AppleScript, and talk to FASS Flow's `/api/v1/comms/*`
endpoints. The backend never touches Apple directly — it only holds a
queue (`comms_messages`). These scripts are the only thing that actually
sends or receives.

## Risk — read before running

Apple's Messages.app and iMessage network are not designed for automated
or bulk sending. There is no official API for this; both scripts work by
operating Messages.app the same way a person clicking around would
(AppleScript for sending, reading the local chat.db for replies). That
means:

- The Apple ID signed into Messages.app on this Mac can be rate-limited,
  flagged, or banned by Apple at any time, without warning, with no
  appeal process. Use a dedicated Apple ID for this — never your personal
  one or the founder's main one.
- Keep volume conversational (reminders, confirmations, replies), not
  blast/marketing-style. Marketing-style bulk sends are the pattern most
  likely to get an Apple ID flagged.
- If the Apple ID gets banned, the relay silently stops working until a
  new Apple ID + phone number is set up. Plan for that — don't make this
  the only channel for anything time-critical.

## Requirements

- A Mac (physical or a cloud Mac like MacStadium/Scaleway/AWS Mac
  instances), signed into Messages.app with a dedicated Apple ID.
- Full Disk Access granted to whatever runs `watch_replies.py` (Terminal,
  or the specific binary if run via launchd) — System Settings -> Privacy
  & Security -> Full Disk Access. Without this, reading chat.db fails.
- Python 3 + `pip3 install requests`.

## Running

```bash
export FASS_API_BASE="https://your-backend.example.com/api/v1"
export FASS_BUSINESS_USER_ID="<uuid of the business this Mac/Apple ID relays for>"

python3 send_relay.py &
python3 watch_replies.py &
```

One Mac + one Apple ID = one business's relay, per `FASS_BUSINESS_USER_ID`.
Multiple businesses need one Mac/Apple ID/number each.

## Running for real (launchd, survives reboot/crash)

Create `~/Library/LaunchAgents/com.fass.sendrelay.plist` and
`~/Library/LaunchAgents/com.fass.watchreplies.plist` modeled on:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.fass.sendrelay</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/full/path/to/mac-relay/send_relay.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>FASS_API_BASE</key><string>https://your-backend.example.com/api/v1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/fass-sendrelay.log</string>
  <key>StandardErrorPath</key><string>/tmp/fass-sendrelay.log</string>
</dict>
</plist>
```

Duplicate for `watch_replies.py` (add `FASS_BUSINESS_USER_ID` to its env
dict too), then:

```bash
launchctl load ~/Library/LaunchAgents/com.fass.sendrelay.plist
launchctl load ~/Library/LaunchAgents/com.fass.watchreplies.plist
```

## How it maps to the backend

- `send_relay.py` polls `GET /comms/outbox`, sends each message via
  Messages.app (iMessage first, falls back to SMS on failure), then
  reports back via `POST /comms/outbox/{id}/ack`.
- `watch_replies.py` tails `~/Library/Messages/chat.db` for new inbound
  messages and posts each to `POST /comms/inbound`.
- The Comms Hub page in the frontend reads `/comms/threads` and
  `/comms/thread`, and writes new outbound messages via `/comms/send`,
  which is what actually enqueues a row for `send_relay.py` to pick up.
