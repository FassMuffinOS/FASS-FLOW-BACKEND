"""Careers page: public applications + founder-side applicant seeding.

The Careers page's "show us something you've built" challenge needs a real
submission path, not just a mailto link. POST /careers/apply is public (no
auth — most applicants don't have a FASS account yet) and just records the
applicant. The founder reviews submissions via GET /careers/applicants and,
for anyone worth bringing onto the platform, calls POST
/careers/applicants/{id}/invite — which reuses the exact same Supabase
invite-by-email mechanism as admin.py's /admin/invite (a magic-link email,
applicant sets their own password, backend never touches it). Same
shared-secret gate as every other admin tool in this codebase.
"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr
from app.config import settings
from app.database import get_supabase

router = APIRouter(prefix="/careers", tags=["careers"])


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


class ApplyRequest(BaseModel):
    name: str
    email: EmailStr
    role_interest: str = ""
    portfolio_url: str = ""
    note: str = ""


@router.post("/apply")
async def apply(body: ApplyRequest):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    sb = get_supabase()
    result = sb.table("job_applicants").insert({
        "name": body.name.strip(),
        "email": str(body.email),
        "role_interest": body.role_interest.strip(),
        "portfolio_url": body.portfolio_url.strip(),
        "note": body.note.strip(),
        "status": "new",
    }).execute()

    row = (result.data or [{}])[0]
    return {
        "id": row.get("id"),
        "message": "Got it — we'll review what you sent and reach out if it's a fit.",
    }


@router.get("/applicants")
async def list_applicants(x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    result = (
        sb.table("job_applicants")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    return {"applicants": result.data or []}


class InviteApplicantRequest(BaseModel):
    plan: str = "starter"
    note: str = ""


@router.post("/applicants/{applicant_id}/invite")
async def invite_applicant(applicant_id: str, body: InviteApplicantRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()

    existing = sb.table("job_applicants").select("*").eq("id", applicant_id).single().execute()
    applicant = existing.data
    if not applicant:
        raise HTTPException(status_code=404, detail="Applicant not found")
    if applicant.get("user_id"):
        raise HTTPException(status_code=400, detail="This applicant already has an account")

    invite_options = {"data": {"full_name": applicant.get("name")}} if applicant.get("name") else {}
    try:
        resp = sb.auth.admin.invite_user_by_email(applicant["email"], invite_options)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not invite: {e}") from e

    user = resp.user
    if not user:
        raise HTTPException(status_code=502, detail="Supabase did not return a user")

    sb.table("profiles").upsert({
        "id": user.id,
        "full_name": applicant.get("name") or None,
        "plan": body.plan,
        "subscription_status": "active",
        "admin_note": body.note or f"Seeded from job applicant ({applicant.get('role_interest') or 'Careers'})",
    }).execute()

    sb.table("job_applicants").update({
        "status": "invited",
        "user_id": user.id,
    }).eq("id", applicant_id).execute()

    return {
        "user_id": user.id,
        "email": user.email,
        "message": "Invite sent — they'll get a magic-link email to set their password and sign in.",
    }
