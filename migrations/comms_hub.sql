-- Comms Hub: a message queue/timeline for the self-hosted iMessage/SMS
-- relay ("our own Sendblue"). The backend never talks to Apple directly —
-- it only queues outbound messages and accepts inbound ones. A Mac running
-- Messages.app (see mac-relay/) polls the outbox, sends via AppleScript,
-- and posts replies back. This keeps the backend's contract dead simple
-- (an HTTP queue) regardless of how delivery actually happens on the Mac
-- side.
--
-- No dedicated "contacts" table exists yet in this schema — wallet_campaigns'
-- reward_cards (slug text PK, customer_name, customer_contact) is the closest
-- thing to a contact list. card_slug is kept as a soft reference (no FK),
-- matching the existing convention in wallet_campaign_redemptions.card_slug,
-- since reward_cards.slug isn't guaranteed unique across businesses and a
-- message thread should still work for a phone number with no matching card.

create table if not exists public.comms_messages (
  id uuid primary key default gen_random_uuid(),
  business_user_id uuid not null references auth.users(id) on delete cascade,
  card_slug text,                 -- soft reference to reward_cards.slug, nullable
  phone text not null,            -- E.164-ish, whatever the relay/customer record has
  direction text not null check (direction in ('out', 'in')),
  channel text not null default 'imessage' check (channel in ('imessage', 'sms')),
  body text not null,
  status text not null default 'queued' check (status in ('queued', 'sent', 'delivered', 'failed', 'received')),
  external_id text,               -- Messages.app GUID, for de-duping relay re-polls
  error text,                     -- failure reason, if status = 'failed'
  created_at timestamptz not null default now(),
  sent_at timestamptz
);

create index if not exists comms_messages_business_idx on public.comms_messages (business_user_id, created_at desc);
create index if not exists comms_messages_card_idx on public.comms_messages (card_slug);
create index if not exists comms_messages_status_idx on public.comms_messages (status) where status = 'queued';
create unique index if not exists comms_messages_external_id_idx on public.comms_messages (external_id) where external_id is not null;

alter table public.comms_messages enable row level security;

create policy "comms_messages_owner_select" on public.comms_messages
  for select using (auth.uid() = business_user_id);

create policy "comms_messages_owner_insert" on public.comms_messages
  for insert with check (auth.uid() = business_user_id);

-- Note: the relay and the /comms/inbound + /comms/outbox endpoints all run
-- through the service-role client (get_supabase()), which bypasses RLS by
-- design — these policies only matter for any direct client-side Supabase
-- queries the frontend might add later.
