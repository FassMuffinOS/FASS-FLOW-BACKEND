"""FASS Growth Challenge — "Build Your Business in 30 Days."

A gamified onboarding sequence: 30 daily missions across 4 weeks, a 10-tier
level ladder (Dreamer -> ... -> FASS Certified), and 10 real-business
achievements. Mirrors the codebase's existing one-ledger-per-feature
convention (see classroom_rewards / affiliates.xp) instead of touching
either of those — see migrations/growth_challenge.sql for the schema this
router reads and writes.

Two kinds of progress, deliberately handled differently:

1. Missions (`growth_challenge_completions`) — most have an `auto_key`: a
   real signal already sitting in the account (a row in wallet_passes, a
   business_events action, etc.) that means the mission is genuinely done.
   `POST /check` re-evaluates every auto-detectable mission against live
   account state and silently completes anything newly true — no separate
   "mark complete" button needed for those. Missions with `auto_key: None`
   have no reliable signal yet (nothing in the schema tracks them), so the
   frontend offers a manual "Mark complete" action instead — see
   `POST /complete`.

2. Achievements (`growth_achievements`) — same auto-vs-manual split, scoped
   to the 10 specific real-business milestones from the concept doc rather
   than daily tasks. `POST /check` evaluates these too and, when one is
   newly earned, also drops a 'achievement' post on the business feed
   (mirrors Pipeline.jsx's existing auto-post-on-contract-awarded pattern)
   so it shows up in the community live feed.

XP total -> level/title is a fixed ladder (LEVEL_LADDER below), separate
from classroom_rewards' level math and affiliates' rank math — same
one-row-per-user + append-only-log shape, different curve, because the
three features measure different things.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/growth-challenge", tags=["growth-challenge"])

# ── Level ladder ──────────────────────────────────────────────────────

LEVEL_LADDER = [
    (0, "Dreamer"),
    (300, "Founder"),
    (700, "Operator"),
    (1200, "Builder"),
    (1800, "Contractor"),
    (2500, "Business Owner"),
    (3300, "Employer"),
    (4200, "Leader"),
    (5200, "CEO"),
    (6300, "FASS Certified"),
]


def _level_for(xp: int) -> dict:
    title = LEVEL_LADDER[0][1]
    level_num = 1
    next_threshold = LEVEL_LADDER[1][0] if len(LEVEL_LADDER) > 1 else None
    for i, (threshold, name) in enumerate(LEVEL_LADDER):
        if xp >= threshold:
            title = name
            level_num = i + 1
            next_threshold = LEVEL_LADDER[i + 1][0] if i + 1 < len(LEVEL_LADDER) else None
        else:
            break
    return {"level": level_num, "title": title, "xp": xp, "next_threshold": next_threshold}


# ── Mission definitions (fixed content, mirrors masterclassNights.js) ──
# auto_key references a checker in AUTO_CHECKS below; None = manual-complete only.

MISSIONS = [
    # Week 1 — Business HQ / Wallet / CRM / WARDOG / R-E-A-D / Proposal setup
    {"key": "day-1-business-hq", "day": 1, "week": 1, "title": "Set up your Business HQ",
     "mission": "Fill in your business name, NAICS code, and structure in Start Business.",
     "xp": 50, "auto_key": "profile_basics", "cta_href": "/start"},
    {"key": "day-2-wallet-card", "day": 2, "week": 1, "title": "Create your Wallet Card",
     "mission": "Generate your FASS Wallet capability card.",
     "xp": 50, "auto_key": "wallet_pass", "cta_href": "/wallet"},
    {"key": "day-3-capability-statement", "day": 3, "week": 1, "title": "Publish your capability statement",
     "mission": "Open your public capability page from Passport and share the link once.",
     "xp": 50, "auto_key": None, "cta_href": "/passport"},
    {"key": "day-4-rewards-program", "day": 4, "week": 1, "title": "Launch your customer CRM",
     "mission": "Set up a FASS Rewards loyalty program so you can start tracking customers.",
     "xp": 50, "auto_key": "reward_program", "cta_href": "/rewards"},
    {"key": "day-5-wardog-search", "day": 5, "week": 1, "title": "Run your first WARDOG search",
     "mission": "Search WARDOG for opportunities in your NAICS code and save three.",
     "xp": 75, "auto_key": "proposals_exist", "cta_href": "/wardog"},
    {"key": "day-6-read-analysis", "day": 6, "week": 1, "title": "Run your first R-E-A-D analysis",
     "mission": "Open a saved opportunity and run R-E-A-D to get your match score.",
     "xp": 75, "auto_key": "read_score_exists", "cta_href": "/pipeline"},
    {"key": "day-7-proposal-draft", "day": 7, "week": 1, "title": "Draft your first proposal",
     "mission": "Use FASS FILL to draft a proposal document on a real opportunity.",
     "xp": 100, "auto_key": "proposal_drafted", "cta_href": "/pipeline"},
    # Week 2 — Marketing / Reviews / Wallet / Gift Cards / Social / Referral
    {"key": "day-8-wallet-campaign", "day": 8, "week": 2, "title": "Send your first Wallet campaign",
     "mission": "Push an offer to customer Wallet passes through Wallet Messaging.",
     "xp": 75, "auto_key": "wallet_campaign_sent", "cta_href": "/campaigns"},
    {"key": "day-9-gift-card", "day": 9, "week": 2, "title": "Sell your first gift card",
     "mission": "Issue a gift card to a real customer.",
     "xp": 75, "auto_key": "gift_card_issued", "cta_href": "/giftcards"},
    {"key": "day-10-social-post", "day": 10, "week": 2, "title": "Post to the community feed",
     "mission": "Share an update, win, or milestone on the FASS Feed.",
     "xp": 50, "auto_key": "feed_post", "cta_href": "/feed"},
    {"key": "day-11-team-up-post", "day": 11, "week": 2, "title": "Post a teaming opportunity",
     "mission": "Post what you bring and what you need on Team Up.",
     "xp": 50, "auto_key": "partner_post", "cta_href": "/teamup"},
    {"key": "day-12-referral-invite", "day": 12, "week": 2, "title": "Invite your first referral",
     "mission": "Share your affiliate link with one person who could use FASS Flow.",
     "xp": 50, "auto_key": None, "cta_href": "/affiliates"},
    {"key": "day-13-customer-checkin", "day": 13, "week": 2, "title": "Check in with a past customer",
     "mission": "Message a past customer to ask for a review or repeat business.",
     "xp": 50, "auto_key": None, "cta_href": "/messages"},
    {"key": "day-14-reward-customer", "day": 14, "week": 2, "title": "Sign up your first loyalty customer",
     "mission": "Get one real customer onto your Rewards card.",
     "xp": 75, "auto_key": "first_reward_customer", "cta_href": "/rewards"},
    # Week 3 — Execution / Witness / Foreman / Documentation / AI
    {"key": "day-15-bid-submitted", "day": 15, "week": 3, "title": "Submit your first bid",
     "mission": "Move a proposal to Submitted in Pipeline.",
     "xp": 100, "auto_key": "bid_submitted", "cta_href": "/pipeline"},
    {"key": "day-16-witness-milestone", "day": 16, "week": 3, "title": "Log your first milestone",
     "mission": "Record a project milestone in Witness.",
     "xp": 50, "auto_key": "witness_milestone", "cta_href": "/witness"},
    {"key": "day-17-witness-document", "day": 17, "week": 3, "title": "Upload your first project document",
     "mission": "Upload a contract document or photo in Witness.",
     "xp": 50, "auto_key": "witness_document", "cta_href": "/witness"},
    {"key": "day-18-fill-compliance", "day": 18, "week": 3, "title": "Build a compliance matrix",
     "mission": "Use FASS FILL to build a compliance matrix for an open bid.",
     "xp": 75, "auto_key": "fill_document_created", "cta_href": "/pipeline"},
    {"key": "day-19-ai-notebook", "day": 19, "week": 3, "title": "Ask the AI Chief of Staff a question",
     "mission": "Ask your AI assistant a real question about your business on the Dashboard.",
     "xp": 50, "auto_key": None, "cta_href": "/dashboard"},
    {"key": "day-20-masterclass-mission", "day": 20, "week": 3, "title": "Complete a Masterclass mission",
     "mission": "Finish one Masterclass mission to sharpen your gov-con skills.",
     "xp": 50, "auto_key": "masterclass_mission", "cta_href": "/classroom"},
    {"key": "day-21-contract-won", "day": 21, "week": 3, "title": "Win your first contract",
     "mission": "Mark a proposal as Awarded in Pipeline.",
     "xp": 150, "auto_key": "contract_awarded", "cta_href": "/pipeline"},
    # Week 4 — Growth / Affiliate / Hiring / Automation / Scaling
    {"key": "day-22-affiliate-signup", "day": 22, "week": 4, "title": "Become an affiliate",
     "mission": "Sign up for the FASS affiliate program to earn from referrals.",
     "xp": 50, "auto_key": "affiliate_signup", "cta_href": "/affiliates"},
    {"key": "day-23-first-referral-conversion", "day": 23, "week": 4, "title": "Land your first referral",
     "mission": "Get one person to sign up through your affiliate link.",
     "xp": 100, "auto_key": "affiliate_conversion", "cta_href": "/affiliates"},
    {"key": "day-24-careers-post", "day": 24, "week": 4, "title": "Explore hiring help",
     "mission": "Review the FASS Careers board to see how hiring support works.",
     "xp": 25, "auto_key": None, "cta_href": "/careers"},
    {"key": "day-25-bd-partner", "day": 25, "week": 4, "title": "Consider a BD Partner",
     "mission": "Look at upgrading to a dedicated Business Development Partner.",
     "xp": 25, "auto_key": None, "cta_href": "/bd-partner"},
    {"key": "day-26-comms-hub", "day": 26, "week": 4, "title": "Centralize your messages",
     "mission": "Connect a number in Comms Hub so all customer texts land in one place.",
     "xp": 50, "auto_key": None, "cta_href": "/comms"},
    {"key": "day-27-second-proposal", "day": 27, "week": 4, "title": "Submit a second bid",
     "mission": "Keep your pipeline moving — submit another proposal.",
     "xp": 100, "auto_key": "second_bid_submitted", "cta_href": "/pipeline"},
    {"key": "day-28-business-health-80", "day": 28, "week": 4, "title": "Push your Business Health score up",
     "mission": "Get your Business Health score to 80 or higher.",
     "xp": 100, "auto_key": "business_health_80", "cta_href": "/dashboard"},
    {"key": "day-29-grow-customer-base", "day": 29, "week": 4, "title": "Grow your customer base",
     "mission": "Get 10 customers onto your Rewards card.",
     "xp": 100, "auto_key": "ten_reward_customers", "cta_href": "/rewards"},
    {"key": "day-30-fass-certified", "day": 30, "week": 4, "title": "Finish the Challenge",
     "mission": "You made it — review everything you built and keep the momentum going.",
     "xp": 150, "auto_key": None, "cta_href": "/dashboard"},
]

MISSIONS_BY_KEY = {m["key"]: m for m in MISSIONS}
TOTAL_DAYS = len(MISSIONS)

# ── Achievement definitions — the 10 real-business milestones ──────────

ACHIEVEMENTS = [
    {"key": "first_customer", "label": "First Customer", "xp": 100, "auto_key": "first_customer"},
    {"key": "first_proposal", "label": "First Proposal", "xp": 75, "auto_key": "first_proposal"},
    {"key": "first_contract_submitted", "label": "First Contract Submitted", "xp": 100, "auto_key": "first_bid_submitted"},
    {"key": "first_contract_won", "label": "First Contract Won", "xp": 250, "auto_key": "first_contract_won"},
    {"key": "first_10000", "label": "First $10,000", "xp": 250, "auto_key": None},
    {"key": "first_employee", "label": "First Employee", "xp": 200, "auto_key": None},
    {"key": "first_100_customers", "label": "First 100 Customers", "xp": 300, "auto_key": "first_100_customers"},
    {"key": "first_gift_card_sold", "label": "First Gift Card Sold", "xp": 75, "auto_key": "first_gift_card_sold"},
    {"key": "first_wallet_campaign", "label": "First Wallet Campaign", "xp": 75, "auto_key": "first_wallet_campaign"},
    {"key": "first_referral", "label": "First Referral", "xp": 100, "auto_key": "first_referral"},
]

ACHIEVEMENTS_BY_KEY = {a["key"]: a for a in ACHIEVEMENTS}


# ── Live signal gathering — one round of reads, reused by every checker ─

def _gather_signals(sb, user_id: str) -> dict:
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", user_id).maybe_single().execute()
    ) or {}

    events = (
        sb.table("business_events").select("action").eq("user_id", user_id).execute().data or []
    )
    action_counts: dict[str, int] = {}
    for e in events:
        action_counts[e["action"]] = action_counts.get(e["action"], 0) + 1

    wallet_pass = single_data(
        sb.table("wallet_passes").select("user_id").eq("user_id", user_id).maybe_single().execute()
    )
    reward_program = single_data(
        sb.table("reward_programs").select("business_user_id").eq("business_user_id", user_id).maybe_single().execute()
    )
    reward_customers = (
        sb.table("reward_cards").select("slug").eq("business_user_id", user_id).execute().data or []
    )
    gift_cards = sb.table("gift_cards").select("id").eq("business_user_id", user_id).execute().data or []
    wallet_campaigns = (
        sb.table("wallet_campaigns").select("id").eq("business_user_id", user_id).execute().data or []
    )
    proposals = sb.table("proposals").select("id, stage, read_score").eq("user_id", user_id).execute().data or []
    feed_posts = sb.table("business_posts").select("id").eq("user_id", user_id).execute().data or []
    partner_posts = sb.table("partner_posts").select("id").eq("user_id", user_id).execute().data or []
    witness_milestones = (
        sb.table("witness_milestones").select("id").eq("user_id", user_id).execute().data or []
    )
    witness_documents = (
        sb.table("witness_documents").select("id").eq("user_id", user_id).execute().data or []
    )
    masterclass = single_data(
        sb.table("classroom_rewards").select("stamps").eq("user_id", user_id).maybe_single().execute()
    ) or {}
    affiliate = single_data(
        sb.table("affiliates").select("user_id").eq("user_id", user_id).maybe_single().execute()
    )
    affiliate_conversions = (
        sb.table("affiliate_conversions").select("id").eq("affiliate_user_id", user_id).execute().data or []
    )
    health_events = (
        sb.table("business_events").select("category, points, created_at").eq("user_id", user_id).execute().data or []
    )

    return {
        "profile": profile,
        "action_counts": action_counts,
        "wallet_pass": bool(wallet_pass),
        "reward_program": bool(reward_program),
        "reward_customer_count": len(reward_customers),
        "gift_card_count": len(gift_cards),
        "wallet_campaign_count": len(wallet_campaigns),
        "proposal_count": len(proposals),
        "read_score_count": sum(1 for p in proposals if p.get("read_score") is not None),
        "submitted_count": sum(1 for p in proposals if p.get("stage") in ("submitted", "awarded")),
        "awarded_count": sum(1 for p in proposals if p.get("stage") == "awarded"),
        "feed_post_count": len(feed_posts),
        "partner_post_count": len(partner_posts),
        "witness_milestone_count": len(witness_milestones),
        "witness_document_count": len(witness_documents),
        "masterclass_stamps": masterclass.get("stamps") or 0,
        "is_affiliate": bool(affiliate),
        "affiliate_conversion_count": len(affiliate_conversions),
        "health_events": health_events,
    }


def _business_health_total(events: list[dict]) -> int:
    cap = 40
    sums: dict[str, int] = {}
    for e in events:
        sums[e["category"]] = sums.get(e["category"], 0) + (e.get("points") or 0)
    return sum(min(20, round((v / cap) * 20)) for v in sums.values())


# auto_key -> bool(signals)
AUTO_CHECKS = {
    "profile_basics": lambda s: bool(s["profile"].get("business_name") and s["profile"].get("naics")),
    "wallet_pass": lambda s: s["wallet_pass"],
    "reward_program": lambda s: s["reward_program"],
    "proposals_exist": lambda s: s["proposal_count"] >= 1,
    "read_score_exists": lambda s: s["read_score_count"] >= 1,
    "proposal_drafted": lambda s: s["action_counts"].get("proposal_drafted", 0) >= 1,
    "wallet_campaign_sent": lambda s: s["action_counts"].get("campaign_sent", 0) >= 1,
    "gift_card_issued": lambda s: s["action_counts"].get("gift_card_issued", 0) >= 1,
    "feed_post": lambda s: s["feed_post_count"] >= 1,
    "partner_post": lambda s: s["partner_post_count"] >= 1,
    "first_reward_customer": lambda s: s["reward_customer_count"] >= 1,
    "bid_submitted": lambda s: s["action_counts"].get("bid_submitted", 0) >= 1,
    "witness_milestone": lambda s: s["witness_milestone_count"] >= 1,
    "witness_document": lambda s: s["witness_document_count"] >= 1,
    "fill_document_created": lambda s: s["action_counts"].get("fill_document_created", 0) >= 1,
    "masterclass_mission": lambda s: s["masterclass_stamps"] >= 1,
    "contract_awarded": lambda s: s["action_counts"].get("contract_awarded", 0) >= 1,
    "affiliate_signup": lambda s: s["is_affiliate"],
    "affiliate_conversion": lambda s: s["affiliate_conversion_count"] >= 1,
    "second_bid_submitted": lambda s: s["submitted_count"] >= 2,
    "business_health_80": lambda s: _business_health_total(s["health_events"]) >= 80,
    "ten_reward_customers": lambda s: s["reward_customer_count"] >= 10,
    # achievements
    "first_customer": lambda s: s["reward_customer_count"] >= 1,
    "first_proposal": lambda s: s["proposal_count"] >= 1 or s["action_counts"].get("proposal_drafted", 0) >= 1,
    "first_bid_submitted": lambda s: s["action_counts"].get("bid_submitted", 0) >= 1,
    "first_contract_won": lambda s: s["awarded_count"] >= 1,
    "first_100_customers": lambda s: s["reward_customer_count"] >= 100,
    "first_gift_card_sold": lambda s: s["gift_card_count"] >= 1,
    "first_wallet_campaign": lambda s: s["wallet_campaign_count"] >= 1,
    "first_referral": lambda s: s["affiliate_conversion_count"] >= 1,
}


def _get_state(sb, user_id: str) -> dict:
    return single_data(
        sb.table("growth_challenge_state").select("*").eq("user_id", user_id).maybe_single().execute()
    ) or {"user_id": user_id, "xp": 0, "current_day": 1, "last_activity_date": None}


def _award_xp(sb, user_id: str, xp_gain: int):
    if xp_gain <= 0:
        return
    state = _get_state(sb, user_id)
    new_xp = (state.get("xp") or 0) + xp_gain
    sb.table("growth_challenge_state").upsert(
        {"user_id": user_id, "xp": new_xp, "current_day": state.get("current_day") or 1}, on_conflict="user_id"
    ).execute()


def _post_achievement_to_feed(sb, user_id: str, label: str):
    # Mirrors Pipeline.jsx's existing auto-post-on-contract-awarded shape —
    # 'category' is unconstrained text on business_posts so 'achievement'
    # needs no migration. Best-effort: a feed-post failure should never
    # block the achievement itself from being recorded.
    try:
        sb.table("business_posts").insert({
            "user_id": user_id,
            "category": "achievement",
            "source": "auto",
            "content": f"Unlocked the \"{label}\" achievement in the FASS Growth Challenge.",
        }).execute()
    except Exception:
        pass


def _run_checks(sb, user_id: str) -> dict:
    signals = _gather_signals(sb, user_id)

    already_done = {
        c["mission_key"]
        for c in (sb.table("growth_challenge_completions").select("mission_key").eq("user_id", user_id).execute().data or [])
    }
    already_earned = {
        a["achievement_key"]
        for a in (sb.table("growth_achievements").select("achievement_key").eq("user_id", user_id).execute().data or [])
    }

    newly_completed = []
    xp_gain = 0
    for m in MISSIONS:
        if m["key"] in already_done or not m["auto_key"]:
            continue
        check = AUTO_CHECKS.get(m["auto_key"])
        if check and check(signals):
            sb.table("growth_challenge_completions").insert({
                "user_id": user_id, "mission_key": m["key"], "xp_awarded": m["xp"],
            }).execute()
            newly_completed.append(m["key"])
            xp_gain += m["xp"]

    newly_earned = []
    for a in ACHIEVEMENTS:
        if a["key"] in already_earned or not a["auto_key"]:
            continue
        check = AUTO_CHECKS.get(a["auto_key"])
        if check and check(signals):
            sb.table("growth_achievements").insert({
                "user_id": user_id, "achievement_key": a["key"], "label": a["label"], "xp_awarded": a["xp"],
            }).execute()
            newly_earned.append(a["key"])
            xp_gain += a["xp"]
            _post_achievement_to_feed(sb, user_id, a["label"])

    if xp_gain:
        _award_xp(sb, user_id, xp_gain)

    return {"newly_completed_missions": newly_completed, "newly_earned_achievements": newly_earned, "xp_gained": xp_gain}


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/mine")
async def get_my_growth_challenge(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own growth challenge")
    sb = get_supabase()
    state = _get_state(sb, user_id)
    completions = (
        sb.table("growth_challenge_completions").select("mission_key, xp_awarded, completed_at")
        .eq("user_id", user_id).execute().data or []
    )
    done_keys = {c["mission_key"] for c in completions}
    achievements = (
        sb.table("growth_achievements").select("achievement_key, label, xp_awarded, earned_at")
        .eq("user_id", user_id).execute().data or []
    )
    earned_keys = {a["achievement_key"] for a in achievements}

    missions_out = [
        {**m, "completed": m["key"] in done_keys, "manual_only": m["auto_key"] is None}
        for m in MISSIONS
    ]
    achievements_out = [
        {**a, "earned": a["key"] in earned_keys, "manual_only": a["auto_key"] is None}
        for a in ACHIEVEMENTS
    ]

    level_info = _level_for(state.get("xp") or 0)
    completed_count = len(done_keys)
    today_mission = next((m for m in missions_out if not m["completed"]), None)

    return {
        "state": {**state, **level_info},
        "missions": missions_out,
        "achievements": achievements_out,
        "completed_count": completed_count,
        "total_missions": TOTAL_DAYS,
        "today_mission": today_mission,
    }


@router.post("/check")
async def check_growth_challenge(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    """Re-evaluate every auto-detectable mission/achievement against live
    account state. Cheap to call often — it's pure reads plus inserts for
    whatever is newly true, same idempotent shape as business_events."""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    require_owner(current_user, user_id, detail="You can only check your own growth challenge")
    sb = get_supabase()
    result = _run_checks(sb, user_id)
    return result


class CompleteMissionRequest(BaseModel):
    user_id: str
    mission_key: str


@router.post("/complete")
async def complete_mission(body: CompleteMissionRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Manual completion for missions with no reliable auto-signal (see
    auto_key: None in MISSIONS). Auto-detectable missions are rejected here
    so the ledger only ever records genuine, verifiable progress — call
    /check instead for those."""
    require_owner(current_user, body.user_id, detail="You can only complete missions for your own account")
    mission = MISSIONS_BY_KEY.get(body.mission_key)
    if not mission:
        raise HTTPException(status_code=404, detail="Unknown mission")
    if mission["auto_key"]:
        raise HTTPException(status_code=400, detail="This mission is auto-detected — call /check instead")

    sb = get_supabase()
    already = single_data(
        sb.table("growth_challenge_completions").select("mission_key")
        .eq("user_id", body.user_id).eq("mission_key", body.mission_key).maybe_single().execute()
    )
    if already:
        return {"already_completed": True}

    sb.table("growth_challenge_completions").insert({
        "user_id": body.user_id, "mission_key": body.mission_key, "xp_awarded": mission["xp"],
    }).execute()
    _award_xp(sb, body.user_id, mission["xp"])
    return {"completed": True, "xp_awarded": mission["xp"]}


class ClaimAchievementRequest(BaseModel):
    user_id: str
    achievement_key: str


@router.post("/claim-achievement")
async def claim_achievement(body: ClaimAchievementRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Manual claim for the two achievements with no ledger to verify
    against yet (First $10,000, First Employee). Honor-system by design —
    same trust model as bd_partner_activity's manual logging elsewhere in
    this codebase."""
    require_owner(current_user, body.user_id, detail="You can only claim achievements for your own account")
    achievement = ACHIEVEMENTS_BY_KEY.get(body.achievement_key)
    if not achievement:
        raise HTTPException(status_code=404, detail="Unknown achievement")
    if achievement["auto_key"]:
        raise HTTPException(status_code=400, detail="This achievement is auto-detected — call /check instead")

    sb = get_supabase()
    already = single_data(
        sb.table("growth_achievements").select("achievement_key")
        .eq("user_id", body.user_id).eq("achievement_key", body.achievement_key).maybe_single().execute()
    )
    if already:
        return {"already_earned": True}

    sb.table("growth_achievements").insert({
        "user_id": body.user_id, "achievement_key": body.achievement_key,
        "label": achievement["label"], "xp_awarded": achievement["xp"],
    }).execute()
    _award_xp(sb, body.user_id, achievement["xp"])
    _post_achievement_to_feed(sb, body.user_id, achievement["label"])
    return {"earned": True, "xp_awarded": achievement["xp"]}
