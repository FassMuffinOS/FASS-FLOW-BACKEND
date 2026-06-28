-- Adds a dollar-value column to opportunities so the discoverable Business
-- Profile page (profiles.py) can total "contracts won" value across a user's
-- awarded proposals. The opportunities table carried no $ column originally;
-- this backfills that gap. Existing rows get null, and profiles.py already
-- skips nulls, so "contracts won value" reads $0 until rows are populated.
-- Applied directly to production on 2026-06-27; this file records it so the
-- repo's migration history matches the live schema.

alter table public.opportunities
  add column if not exists value_estimate numeric;
