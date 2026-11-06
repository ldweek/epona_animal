# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the FX revaluation run (IFRS 10/10 UI layer).

Runs the eh_fx_revaluation_test_tour in a real headless Chrome via
HttpCase. The eh_tour tag keeps browser cycles out of the default
per-module runs; the matrix runner selects them with --tours
(test-tags eh_tour).

The run form has three required many2one fields (journal, gain
account, loss account). Many2one autocomplete fills are not part of
the mechanical tour step template, so this wrapper pre-seeds them as
ir.default records: the form opens with all three already set and the
tour only touches plain char / date inputs.
"""

from datetime import date

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxRevaluationTour(HttpCase):

    def _seed_form_defaults(self):
        """Provide journal / gain / loss defaults so the tour never has
        to drive a many2one autocomplete."""
        company = self.env.company
        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            journal = self.env['account.journal'].create({
                'name': 'EH FX Tour Miscellaneous',
                'code': 'EHFXT',
                'type': 'general',
                'company_id': company.id,
            })
        Account = self.env['account.account']
        gain = Account.search([('code', '=', 'EHFXTG')], limit=1)
        if not gain:
            gain = Account.create({
                'code': 'EHFXTG',
                'name': 'EH FX Tour Unrealised Gain',
                'account_type': 'income_other',
            })
        loss = Account.search([('code', '=', 'EHFXTL')], limit=1)
        if not loss:
            loss = Account.create({
                'code': 'EHFXTL',
                'name': 'EH FX Tour Unrealised Loss',
                'account_type': 'expense',
            })
        IrDefault = self.env['ir.default']
        IrDefault.set('eh.fx.revaluation.run', 'journal_id', journal.id)
        IrDefault.set('eh.fx.revaluation.run', 'gain_account_id', gain.id)
        IrDefault.set('eh.fx.revaluation.run', 'loss_account_id', loss.id)
        return journal, gain, loss

    def test_fx_revaluation_create_tour(self):
        journal, gain, loss = self._seed_form_defaults()
        before = self.env['eh.fx.revaluation.run'].search([])
        self.start_tour('/odoo', 'eh_fx_revaluation_test_tour', login='admin')
        run = self.env['eh.fx.revaluation.run'].search([]) - before
        self.assertEqual(len(run), 1,
                         'tour did not create the FX revaluation run')
        self.assertEqual(run.description, 'FX tour run')
        self.assertEqual(run.revaluation_date, date(2026, 6, 15))
        self.assertNotEqual(run.name, '/',
                            'sequence did not assign a run reference')
        self.assertEqual(run.journal_id, journal)
        self.assertEqual(run.gain_account_id, gain)
        self.assertEqual(run.loss_account_id, loss)
        # The tour ends at the saved draft; Compute Lines needs open
        # monetary balances to revalue and is covered by the golden tests.
        self.assertEqual(run.state, 'draft')
