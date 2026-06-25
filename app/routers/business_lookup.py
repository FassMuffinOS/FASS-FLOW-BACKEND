"""Business lookup — Google Places (New) Text Search, proxied server-side.

Same shape as wardog.py's SAM.gov proxy: the API key lives only in this
service's environment, never in the frontend bundle. Powers Passport's
"Find my business" quick-setup flow — type a business name, get back a
real name/address/category from Google, plus a best-guess NAICS code and
a few live WARDOG opportunities for that NAICS so a brand-new signup sees
a tailored result before they've typed anything else.

Everything this endpoint infers (NAICS, suggested plan) is a soft guess
the user can overwrite on Passport — Google's business categories say
nothing about federal contracting readiness, certifications, or size
standards, so none of this should ever gate access on its own.
"""
import httpx
from fastapi import APIRouter, HTTPException, Query
from app.config import settings
from app.cache import cache_get, cache_set
from app.routers.wardog import search_opportunities

router = APIRouter(prefix="/business", tags=["business"])

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Google's "types" taxonomy -> the NAICS codes WARDOG's filter already
# knows about (see Wardog.jsx's NAICS_OPTIONS). Deliberately small and
# curated rather than exhaustive — covers the trades and service
# businesses most likely to be a FASS signup. First match wins.
GOOGLE_TYPE_TO_NAICS = {
    "restaurant": "722310",
    "cafe": "722310",
    "bakery": "722310",
    "meal_takeaway": "722310",
    "meal_delivery": "722310",
    "catering_service": "722310",
    "plumber": "238220",
    "hvac_contractor": "238220",
    "electrician": "238210",
    "roofing_contractor": "238160",
    "painter": "238320",
    "general_contractor": "236220",
    "moving_company": "484210",
    "trucking_company": "484110",
    "landscaping": "561730",
    "lawn_care": "561730",
    "house_cleaning_service": "561720",
    "janitorial_service": "561720",
    "security_guard_service": "561612",
    "staffing_agency": "561320",
    "employment_agency": "561320",
    "event_planner": "711310",
    "locksmith": "561622",
    "storage": "531130",
}

# Soft, low-confidence nudge only — Google review volume measures
# consumer-facing reputation, not federal contracting capacity or past
# performance. Shown as a suggestion the user can ignore on /pricing.
def _suggest_plan(rating_count: int | None) -> str:
    if not rating_count:
        return "starter"
    if rating_count < 50:
        return "starter"
    if rating_count < 200:
        return "pro"
    return "team"


def _guess_naics(types: list[str]) -> str | None:
    for t in types or []:
        if t in GOOGLE_TYPE_TO_NAICS:
            return GOOGLE_TYPE_TO_NAICS[t]
    return None


@router.get("/lookup")
async def lookup_business(query: str = Query(..., min_length=2)):
    if not settings.google_places_api_key:
        # No key configured yet — frontend just skips the lookup and falls
        # back to manual entry, same pattern as WARDOG's 503 handling.
        raise HTTPException(status_code=503, detail="Business lookup not configured")

    cache_key = f"bizlookup:{query.lower().strip()}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    body = {"textQuery": query}
    headers = {
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.types,"
            "places.websiteUri,places.nationalPhoneNumber,places.rating,"
            "places.userRatingCount,places.businessStatus"
        ),
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(PLACES_SEARCH_URL, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Google Places: {e}") from e

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=502, detail=f"Places API returned {resp.status_code}: {detail}")

    places = resp.json().get("places", [])
    if not places:
        result = {"found": False}
        await cache_set(cache_key, result, ex=3600)
        return result

    p = places[0]
    types = p.get("types", [])
    naics_guess = _guess_naics(types)
    rating_count = p.get("userRatingCount")

    opportunities = []
    if naics_guess:
        try:
            wardog_result = await search_opportunities(naics=naics_guess, state="", keyword="", limit=5)
            opportunities = wardog_result.get("opportunities", [])
        except HTTPException:
            # SAM.gov not configured or unreachable — lookup result still
            # stands without the "here's what's out there" preview.
            opportunities = []

    result = {
        "found": True,
        "name": p.get("displayName", {}).get("text"),
        "address": p.get("formattedAddress"),
        "website": p.get("websiteUri"),
        "phone": p.get("nationalPhoneNumber"),
        "types": types,
        "rating": p.get("rating"),
        "rating_count": rating_count,
        "business_status": p.get("businessStatus"),
        "naics_guess": naics_guess,
        "suggested_plan": _suggest_plan(rating_count),
        "matching_opportunities": opportunities[:5],
    }
    await cache_set(cache_key, result, ex=3600)
    return result
