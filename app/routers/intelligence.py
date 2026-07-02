"""WARDOG Intel — Enterprise-tier incumbent & award-history intelligence.

Looks up an agency + NAICS code's recent prime-award history directly from
USASpending.gov's public Awards Search API (no key required, unlike
SAM.gov) and asks the LLM to synthesize it into an actionable read: who
holds it now, what they were paid, who else has won it, and a likely
re-compete strategy.

Deliberately a new router rather than bolted onto wardog.py — WARDOG is
"what's open right now" (SAM.gov), this is "who has won this before"
(USASpending.gov). Different data source, different gate (Enterprise plan
only), different audience (the Enterprise buyer, not every free user).

v1 is intentionally synchronous: a live USASpending.gov call on request,
cached for an hour (award history doesn't move minute to minute the way
SAM.gov postings do). No nightly ingestion table yet — that's the 0-30
day roadmap item once this is validated with real Enterprise customers.
"""
from datetime import datetime, timezone

import stripe
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.cache import cache_get, cache_set
from app.config import settings
from app.database import get_supabase, single_data
from app.routers.credits import consume_credits
from app.services.llm import llm_router, LLMUnavailableError

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/intelligence", tags=["intelligence"])

USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# Plans that unlock unlimited WARDOG Intel. Kept as a set (not a single "==
# enterprise" check) so a future higher tier can inherit access without
# touching this file. Everyone else can still get in à la carte — see
# wardog_intel_reports below — $39 buys exactly one report (one incumbent
# pull + one forecast, scoped to a single naics/agency pair).
INTEL_PLANS = {"enterprise"}


def _get_plan(user_id: str) -> str | None:
    sb = get_supabase()
    profile = single_data(
        sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()
    )
    return (profile or {}).get("plan")


def _require_intel_plan(user_id: str) -> None:
    """Enterprise-only gate, kept for the unbounded-access path. Non-Enterprise
    callers now have a second way in — a purchased report id, checked
    separately in /incumbent and /forecast via _consume_report / _get_report."""
    if _get_plan(user_id) not in INTEL_PLANS:
        raise HTTPException(
            status_code=402,
            detail="WARDOG Intel is an Enterprise-tier feature, or buy a single report à la carte for $39.",
        )


def _get_report(report_id: str, user_id: str) -> dict:
    sb = get_supabase()
    row = single_data(
        sb.table("wardog_intel_reports")
        .select("*")
        .eq("id", report_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    return row


@router.get("/incumbent")
async def get_incumbent_history(
    naics: str = Query(..., description="NAICS code, e.g. 561720"),
    agency: str = Query("", description="Top-tier awarding agency name, blank = all agencies"),
    user_id: str = Query(..., description="Caller's user id, for the owner + plan gate"),
    report_id: str = Query("", description="A purchased à la carte report id — required for non-Enterprise users"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Recent prime awards matching this NAICS (+ optional agency), pulled
    live from USASpending.gov. No API key required — it's a public
    dataset — so unlike wardog.py there's no secret to keep off the
    frontend; this still lives server-side to centralize the Enterprise
    gate and the cache.

    Two ways in: Enterprise plan (unlimited), or a purchased report_id
    (à la carte, $39/report — see POST /intelligence/checkout). The report
    is consumed here, on the first pull — opening a report spends it."""
    require_owner(current_user, user_id, detail="You can only run intel lookups for your own account")

    if _get_plan(user_id) not in INTEL_PLANS:
        if not report_id:
            raise HTTPException(
                status_code=402,
                detail="WARDOG Intel is an Enterprise-tier feature, or buy a single report à la carte for $39.",
            )
        report = _get_report(report_id, user_id)
        if report["status"] == "pending_payment":
            raise HTTPException(status_code=402, detail="This report hasn't been paid for yet.")
        if report["status"] != "unused":
            raise HTTPException(status_code=409, detail="This report has already been opened.")
        sb = get_supabase()
        sb.table("wardog_intel_reports").update({
            "status": "opened", "naics": naics, "agency": agency,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", report_id).execute()

    return await fetch_incumbent_awards(naics, agency)


async def fetch_incumbent_awards(naics: str, agency: str = "") -> dict:
    """The actual USASpending.gov pull + shaping, factored out of the route
    above so app/routers/data_api.py's external endpoint can call the exact
    same logic (including the cache) without duplicating it or importing a
    FastAPI route function directly."""
    cache_key = f"intel:incumbent:{naics}:{agency}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    filters = {
        "naics_codes": [naics],
        "award_type_codes": ["A", "B", "C", "D"],
        "time_period": [{"start_date": "2021-10-01", "end_date": "2026-09-30"}],
    }
    if agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": agency}]

    payload = {
        "filters": filters,
        "fields": [
            "Recipient Name", "Award Amount", "Awarding Agency",
            "Period of Performance Start Date", "Period of Performance Current End Date",
            "Award ID", "NAICS Code",
        ],
        "page": 1,
        "limit": 10,
        "sort": "Award Amount",
        "order": "desc",
        "subawards": False,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(USASPENDING_AWARDS_URL, json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach USASpending.gov: {e}") from e

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"USASpending.gov returned {resp.status_code}: {resp.text[:300]}",
        )

    data = resp.json()
    raw_results = data.get("results", [])
    awards = [
        {
            "recipient_name": r.get("Recipient Name"),
            "award_amount": r.get("Award Amount"),
            "awarding_agency": r.get("Awarding Agency"),
            "period_of_performance_start": r.get("Period of Performance Start Date"),
            "period_of_performance_end": r.get("Period of Performance Current End Date"),
            "award_id": r.get("Award ID") or r.get("generated_internal_id"),
            "naics_code": r.get("NAICS Code") or naics,
        }
        for r in raw_results
    ]

    result = {"awards": awards, "naics": naics, "agency": agency, "total": len(awards)}
    await cache_set(cache_key, result, ex=3600)  # 1 hr — award history doesn't move like SAM.gov postings
    return result


class ForecastRequest(BaseModel):
    naics: str
    agency: str = ""
    title: str = ""
    awards: list[dict] = []
    user_id: str
    report_id: str = ""  # required for non-Enterprise users — same report /incumbent opened


FORECAST_SYSTEM_PROMPT = (
    "You are a federal capture strategist reading a list of past prime awards "
    "for a NAICS code and agency. Given the award history, write a short, "
    "decision-useful re-compete read for a small business considering whether "
    "to bid. Be concrete and concise — no filler, no disclaimers."
)


@router.post("/forecast")
async def forecast_recompete(
    body: ForecastRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """AI synthesis on top of /incumbent's raw award list: incumbent read,
    re-compete odds, likely price band, and an entry strategy. Costs 1 AI
    credit per call — same metering as /ai/draft-section — since this is a
    judgment call layered on top of the raw lookup above. For à la carte
    buyers the $39 report already paid for the incumbent pull; this just
    needs the SAME report (now 'opened') to confirm they're still inside
    the report they bought, and marks it 'completed' once the forecast runs."""
    require_owner(current_user, body.user_id, detail="You can only run intel forecasts for your own account")

    if _get_plan(body.user_id) not in INTEL_PLANS:
        if not body.report_id:
            raise HTTPException(
                status_code=402,
                detail="WARDOG Intel is an Enterprise-tier feature, or buy a single report à la carte for $39.",
            )
        report = _get_report(body.report_id, body.user_id)
        if report["status"] not in ("opened", "completed"):
            raise HTTPException(status_code=409, detail="Pull the award history for this report before forecasting.")
        sb = get_supabase()
        sb.table("wardog_intel_reports").update({
            "status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.report_id).execute()

    ok, balance = consume_credits(body.user_id, n=1, reason="wardog_intel_forecast")
    if not ok:
        raise HTTPException(status_code=402, detail="Out of AI credits. Add credits to keep running forecasts.")

    awards_summary = "\n".join(
        f"- {a.get('recipient_name') or 'Unknown recipient'}: "
        f"${(a.get('award_amount') or 0):,.0f} "
        f"({a.get('period_of_performance_start') or '?'} to {a.get('period_of_performance_end') or '?'})"
        for a in body.awards[:10]
    ) or "No prior award history found for this NAICS/agency pair."

    prompt = (
        f"NAICS: {body.naics}\n"
        f"Agency: {body.agency or 'Not specified'}\n"
        f"Opportunity: {body.title or 'Not specified'}\n\n"
        f"Prior awards:\n{awards_summary}\n\n"
        "Return a short read covering, in this order:\n"
        "1. Who the incumbent is and how entrenched they look\n"
        "2. Re-compete odds for a new entrant (high/medium/low + why)\n"
        "3. A likely award price band based on the history above\n"
        "4. One concrete entry-strategy recommendation"
    )

    try:
        result = await llm_router.complete(FORECAST_SYSTEM_PROMPT, prompt, max_tokens=500)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return {
        "forecast": result.text,
        "provider": result.provider,
        "model": result.model,
        "credits_remaining": balance,
    }


@router.get("/reports")
async def list_my_reports(user_id: str = Query(...), current_user: CurrentUser = Depends(get_current_user)):
    """A buyer's report inventory — paid-but-unopened reports they can spend
    on a search, plus opened/completed ones for history. Frontend uses the
    'unused' ones to skip straight to a search instead of re-buying."""
    require_owner(current_user, user_id, detail="You can only view your own reports")
    sb = get_supabase()
    rows = (
        sb.table("wardog_intel_reports")
        .select("id, status, naics, agency, created_at, opened_at, completed_at")
        .eq("user_id", user_id)
        .neq("status", "pending_payment")
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return {"reports": rows.data or []}


class IntelCheckoutRequest(BaseModel):
    user_id: str
    email: str


@router.post("/checkout")
async def create_report_checkout(body: IntelCheckoutRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Starts a $39 Stripe Checkout session for one à la carte WARDOG Intel
    report. A 'pending_payment' row is created up front (not after payment)
    so its id can ride in checkout metadata — the webhook then just flips
    it to 'unused' rather than having to create it from scratch, keeping
    the same create-before-pay pattern gift_cards.py / wallet passes use
    for one-time purchases that need a pre-existing row to update."""
    require_owner(current_user, body.user_id, detail="You can only start checkout for your own account")
    if not settings.stripe_price_wardog_intel_report:
        raise HTTPException(status_code=503, detail="WARDOG Intel à la carte isn't configured yet")

    sb = get_supabase()
    inserted = (
        sb.table("wardog_intel_reports")
        .insert({"user_id": body.user_id, "status": "pending_payment"})
        .execute()
    )
    report_id = inserted.data[0]["id"]

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{"price": settings.stripe_price_wardog_intel_report, "quantity": 1}],
        customer_email=body.email,
        metadata={"kind": "wardog_intel_report", "user_id": body.user_id, "report_id": report_id},
        success_url=f"{settings.frontend_url}/dashboard?intel_unlock=success&report_id={report_id}",
        cancel_url=f"{settings.frontend_url}/dashboard?intel_unlock=cancelled",
    )
    return {"url": session.url, "report_id": report_id}
