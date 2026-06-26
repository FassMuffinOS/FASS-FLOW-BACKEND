"""Unified business profile — the shared "Customer 360" record that Start
Business, Wallet, and Rewards all read from and write to, so a business's
identity (name/address/naics/website/phone, captured once via Wallet's
Google Places lookup) and progress (structure, product/service path,
checklist) entered in any one tool show up in the others, instead of each
tool keeping its own disconnected localStorage/table silo.

Each tool only ever sends the fields it owns:
  - Wallet writes business_name/address/naics/website/phone after a
    successful lookup or free-card claim.
  - Start Business writes structure/biz_path/checklist as the user
    progresses through the wizard.
  - Rewards only ever READS business_name, to prefill its setup form.

POST /mine is therefore a partial-merge upsert, not a full overwrite — a
tool that only owns half the fields must never blank out the other half.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import get_supabase, single_data

router = APIRouter(prefix="/business-profile", tags=["business-profile"])


@router.get("/mine")
async def get_my_profile(user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="No business profile yet")
    return profile


class ProfileUpdate(BaseModel):
    user_id: str
    business_name: str | None = None
    address: str | None = None
    naics: str | None = None
    website: str | None = None
    phone: str | None = None
    structure: str | None = None
    biz_path: str | None = None
    checklist: dict | None = None


@router.post("/mine")
async def upsert_my_profile(body: ProfileUpdate):
    """Partial merge upsert keyed on user_id. Only fields the caller
    actually set are applied (pydantic's exclude_unset), so e.g. Start
    Business saving a checklist toggle never wipes out the business_name
    Wallet already wrote. checklist itself merges key-by-key on top of
    that, since it's the one field multiple wizard steps write to
    incrementally rather than all at once."""
    sb = get_supabase()
    existing = (
        single_data(
            sb.table("business_profiles")
            .select("*")
            .eq("user_id", body.user_id)
            .maybe_single()
            .execute()
        )
        or {}
    )

    updates = body.model_dump(exclude_unset=True, exclude={"user_id"})
    if "checklist" in updates and isinstance(existing.get("checklist"), dict):
        updates["checklist"] = {**existing["checklist"], **(updates["checklist"] or {})}

    row = {**existing, **updates, "user_id": body.user_id}
    row.pop("created_at", None)
    row.pop("updated_at", None)

    sb.table("business_profiles").upsert(row, on_conflict="user_id").execute()
    return {"ok": True}
