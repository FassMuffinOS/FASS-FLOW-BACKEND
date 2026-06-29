"""User profile routes."""
from fastapi import APIRouter, Depends, HTTPException
from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase
from app.cache import cache_get, cache_set, cache_delete

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/{user_id}/profile")
async def get_profile(user_id: str):
    """Deliberately public read — same display-name/company-name data
    already joined with no auth check in feed.py's /feed and chat.py's
    /profile/{other_user_id}. See PUBLIC_ALLOWLIST in security_scan.py."""
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
async def update_profile(user_id: str, data: dict, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only update your own profile")
    sb = get_supabase()
    result = sb.table("profiles").update(data).eq("id", user_id).execute()
    await cache_delete(f"profile:{user_id}")
    return result.data
