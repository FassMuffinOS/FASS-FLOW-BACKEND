-- FASS Team Up — public "looking for partners" board + the minimal 1:1/group
-- chat needed to actually negotiate a partnership once two businesses find
-- each other. Run in the Supabase SQL editor.
--
-- partner_posts is deliberately a public board (any authenticated user can
-- read any post) — liquidity of partners matters more than privacy here,
-- and a business sharing "looking for a partner on X" is already choosing
-- to be visible. proposal_id is optional: posts can originate from a real
-- Pipeline record (the "Find a Partner" button) or be written standalone
-- from the Team Up page itself.
--
-- Chat is modeled as threads + participants (not a fixed two-column
-- user_a/user_b) so a thread can later grow beyond two people (e.g. a prime
-- pulling in two subs on the same opportunity) without a schema change.

create table if not exists public.partner_posts (
  id              uuid primary key default uuid_generate_v4(),
  user_id         uuid not null references auth.users(id) on delete cascade,
  proposal_id     uuid references public.proposals(id) on delete set null,
  title           text not null,
  what_i_bring    text not null,
  what_i_need     text not null,
  naics_code      text,
  status          text not null default 'open',  -- open | closed
  created_at      timestamptz not null default now()
);

create index if not exists idx_partner_posts_status on public.partner_posts(status, created_at desc);
create index if not exists idx_partner_posts_user on public.partner_posts(user_id);

create table if not exists public.chat_threads (
  id          uuid primary key default uuid_generate_v4(),
  post_id     uuid references public.partner_posts(id) on delete set null,
  created_at  timestamptz not null default now()
);

create table if not exists public.chat_thread_participants (
  thread_id   uuid not null references public.chat_threads(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  joined_at   timestamptz not null default now(),
  primary key (thread_id, user_id)
);

create index if not exists idx_chat_participants_user on public.chat_thread_participants(user_id);

create table if not exists public.chat_messages (
  id          uuid primary key default uuid_generate_v4(),
  thread_id   uuid not null references public.chat_threads(id) on delete cascade,
  sender_id   uuid not null references auth.users(id) on delete cascade,
  body        text not null,
  read_by     jsonb not null default '[]'::jsonb,  -- array of user_ids who've seen this message; sender is implicitly included by the API
  created_at  timestamptz not null default now()
);

create index if not exists idx_chat_messages_thread on public.chat_messages(thread_id, created_at);

alter table public.partner_posts enable row level security;
alter table public.chat_threads enable row level security;
alter table public.chat_thread_participants enable row level security;
alter table public.chat_messages enable row level security;

-- Posts: readable by anyone signed in (the whole point of a board); only the
-- author can write/edit/close their own.
drop policy if exists "Posts are publicly readable" on public.partner_posts;
create policy "Posts are publicly readable" on public.partner_posts
  for select using (auth.role() = 'authenticated');

drop policy if exists "Authors manage their own posts" on public.partner_posts;
create policy "Authors manage their own posts" on public.partner_posts
  for all using (auth.uid() = user_id);

-- Threads/messages: only visible to participants. The backend (service-role
-- key) creates threads and adds participants on behalf of users via the API
-- rather than letting clients write these directly, so the only client-facing
-- policies that matter in practice are the SELECT ones — but for
-- defense in depth they're scoped to participants either way.
drop policy if exists "Participants can view their threads" on public.chat_threads;
create policy "Participants can view their threads" on public.chat_threads
  for select using (
    exists (
      select 1 from public.chat_thread_participants p
      where p.thread_id = chat_threads.id and p.user_id = auth.uid()
    )
  );

drop policy if exists "Participants can view participant rows for their threads" on public.chat_thread_participants;
create policy "Participants can view participant rows for their threads" on public.chat_thread_participants
  for select using (
    exists (
      select 1 from public.chat_thread_participants p2
      where p2.thread_id = chat_thread_participants.thread_id and p2.user_id = auth.uid()
    )
  );

drop policy if exists "Participants can view messages in their threads" on public.chat_messages;
create policy "Participants can view messages in their threads" on public.chat_messages
  for select using (
    exists (
      select 1 from public.chat_thread_participants p
      where p.thread_id = chat_messages.thread_id and p.user_id = auth.uid()
    )
  );

drop policy if exists "Participants can send messages in their threads" on public.chat_messages;
create policy "Participants can send messages in their threads" on public.chat_messages
  for insert with check (
    auth.uid() = sender_id
    and exists (
      select 1 from public.chat_thread_participants p
      where p.thread_id = chat_messages.thread_id and p.user_id = auth.uid()
    )
  );
