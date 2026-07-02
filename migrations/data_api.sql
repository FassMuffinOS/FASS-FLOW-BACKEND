-- FASS Data API — external, B2B programmatic access to data FASS creates
-- (starting with WARDOG Intel's incumbent/award-history synthesis). This is
-- deliberately a parallel system to profiles/ai_credits: buyers here are
-- OUTSIDE companies authenticating with an API key, not logged-in FASS Flow
-- users authenticating with a Supabase session — see app/auth_deps.py's
-- require_api_key and app/routers/data_api.py.

create table if not exists data_api_customers (
  id uuid primary key default gen_random_uuid(),
  company_name text not null,
  contact_email text not null,
  stripe_customer_id text,
  created_at timestamptz not null default now()
);

-- Keys are never stored in plaintext — only a sha256 hash, checked at auth
-- time. key_prefix (e.g. "fk_live_ab12") is stored so the customer can
-- recognize which key is which in a list without us ever holding the full
-- secret again after creation, same convention GitHub/Stripe use.
create table if not exists data_api_keys (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references data_api_customers(id) on delete cascade,
  key_hash text not null unique,
  key_prefix text not null,
  environment text not null default 'live' check (environment in ('live', 'test')),
  name text,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);
create index if not exists data_api_keys_customer_idx on data_api_keys(customer_id);

-- One row per customer. balance = pay-per-call credits (each metered call
-- debits 1, regardless of plan). plan/plan_quota/plan_period_* track an
-- optional monthly subscription's included-call allowance, refreshed by the
-- Stripe webhook on invoice.paid — a call is allowed if EITHER the plan
-- quota for the current period isn't exhausted OR balance > 0 (quota is
-- tried first so paid-up subscribers never burn pay-per-call credits
-- unnecessarily).
create table if not exists data_api_credits (
  customer_id uuid primary key references data_api_customers(id) on delete cascade,
  balance integer not null default 0,
  plan text,
  plan_quota integer,
  plan_used integer not null default 0,
  plan_period_start timestamptz,
  plan_period_end timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists data_api_credit_ledger (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references data_api_customers(id) on delete cascade,
  delta integer not null,
  reason text not null,
  balance_after integer not null,
  external_ref text,
  created_at timestamptz not null default now()
);
create unique index if not exists data_api_credit_ledger_external_ref_idx
  on data_api_credit_ledger(external_ref) where external_ref is not null;

-- Lightweight per-call log — powers a usage dashboard later and is useful
-- for debugging/support right now. Not a replacement for the ledger (which
-- is the source of truth for billing), just an audit trail of what was hit.
create table if not exists data_api_usage_log (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references data_api_customers(id) on delete cascade,
  api_key_id uuid references data_api_keys(id) on delete set null,
  endpoint text not null,
  status_code integer not null,
  created_at timestamptz not null default now()
);
create index if not exists data_api_usage_log_customer_idx on data_api_usage_log(customer_id, created_at desc);
