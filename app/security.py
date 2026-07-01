"""Pre-launch hardening: rate limiting, origin enforcement, and structured
request logging, all as ASGI middleware so every route is covered without
touching each router file individually.

Context: `slowapi` (the usual FastAPI rate-limit library) can't be installed
in this environment — no outbound package-registry access, confirmed via
repeated `pip install ... --break-system-packages` failures. Everything
below is hand-rolled on top of the Upstash Redis connection this app
already has wired up in app/cache.py, which is already a live dependency
(upstash-redis==1.1.0 in requirements.txt), so nothing new needs installing.

What this does NOT do: it is not a WAF, not a firewall, not DDoS scrubbing.
Real network/edge DDoS protection lives in front of Railway (Cloudflare or
Railway's own edge), not in application code — no amount of Python here
stops a volumetric flood before it reaches this process. What IS in scope
for app code, and what's implemented here:
  - per-IP request throttling (this file: RateLimitMiddleware)
  - rejecting browser requests whose Origin isn't one of our own domains
    (this file: OriginEnforcementMiddleware)
  - structured access logging with request IDs, so abuse/errors are
    actually visible after the fact (this file: RequestLogMiddleware)
  - a global exception handler so unhandled errors return a clean 500
    instead of leaking a stack trace to the client (main.py wires this up
    using log_unhandled_exception below)
"""
import logging
import re
import time
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.cache import get_redis, cache_get, cache_set

logger = logging.getLogger("fass")


def configure_logging() -> None:
    """Plain, greppable structured-ish logging to stdout — Railway captures
    stdout and makes it searchable, so no external logging service is
    required to get real monitoring out of this. Idempotent (safe to call
    more than once, e.g. under a reloader) via the `if not handlers` guard."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Shared origin allow-list — the same set main.py's CORSMiddleware uses, kept
# here as the single source of truth so origin-enforcement and CORS can
# never silently drift apart from each other.
# ---------------------------------------------------------------------------
def allowed_origins() -> list[str]:
    from app.config import settings
    return [
        settings.frontend_url,
        "https://flow.fass.systems",
        "https://regulars.fass.systems",
        "https://affiliates.fass.systems",
        "https://fass.systems",
        "https://fassflow.com",
        "https://fass-flow-frontend.vercel.app",
    ]


# 2026-07-01, subdomain rollout: a fixed list can't keep up with every
# white-label tenant getting its own {slug}.fass.systems subdomain (see
# app/routers/tenants.py) — new tenants would need a code deploy just to
# be allowed to call their own API. Matching any *.fass.systems origin by
# pattern instead covers flow/regulars/affiliates AND every future tenant
# subdomain automatically. A tenant's fully custom domain (tenants.
# custom_domain) still isn't covered by this — that's a real gap, but it
# only matters once a tenant actually points their own DNS at us, which is
# a distinct, later step (see tenants.py's Phase 1 scope note).
_FASS_SYSTEMS_ORIGIN_RE = re.compile(r"^https://([a-z0-9-]+\.)*fass\.systems$")


def is_allowed_origin(origin: str) -> bool:
    if origin in allowed_origins():
        return True
    return bool(_FASS_SYSTEMS_ORIGIN_RE.match(origin))


# Endpoints hit by other servers, not browsers — Stripe/Twilio/Apple never
# send an Origin header that would match our frontend, and they carry their
# own signature verification inside the handler (Stripe-Signature header,
# Twilio request signature, Apple device auth token), so origin-checking
# them would only ever break legitimate webhook delivery.
ORIGIN_EXEMPT_PATHS = {
    "/health",
    "/api/v1/webhook",
    "/api/v1/comms/twilio/inbound",
}
ORIGIN_EXEMPT_PREFIXES = ("/api/v1/wallet_passkit/",)


class OriginEnforcementMiddleware(BaseHTTPMiddleware):
    """Rejects state-changing browser requests whose declared Origin isn't
    one of our own domains.

    Honest limits: the Origin header is a browser-enforced convention, not a
    cryptographic proof — a non-browser client (curl, a script, Postman) can
    set any Origin it wants or omit it entirely, and this check can't stop
    that. Real per-resource authorization still comes from
    auth_deps.get_current_user + require_owner, which verify the actual
    Supabase session token; that's the real security boundary. This
    middleware is a cheap, real second layer against a malicious *website*
    (running in someone else's browser tab) making state-changing fetch()
    calls at our API — the literal ask was "api requests only from our own
    domain," and this is what that means for a bearer-token API with no
    ambient cookie auth to CSRF in the first place.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            request.method in ("GET", "HEAD", "OPTIONS")
            or path in ORIGIN_EXEMPT_PATHS
            or path.startswith(ORIGIN_EXEMPT_PREFIXES)
        ):
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin and not is_allowed_origin(origin):
            logger.warning("origin_rejected origin=%s path=%s ip=%s", origin, path, _client_ip(request))
            return JSONResponse(status_code=403, content={"detail": "Requests from this origin are not allowed."})

        return await call_next(request)


# ---------------------------------------------------------------------------
# IP blocking — a manual blocklist an admin controls from the Security
# Dashboard (see app/routers/admin.py's /admin/ip-blocks endpoints and
# frontend SecurityDashboard.jsx). Stored as one JSON list under a single
# persistent Redis key rather than per-IP keys, since the upstash-redis
# client's SET/SADD-family commands weren't confirmed available in this
# environment (no network access to verify against the installed package),
# while get/set/incr/expire are all confirmed working and already proven
# live elsewhere in this codebase (cache.py, used throughout wardog.py,
# intelligence.py, credits.py). A few hundred blocked IPs in one JSON blob
# is trivial for Redis either way.
# ---------------------------------------------------------------------------
BLOCKED_IPS_KEY = "security:blocked_ips"


async def list_blocked_ips() -> list[dict]:
    data = await cache_get(BLOCKED_IPS_KEY)
    return data or []


async def block_ip(ip: str, reason: str = "") -> list[dict]:
    ips = [e for e in await list_blocked_ips() if e.get("ip") != ip]
    ips.append({"ip": ip, "reason": reason, "blocked_at": time.time()})
    await cache_set(BLOCKED_IPS_KEY, ips, ex=None)
    return ips


async def unblock_ip(ip: str) -> list[dict]:
    ips = [e for e in await list_blocked_ips() if e.get("ip") != ip]
    await cache_set(BLOCKED_IPS_KEY, ips, ex=None)
    return ips


class IPBlockMiddleware(BaseHTTPMiddleware):
    """Runs before rate limiting — a blocked IP should be rejected outright,
    not merely throttled. Fails OPEN on a Redis error, same posture as
    RateLimitMiddleware below: an availability blip in the caching layer
    should never itself become an outage."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)

        ip = _client_ip(request)
        try:
            blocked = await list_blocked_ips()
            if any(e.get("ip") == ip for e in blocked):
                logger.warning("blocked_ip_rejected ip=%s path=%s", ip, path)
                return JSONResponse(status_code=403, content={"detail": "Access denied."})
        except Exception:
            logger.exception("ip_block_check_failed path=%s", path)
            # fail open — see docstring

        return await call_next(request)


# ---------------------------------------------------------------------------
# Rate limiting — fixed-window counters in Redis, per client IP.
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    # Railway sits in front of this process as a reverse proxy, so the real
    # client IP arrives via X-Forwarded-For, not request.client.host (which
    # would just be Railway's internal proxy address).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# path prefix -> (max requests, window seconds). Anything not listed here
# falls under DEFAULT_LIMIT. Tight limits are on account-creation/credential
# endpoints specifically — the ones bots and credential-stuffing scripts
# actually hammer — not on normal app usage.
TIGHT_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/auth/signup": (5, 60),
    "/api/v1/auth/signin": (10, 60),
    "/api/v1/regulars/signup": (5, 60),
    "/api/v1/affiliates/apply": (5, 60),
    "/api/v1/affiliates/apply-oauth": (5, 60),
    "/api/v1/settings/account/password": (5, 60),
}
DEFAULT_LIMIT = (240, 60)  # general API traffic, per IP, per minute

RATE_LIMIT_EXEMPT_PATHS = {"/health"}


def _tier(path: str) -> tuple[str, tuple[int, int]]:
    for prefix, limit in TIGHT_LIMITS.items():
        if path.startswith(prefix):
            return prefix, limit
    return "default", DEFAULT_LIMIT


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fails OPEN: if Upstash is slow or unreachable, requests pass through
    rather than taking the whole API down over a caching-layer blip — same
    best-effort posture app/cache.py already has everywhere else it's used.
    Redis calls run in a threadpool (via run_in_threadpool) since the
    upstash-redis client is synchronous under the hood and this middleware
    runs on every single request; blocking the event loop here would be a
    much worse problem than the one it's solving.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)

        tier_key, (max_requests, window) = _tier(path)
        ip = _client_ip(request)
        bucket = int(time.time() // window)
        key = f"rl:{ip}:{tier_key}:{bucket}"

        try:
            r = get_redis()
            count = await run_in_threadpool(r.incr, key)
            if count == 1:
                await run_in_threadpool(r.expire, key, window)
            if count > max_requests:
                logger.warning("rate_limit_exceeded ip=%s path=%s tier=%s count=%s", ip, path, tier_key, count)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down and try again shortly."},
                    headers={"Retry-After": str(window)},
                )
        except Exception:
            logger.exception("rate_limit_check_failed path=%s", path)
            # fail open — see docstring

        return await call_next(request)


# ---------------------------------------------------------------------------
# Structured access logging — every request gets a request id, method,
# path, status, duration, and client ip in one greppable line. This is the
# "logging and monitoring" piece: it doesn't require standing up a new
# service, just makes Railway's existing stdout log stream actually useful
# for spotting abuse, slow endpoints, and error spikes after the fact.
# ---------------------------------------------------------------------------
class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.exception(
                "request_error request_id=%s method=%s path=%s ip=%s duration_ms=%s",
                request_id, request.method, request.url.path, _client_ip(request), duration_ms,
            )
            raise
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        log_fn = logger.warning if response.status_code >= 500 else (
            logger.info if response.status_code < 400 else logger.warning
        )
        log_fn(
            "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%s ip=%s",
            request_id, request.method, request.url.path, response.status_code, duration_ms, _client_ip(request),
        )
        response.headers["X-Request-ID"] = request_id
        return response


async def log_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Registered in main.py as the catch-all @app.exception_handler(Exception).
    Without this, an unhandled exception in a route returns FastAPI's debug
    traceback (or, on some ASGI setups, a bare connection reset) — either
    leaks internals or gives the client nothing actionable. This logs the
    full traceback server-side (via logger.exception, called from within the
    RequestLogMiddleware except-block above, which re-raises after logging)
    and returns one clean, generic message to the client."""
    request_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our end. Please try again.", "request_id": request_id},
    )
