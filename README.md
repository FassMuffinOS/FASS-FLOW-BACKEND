# FASS Flow — Backend API

## What this is

The FastAPI backend for FASS Flow, a federal contracting workflow tool for
small businesses. This service handles authentication helpers, subscription
management (Stripe), and an optional AI-assisted layer for solicitation
analysis and proposal drafting.

This backend **is deployed**, on Railway. The frontend's `VITE_API_URL`
points at the live Railway instance and depends on it for WARDOG's live
SAM.gov proxy, AI synthesis/draft endpoints, and Stripe subscription
checkout + webhook handling. See
[FassMuffinOS/FASS-FLOW](https://github.com/FassMuffinOS/FASS-FLOW) for the
frontend that consumes this API.

## Live URL

Deployed on Railway. The production base URL is set in the frontend's
`VITE_API_URL` (Vercel env var) — check there or in the Railway project
dashboard for the exact current URL, since Railway can reassign it on
redeploy unless a custom domain is attached.

## Tech stack

- **Framework:** FastAPI + Uvicorn
- **Data:** Supabase (Postgres), accessed via service-role key for
  server-side operations
- **Caching:** Upstash Redis (REST API)
- **Payments:** Stripe — Checkout sessions, customer portal, and a webhook
  handler (`/api/v1/subscriptions/webhook`) covering
  `checkout.session.completed`, `customer.subscription.created/updated`,
  `customer.subscription.deleted`, and invoice payment events, syncing
  `profiles.plan` / `profiles.subscription_status` in Supabase
- **AI layer (optional):** a provider-agnostic LLM router (Anthropic /
  OpenAI / Gemini / DeepSeek via raw HTTP calls, no vendor SDKs) plus a
  lightweight TF-IDF retrieval helper for grounding proposal drafts in a
  user's past performance. Every AI endpoint degrades gracefully with no
  key configured.
- **WARDOG proxy:** `/api/v1/wardog/search` calls the real SAM.gov
  opportunities API server-side (so the SAM.gov key never reaches the
  browser) and returns 503 when no key is configured for that deploy,
  which the frontend treats as a signal to fall back to sample data.

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
| `STRIPE_WEBHOOK_SECRET` | Yes | Stripe webhook signing secret, used by the live handler at `/api/v1/subscriptions/webhook` |
| `STRIPE_PRICE_LITE` / `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` / `STRIPE_PRICE_TEAM` | No | Stripe Price IDs for each tiered plan |
| `SAM_GOV_API_KEY` | No | SAM.gov opportunities API key. Without it, `/api/v1/wardog/search` returns 503 and the frontend falls back to sample data. |
| `GOOGLE_PLACES_API_KEY` | No | Google Places API (New) server key, used by `/api/v1/business/lookup` for the Passport "Find my business" search. Requires a Google Cloud project with billing enabled and "Places API (New)" turned on; restrict the key to server IPs, not HTTP referrers, since it's only ever called from this backend. Without it, the endpoint returns 503 and Passport's quick-setup falls back to manual entry. |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Yes | Upstash Redis REST credentials |
| `FRONTEND_URL` | No | Used for CORS; defaults to local dev |
| `JWT_SECRET` | Yes | Used for any locally-issued tokens |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | No | Optional — AI endpoints no-op without at least one |
| `LLM_PROVIDER_ORDER` | No | Comma-separated fallback order for the above |

## What is real now

- FastAPI app deployed on Railway, with `auth`, `subscriptions`, `users`,
  `ai`, `admin`, `wardog`, and `network` routers mounted under `/api/v1`
- `/api/v1/wardog/search` — live SAM.gov opportunities proxy consumed by the
  frontend's WARDOG page
- Stripe Checkout + customer portal + webhook handler covering the full
  subscription lifecycle (created/updated/deleted, invoice payment events),
  syncing plan/status to `profiles` in Supabase
- LLM router with provider fallback (Anthropic → DeepSeek → OpenAI →
  Gemini), raw HTTP, no SDK lock-in
- Lightweight pure-Python TF-IDF retrieval for ranking a user's past
  performance entries against a solicitation
- `/api/v1/analyze-solicitation` — hybrid regex + LLM solicitation parsing
  (regex stays source-of-truth for deterministic fields; LLM only fills gaps
  and adds judgment-call fields)
- `/api/v1/draft-section` — RAG-grounded proposal section drafting
- `/api/v1/ai/read-synthesis` — per-section AI synthesis grounding R-E-A-D's
  worksheet guidance in the actual solicitation text
- An eval harness (`app/evals/run_eval.py`) comparing regex-only vs.
  LLM-enhanced extraction against a hand-labeled gold set

## What is demo/mock now

- Nothing in this repo is mock data — the AI endpoints either call a real
  provider or return a clear "unavailable" response. WARDOG's sample-data
  fallback lives in the frontend, not here, and only activates if this
  service is unreachable or `SAM_GOV_API_KEY` is unset.

## Current roadmap

- Attach a custom domain to the Railway deployment so the API URL doesn't
  shift on redeploy
- Run the eval harness against a larger gold set before treating the AI
  layer as fully validated
- Add monitoring/alerting on the Stripe webhook endpoint (signature
  failures, unhandled event types) now that real subscriptions depend on it
