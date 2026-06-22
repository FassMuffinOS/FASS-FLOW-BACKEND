"""WARDOG — live SAM.gov opportunity search, proxied server-side.

The SAM.gov API key must never reach the browser. Vite bundles any
VITE_-prefixed env var straight into the public JS, so calling
api.sam.gov directly from the frontend with the real key would expose
it to anyone who opens dev tools. This router holds the key in the
backend's environment only and forwards a narrowed set of query
params, caching results briefly to stay polite to SAM.gov's rate limit.
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query
import httpx
from app.config import settings
from app.cache import cache_get, cache_set

router = APIRouter(prefix="/wardog", tags=["wardog"])

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"


@router.get("/search")
async def search_opportunities(
    naics: str = Query(..., description="NAICS code, e.g. 561720"),
    state: str = Query("", description="Two-letter state code, blank = all"),
    keyword: str = Query("", description="Free-text keyword"),
    limit: int = Query(50, le=100),
):
    if not settings.sam_gov_api_key:
        # No key configured yet — let the frontend fall back to its own demo data.
        raise HTTPException(status_code=503, detail="SAM.gov integration not configured")

    cache_key = f"wardog:{naics}:{state}:{keyword}:{limit}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    # SAM.gov requires BOTH postedFrom and postedTo — omitting either causes
    # a 400 from their API (which we were turning into an opaque 502 here).
    today = datetime.utcnow()
    posted_from = (today - timedelta(days=30)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")

    params = {
        "api_key": settings.sam_gov_api_key,
        "limit": str(limit),
        "postedFrom": posted_from,
        "postedTo": posted_to,
        # SAM.gov's actual NAICS query param is "ncode", not "naics".
        "ncode": naics,
        "active": "true",
    }
    if state:
        params["state"] = state
    if keyword:
        params["q"] = keyword

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(SAM_SEARCH_URL, params=params)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach SAM.gov: {e}") from e

    if resp.status_code != 200:
        # Surface SAM.gov's actual error body instead of a bare status code —
        # their API returns a useful message (e.g. "PostedFrom and PostedTo
        # are mandatory", "An invalid api_key was supplied") that otherwise
        # gets lost behind a generic 502 in the frontend's error banner.
        try:
            sam_detail = resp.json()
        except ValueError:
            sam_detail = resp.text
        raise HTTPException(
            status_code=502,
            detail=f"SAM.gov returned {resp.status_code}: {sam_detail}",
        )

    data = resp.json()
    opportunities = data.get("opportunitiesData", [])

    result = {"opportunities": opportunities, "total": data.get("totalRecords", len(opportunities))}
    await cache_set(cache_key, result, ex=180)  # 3 min — SAM.gov data doesn't change that fast
    return result
