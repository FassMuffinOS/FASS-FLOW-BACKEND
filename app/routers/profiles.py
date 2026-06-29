"""Discoverable Business Profiles — the "reputation" layer of the Community
world. Every user already has scattered identity data (business_profiles,
wallet_passes) and scattered activity data (affiliates' XP/rank, proposals,
reward_cards), but nothing surfaces it together as a single, linkable page.

This router does no writes and adds no new tables — every stat below is
computed live off tables that already exist (see affiliates.py, the
proposals/opportunities tables in supabase_schema.sql, and rewards_cards.sql)
so there's nothing new to keep in sync. Worth revisiting as a materialized
view if these queries ever show up as a real load problem, but at current
scale a handful of indexed .eq() lookups per profile view is cheap.
"""
import logging

from fastapi import APIRouter, HTTPException

from app.database import get_supabase, single_data
from app.routers.affiliates import _gamification_snapshot

logger = logging.getLogger("fass_flow.profiles")

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("/{user_id}")
async def get_profile(user_id: str):
    """Combined public profile: identity (name/company + business card if
    they've claimed one) and reputation stats (creator rank, contracts won,
    wallet members, businesses helped). Never 404s on a real auth user —
    a person with none of these set up yet still gets a valid, mostly-empty
    profile rather than an error, since "no stats yet" is the normal state
    for a brand-new account, not a bug.

    Deliberately public, no auth required — this IS the discoverable
    Business Profiles feature (linked from chat, search, Team Up; see
    Profile.jsx), the whole point of which is that anyone can look someone
    up by user_id. See PUBLIC_ALLOWLIST in scripts/security_scan.py."""
    sb = get_supabase()

    profile = single_data(
        sb.table("profiles").select("id, full_name, company_name").eq("id", user_id).maybe_single().execute()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="No profile found for that user")

    biz = single_data(
        sb.table("business_profiles")
        .select("business_name, address, naics, website, phone")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )

    card = single_data(
        sb.table("wallet_passes")
        .select("slug, business_name, naics, address, website, phone")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )

    # Creator rank — affiliates.py already computes level/rank/xp from a
    # single xp column; reuse that helper rather than re-deriving it here.
    affiliate = single_data(
        sb.table("affiliates").select("xp, status").eq("user_id", user_id).maybe_single().execute()
    )
    gamification = _gamification_snapshot(affiliate.get("xp")) if affiliate else None

    # Businesses helped — best available proxy is affiliates this person
    # personally recruited (recruited_by_user_id), since there's no
    # "successfully partnered" outcome tracked anywhere else yet (Team Up
    # posts only have open/closed, not a matched state).
    recruits = (
        sb.table("affiliates").select("user_id").eq("recruited_by_user_id", user_id).execute().data or []
    )

    # Contracts won — proposals.status reaches 'awarded' (no separate "won"
    # value exists), joined to opportunities for a dollar estimate since
    # proposals itself carries no $ column.
    won_proposals = (
        sb.table("proposals")
        .select("id, opportunity_id")
        .eq("user_id", user_id)
        .eq("status", "awarded")
        .execute()
        .data
        or []
    )
    won_value = 0
    opp_ids = [p["opportunity_id"] for p in won_proposals if p.get("opportunity_id")]
    if opp_ids:
        opps = (
            sb.table("opportunities").select("id, value_estimate").in_("id", opp_ids).execute().data or []
        )
        won_value = sum(float(o["value_estimate"]) for o in opps if o.get("value_estimate"))

    # Wallet members — count of customers enrolled in this person's own
    # rewards program, if they run one (reward_cards, not wallet_passes —
    # that table is their own card, not their customers').
    wallet_members = (
        sb.table("reward_cards")
        .select("slug")
        .eq("business_user_id", user_id)
        .execute()
        .data
        or []
    )

    return {
        "user_id": user_id,
        "full_name": profile.get("full_name"),
        "company_name": profile.get("company_name"),
        "business": biz,
        "has_card": bool(card),
        "card": card,
        "gamification": gamification,
        "stats": {
            "businesses_helped": len(recruits),
            "contracts_won": len(won_proposals),
            "contracts_won_value": won_value,
            "wallet_members": len(wallet_members),
        },
    }
