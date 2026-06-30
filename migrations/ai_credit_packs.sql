-- AI credit packs — real Stripe purchase path for ai_credits, replacing the
-- honor-system refill described in ai_credits.sql's original comment. Eight
-- one-time prices ($5..$1000) live on the "FASS Flow — AI Credits" Stripe
-- product (prod_UnivB57nfHRmaF), each tagged metadata.credits=<n> so the
-- catalog is driven entirely from Stripe — see GET /credits/packs. Run in
-- the Supabase SQL editor.

-- Stripe's webhook delivery is at-least-once (same fact documented on
-- affiliate_conversions.external_ref in affiliates_partner_program.sql).
-- Without this, a redelivered checkout.session.completed would double-grant
-- credits the user already received. external_ref holds the Stripe
-- Checkout Session id; the partial unique index makes a second grant
-- attempt for the same session a no-op (see credits.py's duplicate-key
-- catch) instead of a second credit.
alter table ai_credit_ledger
  add column if not exists external_ref text;

create unique index if not exists ai_credit_ledger_external_ref_uniq
  on public.ai_credit_ledger (external_ref)
  where external_ref is not null;
