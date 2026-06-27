-- Messenger v2: brings chat up to 2026-baseline expectations — attachments,
-- edit/delete, emoji reactions, and the storage + push-subscription plumbing
-- those features need. Presence, typing indicators, and "Seen" receipts need
-- no schema changes at all (presence/typing are ephemeral Supabase Realtime
-- Presence/Broadcast; "Seen" already reads the existing read_by column) —
-- this migration only covers what genuinely needs persistence.

-- 1. New chat_messages columns -------------------------------------------------
alter table public.chat_messages add column if not exists attachment_url  text;
alter table public.chat_messages add column if not exists attachment_name text;
alter table public.chat_messages add column if not exists attachment_type text; -- mime type, e.g. image/png
alter table public.chat_messages add column if not exists edited_at  timestamptz;
alter table public.chat_messages add column if not exists deleted_at timestamptz;

-- A deleted message keeps its row (so thread ordering/read receipts don't
-- shift) but the API clears body/attachment and the frontend renders a
-- "message deleted" placeholder instead of removing it outright.

-- 2. Emoji reactions ------------------------------------------------------------
create table if not exists public.chat_message_reactions (
  id          uuid primary key default uuid_generate_v4(),
  message_id  uuid not null references public.chat_messages(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  emoji       text not null,
  created_at  timestamptz not null default now(),
  unique (message_id, user_id, emoji)
);

create index if not exists idx_chat_reactions_message on public.chat_message_reactions(message_id);

alter table public.chat_message_reactions enable row level security;

drop policy if exists "Participants can view reactions in their threads" on public.chat_message_reactions;
create policy "Participants can view reactions in their threads" on public.chat_message_reactions
  for select using (
    exists (
      select 1 from public.chat_messages m
      join public.chat_thread_participants p on p.thread_id = m.thread_id
      where m.id = chat_message_reactions.message_id and p.user_id = auth.uid()
    )
  );

drop policy if exists "Participants can react to messages in their threads" on public.chat_message_reactions;
create policy "Participants can react to messages in their threads" on public.chat_message_reactions
  for insert with check (
    auth.uid() = user_id
    and exists (
      select 1 from public.chat_messages m
      join public.chat_thread_participants p on p.thread_id = m.thread_id
      where m.id = chat_message_reactions.message_id and p.user_id = auth.uid()
    )
  );

drop policy if exists "Users remove their own reactions" on public.chat_message_reactions;
create policy "Users remove their own reactions" on public.chat_message_reactions
  for delete using (auth.uid() = user_id);

-- 3. Web Push subscriptions ------------------------------------------------------
-- One browser/device subscription per row; a user can have several (phone +
-- laptop, etc.). The backend (service-role key) is the only writer/reader in
-- practice — these RLS policies are defense-in-depth, matching the rest of
-- this schema's pattern of scoping by auth.uid() even though the API never
-- queries this table with the anon key.
create table if not exists public.push_subscriptions (
  id          uuid primary key default uuid_generate_v4(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  endpoint    text not null,
  p256dh      text not null,
  auth_key    text not null,
  created_at  timestamptz not null default now(),
  unique (endpoint)
);

create index if not exists idx_push_subscriptions_user on public.push_subscriptions(user_id);

alter table public.push_subscriptions enable row level security;

drop policy if exists "Users manage their own push subscriptions" on public.push_subscriptions;
create policy "Users manage their own push subscriptions" on public.push_subscriptions
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- 4. Realtime + attachment storage ------------------------------------------------
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'chat_message_reactions'
  ) then
    alter publication supabase_realtime add table public.chat_message_reactions;
  end if;
end $$;

-- Bucket for chat attachments, path convention "{thread_id}/{uuid}-{filename}"
-- so storage RLS can check thread membership directly from the object path
-- without a lookup table. Not public — every read goes through a signed URL
-- the backend hands back from a participant-gated endpoint.
insert into storage.buckets (id, name, public)
values ('chat-attachments', 'chat-attachments', false)
on conflict (id) do nothing;

drop policy if exists "Participants read chat attachments" on storage.objects;
create policy "Participants read chat attachments" on storage.objects
  for select using (
    bucket_id = 'chat-attachments'
    and exists (
      select 1 from public.chat_thread_participants p
      where p.user_id = auth.uid()
        and p.thread_id::text = (storage.foldername(name))[1]
    )
  );

drop policy if exists "Participants upload chat attachments" on storage.objects;
create policy "Participants upload chat attachments" on storage.objects
  for insert with check (
    bucket_id = 'chat-attachments'
    and exists (
      select 1 from public.chat_thread_participants p
      where p.user_id = auth.uid()
        and p.thread_id::text = (storage.foldername(name))[1]
    )
  );
