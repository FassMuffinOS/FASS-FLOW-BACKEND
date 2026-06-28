-- Group/teaming threads (#257): chat_threads/chat_thread_participants were
-- already participant-based (see partner_network.sql's comment — "a thread
-- can later grow beyond two people... without a schema change"), so the only
-- gap is a place to put an optional group name. 1:1 threads leave this null
-- and keep displaying as the other person's name, exactly as before.
-- Run in the Supabase SQL editor.

alter table public.chat_threads
  add column if not exists title text;

comment on column public.chat_threads.title is
  'Optional display name for a group/teaming thread (3+ participants). Null for 1:1 threads, which display as the other participant''s name instead.';
