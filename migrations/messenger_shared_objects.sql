-- Messenger as the govcon hub, step 1: let a message carry a reference to a
-- real platform object (a WARDOG opportunity, an R-E-A-D proposal, a Team Up
-- partner post, or a Wallet Passport capability statement) instead of just
-- text. The frontend renders these as a card instead of a chat bubble.
--
-- Design choice: store a denormalized snapshot (title + a couple of display
-- fields) alongside the type/id, rather than making the frontend re-fetch
-- the live object on every render. Two reasons: (1) the recipient may not
-- have permission to read the source row directly (e.g. another user's
-- private proposal) — the act of sharing it into a thread is the grant; the
-- backend reads it once with the service-role key at send time and embeds
-- what's safe to show. (2) if the source object is later edited or deleted,
-- the shared card in chat history should still show what was shared at the
-- time, like a forwarded message rather than a live transclusion.

alter table public.chat_messages add column if not exists shared_object_type text;
  -- 'opportunity' | 'opportunity_live' | 'proposal' | 'partner_post' | 'passport'
alter table public.chat_messages add column if not exists shared_object_id text;
  -- text, not uuid: opportunity_live's id is a SAM.gov noticeId (not a uuid),
  -- everything else's id happens to be a uuid string but doesn't need the
  -- column typed that strictly.
alter table public.chat_messages add column if not exists shared_object_snapshot jsonb;
  -- e.g. {"title": "...", "agency": "...", "naics_code": "...", "value_estimate": ...}
  -- for an opportunity; shape varies by shared_object_type.

create index if not exists idx_chat_messages_shared_object
  on public.chat_messages(shared_object_type, shared_object_id)
  where shared_object_type is not null;
