-- Persist the full proposal draft body (not just comments/approvals), so a
-- user can save, leave, switch to another solicitation, and come back to the
-- exact draft. content = the editor's document JSON; format = the agency
-- format rules so the doc reopens correctly. Additive, safe. Run in Supabase.

alter table proposal_documents add column if not exists content jsonb;
alter table proposal_documents add column if not exists format  jsonb;
