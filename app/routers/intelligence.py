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
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.cache import cache_get, cache_set
from app.database import get_supabase, single_data
from app.routers.credits import consume_credits
from app.services.llm import llm_router, LLMUnavailableError

router = APIRouter(prefix="/intelligence", tags=["intelligence"])

USASPENDING_AWARDS_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# Plans that unlock WARDOG Intel. Kept as a set (not a single "==
# enterprise" check) so a future higher tier can inherit access without
# touching this file.
INTEL_PLANS = {"enterprise"}


def _require_intel_plan(user_id: str) -> None:
    """Server-side gate, not just a hidden UI panel — this is the API
    surface that justifies the Enterprise tier's price, so it has to be
    enforced here even if the frontend never renders a button for it."""
    sb = get_supabase()
    profile = single_data(
        sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()
    )
    plan = (profile or {}).get("plan")
    if plan not in INTEL_PLANS:
        raise HTTPException(
            status_code=402,
            detail="WARDOG Intel is an Enterprise-tier feature. Upgrade to unlock incumbent and award-history intelligence.",
        )


@router.get("/incumbent")
async def get_incumbent_history(
    naics: str = Query(..., description="NAICS code, e.g. 561720"),
    agency: str = Query("", description="Top-tier awarding agency name, blank = all agencies"),
    user_id: str = Query(..., description="Caller's user id, for the owner + plan gate"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Recent prime awards matching this NAICS (+ optional agency), pulled
    live from USASpending.gov. No API key required — it's a public
    dataset — so unlike wardog.py there's no secret to keep off the
    frontend; this still lives server-side to centralize the Enterprise
    gate and the cache."""
    require_owner(current_user, user_id, detail="You can only run intel lookups for your own account")
    _require_intel_plan(user_id)

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
    judgment call layered on top of the (free, cached) raw lookup above."""
    require_owner(current_user, body.user_id, detail="You can only run intel forecasts for your own account")
    _require_intel_plan(body.user_id)

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
