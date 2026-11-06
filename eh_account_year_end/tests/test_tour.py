# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the year-end closing run (IFRS 10/10 UI layer).

Runs the eh_year_end_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).

The run form has two required many2one fields (journal, retained
earnings account). Many2one autocomplete fills are not part of the
mechanical tour step template, so this wrapper pre-seeds them as
ir.default records: the form opens with both already set and the tour
only touches the two plain date inputs.
"""

from datetime import date

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_year_end', 'post_install', '-at_install')
class TestYearEndTour(HttpCase):

    def _seed_form_defaults(self):
        """Provide journal / retained earnings defaults so the tour never
        has to drive a many2one autocomplete."""
        company = self.env.company
        # Full-suite O17 installs the Australian localisation and may leave a
        # 30 June calendar. The mechanical tour enters a calendar-year run,
        # so make that fixture policy explicit. Use a valid one-day bridge:
        # delegated O17 company writes apply day before month, and 31 June is
        # invalid during a direct 30/6 -> 31/12 transition.
        company.sudo().write({'fiscalyear_last_day': 1})
        company.sudo().write({'fiscalyear_last_month': '12'})
        company.sudo().write({'fiscalyear_last_day': 31})
        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            journal = self.env['account.journal'].create({
                'name': 'EH Year End Tour Miscellaneous',
                'code': 'EHYET',
                'type': 'general',
                'company_id': company.id,
            })
        Account = self.env['account.account']
        retained = Account.search([('code', '=', 'EHYERE')], limit=1)
        if not retained:
            retained = Account.create({
                'code': 'EHYERE',
                'name': 'EH Year End Tour Retained Earnings',
                'account_type': 'equity',
            })
        IrDefault = self.env['ir.default']
        IrDefault.set('eh.year.end.run', 'journal_id', journal.id)
        IrDefault.set(
            'eh.year.end.run', 'retained_earnings_account_id', retained.id)
        return journal, retained

    def test_year_end_create_compute_tour(self):
        # The year-end menu is gated to the EH manager group; the seeding
        # migration grants it to existing accounting managers on install,
        # but a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        journal, retained = self._seed_form_defaults()
        before = self.env['eh.year.end.run'].search([])
        self.start_tour('/odoo', 'eh_year_end_test_tour', login='admin')
        run = self.env['eh.year.end.run'].search([]) - before
        self.assertEqual(len(run), 1,
                         'tour did not create the year-end run record')
        self.assertEqual(run.fiscal_year_start, date(2024, 1, 1))
        self.assertEqual(run.fiscal_year_end, date(2024, 12, 31))
        self.assertEqual(run.journal_id, journal)
        self.assertEqual(run.retained_earnings_account_id, retained)
        self.assertNotEqual(run.name, '/',
                            'sequence did not assign a run reference')
        self.assertTrue(run.lock_after_post,
                        'lock_after_post default was not preserved')
        # The tour ends at the saved draft; Compute needs seeded P&L
        # balances and is covered by the module's golden tests.
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.move_id,
                         'Compute must not post a closing entry')
