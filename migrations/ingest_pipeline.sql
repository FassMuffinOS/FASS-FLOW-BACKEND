-- Ingest pipeline: one pipe that any input (Chrome capture extension,
-- email-forward webhook, manual upload) feeds, keyed by BPM ID, to create or
-- backfill an opportunity with the real solicitation content. Applied live on
-- 2026-06-27; this file records it so the repo matches production.

create table if not exists public.ingest_keys (
  user_id uuid primary key references auth.users(id) on delete cascade,
  key text not null unique,
  created_at timestamptz not null default now()
);

-- The captured solicitation body + extracted fields land on the inbox row
-- (keyed by bpm_id); when that row already has a linked proposal, the backend
-- also backfills proposals.description/naics_code so R-E-A-D et al. light up.
alter table public.solicitation_inbox add column if not exists captured_text text;
alter table public.solicitation_inbox add column if not exists naics text;
alter table public.solicitation_inbox add column if not exists set_aside text;
alter table public.solicitation_inbox add column if not exists captured_at timestamptz;

-- Parsed structured extras (set_aside, required_docs, page limits, etc.).
alter table public.proposals add column if not exists solicitation_meta jsonb;

-- ingest_keys is read/written only by the backend service-role client (the
-- capture key is the extension's bearer credential), so RLS stays on with no
-- policies — the anon client never touches it.
alter table public.ingest_keys enable row level security;
