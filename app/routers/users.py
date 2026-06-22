"""User profile routes."""
from fastapi import APIRouter, HTTPException
from app.database import get_supabase
from app.cache import cache_get, cache_set, cache_delete

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/{user_id}/profile")
async def get_profile(user_id: str):
    cache_key = f"profile:{user_id}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found")

    await cache_set(cache_key, result.data, ex=120)
    return result.data


@router.patch("/{user_id}/profile")
async def update_profile(user_id: str, data: dict):
    sb = get_supabase()
    result = sb.table("profiles").update(data).eq("id", user_id).execute()
    await cache_delete(f"profile:{user_id}")
    return result.data
