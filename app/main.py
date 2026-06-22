from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import auth, subscriptions, users, ai

app = FastAPI(
    title="FASS Flow API",
    description="Government contracting SaaS platform — API",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "https://fassflow.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router,          prefix="/api/v1")
app.include_router(subscriptions.router, prefix="/api/v1")
app.include_router(users.router,         prefix="/api/v1")
app.include_router(ai.router,            prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "fass-flow-api"}
