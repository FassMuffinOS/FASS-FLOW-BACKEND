-- Fills every "unindexed foreign key" flagged by Supabase's performance
-- advisor (get_advisors type=performance, lint name=unindexed_foreign_keys)
-- as of 2026-06-30 — 34 foreign-key columns across chat, foreman/GC ops,
-- proposals, rewards, vendor management, and witness/restoration tables
-- with no covering index. Every one of these is a column routinely used in
-- a WHERE/JOIN (owner-scoped lookups like `.eq("user_id", ...)`,
-- `.eq("proposal_id", ...)` etc. throughout the routers), so an unindexed
-- FK here means a sequential scan on every such query as these tables grow.
-- IF NOT EXISTS makes this safe to re-run.

CREATE INDEX IF NOT EXISTS idx_affiliate_conversions_referred_user_id ON public.affiliate_conversions(referred_user_id);
CREATE INDEX IF NOT EXISTS idx_business_post_comments_user_id ON public.business_post_comments(user_id);
CREATE INDEX IF NOT EXISTS idx_business_post_likes_user_id ON public.business_post_likes(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_message_reactions_user_id ON public.chat_message_reactions(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_sender_id ON public.chat_messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_chat_threads_post_id ON public.chat_threads(post_id);
CREATE INDEX IF NOT EXISTS idx_client_estimates_proposal_id ON public.client_estimates(proposal_id);
CREATE INDEX IF NOT EXISTS idx_fass_fill_documents_proposal_id ON public.fass_fill_documents(proposal_id);
CREATE INDEX IF NOT EXISTS idx_foreman_daily_logs_user_id ON public.foreman_daily_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_pay_app_lines_sov_item_id ON public.foreman_pay_app_lines(sov_item_id);
CREATE INDEX IF NOT EXISTS idx_foreman_pay_app_lines_user_id ON public.foreman_pay_app_lines(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_pay_apps_user_id ON public.foreman_pay_apps(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_rfis_user_id ON public.foreman_rfis(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_sov_items_user_id ON public.foreman_sov_items(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_submittals_user_id ON public.foreman_submittals(user_id);
CREATE INDEX IF NOT EXISTS idx_foreman_tm_tickets_user_id ON public.foreman_tm_tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_job_applicants_user_id ON public.job_applicants(user_id);
CREATE INDEX IF NOT EXISTS idx_network_vendors_signed_up_by ON public.network_vendors(signed_up_by);
CREATE INDEX IF NOT EXISTS idx_partner_posts_proposal_id ON public.partner_posts(proposal_id);
CREATE INDEX IF NOT EXISTS idx_proposal_comments_author_id ON public.proposal_comments(author_id);
CREATE INDEX IF NOT EXISTS idx_proposals_opportunity_id ON public.proposals(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_restoration_items_user_id ON public.restoration_items(user_id);
CREATE INDEX IF NOT EXISTS idx_restoration_projects_user_id ON public.restoration_projects(user_id);
CREATE INDEX IF NOT EXISTS idx_reward_cards_active_campaign_id ON public.reward_cards(active_campaign_id);
CREATE INDEX IF NOT EXISTS idx_reward_redemptions_card_slug ON public.reward_redemptions(card_slug);
CREATE INDEX IF NOT EXISTS idx_solicitation_inbox_proposal_id ON public.solicitation_inbox(proposal_id);
CREATE INDEX IF NOT EXISTS idx_solicitation_inbox_user_id ON public.solicitation_inbox(user_id);
CREATE INDEX IF NOT EXISTS idx_vendor_contracts_assignment_id ON public.vendor_contracts(assignment_id);
CREATE INDEX IF NOT EXISTS idx_vendor_contracts_user_id ON public.vendor_contracts(user_id);
CREATE INDEX IF NOT EXISTS idx_vendor_team_assignments_user_id ON public.vendor_team_assignments(user_id);
CREATE INDEX IF NOT EXISTS idx_vendor_team_assignments_vendor_id ON public.vendor_team_assignments(vendor_id);
CREATE INDEX IF NOT EXISTS idx_wallet_passes_user_id ON public.wallet_passes(user_id);
CREATE INDEX IF NOT EXISTS idx_witness_documents_user_id ON public.witness_documents(user_id);
CREATE INDEX IF NOT EXISTS idx_witness_milestones_user_id ON public.witness_milestones(user_id);
