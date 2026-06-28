-- Business feed — the "LinkedIn+Slack" social layer sitting on top of
-- Discoverable Business Profiles (see profiles.py / business_profiles_public
-- migration). Every post belongs to one author and shows up two places: the
-- global /feed page (everyone's posts, newest first) and that author's own
-- Profile.jsx page. Posts come from two sources:
--   'manual' — written by the user in the Feed composer
--   'auto'   — generated server-side off a real event (contract awarded,
--              Wallet launched, etc.) so the feed has real signal on day one
--              instead of depending on people remembering to post.
-- All reads go through the backend (service-role client, same pattern as
-- partner_posts/profiles.py) rather than direct client queries, since the
-- feed fans out across every user's posts and Supabase RLS would otherwise
-- need a much wider read policy than "see your own row." Run in the
-- Supabase SQL editor.

create table if not exists public.business_posts (
  id          uuid primary key default uuid_generate_v4(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  body        text not null,
  source      text not null default 'manual',  -- manual | auto
  category    text,                             -- e.g. customer_growth, operations — mirrors business_events.category for auto posts
  created_at  timestamptz not null default now()
);

create index if not exists idx_business_posts_created on public.business_posts(created_at desc);
create index if not exists idx_business_posts_user on public.business_posts(user_id, created_at desc);

create table if not exists public.business_post_likes (
  post_id     uuid not null references public.business_posts(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (post_id, user_id)
);

create index if not exists idx_business_post_likes_post on public.business_post_likes(post_id);

create table if not exists public.business_post_comments (
  id          uuid primary key default uuid_generate_v4(),
  post_id     uuid not null references public.business_posts(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  body        text not null,
  created_at  timestamptz not null default now()
);

create index if not exists idx_business_post_comments_post on public.business_post_comments(post_id, created_at);

alter table public.business_posts enable row level security;
alter table public.business_post_likes enable row level security;
alter table public.business_post_comments enable row level security;

-- Public-board model, same as partner_posts: any signed-in member can read
-- the whole feed; only the author can write/edit/delete their own rows.
-- The backend uses the service-role key for every actual write (see
-- feed.py), but these policies stay in place for defense in depth and so a
-- future client-side read path isn't blocked.
drop policy if exists "Posts are readable by any signed-in member" on public.business_posts;
create policy "Posts are readable by any signed-in member" on public.business_posts
  for select using (auth.role() = 'authenticated');

drop policy if exists "Authors manage their own posts" on public.business_posts;
create policy "Authors manage their own posts" on public.business_posts
  for all using (auth.uid() = user_id);

drop policy if exists "Likes are readable by any signed-in member" on public.business_post_likes;
create policy "Likes are readable by any signed-in member" on public.business_post_likes
  for select using (auth.role() = 'authenticated');

drop policy if exists "Members manage their own likes" on public.business_post_likes;
create policy "Members manage their own likes" on public.business_post_likes
  for all using (auth.uid() = user_id);

drop policy if exists "Comments are readable by any signed-in member" on public.business_post_comments;
create policy "Comments are readable by any signed-in member" on public.business_post_comments
  for select using (auth.role() = 'authenticated');

drop policy if exists "Authors manage their own comments" on public.business_post_comments;
create policy "Authors manage their own comments" on public.business_post_comments
  for all using (auth.uid() = user_id);
