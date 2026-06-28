-- Careers page applicants. Public POST /careers/apply inserts a row here
-- (no auth required — most applicants don't have an account yet). The
-- founder reviews them via GET /careers/applicants (admin-secret gated,
-- same pattern as admin.py) and can seed a real platform account for a
-- promising applicant via POST /careers/applicants/{id}/invite, which
-- reuses the exact Supabase invite-by-email mechanism already used in
-- admin.py's /admin/invite (no password ever touches the backend).
create table if not exists job_applicants (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text not null,
  role_interest text default '',
  portfolio_url text default '',
  note text default '',
  status text not null default 'new',  -- new | reviewing | invited | passed
  user_id uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create index if not exists job_applicants_created_at_idx on job_applicants (created_at desc);
create index if not exists job_applicants_status_idx on job_applicants (status);

-- RLS: this table is only ever touched by the backend using the Supabase
-- service-role key (same as every other admin-facing table in this repo),
-- so lock it down from anon/authenticated entirely.
alter table job_applicants enable row level security;
