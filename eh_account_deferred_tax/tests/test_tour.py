# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the deferred tax run (IFRS 10/10 UI layer).

Runs the eh_deferred_tax_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_deferred_tax', 'post_install', '-at_install')
class TestDeferredTaxTour(HttpCase):

    def test_deferred_tax_run_create_compute_tour(self):
        # The deferred tax menu is gated to the EH manager group; the
        # seeding migration grants it to existing accounting managers on
        # install, but a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        before = self.env['eh.deferred.tax.run'].search([])
        self.start_tour('/odoo', 'eh_deferred_tax_test_tour', login='admin')
        run = self.env['eh.deferred.tax.run'].search([]) - before
        self.assertEqual(len(run), 1,
                         'tour did not create the deferred tax run record')
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.statutory_rate, 30.0, places=3)
        self.assertEqual(len(run.line_ids), 1)
        line = run.line_ids
        self.assertAlmostEqual(line.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(line.tax_base, 600.0, places=2)
        # Asset, carrying 1000 vs tax base 600 -> taxable difference 400;
        # compute falls back to the run's statutory rate: 400 at 30% -> 120.
        self.assertAlmostEqual(line.temp_diff, 400.0, places=2)
        self.assertAlmostEqual(run.closing_dtl, 120.0, places=2)
