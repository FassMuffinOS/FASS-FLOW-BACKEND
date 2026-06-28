"""Messenger — platform-wide 1:1 (extensible to small-group) chat. Started
life as the negotiation layer behind Team Up's partner board; now also
reachable directly via people search (GET /people/search + POST
/threads/start with no post_id) so any user can find and message any other
user, not just someone who posted to the board.

Threads are participant-based (chat_thread_participants), not a fixed
user_a/user_b pair, so a thread can grow beyond two people (e.g. a prime
pulling in two subs on the same opportunity) without a schema change.
start_thread accepts either a single other_user_id (1:1, unchanged) or
other_user_ids (group/teaming threads, 3+ participants total once the caller
is counted) plus an optional title for the group's display name. Existing
threads with that exact participant set are reused either way. Participants
can be added to an already-created thread via POST /threads/{id}/participants
(e.g. inviting one more sub into an existing teaming conversation).

Delivery is push-based: chat_messages is in the supabase_realtime publication
(see migrations/messenger_realtime.sql) and the frontend subscribes via
supabase.channel(...).on('postgres_changes', ...) instead of polling. The
REST endpoints below remain the source of truth for initial loads, sending,
and read receipts — realtime only pushes the "something changed, refetch /
append" signal.
"""
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase, single_data
from app.services.llm import LLMUnavailableError, llm_router
from app.web_push import send_push_to_user

logger = logging.getLogger("fass_flow.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

ATTACHMENT_BUCKET = "chat-attachments"


def _pinned_contacts(user_id: str) -> list[dict]:
    """Admin + AI Assistant are always reachable — pinned at the top of every
    search result regardless of query text or how many real profiles exist.
    This matters most during cold start, when a fresh account's first "New
    message" search would otherwise hit an empty profiles table and dead-end
    on "No one found." Configured via env (admin_user_id/ai_assistant_user_id)
    rather than hardcoded so the underlying account can change without a
    deploy. Each id must be a real auth.users row (profiles.id is FK'd to
    auth.users) — see migrations/messenger_pinned_contacts.sql."""
    pinned = []
    if settings.admin_user_id and settings.admin_user_id != user_id:
        pinned.append({
            "id": settings.admin_user_id,
            "full_name": "Admin",
            "company_name": "FASS Flow Support",
        })
    if settings.ai_assistant_user_id and settings.ai_assistant_user_id != user_id:
        pinned.append({
            "id": settings.ai_assistant_user_id,
            "full_name": "AI Assistant",
            "company_name": "FASS Flow AI",
        })
    return pinned


@router.get("/people/search")
async def search_people(
    user_id: str,
    q: str = "",
    naics: str = "",
    certifications: str = "",
    min_contracts_won: int = 0,
):
    """Platform-wide people finder for starting a new DM — deliberately not
    scoped to existing connections (Team Up posts, recruits, etc.) per the
    "find and engage with one another" ask. Matches on full_name or
    company_name; empty q returns a default browseable page rather than
    nothing, so the "New message" picker isn't a dead end before you type.
    Admin + AI Assistant are always prepended on top, unfiltered by q, so
    they're reachable even from a totally cold/empty profiles table.

    Capability filters (naics/certifications/min_contracts_won) narrow that
    name search rather than replace it — this is the "capability-based
    people search" ask: find subs/teammates by what they can actually do
    (NAICS code, self-declared set-aside certs on business_profiles) or have
    actually done (contracts won, via proposals.status='awarded'), not just
    by name. All three are optional and additive (AND, not OR)."""
    sb = get_supabase()
    has_capability_filter = bool(naics.strip() or certifications.strip() or min_contracts_won)

    query = sb.table("profiles").select("id, full_name, company_name").neq("id", user_id)
    q = q.strip()
    if q:
        query = query.or_(f"full_name.ilike.%{q}%,company_name.ilike.%{q}%")
    # Capability filters apply in Python below (certifications is an array
    # column, and contracts-won needs a second-table aggregation, so neither
    # can be pushed into the profiles query above) — pull a wider candidate
    # page when they're active so filtering down doesn't lose real matches
    # that just weren't in the default top-25.
    rows = query.order("full_name").limit(200 if has_capability_filter else 25).execute().data or []

    if has_capability_filter and rows:
        candidate_ids = [r["id"] for r in rows]
        biz_rows = (
            sb.table("business_profiles")
            .select("user_id, naics, certifications")
            .in_("user_id", candidate_ids)
            .execute()
            .data
            or []
        )
        biz_by_user = {b["user_id"]: b for b in biz_rows}

        naics_q = naics.strip().lower()
        cert_list = [c.strip().lower() for c in certifications.split(",") if c.strip()]

        won_counts: dict[str, int] = {}
        if min_contracts_won:
            # One extra query (not per row) + a Python aggregation pass —
            # same N+1-avoidance pattern as chat.py's unread_by_thread /
            # feed.py's _attach_engagement.
            won = (
                sb.table("proposals")
                .select("user_id")
                .in_("user_id", candidate_ids)
                .eq("status", "awarded")
                .execute()
                .data
                or []
            )
            for w in won:
                won_counts[w["user_id"]] = won_counts.get(w["user_id"], 0) + 1

        def matches(r):
            b = biz_by_user.get(r["id"])
            if naics_q and not (b and naics_q in (b.get("naics") or "").lower()):
                return False
            if cert_list:
                row_certs = [c.lower() for c in (b.get("certifications") or [])] if b else []
                if not any(c in row_certs for c in cert_list):
                    return False
            if min_contracts_won and won_counts.get(r["id"], 0) < min_contracts_won:
                return False
            return True

        rows = [r for r in rows if matches(r)]
        for r in rows:
            b = biz_by_user.get(r["id"]) or {}
            r["naics"] = b.get("naics")
            r["certifications"] = b.get("certifications") or []
            r["contracts_won"] = won_counts.get(r["id"], 0)

    pinned = _pinned_contacts(user_id)
    pinned_ids = {p["id"] for p in pinned}
    rows = [r for r in rows if r["id"] not in pinned_ids]
    return {"people": pinned + rows}


@router.get("/profile/{other_user_id}")
async def get_chat_profile(other_user_id: str):
    """Verified profile panel data for a thread's other participant — same
    underlying signal as the public /c/{slug} capability page (a published
    FASS Wallet card = "Verified via FASS Wallet"), just looked up by user_id
    instead of slug since that's all the thread/message rows carry. Returns
    has_card=False (not a 404) when the person never claimed a card, so the
    frontend can render an empty-state panel instead of treating it as an
    error — most people in a govcon thread will have one, but not all."""
    sb = get_supabase()
    profile = single_data(
        sb.table("profiles").select("full_name, company_name").eq("id", other_user_id).maybe_single().execute()
    )
    card = single_data(
        sb.table("wallet_passes")
        .select("slug, business_name, address, naics, website, phone, purchased")
        .eq("user_id", other_user_id)
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    return {
        "full_name": profile.get("full_name") if profile else None,
        "company_name": profile.get("company_name") if profile else None,
        "has_card": bool(card),
        "card": card,
    }


class StartThreadRequest(BaseModel):
    user_id: str                       # the caller, i.e. whoever clicked "Message"
    other_user_id: str | None = None   # 1:1 case — a partner post's author, or a people-search result
    other_user_ids: list[str] = []     # group/teaming case — 2+ other people; combined with other_user_id if both are sent
    post_id: str | None = None
    title: str | None = None           # optional group display name; ignored for 1:1 threads


@router.post("/threads/start")
async def start_thread(body: StartThreadRequest):
    # Normalize to one recipient set regardless of which field(s) the caller
    # used, so a single endpoint covers both the 1:1 DM picker and the
    # "start a group" multi-select flow.
    recipient_ids = {*body.other_user_ids}
    if body.other_user_id:
        recipient_ids.add(body.other_user_id)
    recipient_ids.discard(body.user_id)
    if not recipient_ids:
        raise HTTPException(status_code=400, detail="Cannot start a thread with yourself")

    sb = get_supabase()
    participant_ids = {body.user_id, *recipient_ids}
    is_group = len(participant_ids) > 2
    title = body.title.strip() if (is_group and body.title and body.title.strip()) else None

    # Reuse any existing thread between this exact set of people instead of
    # spawning duplicates — regardless of whether this start came from the
    # same board post, a different post, or a fresh people-search DM/group
    # pick. If more than one already exists (e.g. one tied to a post, one
    # freeform), prefer the one matching this request's post_id so board-post
    # conversations stay attached to their post.
    my_thread_ids = {
        r["thread_id"]
        for r in (
            sb.table("chat_thread_participants")
            .select("thread_id")
            .eq("user_id", body.user_id)
            .execute()
            .data
            or []
        )
    }
    if my_thread_ids:
        candidates = (
            sb.table("chat_threads")
            .select("id, post_id, chat_thread_participants(user_id)")
            .in_("id", list(my_thread_ids))
            .execute()
            .data
            or []
        )
        matches = [
            t for t in candidates
            if {p["user_id"] for p in (t.get("chat_thread_participants") or [])} == participant_ids
        ]
        if matches:
            exact = next((t for t in matches if t["post_id"] == body.post_id), None)
            return {"thread_id": (exact or matches[0])["id"]}

    try:
        thread_rows = sb.table("chat_threads").insert({"post_id": body.post_id, "title": title}).execute().data
        thread_id = thread_rows[0]["id"]
        sb.table("chat_thread_participants").insert([
            {"thread_id": thread_id, "user_id": uid} for uid in participant_ids
        ]).execute()
    except Exception as e:
        # Most common cause: one of the recipient ids doesn't correspond to a
        # real auth.users row (e.g. a pinned contact's env-var id is unset,
        # typo'd, or its one-time profiles/auth provisioning was skipped) —
        # the FK on chat_thread_participants.user_id rejects the insert.
        # Surfacing the real detail here (instead of letting it 500 opaquely)
        # is what makes that diagnosable from the frontend network tab.
        raise HTTPException(status_code=400, detail=f"Could not start thread: {e}") from e
    return {"thread_id": thread_id}


class AddParticipantRequest(BaseModel):
    user_id: str      # caller — must already be a participant
    new_user_id: str


@router.post("/threads/{thread_id}/participants")
async def add_participant(thread_id: str, body: AddParticipantRequest):
    """Invite one more person into an existing thread — what turns a 1:1
    into a group, or grows an existing teaming thread (e.g. a prime pulling
    in a second sub partway through negotiating an opportunity)."""
    sb = get_supabase()
    _assert_participant(sb, thread_id, body.user_id)

    already = single_data(
        sb.table("chat_thread_participants")
        .select("user_id")
        .eq("thread_id", thread_id)
        .eq("user_id", body.new_user_id)
        .maybe_single()
        .execute()
    )
    if already:
        return {"status": "ok"}

    try:
        sb.table("chat_thread_participants").insert({"thread_id": thread_id, "user_id": body.new_user_id}).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not add participant: {e}") from e

    adder = single_data(
        sb.table("profiles").select("full_name").eq("id", body.user_id).maybe_single().execute()
    )
    adder_name = (adder or {}).get("full_name") or "Someone"
    sb.table("chat_messages").insert({
        "thread_id": thread_id,
        "sender_id": body.user_id,
        "body": f"{adder_name} added a new participant to this conversation.",
        "read_by": [body.user_id],
    }).execute()
    return {"status": "ok"}


@router.get("/threads/{thread_id}/participants")
async def list_participants(thread_id: str, user_id: str):
    """Full member list (with names) for a thread — used for the group info
    panel, where a single "other participant" isn't enough once there are
    3+ people in the conversation."""
    sb = get_supabase()
    _assert_participant(sb, thread_id, user_id)
    rows = (
        sb.table("chat_thread_participants")
        .select("user_id, profiles(full_name, company_name)")
        .eq("thread_id", thread_id)
        .execute()
        .data
        or []
    )
    return {
        "participants": [
            {
                "user_id": r["user_id"],
                "full_name": (r.get("profiles") or {}).get("full_name"),
                "company_name": (r.get("profiles") or {}).get("company_name"),
            }
            for r in rows
        ]
    }


@router.get("/threads/mine")
async def my_threads(user_id: str):
    """Inbox list: every thread the user's in, with the other participant(s),
    the linked post (if any), and the most recent message for a preview line.

    Wrapped in a broad try/except purely for visibility: this query embeds
    chat_thread_participants -> profiles, which silently 500s if that FK
    relationship is ever missing/renamed (see partner_network_fk_fix.sql).
    Before this, a failure here meant the sidebar just looked permanently
    empty with zero trace in the logs — same failure mode start_thread had
    before it got the same treatment."""
    sb = get_supabase()
    try:
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

        all_messages = (
            sb.table("chat_messages")
            .select("id, thread_id, body, sender_id, read_by, created_at")
            .in_("thread_id", thread_ids)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        last_by_thread = {}
        unread_by_thread = {}
        for m in all_messages:
            last_by_thread.setdefault(m["thread_id"], m)  # first hit per thread = newest, since already sorted desc
            if m["sender_id"] != user_id and user_id not in (m.get("read_by") or []):
                unread_by_thread[m["thread_id"]] = unread_by_thread.get(m["thread_id"], 0) + 1

        out = []
        for t in threads:
            others = [
                p for p in (t.get("chat_thread_participants") or [])
                if p["user_id"] != user_id
            ]
            last = last_by_thread.get(t["id"])
            out.append({
                "id": t["id"],
                "title": t.get("title"),
                "is_group": len(others) > 1,
                "post_title": (t.get("partner_posts") or {}).get("title") if t.get("partner_posts") else None,
                "other_participants": [
                    {"user_id": p["user_id"], "full_name": (p.get("profiles") or {}).get("full_name")}
                    for p in others
                ],
                "last_message": last,
                "unread_count": unread_by_thread.get(t["id"], 0),
                # Sort key only — not meant for display. Falls back to the
                # thread's own created_at so a brand-new, message-less
                # conversation still slots in by recency.
                "_sort_at": (last or {}).get("created_at") or t["created_at"],
            })
        out.sort(key=lambda r: r["_sort_at"], reverse=True)
        for r in out:
            del r["_sort_at"]
        return {"threads": out}
    except Exception as e:
        logger.exception("my_threads failed for user %s", user_id)
        raise HTTPException(status_code=500, detail=f"Could not load conversations: {e}") from e


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
    _attach_extras(sb, rows)
    return {"messages": rows}


def _attach_extras(sb, rows: list[dict]) -> None:
    """Mutates each message row in place: turns a stored attachment storage
    path into a short-lived signed URL (bucket is private), and adds a
    `reactions` list of {emoji, user_ids} grouped per message. Shared by
    get_messages/send_message/edit/delete/attachment-upload so every
    response shape stays consistent for the frontend."""
    if not rows:
        return
    for r in rows:
        if r.get("attachment_url"):
            try:
                signed = sb.storage.from_(ATTACHMENT_BUCKET).create_signed_url(r["attachment_url"], 3600)
                r["attachment_url"] = signed.get("signedURL") or signed.get("signed_url") or r["attachment_url"]
            except Exception:
                pass  # leave the raw path if signing fails; frontend just won't render it

    message_ids = [r["id"] for r in rows]
    reactions = (
        sb.table("chat_message_reactions")
        .select("message_id, user_id, emoji")
        .in_("message_id", message_ids)
        .execute()
        .data
        or []
    )
    grouped: dict[str, dict[str, list[str]]] = {}
    for rx in reactions:
        by_emoji = grouped.setdefault(rx["message_id"], {})
        by_emoji.setdefault(rx["emoji"], []).append(rx["user_id"])
    for r in rows:
        by_emoji = grouped.get(r["id"], {})
        r["reactions"] = [{"emoji": e, "user_ids": uids} for e, uids in by_emoji.items()]


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
    created = sb.table("chat_messages").insert(row).execute().data[0]
    created["reactions"] = []
    _notify_other_participants(sb, thread_id, req.user_id, req.body.strip())
    await _maybe_ai_reply(sb, thread_id, req.user_id)
    return created


# Tables each shareable object type lives in, plus the columns pulled into
# the message's denormalized snapshot. Keeping this as one lookup table
# means adding a fifth shareable type later (a Pipeline record, a teaming
# agreement, etc.) is a one-line addition here, not a new branch of
# near-identical fetch/insert code.
SHARE_SOURCES: dict[str, dict] = {
    "opportunity": {
        "table": "opportunities",
        "fields": ["title", "agency", "naics_code", "set_aside", "value_estimate", "response_date"],
    },
    "proposal": {
        "table": "proposals",
        "fields": ["title", "status"],
    },
    "partner_post": {
        "table": "partner_posts",
        "fields": ["title", "what_i_bring", "what_i_need", "naics_code"],
    },
    "passport": {
        "table": "wallet_passes",
        "fields": ["business_name", "naics", "website", "phone", "slug"],
        # Wallet.jsx only has the row's slug in hand (not its uuid id) —
        # the slug is unique too, so look up by that instead of forcing
        # the frontend to thread an extra id through just for this one type.
        "lookup_field": "slug",
    },
}


class ShareObjectRequest(BaseModel):
    user_id: str
    object_type: str   # one of SHARE_SOURCES keys, or "opportunity_live"
    object_id: str
    note: str | None = None  # optional caption the sender adds, e.g. "thoughts on this one?"
    snapshot: dict | None = None  # required for opportunity_live; ignored otherwise


@router.post("/threads/{thread_id}/share")
async def share_object(thread_id: str, req: ShareObjectRequest):
    """Drop a real platform object (opportunity, proposal, partner post, or
    Passport capability statement) into a thread as a card instead of a
    plain-text link. The backend reads the source row once here (service-role
    key, so it works even if the recipient couldn't otherwise see it — sharing
    is the access grant) and freezes a snapshot onto the message; see
    messenger_shared_objects.sql for why this is a snapshot, not a live join.

    Special case: "opportunity_live". WARDOG's search results are a live
    SAM.gov API proxy (see wardog.py) — they're never written into
    public.opportunities, so there's no row here to look up by id. SAM.gov
    data is public anyway, so for this one type we trust the client-supplied
    snapshot instead of re-fetching server-side."""
    if req.object_type == "opportunity_live":
        if not req.snapshot:
            raise HTTPException(status_code=400, detail="snapshot is required for opportunity_live")
        snapshot = req.snapshot
        object_type_to_store = "opportunity_live"
        sb = get_supabase()
        _assert_participant(sb, thread_id, req.user_id)
    else:
        source = SHARE_SOURCES.get(req.object_type)
        if not source:
            raise HTTPException(status_code=400, detail=f"Unknown object_type '{req.object_type}'")
        sb = get_supabase()
        _assert_participant(sb, thread_id, req.user_id)

        lookup_field = source.get("lookup_field", "id")
        obj = (
            sb.table(source["table"])
            .select(",".join(["id"] + source["fields"]))
            .eq(lookup_field, req.object_id)
            .maybe_single()
            .execute()
        )
        obj_data = single_data(obj)
        if not obj_data:
            raise HTTPException(status_code=404, detail=f"{req.object_type} not found")
        snapshot = {f: obj_data.get(f) for f in source["fields"]}
        object_type_to_store = req.object_type

    display_title = snapshot.get("title") or snapshot.get("business_name") or req.object_type
    row = {
        "thread_id": thread_id,
        "sender_id": req.user_id,
        "body": (req.note.strip() + "\n" if req.note and req.note.strip() else "") + f"Shared: {display_title}",
        "read_by": [req.user_id],
        "shared_object_type": object_type_to_store,
        "shared_object_id": req.object_id,
        "shared_object_snapshot": snapshot,
    }
    created = sb.table("chat_messages").insert(row).execute().data[0]
    created["reactions"] = []
    _notify_other_participants(sb, thread_id, req.user_id, f"shared {display_title}")
    return created


AI_SYSTEM_PROMPT = (
    "You are the AI Assistant inside FASS Flow, a government-contracting "
    "operations platform for small business contractors. You're replying "
    "directly inside the platform's Messenger, as if you were a teammate the "
    "user can DM any time. Be concise, warm, and conversational — usually a "
    "few sentences, longer only if the question genuinely needs it. You can "
    "discuss government contracting, proposals, or general business "
    "questions. You don't have live access to this user's specific account "
    "data in this chat (no document lookups here) — if they need solicitation "
    "analysis or proposal drafting, point them to FASS FILL or R-E-A-D "
    "elsewhere in the app rather than guessing."
)


async def _maybe_ai_reply(sb, thread_id: str, sender_id: str) -> None:
    """If the AI Assistant is a participant in this thread and isn't the one
    who just sent the message, generate a reply from the recent conversation
    and insert it as an ordinary chat_messages row — from there it flows
    through the exact same Realtime/push pipeline as a human reply, so the
    frontend needs no special-casing. Silently no-ops (never raises) if no
    LLM provider is configured or the call fails, so the human's own send is
    never affected by the AI being unavailable."""
    ai_id = settings.ai_assistant_user_id
    if not ai_id:
        logger.warning("AI reply skipped: AI_ASSISTANT_USER_ID is not configured")
        return
    if sender_id == ai_id:
        return
    try:
        participant_ids = set(_other_participant_ids(sb, thread_id, sender_id)) | {sender_id}
        if ai_id not in participant_ids:
            logger.warning(
                "AI reply skipped: AI (%s) is not a participant in thread %s (participants: %s)",
                ai_id, thread_id, participant_ids,
            )
            return
        history = (
            sb.table("chat_messages")
            .select("sender_id, body, deleted_at")
            .eq("thread_id", thread_id)
            .order("created_at", desc=False)
            .limit(20)
            .execute()
            .data
            or []
        )
        lines = []
        for m in history:
            if m.get("deleted_at") or not m.get("body"):
                continue
            speaker = "Assistant" if m["sender_id"] == ai_id else "User"
            lines.append(f"{speaker}: {m['body']}")
        prompt = "\n".join(lines) + "\nAssistant:"
        result = await llm_router.complete(system=AI_SYSTEM_PROMPT, prompt=prompt, max_tokens=400)
        reply = result.text.strip()
        if not reply:
            return
        sb.table("chat_messages").insert({
            "thread_id": thread_id,
            "sender_id": ai_id,
            "body": reply,
            "read_by": [ai_id],
        }).execute()
        _notify_other_participants(sb, thread_id, ai_id, reply)
    except LLMUnavailableError as e:
        logger.warning("AI reply skipped: no LLM provider available: %s", e)
    except Exception:
        logger.exception("AI reply failed in thread %s", thread_id)


def _other_participant_ids(sb, thread_id: str, exclude_user_id: str) -> list[str]:
    rows = (
        sb.table("chat_thread_participants")
        .select("user_id")
        .eq("thread_id", thread_id)
        .neq("user_id", exclude_user_id)
        .execute()
        .data
        or []
    )
    return [r["user_id"] for r in rows]


def _notify_other_participants(sb, thread_id: str, sender_id: str, preview: str) -> None:
    """Fires a Web Push notification (no-op if VAPID isn't configured) to
    every other participant. The recipient may well have the tab open and
    not need it — Realtime already handles that live-update case instantly —
    but push is what reaches them when the tab/app is closed, so we send
    regardless rather than trying to track "is currently viewing" state."""
    sender = single_data(
        sb.table("profiles").select("full_name").eq("id", sender_id).maybe_single().execute()
    )
    sender_name = (sender or {}).get("full_name") or "Someone"
    for uid in _other_participant_ids(sb, thread_id, sender_id):
        try:
            send_push_to_user(uid, {
                "title": sender_name,
                "body": preview[:120],
                "thread_id": thread_id,
                "url": "/messages",
            })
        except Exception:
            pass  # never let a push failure break message delivery


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


class EditMessageRequest(BaseModel):
    user_id: str
    body: str


@router.patch("/threads/{thread_id}/messages/{message_id}")
async def edit_message(thread_id: str, message_id: str, req: EditMessageRequest):
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    sb = get_supabase()
    _assert_participant(sb, thread_id, req.user_id)
    msg = _assert_sender(sb, thread_id, message_id, req.user_id)
    if msg.get("deleted_at"):
        raise HTTPException(status_code=400, detail="Cannot edit a deleted message")
    updated = (
        sb.table("chat_messages")
        .update({"body": req.body.strip(), "edited_at": "now()"})
        .eq("id", message_id)
        .execute()
        .data[0]
    )
    _attach_extras(sb, [updated])
    return updated


class DeleteMessageRequest(BaseModel):
    user_id: str


@router.delete("/threads/{thread_id}/messages/{message_id}")
async def delete_message(thread_id: str, message_id: str, body: DeleteMessageRequest):
    """Soft-delete: the row stays (so thread ordering / read receipts don't
    shift) but body + any attachment fields are cleared. The frontend
    renders a 'message deleted' placeholder for rows with deleted_at set."""
    sb = get_supabase()
    _assert_participant(sb, thread_id, body.user_id)
    _assert_sender(sb, thread_id, message_id, body.user_id)
    updated = (
        sb.table("chat_messages")
        .update({
            "body": "",
            "attachment_url": None,
            "attachment_name": None,
            "attachment_type": None,
            "deleted_at": "now()",
        })
        .eq("id", message_id)
        .execute()
        .data[0]
    )
    _attach_extras(sb, [updated])
    return updated


def _assert_sender(sb, thread_id: str, message_id: str, user_id: str) -> dict:
    msg = single_data(
        sb.table("chat_messages")
        .select("*")
        .eq("id", message_id)
        .eq("thread_id", thread_id)
        .maybe_single()
        .execute()
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["sender_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the sender can do this")
    return msg


class ReactionRequest(BaseModel):
    user_id: str
    emoji: str


@router.post("/messages/{message_id}/reactions")
async def toggle_reaction(message_id: str, body: ReactionRequest):
    """Toggle semantics: if this user already reacted with this emoji on
    this message, remove it; otherwise add it. One POST covers both react
    and un-react so the frontend doesn't need to track which call to make."""
    sb = get_supabase()
    msg = single_data(
        sb.table("chat_messages").select("thread_id").eq("id", message_id).maybe_single().execute()
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    _assert_participant(sb, msg["thread_id"], body.user_id)

    existing = single_data(
        sb.table("chat_message_reactions")
        .select("id")
        .eq("message_id", message_id)
        .eq("user_id", body.user_id)
        .eq("emoji", body.emoji)
        .maybe_single()
        .execute()
    )
    if existing:
        sb.table("chat_message_reactions").delete().eq("id", existing["id"]).execute()
    else:
        sb.table("chat_message_reactions").insert({
            "message_id": message_id, "user_id": body.user_id, "emoji": body.emoji,
        }).execute()

    rows = (
        sb.table("chat_message_reactions")
        .select("emoji, user_id")
        .eq("message_id", message_id)
        .execute()
        .data
        or []
    )
    grouped: dict[str, list[str]] = {}
    for rx in rows:
        grouped.setdefault(rx["emoji"], []).append(rx["user_id"])
    return {"reactions": [{"emoji": e, "user_ids": uids} for e, uids in grouped.items()]}


@router.post("/threads/{thread_id}/attachments")
async def upload_attachment(thread_id: str, user_id: str = Form(...), file: UploadFile = File(...)):
    """Uploads to the private chat-attachments bucket under
    {thread_id}/{uuid}-{filename}, then creates the chat_messages row in
    the same call (an attachment is just a message with no body text)."""
    sb = get_supabase()
    _assert_participant(sb, thread_id, user_id)

    contents = await file.read()
    max_bytes = 15 * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(status_code=400, detail="File too large (15MB max)")

    safe_name = (file.filename or "file").replace("/", "_")
    path = f"{thread_id}/{uuid.uuid4()}-{safe_name}"
    sb.storage.from_(ATTACHMENT_BUCKET).upload(
        path, contents, {"content-type": file.content_type or "application/octet-stream"}
    )

    row = {
        "thread_id": thread_id,
        "sender_id": user_id,
        "body": "",
        "read_by": [user_id],
        "attachment_url": path,
        "attachment_name": safe_name,
        "attachment_type": file.content_type,
    }
    created = sb.table("chat_messages").insert(row).execute().data[0]
    _attach_extras(sb, [created])
    _notify_other_participants(sb, thread_id, user_id, f"📎 {safe_name}")
    return created


class PushSubscribeRequest(BaseModel):
    user_id: str
    endpoint: str
    p256dh: str
    auth_key: str


@router.post("/push/subscribe")
async def push_subscribe(body: PushSubscribeRequest):
    """Registers (or refreshes) a browser's Web Push subscription. Frontend
    calls this right after Notification.requestPermission() + a successful
    pushManager.subscribe(). Upsert on endpoint so re-subscribing the same
    browser doesn't create duplicate rows."""
    sb = get_supabase()
    sb.table("push_subscriptions").upsert({
        "user_id": body.user_id,
        "endpoint": body.endpoint,
        "p256dh": body.p256dh,
        "auth_key": body.auth_key,
    }, on_conflict="endpoint").execute()
    return {"status": "ok"}


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribeRequest):
    sb = get_supabase()
    sb.table("push_subscriptions").delete().eq("endpoint", body.endpoint).execute()
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
