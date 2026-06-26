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
  6. POST /rewards/redeem — the actual payoff: once a card's stamps reach
     the program's threshold, the business taps Redeem (after physically
     handing over the free item), which carries any extra stamps forward,
     bumps redeemed_count, and logs a row in reward_redemptions. Before
     this existed, stamps just counted up forever with no closing action —
     "REWARD READY!" on the pass had nothing behind it.
"""
import re
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.database import get_supabase, single_data
from app.services.apns import notify_devices
from app.services.applewallet import apple_wallet_configured, generate_storecard_pkpass
from app.routers.wallet_campaigns import active_offer_for_card

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
        .select("slug, customer_name, stamps, redeemed_count, created_at, updated_at")
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
    # Silently push any device that already has this card in Wallet so the
    # new stamp count shows up without the customer re-downloading anything.
    notify_devices(sb, body.slug)
    return {"slug": body.slug, "stamps": new_stamps}


class RedeemRequest(BaseModel):
    slug: str
    business_user_id: str


@router.post("/redeem")
async def redeem_reward(body: RedeemRequest):
    """The business confirms they've handed over the free item. Resets the
    card by subtracting the threshold (not zeroing outright) so any stamps
    earned past the threshold carry forward into the next round instead of
    being thrown away, bumps redeemed_count for the business's own loyalty
    insight, and logs an audit row in reward_redemptions."""
    sb = get_supabase()
    card = single_data(
        sb.table("reward_cards")
        .select("stamps, redeemed_count, business_user_id")
        .eq("slug", body.slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No rewards card found for that link")
    if card["business_user_id"] != body.business_user_id:
        raise HTTPException(status_code=403, detail="This card belongs to a different business")

    program = single_data(
        sb.table("reward_programs")
        .select("reward_threshold")
        .eq("business_user_id", body.business_user_id)
        .maybe_single()
        .execute()
    )
    threshold = (program or {}).get("reward_threshold", 10)
    if card["stamps"] < threshold:
        raise HTTPException(status_code=400, detail=f"Card has {card['stamps']} of {threshold} stamps — not ready to redeem yet")

    new_stamps = card["stamps"] - threshold
    new_redeemed_count = (card.get("redeemed_count") or 0) + 1
    sb.table("reward_cards").update({
        "stamps": new_stamps,
        "redeemed_count": new_redeemed_count,
    }).eq("slug", body.slug).execute()
    sb.table("reward_redemptions").insert({
        "card_slug": body.slug,
        "business_user_id": body.business_user_id,
        "stamps_at_redemption": card["stamps"],
    }).execute()
    notify_devices(sb, body.slug)

    return {"slug": body.slug, "stamps": new_stamps, "redeemed_count": new_redeemed_count}


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

    # Points at the staff redemption confirm page, not a re-download link —
    # this is the SAME QR a customer's Wallet pass already shows, so staff
    # can scan it with their phone's normal camera app to redeem a live
    # Wallet Messaging offer (see wallet_campaigns.py) without any in-app
    # scanner. A customer never needs to scan their own card's QR.
    barcode_url = f"https://flow.fass.systems/rewards/scan/{slug}"
    offer_message, offer_detail = active_offer_for_card(sb, card)

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
            offer_message=offer_message,
            offer_detail=offer_detail,
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
