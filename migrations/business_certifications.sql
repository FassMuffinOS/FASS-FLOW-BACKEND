-- Adds self-declared certifications/set-asides to business_profiles so
-- people search (chat.py search_people) and Profile.jsx can filter/display
-- "capabilities" beyond just NAICS — the other half of task #256
-- (capability-based people search: NAICS + certs + past performance).
-- Self-declared (not government-verified) for now, same trust level as the
-- naics field already on this table; a verification badge can layer on
-- top later without a schema change.
-- Run in the Supabase SQL editor.

alter table public.business_profiles
  add column if not exists certifications text[] not null default '{}';

comment on column public.business_profiles.certifications is
  'Self-declared set-aside/certification codes, e.g. sdvosb, wosb, edwosb, hubzone, 8a, vosb.';

-- business_entities mirrors business_profiles' identity fields (see
-- business_entities.sql / app/routers/business_profile.py's ENTITY_FIELDS)
-- so it needs the same column or switching/creating entities would drop
-- certifications on every mirror.
alter table public.business_entities
  add column if not exists certifications text[] not null default '{}';
