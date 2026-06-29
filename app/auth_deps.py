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
from fastapi import Header, HTTPException
from app.database import get_supabase


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
