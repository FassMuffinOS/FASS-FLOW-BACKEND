"""Apple PassKit Web Service — the 5 endpoints Apple Wallet ITSELF calls
(never the FASS frontend) so a pass already added to a customer's phone can
register for push updates and silently re-fetch itself, instead of needing
a manual re-download every time a stamp, redemption, or customization
happens.

Mounted at /api/v1/passkit. pass.json's webServiceURL is set (in
applewallet.py) to "{backend_base_url}/api/v1/passkit", and Apple Wallet
appends "/v1/devices/...", "/v1/passes/...", "/v1/log" to that base itself
per Apple's PassKit Web Service spec — those paths are NOT chosen by this
file, they're dictated by Apple.

Every device-originated call except /v1/log authenticates via
'Authorization: ApplePass <token>', checked against the per-pass HMAC token
from applewallet.push_auth_token() — see that function's docstring for why
there's no new DB column for this.

serial_number doubles as the primary/unique key across BOTH pass tables in
this repo (wallet_passes.slug, reward_cards.slug) — there's only one Apple
Pass Type ID covering both pass styles (see applewallet.py), so _find_pass()
below just checks both tables rather than needing a passTypeIdentifier
lookup table.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

from app.database import get_supabase, single_data
from app.services.applewallet import (
    apple_wallet_configured,
    generate_pkpass,
    generate_storecard_pkpass,
    push_auth_token,
)

router = APIRouter(prefix="/passkit", tags=["wallet-passkit"])


def _check_auth(authorization: str | None, serial_number: str):
    expected = f"ApplePass {push_auth_token(serial_number)}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid pass authentication token")


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_pass(sb, serial_number: str):
    """Returns (kind, record) — kind is 'wallet' or 'rewards' — or
    (None, None) if no pass anywhere has this serial number."""
    wallet_row = single_data(
        sb.table("wallet_passes").select("*").eq("slug", serial_number).maybe_single().execute()
    )
    if wallet_row:
        return "wallet", wallet_row
    reward_row = single_data(
        sb.table("reward_cards").select("*").eq("slug", serial_number).maybe_single().execute()
    )
    if reward_row:
        return "rewards", reward_row
    return None, None


@router.post("/v1/devices/{device_library_identifier}/registrations/{pass_type_identifier}/{serial_number}")
async def register_device(
    device_library_identifier: str,
    pass_type_identifier: str,
    serial_number: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization, serial_number)
    body = await request.json()
    push_token = body.get("pushToken")
    if not push_token:
        raise HTTPException(status_code=400, detail="Missing pushToken")

    sb = get_supabase()
    sb.table("wallet_push_registrations").upsert(
        {
            "device_library_identifier": device_library_identifier,
            "pass_type_identifier": pass_type_identifier,
            "serial_number": serial_number,
            "push_token": push_token,
        },
        on_conflict="device_library_identifier,pass_type_identifier,serial_number",
    ).execute()
    return Response(status_code=201)


@router.delete("/v1/devices/{device_library_identifier}/registrations/{pass_type_identifier}/{serial_number}")
async def unregister_device(
    device_library_identifier: str,
    pass_type_identifier: str,
    serial_number: str,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization, serial_number)
    sb = get_supabase()
    sb.table("wallet_push_registrations").delete().match(
        {
            "device_library_identifier": device_library_identifier,
            "pass_type_identifier": pass_type_identifier,
            "serial_number": serial_number,
        }
    ).execute()
    return Response(status_code=200)


@router.get("/v1/devices/{device_library_identifier}/registrations/{pass_type_identifier}")
async def list_updated_serials(
    device_library_identifier: str,
    pass_type_identifier: str,
    passesUpdatedSince: str | None = None,
):
    """No per-pass auth header on this one — it's how a device discovers
    WHICH of its registered passes changed, scoped only to that device's own
    registrations, which is what Apple's spec defines for this endpoint."""
    sb = get_supabase()
    rows = (
        sb.table("wallet_push_registrations")
        .select("serial_number")
        .eq("device_library_identifier", device_library_identifier)
        .eq("pass_type_identifier", pass_type_identifier)
        .execute()
    ).data or []
    serials = [r["serial_number"] for r in rows]
    if not serials:
        return Response(status_code=204)

    since = _parse_timestamp(passesUpdatedSince)
    now = datetime.now(timezone.utc)
    updated = []
    for serial in serials:
        _, record = _find_pass(sb, serial)
        if not record:
            continue
        updated_at = _parse_timestamp(record.get("updated_at") or record.get("created_at"))
        if updated_at is None or since is None or updated_at > since:
            updated.append(serial)

    if not updated:
        return Response(status_code=204)
    return {"lastUpdated": now.isoformat(), "serialNumbers": updated}


@router.get("/v1/passes/{pass_type_identifier}/{serial_number}")
async def get_latest_pass(pass_type_identifier: str, serial_number: str, authorization: str | None = Header(default=None)):
    _check_auth(authorization, serial_number)
    if not apple_wallet_configured():
        raise HTTPException(status_code=503, detail="Apple Wallet not configured")

    sb = get_supabase()
    kind, record = _find_pass(sb, serial_number)
    if not record:
        raise HTTPException(status_code=404, detail="No pass found for that serial number")

    if kind == "wallet":
        pkpass_bytes = generate_pkpass(
            business_name=record["business_name"],
            address=record.get("address"),
            naics=record.get("naics"),
            website=record.get("website"),
            phone=record.get("phone"),
            barcode_url=f"https://flow.fass.systems/c/{serial_number}",
            serial_number=serial_number,
            bg_color=record.get("bg_color"),
            logo_url=record.get("logo_url"),
            show_address=record.get("show_address", True),
            show_naics=record.get("show_naics", True),
            show_phone=record.get("show_phone", True),
            show_website=record.get("show_website", True),
            watermark=not record.get("purchased"),
        )
    else:
        program = single_data(
            sb.table("reward_programs")
            .select("*")
            .eq("business_user_id", record["business_user_id"])
            .maybe_single()
            .execute()
        )
        if not program:
            raise HTTPException(status_code=404, detail="This card's business program no longer exists")
        pkpass_bytes = generate_storecard_pkpass(
            business_name=program["business_name"],
            stamps=record["stamps"],
            reward_threshold=program.get("reward_threshold", 10),
            reward_description=program.get("reward_description"),
            barcode_url=f"https://flow.fass.systems/rewards/{serial_number}",
            serial_number=serial_number,
            bg_color=program.get("bg_color"),
            logo_url=program.get("logo_url"),
        )

    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={"Last-Modified": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")},
    )


@router.post("/v1/log")
async def log_errors(request: Request):
    """Apple Wallet posts client-side error logs here (e.g. a device that
    failed to fetch an update). Accepted and discarded — there's no log
    aggregation in this repo yet; Apple just needs a 200 so it stops
    retrying, it doesn't require any particular response body."""
    try:
        await request.json()
    except Exception:
        pass
    return Response(status_code=200)
