# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation performance regression guards.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_fx_revaluation', 'performance',
        'post_install', '-at_install')
class TestFxPerformance(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

    def test_query_budget_compute_with_no_balances(self):
        """Computing a run on an empty ledger should be cheap."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-12-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        with self.assertQueryCount(__system__=120):
            run.action_compute()
        self.assertEqual(run.state, 'computed')

    def test_query_budget_compute_with_populated_partner_buckets(self):
        """Residual reconstruction and line creation must scale in batches."""
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.account_receivable.eh_fx_revalue = True
        self.env['res.currency.rate'].search([
            ('currency_id', '=', eur.id),
            ('company_id', '=', self.company.id),
        ]).unlink()
        self.env['res.currency.rate'].create({
            'currency_id': eur.id,
            'company_id': self.company.id,
            'name': '2026-12-31',
            'rate': 0.8,
        })
        partners = self.env['res.partner'].create([
            {'name': 'FX performance partner %02d' % index}
            for index in range(40)
        ])
        move_lines = []
        for partner in partners:
            move_lines.extend([
                (0, 0, {
                    'name': 'FX performance exposure',
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id,
                    'currency_id': eur.id,
                    'amount_currency': 10.0,
                    'debit': 10.0,
                }),
                (0, 0, {
                    'name': 'FX performance revenue',
                    'account_id': self.account_revenue.id,
                    'credit': 10.0,
                }),
            ])
        exposure = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-12-01',
            'journal_id': self.journal_misc.id,
            'line_ids': move_lines,
        })
        exposure.action_post()
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-12-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })

        with self.assertQueryCount(__system__=220):
            run.action_compute()

        self.assertEqual(len(run.line_ids), 40)
        with self.assertQueryCount(__system__=6):
            settled = self.env['eh.fx.revaluation.line'].search([
                ('run_id', '=', run.id),
                ('is_realized', '=', True),
            ])
        self.assertFalse(settled)
