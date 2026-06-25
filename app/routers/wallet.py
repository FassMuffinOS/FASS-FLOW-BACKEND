"""FASS Wallet — Apple Wallet (.pkpass) generation for a business.

Free preview rendering happens entirely on the frontend (no real pass file,
just styled info from business_lookup.py's result). This router produces
the real, downloadable/shareable .pkpass — paywalled behind a one-time
Stripe Checkout (mode="payment", not a subscription) tracked in the
wallet_passes table.

Flow:
  1. POST /checkout  — creates a wallet_passes row (purchased=false) and a
     Stripe Checkout session; frontend redirects there.
  2. Stripe webhook (subscriptions.py's /subscriptions/webhook, shared
     handler) flips purchased=true on checkout.session.completed when the
     session metadata says kind="wallet_pass".
  3. GET /purchase-status/{slug} — frontend polls this after the Stripe
     success redirect to know when it's safe to show the real download.
  4. GET /pass?slug=... — only returns a signed .pkpass once that slug's
     row is purchased=true. No slug + no purchase record = always blocked,
     even if the Apple certs are configured.
  5. GET /public/{slug} — no auth, no purchased check. This is the data
     behind the QR code on the physical pass (flow.fass.systems/c/{slug}):
     deliberately public, the whole point of scanning it. Only ever
     returns marketing-safe fields — no stripe_session_id, no user_id.
"""
import re
import uuid

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase
from app.services.applewallet import apple_wallet_configured, generate_pkpass

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/wallet", tags=["wallet"])


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "business"
    return f"{base}-{uuid.uuid4().hex[:6]}"


@router.get("/status")
async def wallet_status():
    """Lets the frontend check whether real-pass generation is live yet
    (Apple certs configured) without trying (and failing) a full pass
    request. Independent of whether Stripe pricing for the unlock is set."""
    return {
        "configured": apple_wallet_configured(),
        "checkout_ready": bool(settings.stripe_price_wallet),
    }


class CheckoutRequest(BaseModel):
    user_id: str
    email: str
    business_name: str
    address: str | None = None
    naics: str | None = None
    website: str | None = None
    phone: str | None = None
    # Card customization — free to design in Passport's preview, then
    # carried along here so the real signed .pkpass matches what the user
    # actually designed instead of always using the default look.
    bg_color: str | None = None
    logo_url: str | None = None
    show_address: bool = True
    show_naics: bool = True
    show_phone: bool = True
    show_website: bool = True


@router.post("/checkout")
async def create_wallet_checkout(body: CheckoutRequest):
    if not settings.stripe_price_wallet:
        raise HTTPException(status_code=503, detail="Wallet checkout not configured")

    sb = get_supabase()
    slug = _slugify(body.business_name)

    row = {
        "user_id": body.user_id,
        "slug": slug,
        "business_name": body.business_name,
        "address": body.address,
        "naics": body.naics,
        "website": body.website,
        "phone": body.phone,
        "purchased": False,
        "bg_color": body.bg_color or "#240e41",
        "logo_url": body.logo_url,
        "show_address": body.show_address,
        "show_naics": body.show_naics,
        "show_phone": body.show_phone,
        "show_website": body.show_website,
    }
    sb.table("wallet_passes").insert(row).execute()

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{"price": settings.stripe_price_wallet, "quantity": 1}],
        customer_email=body.email,
        metadata={"kind": "wallet_pass", "slug": slug, "user_id": body.user_id},
        success_url=f"{settings.frontend_url}/passport?wallet=success&slug={slug}",
        cancel_url=f"{settings.frontend_url}/passport?wallet=cancelled",
    )

    sb.table("wallet_passes").update({"stripe_session_id": session.id}).eq("slug", slug).execute()

    return {"url": session.url, "slug": slug}


@router.get("/purchase-status/{slug}")
async def purchase_status(slug: str):
    sb = get_supabase()
    result = sb.table("wallet_passes").select("purchased").eq("slug", slug).maybe_single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="No wallet pass found for that slug")
    return {"purchased": bool(result.data["purchased"])}


@router.get("/public/{slug}")
async def get_public_pass(slug: str):
    """Public capability-statement data for the QR target page. No auth,
    no purchased gate — a pass that was never purchased simply doesn't
    have a row, and that 404s the same as a bad slug."""
    sb = get_supabase()
    result = (
        sb.table("wallet_passes")
        .select(
            "business_name, address, naics, website, phone, purchased, "
            "bg_color, logo_url, show_address, show_naics, show_phone, show_website"
        )
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    record = result.data
    if not record:
        raise HTTPException(status_code=404, detail="No business found for that link")
    return record


@router.get("/pass")
async def get_pass(slug: str = Query(..., min_length=1)):
    if not apple_wallet_configured():
        raise HTTPException(status_code=503, detail="Apple Wallet not configured")

    sb = get_supabase()
    result = sb.table("wallet_passes").select("*").eq("slug", slug).maybe_single().execute()
    record = result.data
    if not record:
        raise HTTPException(status_code=404, detail="No wallet pass found for that slug")
    if not record.get("purchased"):
        raise HTTPException(status_code=402, detail="This pass hasn't been purchased yet")

    barcode_url = f"https://flow.fass.systems/c/{slug}"

    try:
        pkpass_bytes = generate_pkpass(
            business_name=record["business_name"],
            address=record.get("address"),
            naics=record.get("naics"),
            website=record.get("website"),
            phone=record.get("phone"),
            barcode_url=barcode_url,
            serial_number=slug,
            bg_color=record.get("bg_color"),
            logo_url=record.get("logo_url"),
            show_address=record.get("show_address", True),
            show_naics=record.get("show_naics", True),
            show_phone=record.get("show_phone", True),
            show_website=record.get("show_website", True),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    safe_name = "".join(c for c in record["business_name"] if c.isalnum() or c in " -_").strip().replace(" ", "-") or "fass-wallet"

    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.pkpass"',
        },
    )
