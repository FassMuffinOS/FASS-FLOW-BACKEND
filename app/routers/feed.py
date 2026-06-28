"""Business feed — the "LinkedIn+Slack" social layer over Business Profiles.

Every member can post an update (manual) or have one generated for them off
a real milestone (auto — see Pipeline.jsx's contract-awarded hook for the
first example). Posts show up on the global /feed page and on the author's
own Profile.jsx. Likes and comments are intentionally simple (no nesting,
no reaction types beyond a single like) — this is a v1 meant to prove the
feed is alive, not a Facebook clone.

Like Team Up's partner_posts, this uses the service-role client for every
read/write rather than letting clients query business_posts directly, since
the feed fans out across every user's rows.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase, single_data

router = APIRouter(prefix="/feed", tags=["feed"])


def _attach_engagement(sb, posts: list[dict], viewer_id: str | None) -> list[dict]:
    """Adds like_count, comment_count, and liked_by_me to each post in one
    extra round-trip per table (not per post) — same N+1-avoidance shape as
    chat.py's unread_by_thread aggregation."""
    if not posts:
        return posts
    post_ids = [p["id"] for p in posts]

    likes = sb.table("business_post_likes").select("post_id, user_id").in_("post_id", post_ids).execute().data or []
    like_counts: dict[str, int] = {}
    liked_by_me: set[str] = set()
    for like in likes:
        like_counts[like["post_id"]] = like_counts.get(like["post_id"], 0) + 1
        if viewer_id and like["user_id"] == viewer_id:
            liked_by_me.add(like["post_id"])

    comments = sb.table("business_post_comments").select("post_id").in_("post_id", post_ids).execute().data or []
    comment_counts: dict[str, int] = {}
    for c in comments:
        comment_counts[c["post_id"]] = comment_counts.get(c["post_id"], 0) + 1

    for p in posts:
        p["like_count"] = like_counts.get(p["id"], 0)
        p["comment_count"] = comment_counts.get(p["id"], 0)
        p["liked_by_me"] = p["id"] in liked_by_me
    return posts


@router.get("")
async def list_feed(viewer_id: str | None = None, limit: int = 50):
    """The global feed — every member's posts, newest first. `viewer_id` is
    optional (used only to compute liked_by_me); the feed itself is visible
    to anyone signed in, same visibility model as Team Up's board."""
    sb = get_supabase()
    posts = (
        sb.table("business_posts")
        .select("*, profiles(full_name, company_name)")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return {"posts": _attach_engagement(sb, posts, viewer_id)}


@router.get("/user/{user_id}")
async def list_user_posts(user_id: str, viewer_id: str | None = None, limit: int = 20):
    """Posts for one business — backs the "recent updates" section on
    Profile.jsx."""
    sb = get_supabase()
    posts = (
        sb.table("business_posts")
        .select("*, profiles(full_name, company_name)")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return {"posts": _attach_engagement(sb, posts, viewer_id)}


class CreatePostRequest(BaseModel):
    user_id: str
    body: str
    source: str = "manual"
    category: str | None = None


@router.post("/posts")
async def create_post(req: CreatePostRequest):
    """Used both by the Feed composer (source='manual') and by server-side
    milestone hooks (source='auto', e.g. Pipeline.jsx calling this right
    after marking a record 'awarded'). Trusts user_id from the request body
    the same way every other router here does — no separate auth dependency
    exists yet in this codebase (see partners.py's docstring)."""
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    if req.source not in ("manual", "auto"):
        raise HTTPException(status_code=400, detail="source must be 'manual' or 'auto'")

    sb = get_supabase()
    row = {
        "user_id": req.user_id,
        "body": req.body.strip(),
        "source": req.source,
        "category": req.category,
    }
    created = single_data(sb.table("business_posts").insert(row).execute())
    return created


@router.delete("/posts/{post_id}")
async def delete_post(post_id: str, user_id: str):
    sb = get_supabase()
    post = single_data(sb.table("business_posts").select("user_id").eq("id", post_id).maybe_single().execute())
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the post's author can delete it")
    sb.table("business_posts").delete().eq("id", post_id).execute()
    return {"status": "deleted"}


class LikeRequest(BaseModel):
    user_id: str


@router.post("/posts/{post_id}/like")
async def toggle_like(post_id: str, req: LikeRequest):
    """Toggle, not set — matches the heart-button UX (tap to like, tap again
    to unlike) rather than separate like/unlike endpoints."""
    sb = get_supabase()
    existing = (
        sb.table("business_post_likes")
        .select("post_id")
        .eq("post_id", post_id)
        .eq("user_id", req.user_id)
        .maybe_single()
        .execute()
    )
    if existing and existing.data:
        sb.table("business_post_likes").delete().eq("post_id", post_id).eq("user_id", req.user_id).execute()
        return {"liked": False}
    sb.table("business_post_likes").insert({"post_id": post_id, "user_id": req.user_id}).execute()
    return {"liked": True}


@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str):
    sb = get_supabase()
    comments = (
        sb.table("business_post_comments")
        .select("*, profiles(full_name, company_name)")
        .eq("post_id", post_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"comments": comments}


class CreateCommentRequest(BaseModel):
    user_id: str
    body: str


@router.post("/posts/{post_id}/comments")
async def create_comment(post_id: str, req: CreateCommentRequest):
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    sb = get_supabase()
    row = {"post_id": post_id, "user_id": req.user_id, "body": req.body.strip()}
    created = single_data(sb.table("business_post_comments").insert(row).execute())
    return created
