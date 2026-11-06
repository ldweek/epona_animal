# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the ECL run (IFRS 10/10 UI layer).

Runs the eh_ecl_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_ecl', 'post_install', '-at_install')
class TestEclTour(HttpCase):

    def test_ecl_run_create_compute_tour(self):
        # The ECL menu is gated to the EH manager group; the seeding
        # migration grants it to existing accounting managers on install,
        # but a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        before = self.env['eh.ecl.run'].search([])
        self.start_tour('/odoo', 'eh_ecl_test_tour', login='admin')
        run = self.env['eh.ecl.run'].search([]) - before
        self.assertEqual(len(run), 1,
                         'tour did not create the ECL run record')
        self.assertEqual(run.state, 'computed')
        # One bucket keyed in the tour: 1000.00 gross at 25% -> 250.00.
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)
