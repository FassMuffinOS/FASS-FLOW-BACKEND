"""WARDOG — live SAM.gov opportunity search, proxied server-side.

The SAM.gov API key must never reach the browser. Vite bundles any
VITE_-prefixed env var straight into the public JS, so calling
api.sam.gov directly from the frontend with the real key would expose
it to anyone who opens dev tools. This router holds the key in the
backend's environment only and forwards a narrowed set of query
params, caching results briefly to stay polite to SAM.gov's rate limit.
"""
import io
import re
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import httpx
from pypdf import PdfReader
from app.config import settings
from app.cache import cache_get, cache_set

router = APIRouter(prefix="/wardog", tags=["wardog"])

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"


# SAM.gov's documented per-request cap on "limit" is 1000 (their API
# rejects anything higher). We were capping the frontend at 100 — that's
# us, not SAM.gov, artificially shrinking every search down to a tenth of
# what's actually available.
SAM_MAX_LIMIT = 1000

# SAM.gov rejects any postedFrom/postedTo span of 1 year or more — their API
# error is literally "Date range must be null year(s) apart" when the span
# hits exactly 365 days, so the real ceiling is 364, not 365. We default to
# the full 364 days (instead of the old hardcoded 30) so a single search
# surfaces everything postable, while still letting callers request a
# narrower window.
SAM_MAX_DATE_SPAN_DAYS = 364
DEFAULT_DATE_SPAN_DAYS = 364


@router.get("/search")
async def search_opportunities(
    naics: str = Query(..., description="NAICS code, e.g. 561720"),
    state: str = Query("", description="Two-letter state code, blank = all"),
    keyword: str = Query("", description="Free-text keyword"),
    limit: int = Query(100, le=SAM_MAX_LIMIT, description="Results per page, SAM.gov max 1000"),
    offset: int = Query(0, ge=0, description="Paging offset forwarded to SAM.gov"),
    days_back: int = Query(
        DEFAULT_DATE_SPAN_DAYS,
        ge=1,
        le=SAM_MAX_DATE_SPAN_DAYS,
        description="How many days back to search (SAM.gov caps date range at 365 days)",
    ),
):
    if not settings.sam_gov_api_key:
        # No key configured yet — let the frontend fall back to its own demo data.
        raise HTTPException(status_code=503, detail="SAM.gov integration not configured")

    cache_key = f"wardog:{naics}:{state}:{keyword}:{limit}:{offset}:{days_back}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    # SAM.gov requires BOTH postedFrom and postedTo — omitting either causes
    # a 400 from their API (which we were turning into an opaque 502 here).
    today = datetime.utcnow()
    posted_from = (today - timedelta(days=days_back)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")

    params = {
        "api_key": settings.sam_gov_api_key,
        "limit": str(limit),
        "offset": str(offset),
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
    total = data.get("totalRecords", len(opportunities))

    result = {
        "opportunities": opportunities,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(opportunities) < total,
    }
    await cache_set(cache_key, result, ex=180)  # 3 min — SAM.gov data doesn't change that fast
    return result


def _strip_html(text: str) -> str:
    """SAM.gov's noticedesc endpoint returns lightly-HTML-formatted text
    (mostly <p>/<br>/<li>). We just want plain text for the AI synthesis
    prompt, not a rich-text renderer, so a regex strip is enough."""
    text = re.sub(r"<(br|/p|/li|/div)\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_pdf_text(content: bytes, max_pages: int = 40) -> str:
    """Best-effort text extraction. Scanned/image-only PDFs will yield
    nothing (no OCR here) — that's an acceptable gap for v1, the resolver
    just falls back to whatever text it already has."""
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception:
        return ""
    pages = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(p for p in pages if p.strip())


MAX_ATTACHMENTS = 8  # cap how many resourceLinks we fetch per solicitation
MAX_SOLICITATION_CHARS = 60000  # generous cap; /ai/read-synthesis truncates further to 12000


class ResolveSolicitationRequest(BaseModel):
    description: str | None = None
    resource_links: list[str] = []


@router.post("/resolve-solicitation")
async def resolve_solicitation(body: ResolveSolicitationRequest):
    """Turns the "pulse" SAM.gov gives the search results list (a short
    blurb, or worse, a URL to fetch separately) into the actual solicitation
    text WARDOG -> Read should be grounded in: the real noticedesc body plus
    extracted text from any attached PDFs (the SOW/PWS/instructions usually
    live there, not in the notice description).

    Called from Wardog.jsx's saveInterest() right before the proposal row is
    written, so proposals.description ends up with real substance instead of
    a one-line teaser. The SAM.gov api_key stays server-side here exactly
    like /wardog/search above.
    """
    if not settings.sam_gov_api_key:
        raise HTTPException(status_code=503, detail="SAM.gov integration not configured")

    parts: list[str] = []

    # 1. Resolve the description — sometimes literal text, sometimes a URL
    # to SAM.gov's separate noticedesc endpoint.
    desc = (body.description or "").strip()
    if desc.lower().startswith("http"):
        try:
            sep = "&" if "?" in desc else "?"
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{desc}{sep}api_key={settings.sam_gov_api_key}")
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                    text = payload.get("description") or payload.get("body") or ""
                except ValueError:
                    text = resp.text
                if text:
                    parts.append(_strip_html(text))
        except httpx.HTTPError:
            pass  # description resolution is best-effort — fall through to attachments
    elif desc:
        parts.append(desc)

    # 2. Download + extract text from attached PDFs.
    attachments_parsed = 0
    for link in (body.resource_links or [])[:MAX_ATTACHMENTS]:
        if not link:
            continue
        try:
            sep = "&" if "?" in link else "?"
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.get(f"{link}{sep}api_key={settings.sam_gov_api_key}")
            if resp.status_code != 200:
                continue
            content_type = resp.headers.get("content-type", "").lower()
            looks_like_pdf = "pdf" in content_type or link.lower().endswith(".pdf")
            if not looks_like_pdf:
                continue  # docx/xlsx attachments skipped in this pass — PDF covers the common case
            text = _extract_pdf_text(resp.content)
            if text:
                parts.append(text)
                attachments_parsed += 1
        except httpx.HTTPError:
            continue

    full_text = "\n\n---\n\n".join(p for p in parts if p.strip())[:MAX_SOLICITATION_CHARS]
    return {
        "solicitation_text": full_text,
        "attachments_parsed": attachments_parsed,
        "resolved": bool(full_text),
    }
