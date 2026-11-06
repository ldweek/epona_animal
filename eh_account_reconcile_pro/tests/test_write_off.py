# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Write-off and proven FX residual handling."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestWriteOff(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suspense = cls.bank_journal.suspense_account_id
        if not suspense:
            suspense = cls._ensure_account(
                cls.env, '1990', 'Bank Suspense', 'asset_current')
            cls.bank_journal.suspense_account_id = suspense
        suspense.reconcile = True

    def test_write_off_clears_line(self):
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(7.50, payment_ref='Bank fee')

        session.apply_write_off(
            line.id, self.account_expense.id, label='Bank fee')

        line.invalidate_recordset()
        self.assertTrue(line.is_reconciled)
        # The suspense residual was carried onto the write-off account via a
        # separate adjusting entry; the posted statement move is untouched.
        self.assertNotIn(
            self.account_expense,
            line.move_id.line_ids.mapped('account_id'))
        written = self.env['account.move.line'].search([
            ('account_id', '=', self.account_expense.id),
            ('move_id', '!=', line.move_id.id),
            ('parent_state', '=', 'posted'),
        ])
        self.assertTrue(written)

    def _configure_fx_accounts(self):
        gain = self._ensure_account(
            self.env, '7800', 'FX Gain', 'income_other')
        loss = self._ensure_account(
            self.env, '8800', 'FX Loss', 'expense')
        self.company.income_currency_exchange_account_id = gain
        self.company.expense_currency_exchange_account_id = loss
        return gain, loss

    def _make_proven_fx_residual(self, amount):
        line = self.make_statement_line(amount, payment_ref='FX rounding')
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        suspense = session._find_open_suspense(line)
        eur = self.env.ref('base.EUR')
        if not eur.active:
            eur.sudo().active = True
        suspense.flush_recordset()
        # Model a post-reconciliation FX-only remainder: the foreign amount
        # is fully cleared while a small company-currency residual remains.
        self.env.cr.execute(
            "UPDATE account_move_line "
            "SET currency_id = %s, amount_currency = 0, "
            "    amount_residual_currency = 0 "
            "WHERE id IN %s",
            (eur.id, tuple(suspense.ids)),
        )
        suspense.invalidate_recordset([
            'currency_id', 'amount_currency',
            'amount_residual_currency', 'amount_residual',
        ])
        return session, line

    def test_fx_writeoff_rejects_ordinary_company_currency_line(self):
        self._configure_fx_accounts()
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id)
        line = self.make_statement_line(2.0, payment_ref='Bank charge')

        with self.assertRaisesRegex(UserError, 'foreign-currency-only'):
            session.apply_fx_writeoff(line.id)

        # Odoo 16 derives ``is_reconciled`` from the zero foreign residual
        # even while a company-currency FX remainder is still open.  Prove
        # the rejection retained that real residual and emitted no decision
        # instead of relying on the series-specific convenience flag.
        suspense = session._find_open_suspense(line)
        self.assertTrue(suspense)
        self.assertFalse(self.company.currency_id.is_zero(sum(
            suspense.mapped('amount_residual')
        )))
        self.assertFalse(self.env['eh.reconciliation.audit'].search_count([
            ('statement_line_id', '=', line.id),
            ('decision', '=', 'write_off'),
        ]))

    def test_fx_writeoff_clears_proven_small_exchange_residual(self):
        gain, loss = self._configure_fx_accounts()
        session, line = self._make_proven_fx_residual(2.0)

        session.apply_fx_writeoff(line.id)

        line.invalidate_recordset()
        self.assertTrue(line.is_reconciled)
        # The FX residual lands on the exchange account via a separate
        # adjusting entry, not on the posted statement move.
        self.assertFalse(
            line.move_id.line_ids.filtered(
                lambda l: l.account_id in (gain | loss)))
        moved = self.env['account.move.line'].search([
            ('account_id', 'in', (gain | loss).ids),
            ('move_id', '!=', line.move_id.id),
            ('parent_state', '=', 'posted'),
        ])
        self.assertTrue(moved)

    def test_fx_writeoff_caller_cannot_raise_server_cap(self):
        self._configure_fx_accounts()
        session, line = self._make_proven_fx_residual(7.0)

        with self.assertRaisesRegex(UserError, 'cap of 5.00'):
            session.apply_fx_writeoff(line.id, max_amount=1000.0)

        suspense = session._find_open_suspense(line)
        self.assertTrue(suspense)
        self.assertFalse(self.company.currency_id.is_zero(sum(
            suspense.mapped('amount_residual')
        )))
        self.assertFalse(self.env['eh.reconciliation.audit'].search_count([
            ('statement_line_id', '=', line.id),
            ('decision', '=', 'write_off'),
        ]))
