-- Margin Guard + Estimator → Client Proposal linkage.
-- Adds the cost basis a proposal needs to compute profit margin, and lets an
-- Estimator estimate carry its computed totals so a proposal can pull real
-- cost/price numbers from it instead of being a disconnected hand-typed
-- figure. Run in the Supabase SQL editor. All additive (safe, no data loss).

-- Client proposal: the contractor's cost + their target margin %.
alter table client_estimates add column if not exists cost_basis    numeric;
alter table client_estimates add column if not exists target_margin numeric not null default 20;

-- Estimator: snapshot the computed cost (subtotal, before overhead) and the
-- priced total (with overhead) at save time, so Client Proposals can read
-- them without re-running the whole zip/trade cost engine.
alter table estimator_saved_estimates add column if not exists subtotal_low  numeric;
alter table estimator_saved_estimates add column if not exists subtotal_high numeric;
alter table estimator_saved_estimates add column if not exists total_low     numeric;
alter table estimator_saved_estimates add column if not exists total_high    numeric;
