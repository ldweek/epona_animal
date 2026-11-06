# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the standard cost card register (IFRS 10/10 UI layer).

Runs the eh_costing_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_costing', 'post_install', '-at_install')
class TestCostingTour(HttpCase):

    def test_cost_card_create_tour(self):
        before = self.env['eh.cost.card'].search([])
        self.start_tour('/odoo', 'eh_costing_test_tour', login='admin')
        card = self.env['eh.cost.card'].search([]) - before
        self.assertEqual(len(card), 1,
                         'tour did not create the cost card record')
        self.assertEqual(card.item_name, 'Assembly line output')
        self.assertAlmostEqual(card.normal_capacity, 1200.0, places=2)
        self.assertNotEqual(card.name, '/',
                            'cost card sequence was not assigned on create')
        self.assertEqual(card.state, 'draft')
