# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden worked examples for the inter-company elimination pair engine
(IFRS 10.B86) in eh_account_intercompany.

Every expected amount is hand-derivable from the inputs stated in the
test, with the derivation in a comment; assertions are exact to 2dp and
nothing is read back from the engine to build an expectation.

Conventions exercised (read from models/ic_elimination_batch.py):

* A pair is one posted source move plus its posted mirror, both dated in
  the batch period. The OUT side (customer invoice) carries the
  receivable and revenue; the IN side carries the payable and expense.
* Each elimination leg NEGATES the recognised balance, so an invoice
  pair yields Cr receivable / Dr payable and Dr revenue / Cr expense.
* Totals diverging beyond one cent flag the pair on the mismatch tab
  and eliminate at the COMMON (lower) amount only.
* The margin on an unrealised-profit line is invoice line subtotal less
  selling-company standard cost captured when source posts; the
  unrealised amount is margin times the remaining fraction, rounded to
  the elimination currency (2dp).
"""

import importlib.util
import inspect
from pathlib import Path

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_intercompany', 'post_install', '-at_install')
class TestGoldenIcElimination(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Move = cls.env['account.move']
        cls.Batch = cls.env['eh.ic.elimination.batch']
        cls.Config = cls.env['eh.intercompany.config']

        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Elim Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.company_elim = cls.env['res.company'].create({
            'name': 'Group Consolidation Workspace',
            'currency_id': cls.company_a.currency_id.id,
            'eh_ic_consolidation_only': True,
        })
        if 'account_fiscal_country_id' in cls.company_a._fields:
            us = cls.env.ref('base.us')
            for comp in (cls.company_a, cls.company_b, cls.company_elim):
                if not comp.account_fiscal_country_id:
                    comp.sudo().account_fiscal_country_id = us.id
        cls.env.user.company_ids = [
            (4, cls.company_b.id), (4, cls.company_elim.id)]
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

        # Shared partner representing company B from company A's view.
        cls.partner_sub = cls.env['res.partner'].create({
            'name': 'Elim Sister B',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })

        # Company B journal for the bill mirror.
        cls.journal_purchase_b = cls.env['account.journal'].sudo().create({
            'name': 'Purchase B', 'code': 'PURB',
            'type': 'purchase', 'company_id': cls.company_b.id,
        })
        cls.journal_misc_b = cls.env['account.journal'].sudo().create({
            'name': 'Miscellaneous B', 'code': 'MISB',
            'type': 'general', 'company_id': cls.company_b.id,
        })

        # Company B accounts with the SAME CODES as the company A chart
        # seeded by EhAccountIntegrationTestCase (2100 Trade Payables,
        # 5000 Cost of Sales), so posting the elimination move in
        # company A resolves them by code.
        Account = cls.env['account.account'].sudo().with_company(
            cls.company_b)
        cls.expense_b = Account.create({
            'code': '5000',
            'name': 'IC Expense B',
            'account_type': 'expense',
            'company_ids': [(6, 0, cls.company_b.ids)],
        })
        cls.payable_b = Account.create({
            'code': '2100',
            'name': 'IC Payable B',
            'account_type': 'liability_payable',
            'company_ids': [(6, 0, cls.company_b.ids)],
            'reconcile': True,
        })
        cls.clearing_b = Account.create({
            'code': '1015',
            'name': 'Settlement clearing B',
            'account_type': 'asset_cash',
            'company_ids': [(6, 0, cls.company_b.ids)],
        })

        # Partner properties so the receivable/payable auto-lines
        # resolve deterministic accounts on both sides.
        cls.partner_sub.with_company(cls.company_a).write({
            'property_account_receivable_id': cls.account_receivable.id,
        })
        cls.company_a.partner_id.with_company(cls.company_b).write({
            'property_account_payable_id': cls.payable_b.id,
        })

        ElimAccount = cls.env['account.account'].sudo().with_company(
            cls.company_elim)
        seen_codes = set()
        elimination_accounts_by_code = {}
        for source, source_company in (
            (cls.account_receivable, cls.company_a),
            (cls.account_payable, cls.company_a),
            (cls.account_revenue, cls.company_a),
            (cls.account_expense, cls.company_a),
            (cls.payable_b, cls.company_b),
            (cls.expense_b, cls.company_b),
        ):
            code = source.with_company(source_company).code
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            mapped = ElimAccount.create({
                'code': code,
                'name': 'Elimination %s' % source.name,
                'account_type': source.account_type,
                'company_ids': [(6, 0, cls.company_elim.ids)],
                'reconcile': source.reconcile,
            })
            elimination_accounts_by_code[code] = mapped
            # Golden helpers read account.code in the test's current member
            # company context; seed that display value too. The account still
            # belongs only to the consolidation workspace.
            mapped.with_company(cls.company_a).code = code

        # Config on B: destination of the mirror when A sells to the
        # partner representing B. Eliminations book only in the separate
        # consolidation workspace, never company A's statutory ledger.
        cls.config_b = cls.Config.sudo().create({
            'company_id': cls.company_b.id,
            'enabled': True,
            'auto_post_mirror': True,
            'purchase_journal_id': cls.journal_purchase_b.id,
            'fallback_expense_account_id': cls.expense_b.id,
            'elimination_company_id': cls.company_elim.id,
        })
        # Member configs carry the default workspace; its own config stores
        # the dedicated ICEL journal.
        cls.config_a = cls.Config.sudo().create({
            'company_id': cls.company_a.id,
            'enabled': True,
            'elimination_company_id': cls.company_elim.id,
        })
        cls.unrealised_inventory_elim = ElimAccount.create({
            'code': '1300',
            'name': 'Unrealised profit in inventory',
            'account_type': 'asset_current',
            'company_ids': [(6, 0, cls.company_elim.ids)],
        })
        cls.unrealised_inventory_elim.with_company(cls.company_a).code = '1300'
        cls.unrealised_cogs_elim = elimination_accounts_by_code['5000']
        cls.config_elim = cls.Config.sudo().create({
            'company_id': cls.company_elim.id,
            'enabled': False,
            'elimination_company_id': cls.company_elim.id,
            'unrealised_inventory_account_id':
                cls.unrealised_inventory_elim.id,
            'unrealised_cogs_account_id': cls.unrealised_cogs_elim.id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Elim product',
            'list_price': 1000.0,
            'standard_price': 600.0,
        })
        cls.product.categ_id.with_company(cls.company_a).sudo().write({
            'property_account_expense_categ_id': cls.account_expense.id,
        })
        cls.product.categ_id.with_company(cls.company_b).sudo().write({
            'property_account_expense_categ_id': cls.expense_b.id,
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_sale_invoice(self, price=1000.0, qty=1.0,
                           invoice_date='2026-03-15', partner=None):
        """Customer invoice in A towards the partner representing B.
        Taxes are cleared explicitly so amount_total equals the untaxed
        amount and every expectation stays hand-derivable."""
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_invoice',
            'partner_id': (partner or self.partner_sub).id,
            'journal_id': self.journal_sale.id,
            'company_id': self.company_a.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'IC sale',
                'quantity': qty,
                'price_unit': price,
                'product_id': self.product.id,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })

    def _make_sale_credit(self, price=300.0,
                          invoice_date='2026-03-18'):
        return self.Move.with_company(self.company_a).create({
            'move_type': 'out_refund',
            'partner_id': self.partner_sub.id,
            'journal_id': self.journal_sale.id,
            'company_id': self.company_a.id,
            'invoice_date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'IC sale credit',
                'quantity': 1.0,
                'price_unit': price,
                'product_id': self.product.id,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })

    def _make_batch(self, period_from='2026-03-01',
                    period_to='2026-03-31'):
        return self.Batch.create({
            'company_a_id': self.company_a.id,
            'company_b_id': self.company_b.id,
            'period_from': period_from,
            'period_to': period_to,
            'elimination_company_id': self.company_elim.id,
        })

    def _approve_unrealised(self, batch, fraction=None):
        """Attach deterministic source-line cutoff evidence in posting tests."""
        for line in batch.unrealised_line_ids:
            vals = {
                'inventory_evidence_date': batch.period_to,
                'inventory_evidence_reference':
                    'TEST-STOCK-LEDGER-%s-%s' % (batch.id, line.id),
                'inventory_evidence_reviewed': True,
            }
            if fraction is not None:
                vals['remaining_fraction'] = fraction
            line.write(vals)

    def _settle_open_item(self, move, amount, settlement_date):
        """Post and reconcile one production-shaped partial settlement."""
        company = move.company_id
        open_line = move.line_ids.filtered(lambda line: (
            line.account_id.account_type
            in ('asset_receivable', 'liability_payable')
        ))
        self.assertEqual(len(open_line), 1)
        debit_open = open_line.balance > 0.0
        journal = (
            self.journal_misc
            if company == self.company_a else self.journal_misc_b
        )
        clearing = (
            self.account_cash
            if company == self.company_a else self.clearing_b
        )
        settlement = self.Move.sudo().with_company(company).create({
            'move_type': 'entry',
            'company_id': company.id,
            'journal_id': journal.id,
            'date': settlement_date,
            'line_ids': [
                (0, 0, {
                    'name': 'IC settlement',
                    'account_id': open_line.account_id.id,
                    'partner_id': open_line.partner_id.id,
                    'date_maturity': settlement_date,
                    'debit': 0.0 if debit_open else amount,
                    'credit': amount if debit_open else 0.0,
                }),
                (0, 0, {
                    'name': 'IC settlement clearing',
                    'account_id': clearing.id,
                    'debit': amount if debit_open else 0.0,
                    'credit': 0.0 if debit_open else amount,
                }),
            ],
        })
        settlement.action_post()
        settlement_line = settlement.line_ids.filtered(
            lambda line: line.account_id == open_line.account_id
        )
        (open_line | settlement_line).reconcile()
        partial = open_line.matched_debit_ids | open_line.matched_credit_ids
        self.assertTrue(partial)
        return settlement

    # ------------------------------------------------------------------
    # golden: matched pair, full elimination
    # ------------------------------------------------------------------

    def test_golden_matched_pair_elimination(self):
        """Invoice 1,000 A -> B, mirrored and auto-posted.

        Ledger recognised by the pair (no tax):
          A: Dr receivable 1100 1,000 / Cr revenue 4000 1,000
          B: Dr expense 5000 1,000 / Cr payable 2100 1,000

        The batch negates each recognised balance, so the elimination
        entry booked in A (accounts resolved by code) is exactly:
          Dr payable 2100 1,000.00
          Cr receivable 1100 1,000.00
          Dr revenue 4000 1,000.00
          Cr expense 5000 1,000.00
        """
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertTrue(mirror)
        self.assertEqual(mirror.state, 'posted')

        batch = self._make_batch()
        batch.action_compute()
        self.assertEqual(batch.state, 'computed')
        self.assertFalse(batch.mismatch_ids)
        self.assertEqual(len(batch.line_ids), 4)
        by_kind = {
            line.kind: line for line in batch.line_ids
        }
        self.assertEqual(
            set(by_kind), {'receivable', 'payable', 'revenue', 'expense'})
        # Orientation and amounts, leg by leg.
        self.assertAlmostEqual(by_kind['receivable'].credit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['receivable'].debit, 0.00, places=2)
        self.assertAlmostEqual(by_kind['payable'].debit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['revenue'].debit, 1000.00, places=2)
        self.assertAlmostEqual(by_kind['expense'].credit, 1000.00, places=2)
        # Gross totals feeding the consolidation hook.
        self.assertAlmostEqual(batch.receivable_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.payable_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.revenue_total, 1000.00, places=2)
        self.assertAlmostEqual(batch.expense_total, 1000.00, places=2)

        self._approve_unrealised(batch)
        batch.action_post()
        self.assertEqual(batch.state, 'posted')
        move = batch.move_id
        self.assertTrue(move)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.company_id, self.company_elim)
        self.assertMoveLines(move, [
            ('2100', 1000.00, 0.00),
            ('1100', 0.00, 1000.00),
            ('4000', 1000.00, 0.00),
            ('5000', 0.00, 1000.00),
        ])
        self.assertBalanced(move)
        # Journal auto-created on first use, in the elimination company,
        # and stored on that company's inter-company configuration.
        self.assertEqual(move.journal_id.code, 'ICEL')
        self.assertEqual(move.journal_id.type, 'general')
        self.assertEqual(move.journal_id.company_id, self.company_elim)
        self.assertEqual(self.config_elim.elimination_journal_id,
                         move.journal_id)
        # Sealed: the posted counterpart of the batch cannot be unposted
        # outside the sanctioned reset path.
        self.assertTrue(move.eh_sealed)
        with self.assertRaises(UserError):
            move.button_draft()

        # Consolidation hook totals for the period.
        summary = self.Batch.eh_ic_elimination_summary(
            '2026-03-01', '2026-03-31',
            [self.company_a.id, self.company_b.id])
        self.assertAlmostEqual(
            summary['receivable_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['payable_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['revenue_eliminated'], 1000.00, places=2)
        self.assertAlmostEqual(
            summary['expense_eliminated'], 1000.00, places=2)
        self.assertEqual(summary['mismatch_count'], 0)
        self.assertEqual(len(summary['batches']), 1)

    # ------------------------------------------------------------------
    # golden: reset unwinds the sealed move
    # ------------------------------------------------------------------

    def test_golden_reset_reverses_sealed_move(self):
        """Reset retains the sealed original and its sealed counter-entry.

        The two posted entries net the elimination ledger to zero while
        preserving the complete GL evidence chain on the batch.
        """
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        self._approve_unrealised(batch)
        batch.action_post()
        move = batch.move_id
        self.assertTrue(move)
        batch.action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')
        self.assertEqual(batch.move_id, move)
        self.assertTrue(move.exists())
        self.assertTrue(batch.reversal_move_id)
        self.assertEqual(batch.reversal_move_id.state, 'posted')
        self.assertIn(move, batch.move_history_ids)
        self.assertIn(batch.reversal_move_id, batch.move_history_ids)
        self.assertAlmostEqual(
            sum((move | batch.reversal_move_id).line_ids.mapped('balance')),
            0.0,
            places=2,
        )

    # ------------------------------------------------------------------
    # golden: amount mismatch, block, override
    # ------------------------------------------------------------------

    def test_golden_mismatch_blocks_then_override_posts_common_amount(self):
        """Mirror edited to 900 before posting.

        Source total 1,000 vs mirror total 900: difference 100.00 on
        the mismatch tab. The pair is eliminated at the COMMON amount
        min(1000, 900) = 900 per bucket. Posting is refused while
        block_on_mismatch holds; clearing the flag (the audited
        override) posts:
          Dr payable 2100 900.00 / Cr receivable 1100 900.00
          Dr revenue 4000 900.00 / Cr expense 5000 900.00
        """
        self.config_b.auto_post_mirror = False
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(mirror.state, 'draft')
        mirror.sudo().invoice_line_ids.write({'price_unit': 900.0})
        mirror.sudo().with_company(self.company_b).action_post()
        self.assertAlmostEqual(mirror.amount_total, 900.00, places=2)

        batch = self._make_batch()
        batch.action_compute()
        self.assertEqual(len(batch.mismatch_ids), 1)
        mismatch = batch.mismatch_ids
        self.assertEqual(mismatch.kind, 'amount')
        self.assertAlmostEqual(mismatch.source_amount, 1000.00, places=2)
        self.assertAlmostEqual(mismatch.mirror_amount, 900.00, places=2)
        self.assertAlmostEqual(mismatch.difference, 100.00, places=2)
        self.assertTrue(mismatch.reason)

        # Blocked by default.
        self.assertTrue(batch.block_on_mismatch)
        with self.assertRaises(UserError):
            batch.action_post()
        self.assertEqual(batch.state, 'computed')

        # Override: clear the flag, post the common amount.
        batch.block_on_mismatch = False
        self._approve_unrealised(batch)
        batch.action_post()
        self.assertMoveLines(batch.move_id, [
            ('2100', 900.00, 0.00),
            ('1100', 0.00, 900.00),
            ('4000', 900.00, 0.00),
            ('5000', 0.00, 900.00),
        ])
        self.assertBalanced(batch.move_id)
        # The mismatch stays listed for follow-up after the override.
        self.assertEqual(len(batch.mismatch_ids), 1)

    def test_non_expense_purchase_is_exception_not_fake_translation(self):
        """A stock-input/asset purchase is not an IAS 21 difference.

        The reciprocal flow engine cannot eliminate a sale-revenue leg against
        an asset leg as revenue/expense. It must retain an actionable mismatch
        and, on an explicit override, post only the reciprocal AR/AP amount.
        It must never use the configured FX account to force that structurally
        non-reciprocal flow to balance.
        """
        stock_input = self.env['account.account'].sudo().with_company(
            self.company_b,
        ).create({
            'code': '1198',
            'name': 'Anglo-Saxon Stock Input B',
            'account_type': 'asset_current',
            'company_ids': [(6, 0, self.company_b.ids)],
        })
        fake_cta = self._ensure_account(
            self.env, '7998', 'FX Translation Sentinel', 'income_other',
        )
        self.company_a.income_currency_exchange_account_id = fake_cta
        self.config_b.auto_post_mirror = False
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        mirror.sudo().invoice_line_ids.account_id = stock_input
        mirror.sudo().with_company(self.company_b).action_post()

        batch = self._make_batch()
        batch.action_compute()
        self.assertTrue(batch.mismatch_ids)
        self.assertIn('Period flow mismatch', batch.mismatch_ids.reason)
        self.assertEqual(set(batch.line_ids.mapped('kind')), {
            'receivable', 'payable',
        })

        batch.block_on_mismatch = False
        self._approve_unrealised(batch)
        batch.action_post()
        self.assertBalanced(batch.move_id)
        self.assertNotIn(fake_cta, batch.move_id.line_ids.account_id)
        self.assertEqual(
            set(batch.move_id.line_ids.account_id.mapped('code')),
            {'1100', '2100'},
        )

    def test_large_posting_residual_refuses_fx_plug(self):
        """Persisted malformed evidence fails closed, even with FX configured."""
        fake_cta = self._ensure_account(
            self.env, '7998', 'FX Translation Sentinel', 'income_other',
        )
        self.company_a.income_currency_exchange_account_id = fake_cta
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        expense = batch.line_ids.filtered(lambda line: line.kind == 'expense')
        expense.sudo().with_context(eh_ic_elim_engine=True).write({
            'credit': expense.credit - 100.0,
        })
        with self.assertRaisesRegex(UserError, 'structurally unbalanced'):
            batch._eh_build_elimination_move()
        self.assertFalse(batch.move_id)

    def test_golden_unmatched_source_flagged_no_legs(self):
        """With mirroring disabled the posted source has no mirror at
        all: the batch lists it as 'no mirror' with zero legs, and
        posting is refused (nothing to post AND mismatch present)."""
        self.config_b.enabled = False
        sale = self._make_sale_invoice()
        sale.action_post()
        self.assertFalse(sale.eh_intercompany_mirror_id)
        self.config_b.enabled = True

        batch = self._make_batch()
        batch.action_compute()
        self.assertFalse(batch.line_ids)
        self.assertEqual(len(batch.mismatch_ids), 1)
        self.assertEqual(batch.mismatch_ids.kind, 'no_mirror')
        with self.assertRaises(UserError):
            batch.action_post()

    # ------------------------------------------------------------------
    # golden: idempotency
    # ------------------------------------------------------------------

    def test_golden_recompute_replaces_lines_once(self):
        """Recompute is idempotent: two computes leave exactly one set
        of four legs (the engine wipes and rebuilds), and duplicate
        batches per pair + period are refused in both pair orders."""
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        first_ids = set(batch.line_ids.ids)
        self.assertEqual(len(first_ids), 4)
        self._approve_unrealised(batch, fraction=0.25)
        approved_source_line = batch.unrealised_line_ids.source_move_line_id
        batch.action_compute()
        self.assertEqual(len(batch.line_ids), 4)
        # Replaced, not appended.
        self.assertFalse(first_ids & set(batch.line_ids.ids))
        self.assertAlmostEqual(batch.receivable_total, 1000.00, places=2)
        self.assertEqual(
            batch.unrealised_line_ids.source_move_line_id,
            approved_source_line,
        )
        self.assertTrue(
            batch.unrealised_line_ids.inventory_evidence_reviewed)
        self.assertAlmostEqual(
            batch.unrealised_line_ids.remaining_fraction, 0.25, places=4)

        # Same pair + period: DB unique constraint.
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                dup = self.Batch.create({
                    'company_a_id': self.company_a.id,
                    'company_b_id': self.company_b.id,
                    'period_from': '2026-03-01',
                    'period_to': '2026-03-31',
                    'elimination_company_id': self.company_elim.id,
                })
                dup.flush_recordset()

        # Reversed pair, same period: python constraint.
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.Batch.create({
                    'company_a_id': self.company_b.id,
                    'company_b_id': self.company_a.id,
                    'period_from': '2026-03-01',
                    'period_to': '2026-03-31',
                    'elimination_company_id': self.company_elim.id,
                })

    def test_outstanding_aggregates_partial_allocations_and_credit_notes(self):
        """Balance elimination uses pair-level net outstanding, not gross.

        March documents:
          invoices 600 + 400, credit note 300 = net flow 700.
        Settlements through cutoff:
          A allocates 250 to invoice 1; B allocates 250 to invoice 2.

        Per-document residuals differ, but reciprocal company-pair balances
        both equal 600 + 400 - 300 - 250 = 450.  No false mismatch is valid;
        AR/AP elimination is 450 while revenue/expense remains net 700.
        """
        first = self._make_sale_invoice(price=600.0)
        second = self._make_sale_invoice(
            price=400.0, invoice_date='2026-03-16',
        )
        credit = self._make_sale_credit(price=300.0)
        for move in (first, second, credit):
            move.action_post()
            self.assertEqual(move.eh_intercompany_mirror_id.state, 'posted')

        self._settle_open_item(first, 250.0, '2026-03-25')
        self._settle_open_item(
            second.eh_intercompany_mirror_id, 250.0, '2026-03-25',
        )

        batch = self._make_batch()
        batch.action_compute()

        self.assertFalse(batch.mismatch_ids)
        self.assertAlmostEqual(batch.receivable_total, 450.0, places=2)
        self.assertAlmostEqual(batch.payable_total, 450.0, places=2)
        self.assertAlmostEqual(batch.revenue_total, 700.0, places=2)
        self.assertAlmostEqual(batch.expense_total, 700.0, places=2)
        self._approve_unrealised(batch)
        batch.action_post()
        self.assertBalanced(batch.move_id)
        by_kind = {}
        for line in batch.line_ids:
            by_kind[line.kind] = by_kind.get(line.kind, 0.0) + (
                line.debit - line.credit
            )
        self.assertAlmostEqual(by_kind['receivable'], -450.0, places=2)
        self.assertAlmostEqual(by_kind['payable'], 450.0, places=2)
        self.assertAlmostEqual(by_kind['revenue'], 700.0, places=2)
        self.assertAlmostEqual(by_kind['expense'], -700.0, places=2)

    def test_historical_residual_ignores_settlement_after_cutoff(self):
        """A later reconciliation cannot rewrite period-end IC balances."""
        sale = self._make_sale_invoice(invoice_date='2026-02-15')
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self._settle_open_item(sale, 1000.0, '2026-04-15')
        self._settle_open_item(mirror, 1000.0, '2026-04-15')
        sale.invalidate_recordset(['amount_residual'])
        mirror.invalidate_recordset(['amount_residual'])
        self.assertAlmostEqual(sale.amount_residual, 0.0, places=2)
        self.assertAlmostEqual(mirror.amount_residual, 0.0, places=2)

        batch = self._make_batch()
        batch.action_compute()

        # Invoice predates period, so no March P&L flow; it was still open at
        # March 31 because both partial reconciliations carry max_date Apr 15.
        self.assertAlmostEqual(batch.receivable_total, 1000.0, places=2)
        self.assertAlmostEqual(batch.payable_total, 1000.0, places=2)
        self.assertAlmostEqual(batch.revenue_total, 0.0, places=2)
        self.assertAlmostEqual(batch.expense_total, 0.0, places=2)
        self.assertFalse(batch.mismatch_ids)

    def test_period_end_balance_posts_delta_and_reset_replays_idempotently(self):
        """Successive periods leave one current balance in persistent GL."""
        sale = self._make_sale_invoice()
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id

        march = self._make_batch()
        march.action_compute()
        self._approve_unrealised(march)
        march.action_post()
        self.assertMoveLines(march.move_id, [
            ('2100', 1000.0, 0.0),
            ('1100', 0.0, 1000.0),
            ('4000', 1000.0, 0.0),
            ('5000', 0.0, 1000.0),
        ])

        self._settle_open_item(sale, 200.0, '2026-04-10')
        self._settle_open_item(mirror, 200.0, '2026-04-10')
        april = self._make_batch('2026-04-01', '2026-04-30')
        april.action_compute()
        self.assertAlmostEqual(april.receivable_total, 800.0, places=2)
        self.assertAlmostEqual(april.payable_total, 800.0, places=2)
        self.assertAlmostEqual(april.revenue_total, 0.0, places=2)
        self._approve_unrealised(april)
        april.action_post()
        # March left Cr AR / Dr AP 1,000.  April target is 800, so only
        # Dr AR / Cr AP 200 is posted; cumulative active balance is 800.
        self.assertMoveLines(april.move_id, [
            ('1100', 200.0, 0.0),
            ('2100', 0.0, 200.0),
        ])

        def active_elimination_balance(code):
            lines = (march.move_id | april.move_history_ids).mapped('line_ids')
            return sum(
                line.balance for line in lines
                if line.account_id.code == code
            )

        self.assertAlmostEqual(
            active_elimination_balance('1100'), -800.0, places=2,
        )
        self.assertAlmostEqual(
            active_elimination_balance('2100'), 800.0, places=2,
        )
        ytd = self.Batch.eh_ic_elimination_summary(
            '2026-03-01', '2026-04-30',
            [self.company_a.id, self.company_b.id],
        )
        self.assertAlmostEqual(
            ytd['receivable_eliminated'], 800.0, places=2,
        )
        self.assertAlmostEqual(ytd['payable_eliminated'], 800.0, places=2)
        self.assertAlmostEqual(ytd['revenue_eliminated'], 1000.0, places=2)
        self.assertAlmostEqual(ytd['expense_eliminated'], 1000.0, places=2)
        with self.assertRaisesRegex(UserError, 'later/same-cutoff'):
            march.action_reset_to_draft()
        overlapping = self._make_batch('2026-04-15', '2026-05-15')
        overlapping.action_compute()
        self._approve_unrealised(overlapping)
        with self.assertRaisesRegex(UserError, 'overlaps posted batch'):
            overlapping.action_post()

        april.action_reset_to_draft()
        self.assertAlmostEqual(
            active_elimination_balance('1100'), -1000.0, places=2,
        )
        self.assertAlmostEqual(
            active_elimination_balance('2100'), 1000.0, places=2,
        )
        april.action_compute()
        self._approve_unrealised(april)
        april.action_post()
        self.assertAlmostEqual(
            active_elimination_balance('1100'), -800.0, places=2,
        )
        self.assertAlmostEqual(
            active_elimination_balance('2100'), 800.0, places=2,
        )

    def test_reciprocal_functional_balances_compare_in_elimination_currency(self):
        """Different document/functional currencies do not false-mismatch."""
        fx = self.env['res.currency'].create({
            'name': 'ICX',
            'symbol': 'X',
            'rounding': 0.01,
            'active': True,
        })
        self.company_b.currency_id = fx.id
        for day in ('2026-03-01', '2026-03-31'):
            self._set_rate(fx, day, 0.5, company=self.company_a)
            self._set_rate(fx, day, 0.5, company=self.company_elim)
        self.assertAlmostEqual(
            fx._convert(
                500.0, self.company_a.currency_id,
                self.company_a, fields.Date.to_date('2026-03-31'),
            ),
            1000.0,
            places=2,
        )
        self.config_b.auto_post_mirror = False
        sale = self._make_sale_invoice(price=1000.0)
        sale.action_post()
        mirror = sale.eh_intercompany_mirror_id
        self.assertEqual(mirror.state, 'draft')
        mirror.sudo().with_company(self.company_b).write({
            'currency_id': fx.id,
        })
        mirror.sudo().invoice_line_ids.write({'price_unit': 500.0})
        mirror.sudo().with_company(self.company_b).action_post()
        self.assertAlmostEqual(sale.amount_total, 1000.0, places=2)
        self.assertAlmostEqual(mirror.amount_total, 500.0, places=2)

        batch = self._make_batch()
        batch.action_compute()

        self.assertFalse(batch.mismatch_ids)
        self.assertAlmostEqual(batch.receivable_total, 1000.0, places=2)
        self.assertAlmostEqual(batch.payable_total, 1000.0, places=2)
        self.assertAlmostEqual(batch.revenue_total, 1000.0, places=2)
        self.assertAlmostEqual(batch.expense_total, 1000.0, places=2)

    # ------------------------------------------------------------------
    # golden: unrealised profit from source documents
    # ------------------------------------------------------------------

    def test_sale_date_cost_snapshot_survives_later_cost_change(self):
        self.product.with_company(self.company_a).standard_price = 600.0
        sale = self._make_sale_invoice(price=1000.0, qty=1.0)
        sale.action_post()
        mirror_line = sale.eh_intercompany_mirror_id.invoice_line_ids[:1]
        self.assertAlmostEqual(
            mirror_line.eh_ic_source_cost_amount, 600.0, places=2,
        )
        self.assertEqual(
            mirror_line.eh_ic_source_cost_date, sale.date,
        )

        self.product.with_company(self.company_a).standard_price = 950.0
        batch = self._make_batch()
        batch.action_compute()
        line = batch.unrealised_line_ids
        self.assertAlmostEqual(line.unit_cost, 600.0, places=2)
        self.assertAlmostEqual(line.margin, 400.0, places=2)

    def test_cancelled_source_creates_orphan_mismatch(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        self.assertEqual(sale.eh_intercompany_mirror_id.state, 'posted')
        sale.button_draft()
        sale.button_cancel()

        batch = self._make_batch()
        batch.action_compute()
        self.assertEqual(len(batch.mismatch_ids), 1)
        self.assertEqual(batch.mismatch_ids.kind, 'orphan_mirror')
        self.assertEqual(
            batch.mismatch_ids.mirror_move_id,
            sale.eh_intercompany_mirror_id,
        )

    def test_unrealised_profit_uses_point_in_time_delta(self):
        self.product.with_company(self.company_a).standard_price = 600.0
        sale = self._make_sale_invoice(price=1000.0)
        sale.action_post()
        march = self._make_batch()
        march.action_compute()
        self._approve_unrealised(march, fraction=0.5)
        march.action_post()
        march_inventory = march.move_id.line_ids.filtered(
            lambda item:
                item.eh_ic_elimination_role == 'unrealised_inventory'
        )
        self.assertAlmostEqual(march_inventory.credit, 200.0, places=2)

        april = self._make_batch(
            period_from='2026-04-01', period_to='2026-04-30',
        )
        april.action_compute()
        self._approve_unrealised(april, fraction=0.25)
        april.action_post()
        april_inventory = april.move_id.line_ids.filtered(
            lambda item:
                item.eh_ic_elimination_role == 'unrealised_inventory'
        )
        april_cogs = april.move_id.line_ids.filtered(
            lambda item: item.eh_ic_elimination_role == 'unrealised_cogs'
        )
        self.assertAlmostEqual(april_inventory.debit, 100.0, places=2)
        self.assertAlmostEqual(april_cogs.credit, 100.0, places=2)

    def test_lock_and_duplicate_checks_are_scope_safe(self):
        batch_type = type(self._make_batch())
        lock_source = inspect.getsource(
            batch_type._eh_lock_for_transition
        )
        pair_source = inspect.getsource(
            batch_type._check_pair
        )
        self.assertIn('pg_advisory_xact_lock', lock_source)
        self.assertNotIn('FROM res_company', lock_source)
        self.assertIn('self.sudo().search_count', pair_source)

    def test_history_migration_backfills_original_and_reversal(self):
        sale = self._make_sale_invoice()
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()
        self._approve_unrealised(batch)
        batch.action_post()
        original = batch.move_id
        batch.action_reset_to_draft()
        reversal = batch.reversal_move_id
        self.assertTrue(original and reversal)
        batch.flush_recordset(['move_id', 'reversal_move_id'])
        self.env.cr.execute(
            "SELECT move_id, reversal_move_id "
            "FROM eh_ic_elimination_batch WHERE id = %s",
            [batch.id],
        )
        self.assertEqual(
            tuple(self.env.cr.fetchone()), (original.id, reversal.id),
        )
        self.env.cr.execute(
            "DELETE FROM eh_ic_elimination_batch_move_history_rel "
            "WHERE batch_id = %s",
            [batch.id],
        )
        batch.invalidate_recordset(['move_history_ids'])
        self.assertFalse(batch.move_history_ids)

        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.1' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_ic_migration_1_3_1', str(migration_path),
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.0')
        batch.invalidate_recordset(['move_history_ids'])
        self.assertEqual(
            set(batch.move_history_ids.ids), {original.id, reversal.id},
        )

    def test_golden_unrealised_profit_margin_times_fraction(self):
        """Invoice 1,000, product standard cost 600 (selling company):

          margin = 1,000.00 - 600.00 x 1 = 400.00 (engine-derived)

        Remaining fraction 0.5 entered per line (stock fraction is not
        available for a non-storable product):

          unrealised = 400.00 x 0.5 = 200.00

        Summary hook reports it and sealed elimination move books one
        point-in-time inventory/cost-of-sales delta.
        """
        self.product.with_company(self.company_a).standard_price = 600.0
        sale = self._make_sale_invoice(price=1000.0, qty=1.0)
        sale.action_post()
        batch = self._make_batch()
        batch.action_compute()

        self.assertEqual(len(batch.unrealised_line_ids), 1)
        line = batch.unrealised_line_ids
        self.assertEqual(line.product_id, self.product)
        self.assertAlmostEqual(line.price_subtotal, 1000.00, places=2)
        self.assertAlmostEqual(line.unit_cost, 600.00, places=2)
        self.assertAlmostEqual(line.margin, 400.00, places=2)
        # Aggregate stock quants are never accepted as cutoff/provenance
        # evidence: the exact-line fraction starts at zero until reviewed.
        self.assertEqual(line.fraction_source, 'manual')
        self.assertAlmostEqual(line.remaining_fraction, 0.0, places=4)
        self.assertAlmostEqual(line.unrealised_amount, 0.00, places=2)

        with self.assertRaisesRegex(UserError, 'without reviewed'):
            batch.action_post()
        self._approve_unrealised(batch, fraction=0.5)
        self.assertAlmostEqual(line.unrealised_amount, 200.00, places=2)
        self.assertAlmostEqual(batch.unrealised_total, 200.00, places=2)

        summary = self.Batch.eh_ic_elimination_summary(
            '2026-03-01', '2026-03-31',
            [self.company_a.id, self.company_b.id])
        self.assertAlmostEqual(summary['unrealised_profit'], 200.00,
                               places=2)

        # The margin is engine-derived and cannot be hand-typed.
        with self.assertRaises(UserError):
            line.margin = 999.0
        batch.action_post()
        kinds = set()
        for mline in batch.move_id.line_ids:
            kinds.add(mline.account_id.code)
        self.assertEqual(kinds, {'1100', '1300', '2100', '4000', '5000'})
        inventory_leg = batch.move_id.line_ids.filtered(
            lambda item:
                item.eh_ic_elimination_role == 'unrealised_inventory'
        )
        cogs_leg = batch.move_id.line_ids.filtered(
            lambda item: item.eh_ic_elimination_role == 'unrealised_cogs'
        )
        self.assertAlmostEqual(inventory_leg.credit, 200.0, places=2)
        self.assertAlmostEqual(cogs_leg.debit, 200.0, places=2)

    def test_unrealised_inventory_is_scoped_to_exact_source_lines(self):
        first = self._make_sale_invoice(price=1000.0, qty=2.0)
        second = self._make_sale_invoice(price=500.0, qty=1.0)
        first.action_post()
        second.action_post()
        batch = self._make_batch()
        batch.action_compute()

        lines = batch.unrealised_line_ids
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            set(lines.source_move_line_id.ids),
            set((first | second).invoice_line_ids.ids),
        )
        self.assertEqual(set(lines.mapped('fraction_source')), {'manual'})
        self.assertTrue(all(
            not line.remaining_fraction and not line.remaining_qty
            for line in lines
        ))
        # Each workpaper approval is independent: there is no shared buyer
        # on-hand pool that can be allocated in full to both invoices.
        for index, line in enumerate(lines.sorted('source_move_line_id')):
            fraction = (0.25, 0.75)[index]
            line.write({
                'remaining_fraction': fraction,
                'inventory_evidence_date': batch.period_to,
                'inventory_evidence_reference':
                    'TRACE-SOURCE-LINE-%s' % line.source_move_line_id.id,
                'inventory_evidence_reviewed': True,
            })
            self.assertAlmostEqual(
                line.remaining_qty, line.quantity * fraction, places=4)

    # ------------------------------------------------------------------
    # golden: restrict_ic_partners posting guard
    # ------------------------------------------------------------------

    def test_golden_restrict_flag_blocks_unflagged_group_partner(self):
        """With restrict_ic_partners on company A's config, posting an
        invoice towards company B's RAW company partner (no Represented
        Company flag) is refused before anything books; with the flag
        off the same post succeeds (old semantics preserved); and a
        properly flagged partner posts fine with the flag on."""
        group_partner = self.company_b.partner_id
        group_partner.with_company(self.company_a).write({
            'property_account_receivable_id': self.account_receivable.id,
        })

        def make_invoice():
            return self.Move.with_company(self.company_a).create({
                'move_type': 'out_invoice',
                'partner_id': group_partner.id,
                'journal_id': self.journal_sale.id,
                'company_id': self.company_a.id,
                'invoice_date': '2026-03-20',
                'invoice_line_ids': [(0, 0, {
                    'name': 'Mis-keyed IC sale',
                    'quantity': 1.0,
                    'price_unit': 500.0,
                    'account_id': self.account_revenue.id,
                    'tax_ids': [(6, 0, [])],
                })],
            })

        self.config_a.restrict_ic_partners = True
        blocked = make_invoice()
        with self.assertRaises(UserError):
            blocked.action_post()
        self.assertNotEqual(blocked.state, 'posted')

        # Flag off: default semantics, the post goes through.
        self.config_a.restrict_ic_partners = False
        allowed = make_invoice()
        allowed.action_post()
        self.assertEqual(allowed.state, 'posted')

        # Flag on but the partner is properly flagged for mirroring:
        # the sanctioned IC path stays open.
        self.config_a.restrict_ic_partners = True
        flagged = self._make_sale_invoice(invoice_date='2026-03-21')
        flagged.action_post()
        self.assertEqual(flagged.state, 'posted')
        self.assertTrue(flagged.eh_intercompany_mirror_id)
