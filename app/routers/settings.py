"""Account Settings — backs the frontend's /settings page.

Scope deliberately split from what already exists elsewhere so this file
doesn't duplicate it:
  - Plan/billing data -> subscriptions.py (GET /subscriptions/portal/{id})
  - AI credit balance  -> credits.py     (GET /credits/balance)
  - Stripe Connect     -> stripe_connect.py (GET /connect/status)
  - Affiliate program   -> affiliates.py (GET /affiliates/me)
  - Editable name/company -> users.py (PATCH /users/{id}/profile)
This router owns the pieces nothing else does: per-user preferences
(theme/default track/notification toggles), email/password changes, and
privacy requests (export/delete). See migrations/settings.sql for schema.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/settings", tags=["settings"])

DEFAULT_PREFERENCES = {
    "theme": "dark",
    "default_track": "govcon",
    "ai_auto_draft": False,
    "email_notifications": True,
    "sms_notifications": True,
    "push_notifications": True,
}


def _get_or_create_preferences(sb, user_id: str) -> dict:
    row = single_data(
        sb.table("user_preferences").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    if row:
        return row
    created = single_data(
        sb.table("user_preferences").insert({"user_id": user_id, **DEFAULT_PREFERENCES}).execute()
    )
    return created or {"user_id": user_id, **DEFAULT_PREFERENCES}


@router.get("/preferences")
async def get_preferences(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own preferences")
    sb = get_supabase()
    return _get_or_create_preferences(sb, user_id)


class PreferencesUpdate(BaseModel):
    user_id: str
    theme: str | None = None
    default_track: str | None = None
    ai_auto_draft: bool | None = None
    email_notifications: bool | None = None
    sms_notifications: bool | None = None
    push_notifications: bool | None = None


@router.patch("/preferences")
async def update_preferences(body: PreferencesUpdate, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only update your own preferences")
    sb = get_supabase()
    _get_or_create_preferences(sb, body.user_id)  # ensure row exists before patching
    fields = body.model_dump(exclude={"user_id"}, exclude_none=True)
    if not fields:
        return _get_or_create_preferences(sb, body.user_id)
    updated = single_data(
        sb.table("user_preferences").update(fields).eq("user_id", body.user_id).execute()
    )
    return updated or fields


class EmailChangeRequest(BaseModel):
    user_id: str
    new_email: str


@router.post("/account/email")
async def change_email(body: EmailChangeRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Updates the Supabase Auth email via the admin API (service-role
    client, see database.py). Supabase sends its own confirmation email to
    the new address before the change takes effect — this just kicks that
    off, it doesn't flip the email immediately."""
    require_owner(current_user, body.user_id, detail="You can only change your own email")
    sb = get_supabase()
    try:
        sb.auth.admin.update_user_by_id(body.user_id, {"email": body.new_email})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not update email: {exc}")
    return {"ok": True, "message": "Check your new email address to confirm the change."}


class PasswordChangeRequest(BaseModel):
    user_id: str
    new_password: str


@router.post("/account/password")
async def change_password(body: PasswordChangeRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only change your own password")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    sb = get_supabase()
    try:
        sb.auth.admin.update_user_by_id(body.user_id, {"password": body.new_password})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not update password: {exc}")
    return {"ok": True}


class AccountRequest(BaseModel):
    user_id: str
    notes: str | None = None


@router.post("/privacy/export")
async def request_data_export(body: AccountRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Queues a data-export request for manual follow-up rather than
    generating one synchronously — see migrations/settings.sql's note on
    why this is a queue, not an instant action."""
    require_owner(current_user, body.user_id, detail="You can only request an export of your own data")
    sb = get_supabase()
    row = single_data(
        sb.table("account_requests")
        .insert({"user_id": body.user_id, "type": "export", "notes": body.notes})
        .execute()
    )
    return row or {"ok": True}


@router.post("/privacy/delete")
async def request_account_deletion(body: AccountRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only request deletion of your own account")
    sb = get_supabase()
    row = single_data(
        sb.table("account_requests")
        .insert({"user_id": body.user_id, "type": "delete", "notes": body.notes})
        .execute()
    )
    return row or {"ok": True}


@router.get("/privacy/requests")
async def list_account_requests(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own requests")
    sb = get_supabase()
    result = (
        sb.table("account_requests")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []
