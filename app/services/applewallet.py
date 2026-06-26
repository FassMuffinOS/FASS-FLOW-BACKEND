"""Apple Wallet (.pkpass) generation and signing.

Follows the repo's no-SDK-lock-in convention: certs are decoded from the
base64 PEM env vars and signing is done via a raw `openssl smime` subprocess
call rather than a wallet-specific SDK. Mirrors the blank-default pattern
used for google_places_api_key — if the Apple cert env vars aren't set,
callers should treat this as unavailable (see wallet.py router's 503).

A .pkpass file is just a zip archive containing:
  - pass.json       (the pass content/layout)
  - manifest.json   (sha1 of every other file in the archive)
  - signature       (a CMS/PKCS#7 detached signature of manifest.json,
                     signed by the Pass Type ID cert + WWDR intermediate)
  - icon.png, icon@2x.png, icon@3x.png, logo.png, logo@2x.png, logo@3x.png
"""
import base64
import hashlib
import hmac
import json
import subprocess
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

from app.config import settings

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "wallet"
ASSET_FILES = ["icon.png", "icon@2x.png", "icon@3x.png", "logo.png", "logo@2x.png", "logo@3x.png"]
# Logo customization only ever replaces these three — icon.png/@2x/@3x stay
# the bundled FASS mark (Apple Watch/lock-screen notification icon, rarely
# seen, not worth the round trip of resizing a user upload into three sizes
# without an image library in this repo's dependency set).
LOGO_FILES = ["logo.png", "logo@2x.png", "logo@3x.png"]


def _hex_to_rgb_string(hex_color: str | None, fallback: str = "rgb(36, 14, 65)") -> str:
    """pass.json wants 'rgb(r, g, b)' strings, the customization UI hands
    back a '#rrggbb' hex from a plain <input type=color>."""
    if not hex_color:
        return fallback
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        return fallback
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback
    return f"rgb({r}, {g}, {b})"


def _contrast_colors(hex_color: str | None) -> tuple[str, str]:
    """Returns (foregroundColor, labelColor) — white-ish text on a dark
    custom background, dark text on a light one, so a user picking a pale
    color in the customization step doesn't end up with invisible text."""
    h = (hex_color or "").strip().lstrip("#")
    if len(h) != 6:
        return "rgb(255, 255, 255)", "rgb(200, 180, 230)"
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "rgb(255, 255, 255)", "rgb(200, 180, 230)"
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if luminance > 0.6:
        return "rgb(20, 20, 20)", "rgb(80, 80, 80)"
    return "rgb(255, 255, 255)", "rgb(220, 210, 235)"


def _fetch_logo_bytes(logo_url: str) -> bytes | None:
    """Best-effort download of a user-uploaded logo (Supabase Storage public
    URL). Any failure here just falls back to the bundled default logo —
    a bad upload should never block generating the rest of the pass."""
    try:
        resp = httpx.get(logo_url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def apple_wallet_configured() -> bool:
    return bool(
        settings.apple_pass_cert_pem_b64
        and settings.apple_pass_key_pem_b64
        and settings.apple_wwdr_pem_b64
        and settings.apple_team_id
        and settings.apple_pass_type_id
    )


def push_auth_token(serial_number: str) -> str:
    """Stateless per-pass PassKit auth token: HMAC-SHA256(secret, serial)
    instead of a random per-row secret stored in the DB — avoids a schema
    migration entirely, since serial_number (== slug) is already the unique
    key on both wallet_passes and reward_cards. Embedded in pass.json's
    authenticationToken field at issue time, and re-derived (never stored)
    by wallet_passkit.py to validate the 'Authorization: ApplePass <token>'
    header Apple Wallet sends on every device-originated call."""
    secret = settings.wallet_auth_secret or settings.jwt_secret
    return hmac.new(secret.encode(), serial_number.encode(), hashlib.sha256).hexdigest()


def _decode(b64_value: str) -> bytes:
    """Decodes a base64 env var. Tolerates the two most common copy/paste
    mangling issues with Railway (or any dashboard) env var fields: stray
    whitespace/newlines getting embedded, and trailing '=' padding getting
    silently stripped — both produce a binascii 'Incorrect padding' error
    on a value that is otherwise a perfectly valid PEM blob."""
    cleaned = "".join(b64_value.split())
    missing_padding = len(cleaned) % 4
    if missing_padding:
        cleaned += "=" * (4 - missing_padding)
    return base64.b64decode(cleaned)


def build_pass_json(
    *,
    serial_number: str,
    business_name: str,
    address: str | None,
    naics: str | None,
    website: str | None,
    phone: str | None,
    barcode_url: str,
    bg_color: str | None = None,
    show_address: bool = True,
    show_naics: bool = True,
    show_phone: bool = True,
    show_website: bool = True,
    watermark: bool = False,
) -> dict:
    """Generic-style pass (membership/ID card layout) carrying a business's
    FASS Wallet info, with a QR code pointing at its public capability page.

    watermark=True is the free-tier pass: real, signed, fully usable, but
    it visibly says "Free" on the front and carries an upgrade pitch on the
    back. Flips to False (and this disappears) the moment the row's
    purchased flag goes true — no new pass needs to be issued, the next
    /pass?slug=... download just renders without the watermark.
    """
    secondary_fields = []
    if address and show_address:
        secondary_fields.append({"key": "address", "label": "ADDRESS", "value": address})
    if naics and show_naics:
        secondary_fields.append({"key": "naics", "label": "NAICS", "value": naics})

    back_fields = []
    if website and show_website:
        back_fields.append({"key": "website", "label": "Website", "value": website})
    if phone and show_phone:
        back_fields.append({"key": "phone", "label": "Phone", "value": phone})
    back_fields.append({
        "key": "about",
        "label": "About FASS Wallet",
        "value": "Issued by FASS — verify this business and view its full capability statement by scanning the QR code.",
    })
    if watermark:
        back_fields.append({
            "key": "upgrade",
            "label": "Created with FASS — Free",
            "value": "This is a free FASS Wallet card. Upgrade at flow.fass.systems/passport to remove this watermark and unlock the full premium card.",
        })

    foreground_color, label_color = _contrast_colors(bg_color)

    pass_dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id,
        "teamIdentifier": settings.apple_team_id,
        "serialNumber": serial_number,
        "organizationName": "FASS",
        "description": f"{business_name} — FASS Wallet Card",
        "logoText": "FASS Wallet · Free" if watermark else "FASS Wallet",
        "backgroundColor": _hex_to_rgb_string(bg_color),
        "foregroundColor": foreground_color,
        "labelColor": label_color,
        "generic": {
            "primaryFields": [
                {"key": "name", "label": "BUSINESS", "value": business_name}
            ],
            "secondaryFields": secondary_fields,
            "backFields": back_fields,
        },
        "barcodes": [
            {
                "message": barcode_url,
                "format": "PKBarcodeFormatQR",
                "messageEncoding": "iso-8859-1",
            }
        ],
    }
    # Live push — only added once backend_base_url is set in Railway, so
    # turning push on/off never requires re-issuing already-downloaded
    # passes; Wallet simply won't have a webServiceURL to call until then.
    if settings.backend_base_url:
        pass_dict["webServiceURL"] = f"{settings.backend_base_url}/api/v1/passkit"
        pass_dict["authenticationToken"] = push_auth_token(serial_number)
    return pass_dict


def build_storecard_pass_json(
    *,
    serial_number: str,
    business_name: str,
    stamps: int,
    reward_threshold: int,
    reward_description: str | None,
    barcode_url: str,
    bg_color: str | None = None,
) -> dict:
    """storeCard-style pass (Apple's built-in punch-card layout) for the
    restaurant rewards/loyalty program — one of these per customer per
    business, separate from the single business-identity card in
    build_pass_json(). stamps/reward_threshold render as "N / M" so the
    customer can see progress at a glance; re-issuing with an incremented
    stamps value (same serial_number) is how a stamp gets "added" — there's
    no live push yet, so the customer needs to re-download to see the
    update until the Apple PassKit web service + APNs piece is built.
    """
    foreground_color, label_color = _contrast_colors(bg_color)
    earned = stamps >= reward_threshold

    pass_dict = {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id,
        "teamIdentifier": settings.apple_team_id,
        "serialNumber": serial_number,
        "organizationName": "FASS",
        "description": f"{business_name} — Rewards Card",
        "logoText": f"{business_name} Rewards",
        "backgroundColor": _hex_to_rgb_string(bg_color, fallback="rgb(15, 81, 50)"),
        "foregroundColor": foreground_color,
        "labelColor": label_color,
        "storeCard": {
            "primaryFields": [
                {
                    "key": "stamps",
                    "label": "REWARD READY!" if earned else "STAMPS",
                    "value": f"{stamps} / {reward_threshold}" if not earned else "Show this to redeem",
                }
            ],
            "secondaryFields": [
                {"key": "business", "label": "BUSINESS", "value": business_name},
            ],
            "backFields": [
                {
                    "key": "about",
                    "label": "How it works",
                    "value": reward_description or f"Collect {reward_threshold} stamps to earn your reward. Ask staff to stamp this card on each qualifying visit.",
                },
                {
                    "key": "fass",
                    "label": "Powered by",
                    "value": "FASS Wallet — flow.fass.systems",
                },
            ],
        },
        "barcodes": [
            {
                "message": barcode_url,
                "format": "PKBarcodeFormatQR",
                "messageEncoding": "iso-8859-1",
            }
        ],
    }
    if settings.backend_base_url:
        pass_dict["webServiceURL"] = f"{settings.backend_base_url}/api/v1/passkit"
        pass_dict["authenticationToken"] = push_auth_token(serial_number)
    return pass_dict


def _sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sign_manifest(manifest_bytes: bytes) -> bytes:
    """Detached CMS signature over manifest.json, via openssl subprocess —
    no cryptography-library wallet SDK, consistent with the rest of the repo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        manifest_path = tmp_path / "manifest.json"
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"
        wwdr_path = tmp_path / "wwdr.pem"
        sig_path = tmp_path / "signature"

        manifest_path.write_bytes(manifest_bytes)
        cert_path.write_bytes(_decode(settings.apple_pass_cert_pem_b64))
        key_path.write_bytes(_decode(settings.apple_pass_key_pem_b64))
        wwdr_path.write_bytes(_decode(settings.apple_wwdr_pem_b64))

        cmd = [
            "openssl", "smime", "-binary", "-sign",
            "-certfile", str(wwdr_path),
            "-signer", str(cert_path),
            "-inkey", str(key_path),
            "-in", str(manifest_path),
            "-out", str(sig_path),
            "-outform", "DER",
        ]
        if settings.apple_pass_key_password:
            cmd.extend(["-passin", f"pass:{settings.apple_pass_key_password}"])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"openssl signing failed: {result.stderr}")

        return sig_path.read_bytes()


def _zip_and_sign(pass_dict: dict, logo_url: str | None = None) -> bytes:
    """Shared tail end of pass generation: serialize pass.json, pull in the
    bundled (or custom-logo) image assets, build+sign manifest.json, zip it
    all up. Used by both the business-card and storeCard pass builders so
    a new pass type never has to re-implement the signing/packaging part."""
    pass_json_bytes = json.dumps(pass_dict, separators=(",", ":")).encode("utf-8")

    # Best-effort custom logo download — only replaces logo.png/@2x/@3x, never
    # the icon.* files. Falls back to the bundled default on any failure.
    custom_logo_bytes = _fetch_logo_bytes(logo_url) if logo_url else None

    # Build manifest: sha1 of pass.json + every bundled image asset.
    manifest = {"pass.json": _sha1_hex(pass_json_bytes)}
    asset_bytes: dict[str, bytes] = {}
    for fname in ASSET_FILES:
        if custom_logo_bytes is not None and fname in LOGO_FILES:
            data = custom_logo_bytes
        else:
            fpath = ASSETS_DIR / fname
            if not fpath.exists():
                continue
            data = fpath.read_bytes()
        asset_bytes[fname] = data
        manifest[fname] = _sha1_hex(data)

    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    signature_bytes = _sign_manifest(manifest_bytes)

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pass.json", pass_json_bytes)
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("signature", signature_bytes)
        for fname, data in asset_bytes.items():
            zf.writestr(fname, data)

    return buf.getvalue()


def generate_pkpass(
    *,
    business_name: str,
    address: str | None = None,
    naics: str | None = None,
    website: str | None = None,
    phone: str | None = None,
    barcode_url: str | None = None,
    serial_number: str | None = None,
    bg_color: str | None = None,
    logo_url: str | None = None,
    show_address: bool = True,
    show_naics: bool = True,
    show_phone: bool = True,
    show_website: bool = True,
    watermark: bool = False,
) -> bytes:
    """Returns the raw bytes of a signed .pkpass file (business card)."""
    if not apple_wallet_configured():
        raise RuntimeError("Apple Wallet certs are not configured")

    serial = serial_number or str(uuid.uuid4())
    barcode_target = barcode_url or "https://flow.fass.systems"

    pass_dict = build_pass_json(
        serial_number=serial,
        business_name=business_name,
        address=address,
        naics=naics,
        website=website,
        phone=phone,
        barcode_url=barcode_target,
        bg_color=bg_color,
        show_address=show_address,
        show_naics=show_naics,
        show_phone=show_phone,
        show_website=show_website,
        watermark=watermark,
    )
    return _zip_and_sign(pass_dict, logo_url=logo_url)


def generate_storecard_pkpass(
    *,
    business_name: str,
    stamps: int,
    reward_threshold: int = 10,
    reward_description: str | None = None,
    barcode_url: str | None = None,
    serial_number: str | None = None,
    bg_color: str | None = None,
    logo_url: str | None = None,
) -> bytes:
    """Returns the raw bytes of a signed .pkpass file (rewards punch card)."""
    if not apple_wallet_configured():
        raise RuntimeError("Apple Wallet certs are not configured")

    serial = serial_number or str(uuid.uuid4())
    barcode_target = barcode_url or "https://flow.fass.systems"

    pass_dict = build_storecard_pass_json(
        serial_number=serial,
        business_name=business_name,
        stamps=stamps,
        reward_threshold=reward_threshold,
        reward_description=reward_description,
        barcode_url=barcode_target,
        bg_color=bg_color,
    )
    return _zip_and_sign(pass_dict, logo_url=logo_url)
