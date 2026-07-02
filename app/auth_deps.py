"""Real session authentication for protected routes.

Background: a security review on 2026-06-29 found that most routers in
this app trusted a client-supplied `user_id` / `business_user_id` (from a
query param or request body) with no check that the caller was actually
logged in as that user. The Supabase client also runs on the service-role
key everywhere (see database.py), which bypasses Postgres RLS — so the
client-supplied ID was the *only* access control in the system. Concrete
exploit: anyone who knew a gift card's slug + business_user_id could
drain its balance via POST /giftcards/redeem with no login at all.

This module adds the missing piece: a FastAPI dependency that verifies the
`Authorization: Bearer <supabase access token>` header against Supabase
Auth and returns the *actual* authenticated user. Routers that handle
private, owner-scoped data should depend on `get_current_user` and compare
its `.id` against the resource being accessed — never trust a body/query
user_id for authorization decisions, only for things truly meant to be
public (see gift_cards.py's /lookup, /pass, /business, /purchase/*, which
are deliberately public storefront/QR endpoints and are NOT changed here).
"""
import hashlib
import secrets
from datetime import datetime, timezone

from fastapi import Header, HTTPException
from app.database import get_supabase, single_data


class CurrentUser:
    """Minimal shape callers need. Avoids passing the raw Supabase
    UserResponse object around everywhere."""

    def __init__(self, id: str, email: str | None = None):
        self.id = id
        self.email = email


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """Verifies the bearer token against Supabase Auth. Raises 401 if
    missing, malformed, or invalid/expired. Use as a route dependency:

        @router.post("/redeem")
        async def redeem_gift_card(body: RedeemGiftCardRequest,
                                    current_user: CurrentUser = Depends(get_current_user)):
            if current_user.id != body.business_user_id:
                raise HTTPException(status_code=403, detail="Not your gift card program")
            ...
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    sb = get_supabase()
    try:
        resp = sb.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user = getattr(resp, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return CurrentUser(id=user.id, email=getattr(user, "email", None))


def require_owner(current_user: CurrentUser, resource_user_id: str, detail: str = "Not authorized for this resource"):
    """Shared guard: raise 403 unless the authenticated caller owns the
    resource being accessed. Centralized so the same message/behavior is
    used everywhere instead of each router rolling its own check."""
    if current_user.id != resource_user_id:
        raise HTTPException(status_code=403, detail=detail)


# --- FASS Data API — external B2B key auth --------------------------------
#
# Separate from everything above. get_current_user/CurrentUser authenticate
# a logged-in FASS Flow *user* via a Supabase session. The Data API sells
# programmatic access to data FASS creates (starting with WARDOG Intel) to
# outside companies who are not FASS Flow users at all — they hold a
# long-lived `fk_live_`/`fk_test_` key instead, matching the format already
# documented on fass-app-docs. See migrations/data_api.sql for the schema
# and app/routers/data_api.py for the endpoints this gates.

API_KEY_PREFIX_LIVE = "fk_live_"
API_KEY_PREFIX_TEST = "fk_test_"


class DataAPICustomer:
    """Minimal shape route handlers need for an authenticated external
    Data API caller."""

    def __init__(self, customer_id: str, company_name: str, key_id: str, environment: str):
        self.customer_id = customer_id
        self.company_name = company_name
        self.key_id = key_id
        self.environment = environment


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(environment: str = "live") -> tuple[str, str, str]:
    """Mints a new key. Returns (raw_key, key_hash, key_prefix) — raw_key is
    shown to the caller exactly once (at issuance); only key_hash is ever
    persisted, same as a password. key_prefix is a short, safe-to-store
    slice of the raw key so a customer can recognize which key is which in
    a list without us retaining the ability to reconstruct the secret."""
    prefix = API_KEY_PREFIX_LIVE if environment == "live" else API_KEY_PREFIX_TEST
    raw_key = f"{prefix}{secrets.token_urlsafe(32)}"
    return raw_key, _hash_key(raw_key), raw_key[:16]


async def require_api_key(authorization: str | None = Header(default=None)) -> DataAPICustomer:
    """Verifies `Authorization: Bearer fk_live_...` / `fk_test_...` against
    data_api_keys. Raises 401 if missing, malformed, unknown, or revoked.
    Updates last_used_at best-effort (never blocks the request on that
    write failing)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    raw_key = authorization.split(" ", 1)[1].strip()
    if not raw_key.startswith(API_KEY_PREFIX_LIVE) and not raw_key.startswith(API_KEY_PREFIX_TEST):
        raise HTTPException(status_code=401, detail="Malformed API key")

    sb = get_supabase()
    key_row = single_data(
        sb.table("data_api_keys")
        .select("id, customer_id, environment, revoked_at")
        .eq("key_hash", _hash_key(raw_key))
        .maybe_single()
        .execute()
    )
    if not key_row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if key_row.get("revoked_at"):
        raise HTTPException(status_code=401, detail="This API key has been revoked")

    customer = single_data(
        sb.table("data_api_customers")
        .select("id, company_name")
        .eq("id", key_row["customer_id"])
        .maybe_single()
        .execute()
    )
    if not customer:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        sb.table("data_api_keys").update(
            {"last_used_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", key_row["id"]).execute()
    except Exception:
        pass  # best-effort — never fail auth over a bookkeeping write

    return DataAPICustomer(
        customer_id=customer["id"],
        company_name=customer["company_name"],
        key_id=key_row["id"],
        environment=key_row["environment"],
    )
