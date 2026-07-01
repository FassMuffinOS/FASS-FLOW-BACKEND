"""FASS Wallet Messaging — one-way broadcast campaigns (offers/coupons)
pushed onto customers' EXISTING reward_cards storeCard passes.

Flow: business writes a short offer -> POST /campaigns broadcasts it onto
every active customer card for that business (sets reward_cards.
active_campaign_id, then notify_devices() silently pushes each device so
Wallet's changeMessage banner mechanism surfaces it without a re-download)
-> customer sees the offer on their card next time they glance at Wallet
-> customer shows the card in-store -> staff scans the card's EXISTING QR
code with their phone's normal camera app (no in-app scanner needed — the
QR already encodes https://flow.fass.systems/rewards/{slug}, which the
frontend's redemption confirm page reads) -> POST /campaigns/redeem records
it and clears the offer (unless repeat_use) -> GET /campaigns/mine shows
the business sent/redeemed counts + a rough revenue estimate.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data
from app.services.apns import notify_devices
from app.services.llm import llm_router, LLMUnavailableError

router = APIRouter(prefix="/campaigns", tags=["wallet-campaigns"])


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_offer_for_card(sb, card: dict) -> tuple[str | None, str | None]:
    """Returns (offer_message, offer_detail) for a reward_cards row, or
    (None, None) if it has no live offer — used by both rewards.py's
    /rewards/pass and wallet_passkit.py's get_latest_pass so a campaign's
    message actually renders into the storeCard's headerField/changeMessage,
    not just sit in the wallet_campaigns table unused. Lazily clears a
    stale (expired or deactivated) campaign reference off the card so it
    self-heals on next fetch instead of needing a cron job."""
    campaign_id = card.get("active_campaign_id")
    if not campaign_id:
        return None, None

    campaign = single_data(
        sb.table("wallet_campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    )
    if not campaign:
        return None, None

    expired = bool(campaign.get("expires_at")) and _parse_ts(campaign["expires_at"]) and _parse_ts(campaign["expires_at"]) < datetime.now(timezone.utc)
    if not campaign.get("active") or expired:
        sb.table("reward_cards").update({"active_campaign_id": None}).eq("slug", card["slug"]).execute()
        return None, None

    return campaign["message"], campaign.get("detail")


class CreateCampaignRequest(BaseModel):
    business_user_id: str
    message: str
    detail: str | None = None
    expires_at: str | None = None
    repeat_use: bool = False
    estimated_value: float | None = None
    target_slugs: list[str] | None = None  # if given/non-empty, scope the send
    # to just these reward_cards.slug values instead of every card the
    # business has issued — backs the customer list's targeted/segmented send.


@router.post("")
async def create_and_send_campaign(body: CreateCampaignRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Creates the campaign, then immediately broadcasts it onto either every
    customer card this business has issued, or — if target_slugs is set —
    just that selected subset. There's no separate 'draft then send' step in
    the MVP, matching the one-way-broadcast spec (write an offer, it goes
    out, optionally to a chosen segment)."""
    require_owner(current_user, body.business_user_id, detail="You can only send campaigns for your own account")
    sb = get_supabase()
    row = {
        "business_user_id": body.business_user_id,
        "message": body.message.strip(),
        "detail": body.detail,
        "expires_at": body.expires_at,
        "repeat_use": body.repeat_use,
        "estimated_value": body.estimated_value,
    }
    if not row["message"]:
        raise HTTPException(status_code=400, detail="Offer message can't be empty")

    campaign = single_data(sb.table("wallet_campaigns").insert(row).select("*").maybe_single().execute())
    if not campaign:
        # Some supabase-py versions don't return rows from .insert().select() —
        # fall back to re-querying by the fields we just wrote, newest first.
        campaign = single_data(
            sb.table("wallet_campaigns")
            .select("*")
            .eq("business_user_id", body.business_user_id)
            .order("created_at", desc=True)
            .limit(1)
            .maybe_single()
            .execute()
        )
    if not campaign:
        raise HTTPException(status_code=500, detail="Campaign was created but could not be re-read")

    cards_query = sb.table("reward_cards").select("slug").eq("business_user_id", body.business_user_id)
    target_slugs = [s for s in (body.target_slugs or []) if s]
    if target_slugs:
        cards_query = cards_query.in_("slug", target_slugs)
    cards = cards_query.execute().data or []

    for c in cards:
        sb.table("reward_cards").update({"active_campaign_id": campaign["id"]}).eq("slug", c["slug"]).execute()
        notify_devices(sb, c["slug"])

    sb.table("wallet_campaigns").update({"sent_count": len(cards)}).eq("id", campaign["id"]).execute()
    campaign["sent_count"] = len(cards)

    return {"campaign": campaign, "sent_count": len(cards)}


@router.get("/mine")
async def list_my_campaigns(user_id: str = Query(..., min_length=1), current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own campaigns")
    sb = get_supabase()
    campaigns = (
        sb.table("wallet_campaigns")
        .select("*")
        .eq("business_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    cards = (
        sb.table("reward_cards")
        .select("slug, customer_name, customer_contact, stamps, redeemed_count, active_campaign_id, created_at, updated_at")
        .eq("business_user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    ).data or []

    # Per-customer redemption totals across ALL campaigns, in one query
    # rather than N — used below to show each customer's lifetime offer
    # redemptions on the contacts list, the data that makes the list a
    # tool (who's engaged, who's gone quiet) rather than just a head count.
    redemptions = (
        sb.table("wallet_campaign_redemptions")
        .select("card_slug")
        .eq("business_user_id", user_id)
        .execute()
    ).data or []
    redemptions_by_slug: dict[str, int] = {}
    for r in redemptions:
        redemptions_by_slug[r["card_slug"]] = redemptions_by_slug.get(r["card_slug"], 0) + 1

    customers = [
        {
            "slug": c["slug"],
            "customer_name": c.get("customer_name"),
            "customer_contact": c.get("customer_contact"),
            "stamps": c.get("stamps") or 0,
            "redeemed_count": c.get("redeemed_count") or 0,
            "offer_redemptions": redemptions_by_slug.get(c["slug"], 0),
            "has_active_offer": bool(c.get("active_campaign_id")),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        for c in cards
    ]
    customer_count = len(cards)

    results = []
    for c in campaigns:
        redeemed = len(
            (
                sb.table("wallet_campaign_redemptions")
                .select("id")
                .eq("campaign_id", c["id"])
                .execute()
            ).data
            or []
        )
        revenue_estimate = (c.get("estimated_value") or 0) * redeemed
        results.append({**c, "redeemed_count": redeemed, "revenue_estimate": revenue_estimate})

    return {"campaigns": results, "customer_count": customer_count, "customers": customers}


class RedeemCampaignRequest(BaseModel):
    slug: str
    business_user_id: str


@router.post("/redeem")
async def redeem_campaign_offer(body: RedeemCampaignRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Staff-side confirm step after scanning a customer's existing pass QR
    with their phone's camera — lands on the frontend's redemption confirm
    page, which calls this. Ownership-checked the same way /rewards/stamp
    and /rewards/redeem already are."""
    require_owner(current_user, body.business_user_id, detail="You can only redeem offers for your own business")
    sb = get_supabase()
    card = single_data(
        sb.table("reward_cards")
        .select("slug, business_user_id, active_campaign_id")
        .eq("slug", body.slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No rewards card found for that link")
    if card["business_user_id"] != body.business_user_id:
        raise HTTPException(status_code=403, detail="This card belongs to a different business")

    message, _ = active_offer_for_card(sb, card)
    if not message or not card.get("active_campaign_id"):
        raise HTTPException(status_code=400, detail="This card has no active offer to redeem")

    campaign_id = card["active_campaign_id"]
    campaign = single_data(
        sb.table("wallet_campaigns").select("repeat_use").eq("id", campaign_id).maybe_single().execute()
    )

    sb.table("wallet_campaign_redemptions").insert({
        "campaign_id": campaign_id,
        "card_slug": body.slug,
        "business_user_id": body.business_user_id,
    }).execute()

    if not (campaign or {}).get("repeat_use"):
        sb.table("reward_cards").update({"active_campaign_id": None}).eq("slug", body.slug).execute()
        notify_devices(sb, body.slug)

    return {"ok": True, "slug": body.slug, "message": message}


@router.get("/lookup")
async def lookup_card_for_redemption(slug: str = Query(..., min_length=1), business_user_id: str = Query(..., min_length=1), current_user: CurrentUser = Depends(get_current_user)):
    """Backs the staff redemption confirm page — given a slug (read off the
    customer's pass QR, which already encodes a flow.fass.systems URL) and
    the logged-in business's own user id, returns enough info to render a
    'Confirm Redeem' screen before /campaigns/redeem is actually called."""
    require_owner(current_user, business_user_id, detail="You can only look up your own business's cards")
    sb = get_supabase()
    card = single_data(
        sb.table("reward_cards")
        .select("slug, customer_name, stamps, business_user_id, active_campaign_id")
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No rewards card found for that link")
    if card["business_user_id"] != business_user_id:
        raise HTTPException(status_code=403, detail="This card belongs to a different business")

    message, detail = active_offer_for_card(sb, card)
    return {
        "slug": card["slug"],
        "customer_name": card.get("customer_name"),
        "stamps": card["stamps"],
        "offer_message": message,
        "offer_detail": detail,
    }


INSIGHTS_SYSTEM_PROMPT = """You are a marketing advisor for a small local business (a food \
truck, cafe, or similar) that uses digital Apple Wallet loyalty cards and one-way promotional \
campaigns pushed straight to those cards. You will be given real, structured performance data \
for exactly one business — campaign send/redeem counts and customer activity segments. Write a \
short, plain-English suggestion (3-5 sentences, no headers, no bullet points, no markdown) that:

- References the actual numbers you were given (counts, rates, names of campaigns) rather than \
speaking generically.
- Says plainly which campaign (if any) performed best or worst, by redemption rate.
- Calls out the going-quiet customer count if it's non-trivial, and suggests one concrete next \
move to win them back (e.g. a specific kind of offer, or targeting just that segment).
- If there isn't enough data yet to say anything meaningful (e.g. no campaigns sent, or only a \
handful of customers), say that plainly and suggest the single most useful next action instead \
of inventing insights.

Do not invent numbers, customer names, or outcomes that weren't given to you. Do not use the \
words "leverage", "optimize", or "synergy". Respond with ONLY the suggestion paragraph."""


@router.get("/insights")
async def get_campaign_insights(user_id: str = Query(..., min_length=1), current_user: CurrentUser = Depends(get_current_user)):
    """Regulars' Insights tab: per-campaign performance, a going-quiet vs.
    active customer breakdown, and one AI-generated suggestion paragraph
    grounded in that real data — not a canned tip. Reuses the same
    sent/redeemed computation as /campaigns/mine so the numbers here always
    match what the campaign list shows."""
    require_owner(current_user, user_id, detail="You can only view your own insights")
    sb = get_supabase()

    campaigns = (
        sb.table("wallet_campaigns")
        .select("*")
        .eq("business_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    cards = (
        sb.table("reward_cards")
        .select("slug, stamps, redeemed_count, created_at, updated_at")
        .eq("business_user_id", user_id)
        .execute()
    ).data or []

    campaign_stats = []
    for c in campaigns:
        redeemed = len(
            (
                sb.table("wallet_campaign_redemptions")
                .select("id")
                .eq("campaign_id", c["id"])
                .execute()
            ).data
            or []
        )
        sent = c.get("sent_count") or 0
        rate = round((redeemed / sent) * 100, 1) if sent else 0.0
        campaign_stats.append({
            "id": c["id"],
            "message": c["message"],
            "created_at": c.get("created_at"),
            "sent_count": sent,
            "redeemed_count": redeemed,
            "redemption_rate": rate,
        })

    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(days=14)
    quiet_cutoff = now - timedelta(days=30)

    active_count = 0
    going_quiet_count = 0
    lapsed_count = 0
    for card in cards:
        updated = _parse_ts(card.get("updated_at")) or _parse_ts(card.get("created_at"))
        if not updated:
            continue
        if updated >= active_cutoff:
            active_count += 1
        elif updated >= quiet_cutoff:
            going_quiet_count += 1
        else:
            lapsed_count += 1

    segments = {
        "total_customers": len(cards),
        "active": active_count,
        "going_quiet": going_quiet_count,
        "lapsed": lapsed_count,
    }

    best = max(campaign_stats, key=lambda c: c["redemption_rate"], default=None) if any(c["sent_count"] for c in campaign_stats) else None
    worst = min(campaign_stats, key=lambda c: c["redemption_rate"], default=None) if len(campaign_stats) > 1 else None

    prompt = (
        f"Total customers with a wallet card: {segments['total_customers']}\n"
        f"Active (stamped/visited in last 14 days): {segments['active']}\n"
        f"Going quiet (14-30 days since last activity): {segments['going_quiet']}\n"
        f"Lapsed (30+ days since last activity): {segments['lapsed']}\n\n"
        f"Campaigns sent ({len(campaign_stats)} total):\n"
    )
    if campaign_stats:
        for c in campaign_stats:
            prompt += f"- \"{c['message']}\": sent to {c['sent_count']}, redeemed by {c['redeemed_count']} ({c['redemption_rate']}% redemption rate)\n"
    else:
        prompt += "- none sent yet\n"
    if best:
        prompt += f"\nBest performing campaign by redemption rate: \"{best['message']}\" at {best['redemption_rate']}%\n"
    if worst and worst is not best:
        prompt += f"Worst performing campaign by redemption rate: \"{worst['message']}\" at {worst['redemption_rate']}%\n"

    ai_suggestion = None
    ai_error = None
    try:
        result = await llm_router.complete(system=INSIGHTS_SYSTEM_PROMPT, prompt=prompt, max_tokens=300)
        ai_suggestion = result.text.strip()
    except LLMUnavailableError as e:
        ai_error = str(e)

    return {
        "campaigns": campaign_stats,
        "segments": segments,
        "ai_suggestion": ai_suggestion,
        "ai_error": ai_error,
    }
