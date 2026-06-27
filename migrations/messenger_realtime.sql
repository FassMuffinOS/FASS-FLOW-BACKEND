-- Upgrades Team Up's chat from "polling negotiation thread" into a real
-- platform-wide Messenger: instant delivery via Supabase Realtime, plus
-- indexing to support a "people search" (any user can find and DM any
-- other user, not just someone who posted to the Team Up board).
--
-- chat_threads/chat_messages/chat_thread_participants already exist from
-- partner_network.sql — this is additive only.

-- Realtime push: add chat_messages to the supabase_realtime publication so
-- the frontend can subscribe to postgres_changes (INSERT) on this table
-- instead of polling. Existing RLS policy "Participants can view messages
-- in their threads" (partner_network.sql) already scopes delivery
-- correctly per-subscriber — Realtime enforces the same SELECT policy for
-- each connected client, so no new RLS is needed here.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'chat_messages'
  ) then
    alter publication supabase_realtime add table public.chat_messages;
  end if;
end $$;

-- Full replica identity so realtime payloads include all columns on
-- update/delete too (not just the primary key) — harmless for an
-- insert-only table today, cheap insurance if edit/delete ever ships.
alter table public.chat_messages replica identity full;

-- People search reads public.profiles by name — index supports the
-- ILIKE lookup in GET /chat/people/search without a full table scan as
-- the user base grows. pg_trgm is already a common Supabase extension;
-- enable it defensively in case this project hasn't yet.
create extension if not exists pg_trgm;

create index if not exists idx_profiles_full_name_trgm
  on public.profiles using gin (full_name gin_trgm_ops);

create index if not exists idx_profiles_company_name_trgm
  on public.profiles using gin (company_name gin_trgm_ops);
