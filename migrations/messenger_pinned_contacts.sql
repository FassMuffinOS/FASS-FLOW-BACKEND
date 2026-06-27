-- Messenger: pinned "Admin" and "AI Assistant" contacts.
--
-- profiles.id is a foreign key into auth.users(id), so these two contacts
-- can't just be fabricated rows — they need real Supabase Auth accounts
-- behind them. Do this once, in this order:
--
-- 1. Supabase Dashboard → Authentication → Users → "Add user" → create:
--      admin@fass.systems         (skip if this account already exists —
--                                   it's your own founder login)
--      ai-assistant@fass.systems  (new service account; nobody ever logs
--                                   into this one, it just needs to exist
--                                   so its id satisfies the FK)
--    Set any password for each (e.g. a long random string) — auto-confirm
--    the email so no verification email is required.
--
-- 2. Copy each account's UUID from the dashboard's Users list, then replace
--    the two placeholders below and run this in the SQL editor.
--
-- 3. Put the same two UUIDs into Railway env vars on the backend:
--      ADMIN_USER_ID=<admin's uuid>
--      AI_ASSISTANT_USER_ID=<ai-assistant's uuid>

insert into public.profiles (id, full_name, company_name)
values
  ('6d78c497-cc94-4479-8bc2-f0d3c6d2fdde', 'Admin', 'FASS Flow Support'),
  ('ec457c0b-c52f-4992-91dc-aa6d363b25d4', 'AI Assistant', 'FASS Flow AI')
on conflict (id) do update set
  full_name = excluded.full_name,
  company_name = excluded.company_name;
