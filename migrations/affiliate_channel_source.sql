-- Per-channel attribution for affiliate links — "know where it came from."
--
-- Creators drop their link across many channels (Discord, Reddit, TikTok,
-- YouTube, Facebook, Kick, X, newsletter, link-in-bio, …). Until now a click
-- only recorded the referral CODE, so every channel collapsed into one
-- undifferentiated pile and there was no way to tell which placement actually
-- converts. This adds an optional free-text `source` tag (carried on the link
-- as ?src=<channel>) at three points in the funnel:
--
--   1. affiliate_clicks.source        — which channel each click came from
--   2. profiles.referred_source       — the channel of the FIRST click that
--                                        attributed this user (first-click-wins,
--                                        set alongside referred_by_code)
--   3. affiliate_conversions.referred_source
--                                      — copied from the profile when the
--                                        conversion is recorded, so earnings can
--                                        be broken down by channel after the fact
--
-- All three are nullable/optional — a plain ?ref= link with no ?src= keeps
-- working exactly as before and simply shows up as "untagged".
--
-- Run AFTER affiliates.sql + affiliates_partner_program.sql.

-- NOTE: affiliate_conversions already has a `source` column, but it means the
-- conversion TYPE (subscription / wallet_pass / override / …). The MARKETING
-- channel is deliberately a separate column, `referred_source`, to avoid
-- overloading that existing meaning.

alter table public.affiliate_clicks
  add column if not exists source text;

alter table public.profiles
  add column if not exists referred_source text;

alter table public.affiliate_conversions
  add column if not exists referred_source text;

-- Cheap index for the per-channel rollup in GET /affiliates/me (clicks are
-- queried by code, then grouped by source in app code — this keeps the
-- code+source scan tight even for high-volume creators).
create index if not exists affiliate_clicks_code_source_idx
  on public.affiliate_clicks (code, source);
