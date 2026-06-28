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

Multi-entity (added for tiered "manage multiple businesses"): business_
entities is the real multi-business store, one row per business a user
runs. business_profiles itself is left untouched — it now acts as a mirror
of whichever entity is "active," which is what lets Wallet/Rewards/Start
Business keep calling /mine exactly as before with zero changes on their
end. Switching the active entity (or creating/deleting one) just re-mirrors
into business_profiles. Entity counts are capped per plan: Free/Core = 1,
Pro = 3, Team = unlimited.
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import get_supabase, single_data

router = APIRouter(prefix="/business-profile", tags=["business-profile"])

ENTITY_LIMITS = {"free": 1, "starter": 1, "pro": 3, "team": None}
ENTITY_FIELDS = ("business_name", "address", "naics", "website", "phone", "structure", "biz_path", "checklist", "certifications")


def _entity_limit_for(plan: str | None) -> int | None:
    return ENTITY_LIMITS.get(plan or "free", 1)


def _mirror_to_profile(sb, user_id: str, entity: dict):
    """Copy an entity's fields into the legacy single-row business_profiles
    table so Wallet/Rewards/Start Business — which only ever call /mine —
    transparently see whichever entity is active."""
    row = {f: entity.get(f) for f in ENTITY_FIELDS}
    row["user_id"] = user_id
    sb.table("business_profiles").upsert(row, on_conflict="user_id").execute()


def _get_or_create_active_entity(sb, user_id: str) -> dict:
    active = single_data(
        sb.table("business_entities").select("*").eq("user_id", user_id).eq("active", True).maybe_single().execute()
    )
    if active:
        return active

    # No entity yet (pre-multi-entity account or brand new user) — seed one
    # from whatever's already in business_profiles, or start blank.
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", user_id).maybe_single().execute()
    ) or {}
    row = {f: profile.get(f) for f in ENTITY_FIELDS}
    row["checklist"] = row.get("checklist") or {}
    row.update({"user_id": user_id, "is_primary": True, "active": True})
    res = sb.table("business_entities").insert(row).execute()
    return (res.data[0] if res and res.data else row)


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
    certifications: list[str] | None = None


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

    # Keep the active entity row in sync so the entity list reflects edits
    # made through tools that only know about /mine.
    active = _get_or_create_active_entity(sb, body.user_id)
    if active.get("id"):
        entity_updates = {f: row.get(f) for f in ENTITY_FIELDS if f in row}
        sb.table("business_entities").update(entity_updates).eq("id", active["id"]).execute()

    return {"ok": True}


@router.get("/entities")
async def list_entities(user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    _get_or_create_active_entity(sb, user_id)  # ensure at least one exists

    entities = sb.table("business_entities").select("*").eq("user_id", user_id).order("created_at").execute().data or []
    profile = single_data(sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()) or {}
    plan = profile.get("plan") or "free"
    return {"entities": entities, "limit": _entity_limit_for(plan), "plan": plan}


class CreateEntity(BaseModel):
    user_id: str
    business_name: str | None = None


@router.post("/entities")
async def create_entity(body: CreateEntity):
    sb = get_supabase()
    profile = single_data(sb.table("profiles").select("plan").eq("id", body.user_id).maybe_single().execute()) or {}
    limit = _entity_limit_for(profile.get("plan"))

    existing = sb.table("business_entities").select("id").eq("user_id", body.user_id).execute().data or []
    if limit is not None and len(existing) >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Your plan allows {limit} business {'entity' if limit == 1 else 'entities'}. Upgrade to add more.",
        )

    sb.table("business_entities").update({"active": False}).eq("user_id", body.user_id).execute()
    row = {
        "user_id": body.user_id,
        "business_name": body.business_name,
        "is_primary": len(existing) == 0,
        "active": True,
        "checklist": {},
    }
    res = sb.table("business_entities").insert(row).execute()
    created = res.data[0] if res and res.data else row
    _mirror_to_profile(sb, body.user_id, created)
    return created


@router.post("/entities/{entity_id}/activate")
async def activate_entity(entity_id: str, user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    entity = single_data(
        sb.table("business_entities").select("*").eq("id", entity_id).eq("user_id", user_id).maybe_single().execute()
    )
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    sb.table("business_entities").update({"active": False}).eq("user_id", user_id).execute()
    sb.table("business_entities").update({"active": True}).eq("id", entity_id).execute()
    _mirror_to_profile(sb, user_id, entity)
    return {"ok": True}


@router.delete("/entities/{entity_id}")
async def delete_entity(entity_id: str, user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    all_entities = sb.table("business_entities").select("*").eq("user_id", user_id).execute().data or []
    if len(all_entities) <= 1:
        raise HTTPException(status_code=400, detail="Can't delete your only business — add another first.")

    target = next((e for e in all_entities if e["id"] == entity_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Entity not found")

    sb.table("business_entities").delete().eq("id", entity_id).execute()

    if target.get("active"):
        next_active = next(e for e in all_entities if e["id"] != entity_id)
        sb.table("business_entities").update({"active": True}).eq("id", next_active["id"]).execute()
        _mirror_to_profile(sb, user_id, next_active)

    return {"ok": True}
