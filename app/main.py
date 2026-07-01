from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.security import (
    configure_logging,
    allowed_origins,
    OriginEnforcementMiddleware,
    IPBlockMiddleware,
    RateLimitMiddleware,
    RequestLogMiddleware,
    log_unhandled_exception,
)
from app.routers import auth, subscriptions, users, ai, admin, wardog, network, business_lookup, wallet, wallet_passkit, wallet_campaigns, rewards, business_profile, gift_cards, stripe_connect, notebook, partners, chat, comms, bd_partner, affiliates, profiles, ingest, feed, careers, growth_challenge, reuse_library, proposal_docs, credits, settings as settings_router, intelligence, regulars, tenants

configure_logging()

app = FastAPI(
    title="FASS Flow API",
    description="Government contracting SaaS platform — API",
    version="1.0.0",
)

# Global error handler — unhandled exceptions return one clean generic 500
# instead of leaking a stack trace. Full traceback still gets logged
# server-side (see RequestLogMiddleware's except-block, which logs before
# re-raising so this handler can format the client-facing response).
app.add_exception_handler(Exception, log_unhandled_exception)

# Middleware order matters: Starlette runs these outside-in on the request
# and inside-out on the response, so whatever's added LAST here runs FIRST
# on the way in. RequestLogMiddleware is added last so it wraps everything
# (logs every request, including ones RateLimit/Origin/CORS reject) and
# still gets an accurate duration for the full round trip.
app.add_middleware(CORSMiddleware,
    # settings.frontend_url comes from Railway's FRONTEND_URL env var and may
    # be unset or stale, so the live production domain is also hardcoded here
    # as a fallback — a missing/wrong Railway var otherwise silently blocks
    # every browser request with a generic "Failed to fetch" and no useful
    # error message on either side.
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(OriginEnforcementMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(IPBlockMiddleware)
app.add_middleware(RequestLogMiddleware)

# Routers
app.include_router(auth.router,          prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(users.router,         prefix="/api/v1")
app.include_router(ai.router,            prefix="/api/v1")
app.include_router(admin.router,         prefix="/api/v1")
app.include_router(wardog.router,        prefix="/api/v1")
app.include_router(network.router,       prefix="/api/v1")
app.include_router(business_lookup.router, prefix="/api/v1")
app.include_router(wallet.router,        prefix="/api/v1")
app.include_router(wallet_passkit.router, prefix="/api/v1")
app.include_router(wallet_campaigns.router, prefix="/api/v1")
app.include_router(rewards.router,       prefix="/api/v1")
app.include_router(business_profile.router, prefix="/api/v1")
app.include_router(gift_cards.router,    prefix="/api/v1")
app.include_router(stripe_connect.router, prefix="/api/v1")
app.include_router(notebook.router,      prefix="/api/v1")
app.include_router(partners.router,      prefix="/api/v1")
app.include_router(chat.router,          prefix="/api/v1")
app.include_router(comms.router,         prefix="/api/v1")
app.include_router(bd_partner.router,    prefix="/api/v1")
app.include_router(affiliates.router,    prefix="/api/v1")
app.include_router(profiles.router,      prefix="/api/v1")
app.include_router(ingest.router,        prefix="/api/v1")
app.include_router(feed.router,          prefix="/api/v1")
app.include_router(careers.router,       prefix="/api/v1")
app.include_router(growth_challenge.router, prefix="/api/v1")
app.include_router(reuse_library.router, prefix="/api/v1")
app.include_router(proposal_docs.router, prefix="/api/v1")
app.include_router(credits.router, prefix="/api/v1")
app.include_router(settings_router.router, prefix="/api/v1")
app.include_router(intelligence.router, prefix="/api/v1")
app.include_router(regulars.router, prefix="/api/v1")
app.include_router(tenants.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fass-flow-api"}
