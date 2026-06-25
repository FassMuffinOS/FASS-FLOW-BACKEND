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
import json
import subprocess
import tempfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from app.config import settings

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "wallet"
ASSET_FILES = ["icon.png", "icon@2x.png", "icon@3x.png", "logo.png", "logo@2x.png", "logo@3x.png"]


def apple_wallet_configured() -> bool:
    return bool(
        settings.apple_pass_cert_pem_b64
        and settings.apple_pass_key_pem_b64
        and settings.apple_wwdr_pem_b64
        and settings.apple_team_id
        and settings.apple_pass_type_id
    )


def _decode(b64_value: str) -> bytes:
    return base64.b64decode(b64_value)


def build_pass_json(
    *,
    serial_number: str,
    business_name: str,
    address: str | None,
    naics: str | None,
    website: str | None,
    phone: str | None,
    barcode_url: str,
) -> dict:
    """Generic-style pass (membership/ID card layout) carrying a business's
    FASS Wallet info, with a QR code pointing at its public capability page.
    """
    secondary_fields = []
    if address:
        secondary_fields.append({"key": "address", "label": "ADDRESS", "value": address})
    if naics:
        secondary_fields.append({"key": "naics", "label": "NAICS", "value": naics})

    back_fields = []
    if website:
        back_fields.append({"key": "website", "label": "Website", "value": website})
    if phone:
        back_fields.append({"key": "phone", "label": "Phone", "value": phone})
    back_fields.append({
        "key": "about",
        "label": "About FASS Wallet",
        "value": "Issued by FASS — verify this business and view its full capability statement by scanning the QR code.",
    })

    return {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_pass_type_id,
        "teamIdentifier": settings.apple_team_id,
        "serialNumber": serial_number,
        "organizationName": "FASS",
        "description": f"{business_name} — FASS Wallet Card",
        "logoText": "FASS Wallet",
        "backgroundColor": "rgb(36, 14, 65)",
        "foregroundColor": "rgb(255, 255, 255)",
        "labelColor": "rgb(200, 180, 230)",
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


def generate_pkpass(
    *,
    business_name: str,
    address: str | None = None,
    naics: str | None = None,
    website: str | None = None,
    phone: str | None = None,
    barcode_url: str | None = None,
    serial_number: str | None = None,
) -> bytes:
    """Returns the raw bytes of a signed .pkpass file."""
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
    )
    pass_json_bytes = json.dumps(pass_dict, separators=(",", ":")).encode("utf-8")

    # Build manifest: sha1 of pass.json + every bundled image asset.
    manifest = {"pass.json": _sha1_hex(pass_json_bytes)}
    asset_bytes: dict[str, bytes] = {}
    for fname in ASSET_FILES:
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
