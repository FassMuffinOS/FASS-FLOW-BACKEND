"""Minimal chat — the negotiation layer behind Team Up's partner board (and
reusable anywhere else a 1:1-or-small-group conversation is needed later).

Threads are participant-based (chat_thread_participants), not a fixed
user_a/user_b pair, so a thread can grow beyond two people later (e.g. a
prime pulling in two subs on the same opportunity) without a schema change —
but the only entry point right now (start_thread) always creates exactly two
participants: the caller and a partner post's author.

No websockets/real-time push here — the frontend polls GET /threads/{id}/
messages on an interval, the same "good enough, ship it" choice this codebase
already makes elsewhere (e.g. AlertsBell's polling) rather than standing up a
new realtime channel for a first version.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase, single_data

router = APIRouter(prefix="/chat", tags=["chat"])


class StartThreadRequest(BaseModel):
    user_id: str        # the caller, i.e. whoever clicked "Message"
    other_user_id: str  # the partner post's author
    post_id: str | None = None


@router.post("/threads/start")
async def start_thread(body: StartThreadRequest):
    if body.user_id == body.other_user_id:
        raise HTTPException(status_code=400, detail="Cannot start a thread with yourself")

    sb = get_supabase()

    # Reuse an existing thread for this same post + pair of people instead of
    # spawning duplicates every time someone re-opens a conversation.
    if body.post_id:
        existing = (
            sb.table("chat_threads")
            .select("id, chat_thread_participants(user_id)")
            .eq("post_id", body.post_id)
            .execute()
            .data
            or []
        )
        for t in existing:
            participant_ids = {p["user_id"] for p in (t.get("chat_thread_participants") or [])}
            if participant_ids == {body.user_id, body.other_user_id}:
                return {"thread_id": t["id"]}

    thread = single_data(sb.table("chat_threads").insert({"post_id": body.post_id}).execute())
    thread_id = thread["id"]
    sb.table("chat_thread_participants").insert([
        {"thread_id": thread_id, "user_id": body.user_id},
        {"thread_id": thread_id, "user_id": body.other_user_id},
    ]).execute()
    return {"thread_id": thread_id}


@router.get("/threads/mine")
async def my_threads(user_id: str):
    """Inbox list: every thread the user's in, with the other participant(s),
    the linked post (if any), and the most recent message for a preview line."""
    sb = get_supabase()
    my_rows = (
        sb.table("chat_thread_participants")
        .select("thread_id")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    thread_ids = [r["thread_id"] for r in my_rows]
    if not thread_ids:
        return {"threads": []}

    threads = (
        sb.table("chat_threads")
        .select("*, partner_posts(title), chat_thread_participants(user_id, profiles(full_name))")
        .in_("id", thread_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )

    last_messages = (
        sb.table("chat_messages")
        .select("thread_id, body, sender_id, created_at")
        .in_("thread_id", thread_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    last_by_thread = {}
    for m in last_messages:
        last_by_thread.setdefault(m["thread_id"], m)  # first hit per thread = newest, since already sorted desc

    out = []
    for t in threads:
        others = [
            p for p in (t.get("chat_thread_participants") or [])
            if p["user_id"] != user_id
        ]
        out.append({
            "id": t["id"],
            "post_title": (t.get("partner_posts") or {}).get("title") if t.get("partner_posts") else None,
            "other_participants": [
                {"user_id": p["user_id"], "full_name": (p.get("profiles") or {}).get("full_name")}
                for p in others
            ],
            "last_message": last_by_thread.get(t["id"]),
        })
    return {"threads": out}


@router.get("/threads/{thread_id}/messages")
async def get_messages(thread_id: str, user_id: str):
    sb = get_supabase()
    _assert_participant(sb, thread_id, user_id)
    rows = (
        sb.table("chat_messages")
        .select("*")
        .eq("thread_id", thread_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"messages": rows}


class SendMessageRequest(BaseModel):
    user_id: str
    body: str


@router.post("/threads/{thread_id}/messages")
async def send_message(thread_id: str, req: SendMessageRequest):
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    sb = get_supabase()
    _assert_participant(sb, thread_id, req.user_id)
    row = {
        "thread_id": thread_id,
        "sender_id": req.user_id,
        "body": req.body.strip(),
        "read_by": [req.user_id],
    }
    created = single_data(sb.table("chat_messages").insert(row).execute())
    return created


class MarkReadRequest(BaseModel):
    user_id: str


@router.post("/threads/{thread_id}/read")
async def mark_read(thread_id: str, body: MarkReadRequest):
    """Appends user_id to read_by on every message in the thread that doesn't
    already have it — simple unread-count support without a separate
    per-user read-cursor table."""
    sb = get_supabase()
    _assert_participant(sb, thread_id, body.user_id)
    rows = (
        sb.table("chat_messages")
        .select("id, read_by")
        .eq("thread_id", thread_id)
        .execute()
        .data
        or []
    )
    for r in rows:
        read_by = r.get("read_by") or []
        if body.user_id not in read_by:
            sb.table("chat_messages").update({"read_by": read_by + [body.user_id]}).eq("id", r["id"]).execute()
    return {"status": "ok"}


def _assert_participant(sb, thread_id: str, user_id: str) -> None:
    row = single_data(
        sb.table("chat_thread_participants")
        .select("user_id")
        .eq("thread_id", thread_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not row:
        raise HTTPException(status_code=403, detail="Not a participant in this thread")
