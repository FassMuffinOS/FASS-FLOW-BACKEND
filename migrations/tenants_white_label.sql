-- White-label enterprise product: Phase 1 (tenant data model + admin
-- portal). Scoped per Maurice's 2026-06-30 direction: shared database,
-- Row-Level-Security-style isolation via tenant_id (not a separate
-- Supabase project per client — much faster to spin up new clients, same
-- pattern this app already uses everywhere), and the framework is
-- tool-agnostic (enabled_tools is a flexible array, not a fixed schema
-- change per tool) so any client can be handed any subset of tools.
--
-- management_mode captures the three business models Maurice wants to
-- offer per client rather than picking one globally:
--   self_managed    — we stand it up, client runs it day-to-day after handoff
--   fass_managed     — we host/maintain/support it ongoing
--   partner_managed — an enterprise partner runs the client relationship
--
-- This is Phase 1 only: the tenant record + branding fields + which tools
-- are turned on. Actually re-skinning each tool's UI at runtime per tenant,
-- provisioning subdomains, and per-tenant billing are separate, larger
-- follow-on work — not built here.

create table if not exists public.tenants (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,                -- e.g. "acme" -> acme.flow.fass.systems (future subdomain routing)
  name text not null,                       -- display name shown in the white-labeled UI
  custom_domain text,                       -- optional, once a client points their own DNS at us
  logo_url text,
  primary_color text,
  secondary_color text,
  accent_color text,
  enabled_tools jsonb not null default '[]'::jsonb,   -- e.g. ["wardog","proposals","rewards","gift_cards"]
  management_mode text not null default 'self_managed'
    check (management_mode in ('self_managed', 'fass_managed', 'partner_managed')),
  partner_name text,                        -- set when management_mode = 'partner_managed'
  status text not null default 'active' check (status in ('active', 'paused', 'archived')),
  contact_name text,
  contact_email text,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Every user account (GovCon, Regulars, or a future tenant-specific signup)
-- can optionally belong to a tenant. Null tenant_id = a normal, non-white-
-- labeled FASS Flow / Regulars customer — the overwhelming majority of
-- rows today. This is intentionally additive and non-breaking.
alter table public.profiles add column if not exists tenant_id uuid references public.tenants(id);
create index if not exists idx_profiles_tenant_id on public.profiles(tenant_id);

-- RLS enabled to satisfy the security linter and block any accidental
-- anon-key access. No policies are added because — consistent with every
-- other table in this schema (see app/auth_deps.py's docstring) — this app
-- accesses Postgres exclusively through the backend's service-role client,
-- which bypasses RLS entirely; real access control lives in the FastAPI
-- layer (admin-secret gate on /admin/tenants/*), not in Postgres policies.
alter table public.tenants enable row level security;
