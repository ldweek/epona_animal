# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the provision register (IFRS 10/10 UI layer).

Runs the eh_provisions_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from datetime import date

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_provisions', 'post_install', '-at_install')
class TestProvisionTour(HttpCase):

    def test_provision_create_tour(self):
        before = self.env['eh.provision'].search([])
        self.start_tour('/odoo', 'eh_provisions_test_tour', login='admin')
        provision = self.env['eh.provision'].search([]) - before
        self.assertEqual(len(provision), 1,
                         'tour did not create the provision record')
        self.assertAlmostEqual(provision.best_estimate, 1000.0, places=2)
        self.assertEqual(provision.action_accounting_date, date(2026, 1, 1))
