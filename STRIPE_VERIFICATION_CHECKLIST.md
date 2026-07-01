# Stripe End-to-End Verification Checklist

Code-level review completed 2026-07-01. This covers every price/checkout/webhook path built this
session: GovCon subscriptions (monthly + annual), Regulars subscriptions (monthly + annual), AI
credit packs, WARDOG Intel à la carte reports, gift cards, and FASS Wallet passes.

## Part 1 — What's already verified in code (no action needed)

- Every checkout path (`wallet.py`, `subscriptions.py`, `credits.py`, `gift_cards.py`,
  `regulars.py`, `intelligence.py`) creates a **Stripe-hosted Checkout Session** — none of them
  collect card details directly. Card data never touches FASS's servers.
- A single webhook handler (`subscriptions.py` `/subscriptions/webhook`) verifies the Stripe
  signature (`stripe.Webhook.construct_event`) before processing anything, and routes every
  product line off `metadata.kind`. Bad/missing signature → 400, no processing.
- Every authenticated checkout endpoint enforces `require_owner` — a user can only buy credits,
  start an intel report, or check status for their own account (confirmed in `credits.py`,
  `intelligence.py`, `regulars.py`, `gift_cards.py`).
- The AI credit pack checkout re-validates the client-submitted `price_id` against Stripe
  server-side (must belong to the known `stripe_product_ai_credits` product) before creating a
  session — a forged price id can't be used to check out at the wrong amount.
- Webhook handlers for each `kind` are idempotent against Stripe's at-least-once redelivery
  (credit grants dedupe on `external_ref`, gift card creation upserts on `slug`, WARDOG Intel
  report only flips `pending_payment` → `unused` once).
- There's already a built-in **live pricing sweep** at `GET /subscriptions/admin/pricing-check`
  (admin-secret gated) that checks every GovCon + Regulars plan/interval + the WARDOG Intel report
  price against live Stripe data — confirms the price id is set, active, USD, correct interval,
  correct amount, and not accidentally reused across two plans.

## Part 2 — Run this yourself (needs live Stripe access / the deployed backend)

1. **Run the built-in pricing sweep.**
   ```
   curl -s https://<your-railway-backend>/api/v1/subscriptions/admin/pricing-check \
        -H "X-Admin-Secret: $ADMIN_SECRET" | python3 -m json.tool
   ```
   Look at `"all_offered_plans_ok"` — should be `true`. Any plan/interval with a non-empty
   `"issues"` array needs a price id fix in Railway env vars or in Stripe itself.

2. **Confirm the webhook endpoint is actually registered in Stripe** (Stripe dashboard →
   Developers → Webhooks): endpoint URL is your live backend's
   `/api/v1/subscriptions/webhook`, and it's subscribed to at least: `checkout.session.completed`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.payment_succeeded`. Confirm the signing secret shown
   there matches `STRIPE_WEBHOOK_SECRET` in Railway.

3. **Live-test one checkout per product line** (Stripe test mode, test card `4242 4242 4242 4242`):
   - GovCon subscription signup (monthly, then annual) → confirm `profiles.plan` and
     `subscription_status` update after checkout.
   - Regulars subscription signup → confirm `profiles.is_wallet_only` / `wallet_plan` update,
     and that it does **not** touch the GovCon `plan`/`stripe_subscription_id` columns.
   - AI credit pack purchase → confirm `ai_credits.balance` increases by the right amount.
   - WARDOG Intel report purchase → confirm the report flips from `pending_payment` to `unused`
     and is readable from `/intelligence/reports`.
   - Gift card purchase (public storefront, no login) → confirm a `gift_cards` row appears only
     after payment, with the right `original_value`/`balance`.
   - FASS Wallet `.pkpass` unlock → confirm `wallet_passes.purchased` flips to `true` and the
     real pass downloads afterward (not the locked/preview one).

4. **Cancel + billing portal.** From Settings → Billing, open the portal for a GovCon account and
   for a Regulars (`is_wallet_only`) account — confirm each opens Stripe's hosted portal for the
   *correct* Stripe customer id, and that cancelling one doesn't affect the other if a test
   account has both.

5. **Webhook redelivery test.** In the Stripe dashboard, resend a `checkout.session.completed`
   event you already processed (Developers → Webhooks → your endpoint → an event → "Resend").
   Confirm nothing double-grants (credits shouldn't double, gift card balance shouldn't reset).

6. **Sanity-check the gift card storefront's amount field.** `POST /gift-cards/purchase/checkout`
   enforces a $1 floor but no ceiling — worth deciding if you want a max (e.g. $500) before this
   is public-facing, purely a product decision rather than a bug.

7. **Confirm live keys are actually live keys.** In Railway, `STRIPE_SECRET_KEY` and
   `STRIPE_WEBHOOK_SECRET` should be the **live** mode values (not `sk_test_...`) before you
   consider this "out." Easy to forget after a long stretch of test-mode work.

Once steps 1–7 pass, Stripe is verified end-to-end for this version of FASS Flow + Regulars.
