# FASS Flow — Backend API

## What this is

The FastAPI backend for FASS Flow, a federal contracting workflow tool for
small businesses. This service handles authentication helpers, subscription
management (Stripe), and an optional AI-assisted layer for solicitation
analysis and proposal drafting.

This backend is **not currently deployed**. The live product
(landing page, Stripe payment links, Supabase auth, dashboard) runs entirely
on the frontend + Supabase and does not depend on this service. See
[FassMuffinOS/FASS-FLOW](https://github.com/FassMuffinOS/FASS-FLOW) for the
deployed frontend.

## Live URL

Not deployed yet. No production URL exists for this service.

## Tech stack

- **Framework:** FastAPI + Uvicorn
- **Data:** Supabase (Postgres), accessed via service-role key for
  server-side operations
- **Caching:** Upstash Redis (REST API)
- **Payments:** Stripe (subscription objects, webhook secret configured but
  webhook handling itself is not yet implemented)
- **AI layer (optional):** a provider-agnostic LLM router (Anthropic /
  OpenAI / Gemini via raw HTTP calls, no vendor SDKs) plus a lightweight
  TF-IDF retrieval helper for grounding proposal drafts in a user's past
  performance. Every AI endpoint degrades gracefully with no key configured.

## Local setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill in real values, see below
uvicorn app.main:app --reload
```

Health check: `GET /health` → `{"status": "ok", "service": "fass-flow-api"}`

## Required environment variables

See `.env.example` for the full list. Summary:

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_ANON_KEY` | Yes | Supabase anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Server-side Supabase key — **never** expose to a client |
| `STRIPE_SECRET_KEY` | Yes | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | Yes | Stripe webhook signing secret (webhook handler not yet built — see Roadmap) |
| `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` / `STRIPE_PRICE_TEAM` | No | Stripe Price IDs, if/when tiered pricing is wired up |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Yes | Upstash Redis REST credentials |
| `FRONTEND_URL` | No | Used for CORS; defaults to local dev |
| `JWT_SECRET` | Yes | Used for any locally-issued tokens |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | No | Optional — AI endpoints no-op without at least one |
| `LLM_PROVIDER_ORDER` | No | Comma-separated fallback order for the above |

## What is real now

- FastAPI app scaffold with `auth`, `subscriptions`, `users`, and `ai`
  routers mounted under `/api/v1`
- LLM router with provider fallback (Anthropic → OpenAI → Gemini), raw HTTP,
  no SDK lock-in
- Lightweight pure-Python TF-IDF retrieval for ranking a user's past
  performance entries against a solicitation
- `/api/v1/analyze-solicitation` — hybrid regex + LLM solicitation parsing
  (regex stays source-of-truth for deterministic fields; LLM only fills gaps
  and adds judgment-call fields)
- `/api/v1/draft-section` — RAG-grounded proposal section drafting
- An eval harness (`app/evals/run_eval.py`) comparing regex-only vs.
  LLM-enhanced extraction against a hand-labeled gold set

## What is demo/mock now

- Nothing in this repo is mock data — the AI endpoints either call a real
  provider or return a clear "unavailable" response. What's missing is
  deployment and a live connection to the frontend.

## Current roadmap

- Choose a hosting platform (Railway or Fly.io under consideration) and
  deploy this service
- Wire the frontend's `VITE_API_URL` to point at the deployed instance
- Implement actual Stripe webhook handling for subscription lifecycle
  events — the secret is configured but no handler exists yet
- Run the eval harness against a larger gold set before treating the AI
  layer as portfolio-ready
