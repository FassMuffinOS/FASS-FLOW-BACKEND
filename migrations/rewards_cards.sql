-- FASS Rewards — restaurant/business loyalty stamp cards.
-- Run this in the Supabase SQL editor.

create table if not exists reward_programs (
  business_user_id uuid primary key references auth.users(id) on delete cascade,
  business_name text not null,
  reward_threshold int not null default 10,
  reward_description text,
  bg_color text default '#0f5132',
  logo_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists reward_cards (
  slug text primary key,
  business_user_id uuid not null references reward_programs(business_user_id) on delete cascade,
  customer_name text,
  customer_contact text,
  stamps int not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_reward_cards_business on reward_cards(business_user_id);

-- Keep updated_at current on both tables.
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_reward_programs_updated_at on reward_programs;
create trigger trg_reward_programs_updated_at
  before update on reward_programs
  for each row execute function set_updated_at();

drop trigger if exists trg_reward_cards_updated_at on reward_cards;
create trigger trg_reward_cards_updated_at
  before update on reward_cards
  for each row execute function set_updated_at();

-- RLS: service role (used by the backend) bypasses these automatically;
-- these policies just make sure nothing accidentally exposes data if a
-- client ever queries these tables directly with the anon key.
alter table reward_programs enable row level security;
alter table reward_cards enable row level security;

drop policy if exists "Owners manage their own program" on reward_programs;
create policy "Owners manage their own program" on reward_programs
  for all using (auth.uid() = business_user_id);

drop policy if exists "Owners manage their own cards" on reward_cards;
create policy "Owners manage their own cards" on reward_cards
  for all using (auth.uid() = business_user_id);
