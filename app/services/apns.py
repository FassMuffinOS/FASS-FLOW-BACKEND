"""APNs push for live Apple Wallet pass updates.

Wallet pushes carry no payload — they're a bare signal telling the device
"go re-fetch this pass," which is what makes wallet_passkit.py's web
service endpoints actually get called instead of sitting idle until the
customer manually re-downloads. Auth is the SAME Pass Type ID cert/key
already used to sign .pkpass files (mTLS), with the Pass Type ID itself as
the APNs "topic" — no separate Apple Developer registration needed beyond
what's already stored in Railway for pass signing (see applewallet.py).
"""
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.services.applewallet import _decode, apple_wallet_configured

APNS_HOST = "https://api.push.apple.com"


def _write_cert_files(tmp_dir: Path) -> tuple[Path, Path]:
    cert_path = tmp_dir / "cert.pem"
    key_path = tmp_dir / "key.pem"
    cert_path.write_bytes(_decode(settings.apple_pass_cert_pem_b64))
    key_path.write_bytes(_decode(settings.apple_pass_key_pem_b64))
    return cert_path, key_path


def send_push(push_token: str) -> bool:
    """Fires one silent APNs push at a device's token. Swallows every
    failure and returns False rather than raising — a push failing should
    never block or error out the API call that triggered it (a stamp,
    redemption, or customization should always succeed even if APNs is
    down); worst case the customer just has to manually re-open Wallet to
    see the update, same as before this existed."""
    if not apple_wallet_configured():
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = _write_cert_files(Path(tmp))
            with httpx.Client(
                http2=True,
                cert=(str(cert_path), str(key_path)),
                timeout=10.0,
            ) as client:
                resp = client.post(
                    f"{APNS_HOST}/3/device/{push_token}",
                    headers={
                        "apns-topic": settings.apple_pass_type_id,
                        "apns-push-type": "background",
                    },
                    json={},
                )
            return resp.status_code == 200
    except Exception:
        return False


def notify_devices(sb, serial_number: str) -> int:
    """Looks up every device registered for this pass (via the PassKit Web
    Service's register-device endpoint) and pushes each one. Return value
    is only the attempt count, for logging — callers should never branch on
    it, since a 0 here just as often means "nobody's added this pass to
    Wallet yet" as it does an actual failure."""
    rows = (
        sb.table("wallet_push_registrations")
        .select("push_token")
        .eq("serial_number", serial_number)
        .execute()
    ).data or []
    for row in rows:
        send_push(row["push_token"])
    return len(rows)
