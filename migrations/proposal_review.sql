-- Proposal Editor review persistence — comments + section approvals.
-- The editor's review loop (approve a section, comment on it) used to be
-- client-only and vanished on reload. These tables persist it against a
-- document, and are keyed by document (not by a single user) so a future
-- BD Partner / Team Up collaborator can comment too without a migration.
-- See app/routers/proposal_docs.py. Run in the Supabase SQL editor.

create extension if not exists pgcrypto;  -- for gen_random_uuid()

-- One row per (user, proposal). proposal_id is a free-text key: the real
-- opportunity/proposal id, or 'sample' for the demo document.
create table if not exists proposal_documents (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  proposal_id text not null default 'sample',
  title       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, proposal_id)
);

-- Comment threads anchored to a section (section_key = the section heading
-- in v1; stable enough since headings are unique within a document).
create table if not exists proposal_comments (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null references proposal_documents(id) on delete cascade,
  section_key text not null,
  author_id   uuid not null references auth.users(id) on delete cascade,
  body        text not null,
  status      text not null default 'open',  -- 'open' | 'resolved'
  created_at  timestamptz not null default now()
);
create index if not exists idx_proposal_comments_doc on proposal_comments(document_id);

-- Per-section approval state (the review checkmarks; also feeds the
-- compliance matrix's "addressed" status across reloads).
create table if not exists proposal_section_state (
  document_id uuid not null references proposal_documents(id) on delete cascade,
  section_key text not null,
  approved    boolean not null default false,
  updated_at  timestamptz not null default now(),
  primary key (document_id, section_key)
);

-- updated_at trigger on the document (reuses the shared function).
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_proposal_documents_updated_at on proposal_documents;
create trigger trg_proposal_documents_updated_at
  before update on proposal_documents
  for each row execute function set_updated_at();

-- RLS — defense in depth (the API enforces ownership in the router too).
alter table proposal_documents enable row level security;
drop policy if exists "Owners manage their own proposal documents" on proposal_documents;
create policy "Owners manage their own proposal documents" on proposal_documents
  for all using (auth.uid() = user_id);

-- Comments + section state inherit ownership through their document.
alter table proposal_comments enable row level security;
drop policy if exists "Access comments on owned documents" on proposal_comments;
create policy "Access comments on owned documents" on proposal_comments
  for all using (
    exists (
      select 1 from proposal_documents d
      where d.id = proposal_comments.document_id and d.user_id = auth.uid()
    )
  );

alter table proposal_section_state enable row level security;
drop policy if exists "Access section state on owned documents" on proposal_section_state;
create policy "Access section state on owned documents" on proposal_section_state
  for all using (
    exists (
      select 1 from proposal_documents d
      where d.id = proposal_section_state.document_id and d.user_id = auth.uid()
    )
  );
