# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the fair value register (IFRS 10/10 UI layer).

Runs the eh_fair_value_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_fair_value', 'post_install', '-at_install')
class TestFairValueTour(HttpCase):

    def test_fair_value_create_tour(self):
        # The Fair Value menu is gated to the EH user group; the seeding
        # migration grants it to existing accounting users on install, but
        # a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_user')
        before = self.env['eh.fair.value.item'].search([])
        self.start_tour('/odoo', 'eh_fair_value_test_tour', login='admin')
        item = self.env['eh.fair.value.item'].search([]) - before
        self.assertEqual(len(item), 1,
                         'tour did not create the fair value item')
        self.assertAlmostEqual(item.prior_carrying, 1500.0, places=2)
        self.assertAlmostEqual(item.fair_value, 2500.0, places=2)
        # Stored compute: fair value less prior carrying amount.
        self.assertAlmostEqual(item.remeasurement, 1000.0, places=2)
        self.assertEqual(item.state, 'draft')
        self.assertNotEqual(item.name, '/',
                            'sequence did not assign a reference')
