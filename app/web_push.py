"""Thin wrapper around pywebpush for Messenger's "true" browser push —
delivered even when the tab/app is fully closed, via the browser's push
service + a service worker (frontend: public/sw.js).

Mirrors this codebase's established blank-default pattern (Apple Wallet,
Twilio): with no VAPID keys configured, send_push() just logs and returns
instead of raising, so the rest of the app works untouched until Railway's
VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY are set.
"""
import json
import logging

from pywebpush import webpush, WebPushException

from app.config import settings
from app.database import get_supabase

logger = logging.getLogger(__name__)


def push_enabled() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def send_push_to_user(user_id: str, payload: dict) -> None:
    """Sends `payload` (will be JSON-stringified) to every push subscription
    on file for user_id. Silently drops a subscription that the browser has
    invalidated (HTTP 404/410 from the push service) by deleting its row —
    same "self-healing" approach as letting a bounced webhook clean itself up.
    """
    if not push_enabled():
        logger.info("web_push: VAPID keys not configured, skipping push to %s", user_id)
        return

    sb = get_supabase()
    subs = (
        sb.table("push_subscriptions")
        .select("id, endpoint, p256dh, auth_key")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth_key"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
                sb.table("push_subscriptions").delete().eq("id", sub["id"]).execute()
            else:
                logger.warning("web_push: failed to deliver to %s: %s", sub["endpoint"], exc)
