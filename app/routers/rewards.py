"""FASS Rewards — restaurant/business loyalty stamp cards (Apple Wallet
storeCard pass type), separate from wallet.py's single business-identity
card. A business sets up ONE reward_programs row (their punch-card rules:
business name, stamps needed, reward description, branding); every
customer who joins gets their OWN reward_cards row (a real signed
storeCard .pkpass) tracking their personal stamp count against that
program.

Flow:
  1. POST /rewards/program — business creates/updates their program. Upsert
     keyed on business_user_id, so editing it (e.g. changing the stamp
     threshold) doesn't touch any already-issued customer cards' historical
     stamp counts, just how new ones render going forward.
  2. GET /rewards/program/mine — business dashboard: program config + every
     customer card issued under it, so staff can see who's close to a reward.
  3. POST /rewards/join — a customer claims their own card under a business's
     program. This is what the QR/link the business hands out points at.
  4. GET /rewards/pass?slug=... — signed storeCard .pkpass for ONE customer's
     card. No live push yet (no Apple PassKit web service wired up), so a
     stamp added after the card is already in someone's Wallet app needs a
     manual re-download to show — same /pass?slug=... URL, just re-fetched.
  5. POST /rewards/stamp — business adds (or removes, via negative delta) a
     stamp on a specific customer's card. Ownership-checked against
     business_user_id so only the program's own owner can stamp it.
"""
import re
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.database import get_supabase, single_data
from app.services.applewallet import apple_wallet_configured, generate_storecard_pkpass

router = APIRouter(prefix="/rewards", tags=["rewards"])


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rewards"
    return f"{base}-{uuid.uuid4().hex[:6]}"


class ProgramRequest(BaseModel):
    business_user_id: str
    business_name: str
    reward_threshold: int = 10
    reward_description: str | None = None
    bg_color: str | None = None
    logo_url: str | None = None


@router.post("/program")
async def upsert_program(body: ProgramRequest):
    """One program per business — upsert keyed on business_user_id so
    re-running this just edits the existing rules instead of creating a
    second, conflicting program."""
    sb = get_supabase()
    row = {
        "business_user_id": body.business_user_id,
        "business_name": body.business_name,
        "reward_threshold": max(1, body.reward_threshold),
        "reward_description": body.reward_description,
        "bg_color": body.bg_color or "#0f5132",
        "logo_url": body.logo_url,
    }
    sb.table("reward_programs").upsert(row, on_conflict="business_user_id").execute()
    return {"ok": True}


@router.get("/program/mine")
async def get_my_program(user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    program = single_data(
        sb.table("reward_programs")
        .select("*")
        .eq("business_user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not program:
        raise HTTPException(status_code=404, detail="No rewards program set up yet")

    cards = (
        sb.table("reward_cards")
        .select("slug, customer_name, stamps, created_at, updated_at")
        .eq("business_user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    ).data or []

    return {"program": program, "cards": cards}


class JoinRequest(BaseModel):
    business_user_id: str
    customer_name: str | None = None
    customer_contact: str | None = None


@router.post("/join")
async def join_program(body: JoinRequest):
    sb = get_supabase()
    program = single_data(
        sb.table("reward_programs")
        .select("business_name")
        .eq("business_user_id", body.business_user_id)
        .maybe_single()
        .execute()
    )
    if not program:
        raise HTTPException(status_code=404, detail="That business hasn't set up a rewards program")

    slug = _slugify(program["business_name"])
    row = {
        "slug": slug,
        "business_user_id": body.business_user_id,
        "customer_name": body.customer_name,
        "customer_contact": body.customer_contact,
        "stamps": 0,
    }
    sb.table("reward_cards").insert(row).execute()
    return {"slug": slug}


class StampRequest(BaseModel):
    slug: str
    business_user_id: str
    delta: int = 1


@router.post("/stamp")
async def add_stamp(body: StampRequest):
    sb = get_supabase()
    card = single_data(
        sb.table("reward_cards")
        .select("stamps, business_user_id")
        .eq("slug", body.slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No rewards card found for that link")
    if card["business_user_id"] != body.business_user_id:
        raise HTTPException(status_code=403, detail="This card belongs to a different business")

    new_stamps = max(0, card["stamps"] + body.delta)
    sb.table("reward_cards").update({"stamps": new_stamps}).eq("slug", body.slug).execute()
    return {"slug": body.slug, "stamps": new_stamps}


@router.get("/pass")
async def get_rewards_pass(slug: str = Query(..., min_length=1)):
    if not apple_wallet_configured():
        raise HTTPException(status_code=503, detail="Apple Wallet not configured")

    sb = get_supabase()
    card = single_data(sb.table("reward_cards").select("*").eq("slug", slug).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="No rewards card found for that link")

    program = single_data(
        sb.table("reward_programs")
        .select("*")
        .eq("business_user_id", card["business_user_id"])
        .maybe_single()
        .execute()
    )
    if not program:
        raise HTTPException(status_code=404, detail="This card's business program no longer exists")

    barcode_url = f"https://flow.fass.systems/rewards/{slug}"

    try:
        pkpass_bytes = generate_storecard_pkpass(
            business_name=program["business_name"],
            stamps=card["stamps"],
            reward_threshold=program.get("reward_threshold", 10),
            reward_description=program.get("reward_description"),
            barcode_url=barcode_url,
            serial_number=slug,
            bg_color=program.get("bg_color"),
            logo_url=program.get("logo_url"),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    safe_name = "".join(c for c in program["business_name"] if c.isalnum() or c in " -_").strip().replace(" ", "-") or "fass-rewards"

    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}-rewards.pkpass"',
        },
    )
