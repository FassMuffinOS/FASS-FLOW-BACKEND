"""Classroom Notebook — the "college class + Google NotebookLM" layer on
top of the Masterclass.

Three jobs, mirroring migrations/classroom_notebook.sql:

1. Gamification (complete-night): every time a student marks a night
   complete, compute XP/level/streak/badges/stamps server-side and persist
   them. Kept authoritative on the backend (not just a frontend animation)
   because the streak and badge state is meant to be a real transcript, not
   a cosmetic effect that resets if the student clears local state.

2. NotebookLM-style chat (chat): a grounded Q&A assistant scoped to one
   night's lesson content plus the student's own business profile and
   prior notebook entries — "niche down" answers about their specific
   business rather than generic course advice. Every turn is saved so the
   notebook is a real running transcript, not a stateless chat widget.

3. Personalized insight (insight): fired once after a student submits a
   night's homework. Reads their homework answer + business profile,
   asks the LLM for a short, specific insight plus any durable niche
   keywords, and writes those keywords back onto business_profiles so
   WARDOG and the rest of the app can read them — this is the "becomes
   their system" piece, not just a note that sits in the Classroom.
"""
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase, single_data
from app.services.llm import llm_router, extract_json, LLMUnavailableError
from app.services.retrieval import rank_passages
from app.services.quota import check_and_consume_ai_quota

router = APIRouter(prefix="/notebook", tags=["notebook"])

TOTAL_NIGHTS = 10
XP_PER_NIGHT = 100
XP_PER_WORKSHEET_NOTE = 10  # small bonus for actually filling in homework notes, not leaving them blank
LEVEL_XP_STEP = 250  # every 250 XP is a new level

BADGE_RULES = [
    (1, "first-night", "Started the Masterclass"),
    (3, "early-access", "Unlocked WARDOG early"),
    (5, "halfway", "Halfway through the program"),
    (10, "graduate", "Completed all 10 nights"),
]
STREAK_BADGES = [(3, "streak-3", "3-night study streak"), (7, "streak-7", "7-night study streak")]


# ── Rewards: complete-night ──────────────────────────────────────────

class CompleteNightRequest(BaseModel):
    user_id: str
    night: int
    has_notes: bool = False


def _level_for(xp: int) -> int:
    return 1 + xp // LEVEL_XP_STEP


@router.post("/complete-night")
async def complete_night(body: CompleteNightRequest):
    sb = get_supabase()
    row = single_data(
        sb.table("classroom_rewards").select("*").eq("user_id", body.user_id).maybe_single().execute()
    ) or {"user_id": body.user_id, "xp": 0, "level": 1, "streak_count": 0, "last_activity_date": None, "badges": [], "stamps": 0}

    today = date.today()
    last = row.get("last_activity_date")
    last_date = date.fromisoformat(last) if isinstance(last, str) else last

    if last_date == today:
        streak = row.get("streak_count") or 0  # already studied today — don't double-count
    elif last_date == today - timedelta(days=1):
        streak = (row.get("streak_count") or 0) + 1
    else:
        streak = 1

    xp_gain = XP_PER_NIGHT + (XP_PER_WORKSHEET_NOTE if body.has_notes else 0)
    new_xp = (row.get("xp") or 0) + xp_gain
    new_level = _level_for(new_xp)
    stamps = min((row.get("stamps") or 0) + 1, TOTAL_NIGHTS)

    badges = set(row.get("badges") or [])
    for threshold, slug, _label in BADGE_RULES:
        if body.night >= threshold:
            badges.add(slug)
    for threshold, slug, _label in STREAK_BADGES:
        if streak >= threshold:
            badges.add(slug)

    updated = {
        "user_id": body.user_id,
        "xp": new_xp,
        "level": new_level,
        "streak_count": streak,
        "last_activity_date": today.isoformat(),
        "badges": sorted(badges),
        "stamps": stamps,
    }
    sb.table("classroom_rewards").upsert(updated, on_conflict="user_id").execute()

    leveled_up = new_level > (row.get("level") or 1)
    return {**updated, "xp_gain": xp_gain, "leveled_up": leveled_up}


@router.get("/rewards/mine")
async def get_my_rewards(user_id: str):
    sb = get_supabase()
    row = single_data(
        sb.table("classroom_rewards").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    return row or {
        "user_id": user_id, "xp": 0, "level": 1, "streak_count": 0,
        "last_activity_date": None, "badges": [], "stamps": 0,
    }


# ── NotebookLM-style chat ────────────────────────────────────────────

class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    user_id: str
    night: int
    night_title: str = ""
    night_context: str = ""  # joined objectives + section text + homework for this night
    message: str
    history: list[ChatTurn] = []


CHAT_SYSTEM_PROMPT = """You are the FASS Classroom Notebook — an AI study assistant for a small \
business owner taking a government contracting masterclass, styled after a "notebook" tool like \
Google's NotebookLM: you answer strictly from the sources you're given, you say plainly when \
something isn't covered by those sources, and you tie answers back to the student's OWN business \
whenever they ask about it, rather than giving generic advice. You will be given: the current \
night's lesson content, the student's business profile (if on file), and relevant excerpts from \
their own past notebook entries. Niche your answers down to their specific NAICS code, services, \
and location when relevant — a generic answer is a failure here. Keep replies tight: 2-5 sentences \
unless the question genuinely needs a list. If asked something the provided sources don't cover, \
say so and suggest what night or tool in FASS Flow would actually answer it, rather than guessing."""


@router.post("/chat")
async def chat(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")
    check_and_consume_ai_quota(body.user_id)

    sb = get_supabase()
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", body.user_id).maybe_single().execute()
    ) or {}

    past_entries = (
        sb.table("classroom_notebook")
        .select("content")
        .eq("user_id", body.user_id)
        .order("created_at", desc=True)
        .limit(30)
        .execute()
        .data
        or []
    )
    passages = [e["content"] for e in past_entries if e.get("content")]
    grounded = rank_passages(body.message, passages, top_k=3)
    prior_block = "\n".join(f"- {g['text']}" for g in grounded) or "(no relevant prior notebook entries)"

    profile_block = (
        f"Business name: {profile.get('business_name') or '(not set)'}\n"
        f"NAICS: {profile.get('naics') or '(not set)'}\n"
        f"Structure: {profile.get('structure') or '(not set)'}\n"
        f"Niche keywords on file: {', '.join(profile.get('notebook_keywords') or []) or '(none yet)'}"
    )

    history_block = "\n".join(f"{t.role}: {t.content}" for t in body.history[-8:])

    prompt = f"""Current night: {body.night} — {body.night_title}

Night content (source of truth for lesson questions):
{body.night_context[:6000]}

Student's business profile:
{profile_block}

Relevant past notebook entries:
{prior_block}

Conversation so far:
{history_block}

Student's new message: {body.message}"""

    try:
        result = await llm_router.complete(system=CHAT_SYSTEM_PROMPT, prompt=prompt, max_tokens=600)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    sb.table("classroom_notebook").insert([
        {"user_id": body.user_id, "night": body.night, "entry_type": "chat_user", "content": body.message},
        {"user_id": body.user_id, "night": body.night, "entry_type": "chat_assistant", "content": result.text.strip(),
         "meta": {"provider": result.provider, "model": result.model}},
    ]).execute()

    return {"reply": result.text.strip(), "provider": result.provider, "model": result.model}


# ── Personalized insight after homework ──────────────────────────────

class InsightRequest(BaseModel):
    user_id: str
    night: int
    night_title: str = ""
    night_subtitle: str = ""
    homework_prompt: str = ""
    homework_notes: str = ""


INSIGHT_SYSTEM_PROMPT = """You are the FASS Classroom Notebook, generating a short personalized \
insight after a student finishes a night's homework in a government contracting masterclass. You \
will be given the night's homework prompt, what the student actually wrote, and their business \
profile. Respond with ONLY a single JSON object, no prose before or after it:

{
  "insight": string,            // 2-4 sentences: specific, grounded feedback connecting what they wrote to their actual business and what to do next. If they left it blank or vague, say so plainly and tell them what a stronger answer would include.
  "niche_keywords": [string],   // 2-6 short keywords/phrases describing their specific service niche, ONLY if inferable from what they wrote or their profile — empty array if nothing new is learned
  "niche_summary": string or null  // one sentence positioning summary for their business, only if you have enough to write one confidently — null otherwise
}

Do not invent business details not present in the input. If homework_notes is empty, say so in \
the insight and leave niche_keywords empty. Respond with ONLY the JSON object."""


@router.post("/insight")
async def generate_insight(body: InsightRequest):
    check_and_consume_ai_quota(body.user_id)
    sb = get_supabase()
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", body.user_id).maybe_single().execute()
    ) or {}

    prompt = f"""Night {body.night}: {body.night_title} — {body.night_subtitle}

Homework prompt: {body.homework_prompt or '(not provided)'}

What the student wrote: {body.homework_notes.strip() or '(left blank)'}

Business profile on file:
Business name: {profile.get('business_name') or '(not set)'}
NAICS: {profile.get('naics') or '(not set)'}
Structure: {profile.get('structure') or '(not set)'}
Existing niche keywords: {', '.join(profile.get('notebook_keywords') or []) or '(none yet)'}"""

    try:
        result = await llm_router.complete(system=INSIGHT_SYSTEM_PROMPT, prompt=prompt, max_tokens=500)
        fields = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    insight_text = fields.get("insight", "").strip()
    new_keywords = [str(k) for k in (fields.get("niche_keywords") or []) if k]
    niche_summary = fields.get("niche_summary") or None

    sb.table("classroom_notebook").insert({
        "user_id": body.user_id,
        "night": body.night,
        "entry_type": "insight",
        "content": insight_text,
        "meta": {"niche_keywords": new_keywords, "provider": result.provider, "model": result.model},
    }).execute()

    # Sync durable niche signal back onto the shared business profile so
    # WARDOG and everything else downstream can read it — merge, don't
    # overwrite, since each night may add a couple more keywords.
    if new_keywords or niche_summary:
        existing_keywords = set(profile.get("notebook_keywords") or [])
        merged_keywords = sorted(existing_keywords | set(new_keywords))
        update_row = {"user_id": body.user_id, "notebook_keywords": merged_keywords}
        if niche_summary:
            update_row["notebook_summary"] = niche_summary
        sb.table("business_profiles").upsert(update_row, on_conflict="user_id").execute()

    return {
        "insight": insight_text,
        "niche_keywords_added": new_keywords,
        "niche_summary": niche_summary,
        "provider": result.provider,
        "model": result.model,
    }


# ── My Notebook page ─────────────────────────────────────────────────

@router.get("/mine")
async def get_my_notebook(user_id: str):
    sb = get_supabase()
    entries = (
        sb.table("classroom_notebook")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    rewards = single_data(
        sb.table("classroom_rewards").select("*").eq("user_id", user_id).maybe_single().execute()
    ) or {"xp": 0, "level": 1, "streak_count": 0, "badges": [], "stamps": 0}
    profile = single_data(
        sb.table("business_profiles").select("notebook_keywords, notebook_summary").eq("user_id", user_id).maybe_single().execute()
    ) or {}

    return {
        "entries": entries,
        "rewards": rewards,
        "niche_keywords": profile.get("notebook_keywords") or [],
        "niche_summary": profile.get("notebook_summary"),
    }
