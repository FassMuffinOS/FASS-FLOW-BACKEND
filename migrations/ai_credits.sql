-- AI credits — meter every AI draft against a balance, monetize refills.
-- Each "Draft with AI" / "Re-draft" / "Draft all" press in the Proposal
-- Editor consumes 1 credit (see ai.py draft-section). New users get a free
-- starter allotment; refills are granted via /credits/grant (admin-secret,
-- honor-system for beta — same trust model as the Support page; swap in
-- Stripe later without touching this schema). Run in the Supabase SQL editor.

create extension if not exists pgcrypto;

create table if not exists ai_credits (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  balance      int  not null default 0,
  free_granted boolean not null default false,  -- got the one-time starter grant?
  updated_at   timestamptz not null default now()
);

-- Every change is recorded, so balances are auditable and a future Stripe
-- purchase / subscription grant is just another ledger row.
create table if not exists ai_credit_ledger (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  delta        int  not null,            -- + grant/refill, − consumption
  reason       text not null,            -- 'free_starter' | 'proposal_draft' | 'refill' | ...
  balance_after int,
  created_at   timestamptz not null default now()
);
create index if not exists idx_ai_credit_ledger_user on ai_credit_ledger(user_id, created_at desc);

create or replace function set_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_ai_credits_updated_at on ai_credits;
create trigger trg_ai_credits_updated_at
  before update on ai_credits
  for each row execute function set_updated_at();

alter table ai_credits enable row level security;
drop policy if exists "Owners read their own credits" on ai_credits;
create policy "Owners read their own credits" on ai_credits
  for all using (auth.uid() = user_id);

alter table ai_credit_ledger enable row level security;
drop policy if exists "Owners read their own credit ledger" on ai_credit_ledger;
create policy "Owners read their own credit ledger" on ai_credit_ledger
  for all using (auth.uid() = user_id);
