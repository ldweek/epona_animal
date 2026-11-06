# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the share-based payment plan (IFRS 10/10 UI layer).

Runs the eh_sbp_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_share_based_payment',
        'post_install', '-at_install')
class TestSbpTour(HttpCase):

    def test_sbp_plan_create_tour(self):
        # The SBP menus are gated to the EH accounting groups; the seeding
        # migration grants them to existing accounting managers on install,
        # but a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        before = self.env['eh.sbp.plan'].search([])
        self.start_tour('/odoo', 'eh_sbp_test_tour', login='admin')
        plan = self.env['eh.sbp.plan'].search([]) - before
        self.assertEqual(len(plan), 1,
                         'tour did not create the SBP plan record')
        self.assertEqual(plan.state, 'draft')
        self.assertNotEqual(plan.name, '/',
                            'the plan sequence did not assign a reference')
        self.assertEqual(plan.instrument_desc,
                         'Options over ordinary shares')
        self.assertEqual(plan.vesting_years, 4)
        self.assertEqual(plan.vesting_months, 6)
        self.assertTrue(plan.vesting_end_date,
                        'vesting end date should compute from the period')
