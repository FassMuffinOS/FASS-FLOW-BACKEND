from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import auth, subscriptions, users, ai, admin, wardog, network, business_lookup

app = FastAPI(
    title="FASS Flow API",
    description="Government contracting SaaS platform — API",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    # settings.frontend_url comes from Railway's FRONTEND_URL env var and may
    # be unset or stale, so the live production domain is also hardcoded here
    # as a fallback — a missing/wrong Railway var otherwise silently blocks
    # every browser request with a generic "Failed to fetch" and no useful
    # error message on either side.
    allow_origins=[
        settings.frontend_url,
        "https://flow.fass.systems",
        "https://fassflow.com",
        "https://fass-flow-frontend.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router,          prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(users.router,         prefix="/api/v1")
app.include_router(ai.router,            prefix="/api/v1")
app.include_router(admin.router,         prefix="/api/v1")
app.include_router(wardog.router,        prefix="/api/v1")
app.include_router(network.router,       prefix="/api/v1")
app.include_router(business_lookup.router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fass-flow-api"}
