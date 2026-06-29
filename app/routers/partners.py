"""FASS Team Up — the public "looking for partners" board.

A business with a flagged opportunity it can't pursue alone (missing past
performance, capacity, a needed certification, etc.) posts what it brings and
what it needs. Any other FASS Flow user can browse the board and start a chat
with the poster (see chat.py) to work out a teaming arrangement. Posts can
originate two ways: standalone from the Team Up page, or pre-filled from a
specific Pipeline record via the "Find a Partner" button (proposal_id links
back to that proposal so readers see real context, not just a vague ask).

Uses the service-role client (get_supabase) for every write — partner_posts'
RLS already restricts writes to the post's own author, but the backend still
enforces user_id == the authenticated caller at the application layer the
same way every other router here does (no separate auth dependency exists
yet in this codebase), so a client can't post as someone else.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/partners", tags=["partners"])


class CreatePostRequest(BaseModel):
    user_id: str
    title: str
    what_i_bring: str
    what_i_need: str
    naics_code: str | None = None
    proposal_id: str | None = None


@router.post("/posts")
async def create_post(body: CreatePostRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only post as yourself")
    if not body.title.strip() or not body.what_i_bring.strip() or not body.what_i_need.strip():
        raise HTTPException(status_code=400, detail="title, what_i_bring, and what_i_need are required")

    sb = get_supabase()
    row = {
        "user_id": body.user_id,
        "title": body.title.strip(),
        "what_i_bring": body.what_i_bring.strip(),
        "what_i_need": body.what_i_need.strip(),
        "naics_code": body.naics_code,
        "proposal_id": body.proposal_id,
    }
    created = single_data(sb.table("partner_posts").insert(row).execute())
    return created


@router.get("/posts")
async def list_posts(status: str = "open", limit: int = 50):
    """The board feed — open posts first (default), newest first. Joins the
    author's display name/business name and, when present, the linked
    proposal's title/agency so a card never reads as anonymous or context-free."""
    sb = get_supabase()
    query = (
        sb.table("partner_posts")
        .select("*, profiles(full_name), proposals(title, agency)")
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status != "all":
        query = query.eq("status", status)
    rows = query.execute().data or []
    return {"posts": rows}


@router.get("/posts/mine")
async def my_posts(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own posts")
    sb = get_supabase()
    rows = (
        sb.table("partner_posts")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"posts": rows}


class ClosePostRequest(BaseModel):
    user_id: str


@router.post("/posts/{post_id}/close")
async def close_post(post_id: str, body: ClosePostRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only close your own posts")
    sb = get_supabase()
    post = single_data(sb.table("partner_posts").select("user_id").eq("id", post_id).maybe_single().execute())
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post["user_id"] != body.user_id:
        raise HTTPException(status_code=403, detail="Only the post's author can close it")
    sb.table("partner_posts").update({"status": "closed"}).eq("id", post_id).execute()
    return {"status": "closed"}
