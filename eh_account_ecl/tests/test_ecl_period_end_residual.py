# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 9 ECL period-end exposure measurement (point-in-time residual).

A run whose reporting date precedes the day it is computed must measure each
receivable at its balance AS AT the reporting date. A settlement dated after
period end must neither drop the exposure (via the live ``reconciled`` flag)
nor shrink it (via the live ``amount_residual``); otherwise the loss allowance
is understated for the normal month-end-close timing (compute a 30 Jun run in
July after cash has arrived).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'integration', 'post_install', '-at_install')
class TestEclPeriodEndResidual(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5290', 'Impairment Loss', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1290', 'Loss Allowance', 'asset_current')

    def _matrix(self):
        return [
            {'name': 'Current', 'days_from': 0, 'days_to': 30,
             'loss_rate': 1.0},
            {'name': '31-90', 'days_from': 31, 'days_to': 90,
             'loss_rate': 5.0, 'stage': '2'},
            {'name': '90+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3'},
        ]

    def _run(self, reporting_date, buckets=None):
        return self.env['eh.ecl.run'].create({
            'reporting_date': reporting_date,
            'opening_allowance': 0.0,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in (buckets or self._matrix())],
        })

    def _invoice(self, amount=1000.0, invoice_date='2026-01-01',
                 due='2026-01-15'):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': invoice_date,
            'invoice_date_due': due,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale', 'quantity': 1, 'price_unit': amount,
                'account_id': self.account_revenue.id})],
        })
        inv.action_post()
        return inv

    def _settle(self, inv, amount, on_date):
        """Post a cash receipt dated ``on_date`` and reconcile it against the
        invoice's receivable line, so the invoice settles as at ``on_date``.

        The resulting partial reconciliation carries ``max_date == on_date``
        (the later of the invoice date and the receipt date), which is what
        the point-in-time residual filters on.
        """
        recv = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')
        settle = self.env['account.move'].create({
            'move_type': 'entry',
            'date': on_date,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Receipt',
                        'account_id': self.account_cash.id,
                        'partner_id': self.partner_a.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': 'Settle receivable',
                        'account_id': recv.account_id.id,
                        'partner_id': self.partner_a.id,
                        'debit': 0.0, 'credit': amount}),
            ],
        })
        settle.action_post()
        credit = settle.line_ids.filtered(
            lambda l: l.account_id == recv.account_id)
        (recv + credit).reconcile()
        return settle

    def test_full_settlement_after_period_end_still_measured(self):
        # 1,000 receivable open on 30 Jun, paid in full on 5 Jul. The 30 Jun
        # run (populated in July) must still measure the full 1,000 exposure.
        inv = self._invoice(amount=1000.0)
        self._settle(inv, 1000.0, '2026-07-05')
        # The live figures now show a fully reconciled, zero-residual line...
        recv = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')
        self.assertTrue(recv.reconciled)
        self.assertAlmostEqual(recv.amount_residual, 0.0, places=2)
        # ...yet the 30 Jun run measures it at its 30 Jun carrying amount.
        run = self._run('2026-06-30')
        run.action_populate_from_receivables()
        old = run.bucket_ids.filtered(lambda b: b.name == '90+')
        self.assertAlmostEqual(old.gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)

    def test_partial_settlement_after_period_end_measured_gross(self):
        # 1,000 receivable, 400 paid on 5 Jul. The 30 Jun run measures the
        # full 1,000 (its period-end balance), not the reduced 600 live
        # residual.
        inv = self._invoice(amount=1000.0)
        self._settle(inv, 400.0, '2026-07-05')
        run = self._run('2026-06-30')
        run.action_populate_from_receivables()
        old = run.bucket_ids.filtered(lambda b: b.name == '90+')
        self.assertAlmostEqual(old.gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)

    def test_settlement_before_period_end_excluded(self):
        # Same invoice settled on 15 Jun (before the 30 Jun reporting date):
        # the exposure was closed by period end, so it must NOT be measured.
        inv = self._invoice(amount=1000.0)
        self._settle(inv, 1000.0, '2026-06-15')
        run = self._run('2026-06-30')
        run.action_populate_from_receivables()
        old = run.bucket_ids.filtered(lambda b: b.name == '90+')
        self.assertAlmostEqual(old.gross_carrying, 0.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 0.0, places=2)

    def test_open_receivable_unchanged(self):
        # Regression guard: an entirely unreconciled receivable still measures
        # at its full balance (the point-in-time residual must equal the live
        # residual when nothing has settled).
        self._invoice(amount=1000.0)
        run = self._run('2026-06-30')
        run.action_populate_from_receivables()
        old = run.bucket_ids.filtered(lambda b: b.name == '90+')
        self.assertAlmostEqual(old.gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)
