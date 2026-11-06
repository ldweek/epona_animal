# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the business combination register (IFRS 10/10 UI
layer).

Runs the eh_business_combination_test_tour in a real headless Chrome via
HttpCase. The eh_tour tag keeps browser cycles out of the default
per-module runs; the matrix runner selects them with --tours (test-tags
eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_business_combination', 'post_install',
        '-at_install')
class TestBusinessCombinationTour(HttpCase):

    def test_business_combination_create_tour(self):
        # The Business Combinations menu is gated to the EH manager group;
        # the seeding migration grants it to existing accounting managers
        # on install, but a bare test database may not have run it for
        # admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        before = self.env['eh.business.combination'].search([])
        self.start_tour('/odoo', 'eh_business_combination_test_tour',
                        login='admin')
        combo = self.env['eh.business.combination'].search([]) - before
        self.assertEqual(len(combo), 1,
                         'tour did not create the combination record')
        self.assertEqual(combo.acquiree_name, 'Target Holdings')
        self.assertAlmostEqual(
            combo.consideration_transferred, 5000.0, places=2)
        self.assertAlmostEqual(
            combo.fv_identifiable_net_assets, 3000.0, places=2)
        # Goodwill = 5000 consideration - 3000 identifiable net assets.
        self.assertAlmostEqual(combo.goodwill, 2000.0, places=2)
        self.assertEqual(combo.state, 'draft')
        self.assertNotEqual(combo.name, '/')
