"""
AI usage quota — enforces the Lite plan's "1 AI synthesis per billing
cycle" cap. Every other plan (starter/pro/team/promo) is unlimited and
this is a no-op for them.

Deliberately simple rather than a real metering system: one counter
(`ai_quota_used`) and one rolling reset timestamp (`ai_quota_reset_at`)
on `profiles`, checked and incremented atomically-enough for this app's
traffic (a single read-then-write; fine at this scale, would need a
Postgres function with `for update` locking if AI calls ever became
high-concurrency per user, which they won't on a 1-call-per-30-days plan).
"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.database import get_supabase

CYCLE_DAYS = 30
LITE_PLAN_LIMIT = 1


class QuotaExceededError(HTTPException):
    def __init__(self, reset_at: datetime):
        super().__init__(
            status_code=402,
            detail=(
                "You've used this billing cycle's free AI synthesis on the Lite plan. "
                f"It resets {reset_at.date().isoformat()}, or upgrade to Core for unlimited AI synthesis."
            ),
        )


def check_and_consume_ai_quota(user_id: str | None) -> None:
    """Call this at the top of any AI endpoint that should be quota-limited
    on the Lite plan. Raises QuotaExceededError (402) if the Lite user is
    out of quota for the current cycle; otherwise increments their usage
    and returns silently. No-op if user_id is missing (caller didn't pass
    one) or the profile isn't on the Lite plan."""
    if not user_id:
        return  # nothing to gate without a user_id — caller didn't send one

    sb = get_supabase()
    result = (
        sb.table("profiles")
        .select("plan, ai_quota_used, ai_quota_reset_at")
        .eq("id", user_id)
        .single()
        .execute()
    )
    profile = result.data
    if not profile or profile.get("plan") != "lite":
        return  # unlimited on every other plan

    now = datetime.now(timezone.utc)
    reset_at_raw = profile.get("ai_quota_reset_at")
    reset_at = datetime.fromisoformat(reset_at_raw) if reset_at_raw else None

    # First call ever, or a past cycle has elapsed — start a fresh cycle.
    if reset_at is None or now >= reset_at:
        new_reset_at = now + timedelta(days=CYCLE_DAYS)
        sb.table("profiles").update({
            "ai_quota_used": 1,
            "ai_quota_reset_at": new_reset_at.isoformat(),
        }).eq("id", user_id).execute()
        return

    used = profile.get("ai_quota_used") or 0
    if used >= LITE_PLAN_LIMIT:
        raise QuotaExceededError(reset_at)

    sb.table("profiles").update({"ai_quota_used": used + 1}).eq("id", user_id).execute()
