"""Reuse Engine — the company "gold standard" content library.

The single biggest time sink in govcon proposal work is rewriting the same
Quality Control / Management Approach / Past Performance narratives on every
bid. This stores those once, per company, and surfaces them back inside the
Proposal Editor: as the contractor works a section, their best pre-approved
block for that kind of section is one click from being dropped in.

A block is created two ways (mirrors feed.py's manual/auto split):
  - 'manual'   — the user explicitly saves a section to their library.
  - 'captured' — offered automatically when they approve a section in the
                 editor (the silent-capture hook), so the library fills
                 itself from real work instead of staying empty.

Owner-scoped private data: every endpoint requires the caller to be logged
in as the user_id it acts on (require_owner), same model as feed.py and the
2026-06-29 security pass. Uses the service-role client like the rest of the
app; ownership is enforced here, not via RLS.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/reuse", tags=["reuse"])

VALID_CATEGORIES = {
    "technical_approach", "management", "past_performance", "quality_control",
    "staffing", "price", "safety", "transition", "other",
}

_STOP = {"the", "and", "for", "plan", "approach", "volume", "proposal",
         "references", "reference", "statement", "cost", "price", "section"}


def _keywords(text: str) -> set[str]:
    out = set()
    for w in (text or "").lower().replace("/", " ").split():
        w = "".join(c for c in w if c.isalnum())
        if len(w) > 3 and w not in _STOP:
            out.add(w)
    return out


@router.get("")
async def list_blocks(user_id: str, category: str | None = None,
                       current_user: CurrentUser = Depends(get_current_user)):
    """The user's whole library, best-used first. Optional category filter.
    Backs the editor's Library rail."""
    require_owner(current_user, user_id, detail="You can only view your own library")
    sb = get_supabase()
    q = sb.table("reuse_blocks").select("*").eq("user_id", user_id)
    if category:
        q = q.eq("category", category)
    blocks = (
        q.order("use_count", desc=True).order("created_at", desc=True).execute().data or []
    )
    return {"blocks": blocks}


@router.get("/suggest")
async def suggest_blocks(user_id: str, section: str, limit: int = 5,
                          current_user: CurrentUser = Depends(get_current_user)):
    """Rank the user's saved blocks against the section they're writing
    (e.g. "Volume II — Management Approach"), by keyword overlap across the
    block's title/category/body. Deterministic v1 — the same shape an
    embedding-based retriever would return later, so the editor wiring
    doesn't change when that upgrade lands."""
    require_owner(current_user, user_id, detail="You can only search your own library")
    want = _keywords(section)
    sb = get_supabase()
    blocks = sb.table("reuse_blocks").select("*").eq("user_id", user_id).execute().data or []

    scored = []
    for b in blocks:
        hay = _keywords(f"{b.get('title','')} {b.get('category','')} {b.get('body','')[:300]}")
        overlap = len(want & hay)
        # A category that literally appears in the section name is a strong signal.
        cat_hit = 1 if b.get("category") and b["category"].replace("_", " ") in section.lower() else 0
        score = overlap + cat_hit * 3 + min(b.get("use_count", 0), 3) * 0.5
        if score > 0:
            scored.append((score, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {"blocks": [b for _, b in scored[:limit]]}


class CreateBlockRequest(BaseModel):
    user_id: str
    title: str
    body: str
    category: str | None = None
    source: str = "manual"


@router.post("/blocks")
async def create_block(req: CreateBlockRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, req.user_id, detail="You can only add to your own library")
    if not req.title.strip() or not req.body.strip():
        raise HTTPException(status_code=400, detail="title and body are required")
    if req.source not in ("manual", "captured"):
        raise HTTPException(status_code=400, detail="source must be 'manual' or 'captured'")
    category = req.category if req.category in VALID_CATEGORIES else "other"
    sb = get_supabase()
    row = {
        "user_id": req.user_id,
        "title": req.title.strip()[:160],
        "body": req.body.strip(),
        "category": category,
        "source": req.source,
    }
    return single_data(sb.table("reuse_blocks").insert(row).execute())


class UpdateBlockRequest(BaseModel):
    user_id: str
    title: str | None = None
    body: str | None = None
    category: str | None = None


@router.patch("/blocks/{block_id}")
async def update_block(block_id: str, req: UpdateBlockRequest,
                        current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, req.user_id, detail="You can only edit your own library")
    sb = get_supabase()
    existing = single_data(
        sb.table("reuse_blocks").select("user_id").eq("id", block_id).maybe_single().execute()
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Block not found")
    if existing["user_id"] != req.user_id:
        raise HTTPException(status_code=403, detail="Not your block")
    patch = {}
    if req.title is not None:
        patch["title"] = req.title.strip()[:160]
    if req.body is not None:
        patch["body"] = req.body.strip()
    if req.category is not None:
        patch["category"] = req.category if req.category in VALID_CATEGORIES else "other"
    if not patch:
        raise HTTPException(status_code=400, detail="Nothing to update")
    return single_data(sb.table("reuse_blocks").update(patch).eq("id", block_id).execute())


@router.post("/blocks/{block_id}/used")
async def mark_used(block_id: str, user_id: str,
                     current_user: CurrentUser = Depends(get_current_user)):
    """Bump use_count when a block is inserted into a proposal, so the
    most-reused (proven) content floats to the top of suggestions."""
    require_owner(current_user, user_id, detail="You can only update your own library")
    sb = get_supabase()
    block = single_data(
        sb.table("reuse_blocks").select("use_count, user_id").eq("id", block_id).maybe_single().execute()
    )
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    if block["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your block")
    new_count = (block.get("use_count") or 0) + 1
    sb.table("reuse_blocks").update({"use_count": new_count}).eq("id", block_id).execute()
    return {"use_count": new_count}


@router.delete("/blocks/{block_id}")
async def delete_block(block_id: str, user_id: str,
                        current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only delete from your own library")
    sb = get_supabase()
    block = single_data(
        sb.table("reuse_blocks").select("user_id").eq("id", block_id).maybe_single().execute()
    )
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    if block["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your block")
    sb.table("reuse_blocks").delete().eq("id", block_id).execute()
    return {"status": "deleted"}
