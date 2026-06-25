# FASS Flow Backend — Deployment Runbook

Goal: get this FastAPI backend live on Railway and connect the frontend to it,
so the AI features (R-E-A-D synthesis, FASS FILL analysis, Estimator scope-takeoff)
turn on. The code is deploy-ready — CORS already allows `flow.fass.systems`, there's
a `/health` endpoint, and `Procfile` + `requirements.txt` + `.python-version` are set.

---

## Step 1 — Link & deploy on Railway

From the backend folder:

```bash
cd ~/Desktop/r/08_GITHUB_REPOS/fass-flow-backend
railway login            # already done — orders@munchiesgourmets.com
railway link             # pick the existing fass-flow-backend project…
# …or if none exists:
railway init             # name it: fass-flow-backend
```

Railway builds Python automatically from `requirements.txt` + `Procfile`
(`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`).

---

## Step 2 — Set environment variables (REQUIRED to boot)

The app will not start without these (they have no defaults in `app/config.py`).
Set them in the Railway dashboard (Variables tab) or with `railway variables --set KEY=VALUE`.

REQUIRED:
- `SUPABASE_URL` — your Supabase project URL (project `qlqwmxzgjqnvfsqyshoi`)
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY` — Supabase dashboard → Project Settings → API
- `STRIPE_SECRET_KEY` — Stripe dashboard (test key `sk_test_…` is fine just to boot)
- `STRIPE_WEBHOOK_SECRET` — `whsec_…` (any value to boot; real one only needed for billing webhooks)
- `UPSTASH_REDIS_REST_URL` — free Upstash Redis DB → REST URL
- `UPSTASH_REDIS_REST_TOKEN` — from the same Upstash DB

FOR THE AI FEATURES (at least one):
- `ANTHROPIC_API_KEY` — recommended (matches the Haiku/Sonnet defaults)
- `OPENAI_API_KEY` and/or `GEMINI_API_KEY` — optional fallbacks
- `LLM_PROVIDER_ORDER` — e.g. `anthropic,openai,gemini`

RECOMMENDED:
- `FRONTEND_URL=https://flow.fass.systems`
- `SAM_GOV_API_KEY` — only if you want WARDOG's live feed through the backend
- `ADMIN_SECRET` — random string, only if you use `/admin/*`

---

## Step 3 — Deploy & get the public URL

```bash
railway up               # build + deploy this commit
railway domain           # generate/show the public URL
```

You'll get something like `https://fass-flow-backend-production.up.railway.app`.

Verify it's alive — open in a browser:
```
https://<your-railway-url>/health      →  {"status":"ok","service":"fass-flow-api"}
```

---

## Step 4 — Point the frontend at the backend

In Vercel (so the browser knows where the API is):

```bash
cd ~/Desktop/r/08_GITHUB_REPOS/fass-flow-frontend
vercel env add VITE_API_URL production
# paste your Railway URL (no trailing slash): https://<your-railway-url>
vercel --prod            # rebuild so the new env var is baked in
```

(Vite env vars are compiled in at build time, so you MUST redeploy after adding it.)

---

## Step 5 — Verify end to end

On `flow.fass.systems`:
1. Estimator → link a bid → "Read the solicitation" → the scope card appears with a
   job-type badge. (If the bid was saved from WARDOG it carries the real description.)
2. R-E-A-D and FASS FILL now show their AI panels too — the whole AI layer wakes up at once.

If the scope card still doesn't appear: open the browser console on the Estimator page.
- "Failed to fetch" → backend URL wrong or backend down (recheck `/health`).
- 503 from `/scope-takeoff` → no LLM key set on Railway (set `ANTHROPIC_API_KEY`).
- Card never renders → `VITE_API_URL` not baked in (re-run `vercel --prod` after adding it).

---

## Cost note
Railway (~$5/mo hobby), Upstash Redis (free tier fine), and per-call LLM cost
(Haiku is fractions of a cent per scope read). Stripe test keys cost nothing.
