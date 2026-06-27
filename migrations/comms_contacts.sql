-- Contact Identity layer for Comms Hub. comms_messages already has every
-- message; this adds the metadata that makes the thread itself trustworthy
-- to the person receiving it — company, NAICS alignment, last award date —
-- without turning Comms Hub into a full CRM with required fields. Every
-- column here is optional; an empty contact card is a valid, normal state.
create table if not exists public.comms_contacts (
  business_user_id uuid not null references auth.users(id) on delete cascade,
  phone text not null,
  name text,
  company text,
  naics text,
  last_award_date date,
  notes text,
  nudge_dismissed_until timestamptz,
  updated_at timestamptz not null default now(),
  primary key (business_user_id, phone)
);

alter table public.comms_contacts enable row level security;

create policy "comms_contacts_owner_select" on public.comms_contacts
  for select using (auth.uid() = business_user_id);
