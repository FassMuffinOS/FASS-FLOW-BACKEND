"""Admin-only manual onboarding.

Built for promo/cohort sign-ups where a student pays Munch directly
(Cash App, Apple Pay via a Stripe Payment Link, cash at an event, etc.)
instead of going through the in-app Stripe Checkout flow in
subscriptions.py. This still creates a *real* Supabase Auth user —
nothing here stores or touches a password. Supabase's invite-by-email
sends the student a magic link that lets them set their own password;
this endpoint just creates that invite and grants them a profile row
with paid access immediately, so they don't wait on anything.

Locked down with a single shared secret (ADMIN_SECRET env var) rather
than a full admin-role system, since this is a one-person ops tool.
Do not expose this secret in the frontend bundle — it's typed in by
hand each session, never committed, never logged.
"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr
from app.config import settings
from app.database import get_supabase

router = APIRouter(prefix="/admin", tags=["admin"])

ALLOWED_PLANS = {"starter", "pro", "team", "promo"}


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        # Fails closed: if the operator hasn't set a secret, refuse rather
        # than silently allowing free access grants to anyone who finds this URL.
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


class InviteRequest(BaseModel):
    email: EmailStr
    full_name: str = ""
    plan: str = "promo"
    note: str = ""  # e.g. "200 cash app promo 6/22" — shows up in profiles.admin_note


@router.post("/invite")
async def invite_user(body: InviteRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)

    if body.plan not in ALLOWED_PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    sb = get_supabase()

    invite_options = {"data": {"full_name": body.full_name}} if body.full_name else {}
    try:
        resp = sb.auth.admin.invite_user_by_email(body.email, invite_options)
    except Exception as e:
        # Most common case: email already has an account.
        raise HTTPException(status_code=400, detail=f"Could not invite: {e}") from e

    user = resp.user
    if not user:
        raise HTTPException(status_code=502, detail="Supabase did not return a user")

    sb.table("profiles").upsert({
        "id": user.id,
        "full_name": body.full_name or None,
        "plan": body.plan,
        "subscription_status": "active",
        "admin_note": body.note or "Manually granted via admin invite",
    }).execute()

    return {
        "user_id": user.id,
        "email": user.email,
        "plan": body.plan,
        "message": "Invite sent — student will get a magic-link email to set their password and sign in.",
    }


class GrantAccessRequest(BaseModel):
    """For a student who already has an account (e.g. signed up free,
    now paying you directly for the promo) — skip the invite email,
    just flip their plan on."""
    user_id: str
    plan: str = "promo"
    note: str = ""


@router.post("/grant-access")
async def grant_access(body: GrantAccessRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)

    if body.plan not in ALLOWED_PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    sb = get_supabase()
    result = sb.table("profiles").upsert({
        "id": body.user_id,
        "plan": body.plan,
        "subscription_status": "active",
        "admin_note": body.note or "Manually granted via admin grant-access",
    }).execute()

    return {"updated": result.data}
