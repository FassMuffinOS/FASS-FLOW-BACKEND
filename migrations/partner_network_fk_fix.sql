-- Fix for the Team Up board's "stuck loading" bug.
--
-- partner_network.sql originally pointed partner_posts.user_id and
-- chat_thread_participants.user_id at auth.users(id), matching the column's
-- literal type but missing the convention every other table in this schema
-- follows (proposals, wallet_passes, etc. all reference public.profiles(id)
-- instead). That distinction matters here: Supabase's PostgREST layer can
-- only embed a join like `profiles(full_name)` (used in partners.py and
-- chat.py to show the post author's name) when it can find a direct foreign
-- key between the two tables. auth.users isn't queryable via PostgREST and
-- has no inferred relationship to public.profiles from partner_posts' point
-- of view, so those embeds were failing server-side on every request.
--
-- public.profiles.id already references auth.users(id) 1:1 (see
-- supabase_schema.sql), so repointing here is safe and loses nothing.

alter table public.partner_posts
  drop constraint if exists partner_posts_user_id_fkey,
  add constraint partner_posts_user_id_fkey
    foreign key (user_id) references public.profiles(id) on delete cascade;

alter table public.chat_thread_participants
  drop constraint if exists chat_thread_participants_user_id_fkey,
  add constraint chat_thread_participants_user_id_fkey
    foreign key (user_id) references public.profiles(id) on delete cascade;

alter table public.chat_messages
  drop constraint if exists chat_messages_sender_id_fkey,
  add constraint chat_messages_sender_id_fkey
    foreign key (sender_id) references public.profiles(id) on delete cascade;
