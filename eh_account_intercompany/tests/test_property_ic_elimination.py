# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario matrix for the inter-company elimination engine.

Axes:

  direction {p2s, s2p}: the source document is keyed in the parent (A)
      towards the sub (B), or in the sub towards the parent.
  doc {invoice, bill}: the source is a customer invoice (the mirror is
      a vendor bill) or a vendor bill (the mirror is a customer
      invoice). Either way the pair always has exactly one OUT side
      carrying receivable + revenue and one IN side carrying payable +
      expense.
  match {exact, mismatch}: the mirror is posted untouched (totals equal)
      or edited to 900 before posting (source stays 1,000), which must
      flag the pair and eliminate at the common amount 900.
  fraction {0, 0.5, 1}: remaining fraction on the unrealised-profit
      line; unrealised = margin x fraction rounded to 2dp, where
      margin = seller subtotal - posting-date cost 600 x qty 1
      (engine-derived, never typed).

Every case runs on its own one-day period so the pair + period unique
constraint holds and cases cannot bleed into each other. Amount oracles
are computed in the loop from the case axes with the derivation inline.
"""

from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

AXES = {
    'direction': ['p2s', 's2p'],
    'doc': ['invoice', 'bill'],
    'match': ['exact', 'mismatch'],
    'fraction': [0.0, 0.5, 1.0],
}


@tagged('eh_golden', 'eh_account_intercompany', 'post_install', '-at_install')
class TestPropertyIcElimination(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Move = cls.env['account.move']
        cls.Batch = cls.env['eh.ic.elimination.batch']

        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Matrix Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.company_elim = cls.env['res.company'].create({
            'name': 'Matrix Consolidation Workspace',
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

        # Company B chart mirrors the company A codes seeded by the base
        # class (1100 receivable, 2100 payable, 4000 income, 5000
        # expense), so post-time code resolution into A always works.
        Account = cls.env['account.account'].sudo().with_company(
            cls.company_b)
        cls.receivable_b = Account.create({
            'code': '1100', 'name': 'IC Receivable B',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, cls.company_b.ids)],
            'reconcile': True,
        })
        cls.payable_b = Account.create({
            'code': '2100', 'name': 'IC Payable B',
            'account_type': 'liability_payable',
            'company_ids': [(6, 0, cls.company_b.ids)],
            'reconcile': True,
        })
        cls.revenue_b = Account.create({
            'code': '4000', 'name': 'IC Revenue B',
            'account_type': 'income',
            'company_ids': [(6, 0, cls.company_b.ids)],
        })
        cls.expense_b = Account.create({
            'code': '5000', 'name': 'IC Expense B',
            'account_type': 'expense',
            'company_ids': [(6, 0, cls.company_b.ids)],
        })

        Journal = cls.env['account.journal'].sudo()
        cls.journal_sale_b = Journal.create({
            'name': 'Sale B', 'code': 'SALB',
            'type': 'sale', 'company_id': cls.company_b.id,
        })
        cls.journal_purchase_b = Journal.create({
            'name': 'Purchase B', 'code': 'PURB',
            'type': 'purchase', 'company_id': cls.company_b.id,
        })

        # Partners: one represents the sub (used in A), one represents
        # the parent (used in B).
        cls.partner_sub = cls.env['res.partner'].create({
            'name': 'Matrix Sister B',
            'company_id': False,
            'eh_represented_company_id': cls.company_b.id,
        })
        cls.partner_parent = cls.env['res.partner'].create({
            'name': 'Matrix Parent A',
            'company_id': False,
            'eh_represented_company_id': cls.company_a.id,
        })

        # Deterministic receivable/payable resolution on every side.
        for partner in (cls.partner_sub, cls.company_b.partner_id):
            partner.with_company(cls.company_a).write({
                'property_account_receivable_id': cls.account_receivable.id,
                'property_account_payable_id': cls.account_payable.id,
            })
        for partner in (cls.partner_parent, cls.company_a.partner_id):
            partner.with_company(cls.company_b).write({
                'property_account_receivable_id': cls.receivable_b.id,
                'property_account_payable_id': cls.payable_b.id,
            })

        ElimAccount = cls.env['account.account'].sudo().with_company(
            cls.company_elim)
        seen_codes = set()
        for source, source_company in (
            (cls.account_receivable, cls.company_a),
            (cls.account_payable, cls.company_a),
            (cls.account_revenue, cls.company_a),
            (cls.account_expense, cls.company_a),
            (cls.receivable_b, cls.company_b),
            (cls.payable_b, cls.company_b),
            (cls.revenue_b, cls.company_b),
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
            mapped.with_company(cls.company_a).code = code
        Config = cls.env['eh.intercompany.config'].sudo()
        cls.config_a = Config.create({
            'company_id': cls.company_a.id,
            'enabled': True,
            'auto_post_mirror': False,
            'sale_journal_id': cls.journal_sale.id,
            'purchase_journal_id': cls.journal_purchase.id,
            'fallback_revenue_account_id': cls.account_revenue.id,
            'fallback_expense_account_id': cls.account_expense.id,
            'elimination_company_id': cls.company_elim.id,
        })
        cls.config_b = Config.create({
            'company_id': cls.company_b.id,
            'enabled': True,
            'auto_post_mirror': False,
            'sale_journal_id': cls.journal_sale_b.id,
            'purchase_journal_id': cls.journal_purchase_b.id,
            'fallback_revenue_account_id': cls.revenue_b.id,
            'fallback_expense_account_id': cls.expense_b.id,
            'elimination_company_id': cls.company_elim.id,
        })
        cls.unrealised_inventory_elim = ElimAccount.create({
            'code': '1300',
            'name': 'Unrealised profit in inventory',
            'account_type': 'asset_current',
            'company_ids': [(6, 0, cls.company_elim.ids)],
        })
        cls.unrealised_inventory_elim.with_company(cls.company_a).code = '1300'
        cls.unrealised_cogs_elim = ElimAccount.search([
            ('code', '=', '5000'),
        ], limit=1)
        cls.config_elim = Config.create({
            'company_id': cls.company_elim.id,
            'enabled': False,
            'elimination_company_id': cls.company_elim.id,
            'unrealised_inventory_account_id':
                cls.unrealised_inventory_elim.id,
            'unrealised_cogs_account_id': cls.unrealised_cogs_elim.id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Matrix product',
            'list_price': 1000.0,
            'standard_price': 600.0,
        })
        # Product-category defaults differ by chart template and Odoo series.
        # Mirror posting legitimately uses those resolved source accounts, so
        # build the consolidation chart from their actual codes rather than a
        # hard-coded version-specific list.
        for source_company in (cls.company_a, cls.company_b):
            product_accounts = cls.product.with_company(
                source_company,
            )._get_product_accounts()
            for source in product_accounts.values():
                if not source:
                    continue
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
                mapped.with_company(cls.company_a).code = code
        # Standard cost 600 in BOTH companies: the seller of a pair is
        # A for (p2s, invoice) / (s2p, bill) and B otherwise, and the
        # margin must derive from the SELLER company's standard cost.
        cls.product.with_company(cls.company_a).standard_price = 600.0
        cls.product.with_company(cls.company_b).standard_price = 600.0

    # ------------------------------------------------------------------
    # case machinery
    # ------------------------------------------------------------------

    def _source_setup(self, case):
        """Return (company, partner, move_type, journal, line_account)
        for the case's source document."""
        if case['direction'] == 'p2s':
            company, partner = self.company_a, self.partner_sub
            if case['doc'] == 'invoice':
                return (company, partner, 'out_invoice',
                        self.journal_sale, self.account_revenue)
            return (company, partner, 'in_invoice',
                    self.journal_purchase, self.account_expense)
        company, partner = self.company_b, self.partner_parent
        if case['doc'] == 'invoice':
            return (company, partner, 'out_invoice',
                    self.journal_sale_b, self.revenue_b)
        return (company, partner, 'in_invoice',
                self.journal_purchase_b, self.expense_b)

    def _run_case(self, case, day):
        company, partner, move_type, journal, account = (
            self._source_setup(case))
        source = self.Move.with_company(company).create({
            'move_type': move_type,
            'partner_id': partner.id,
            'journal_id': journal.id,
            'company_id': company.id,
            'invoice_date': day,
            'date': day,
            'invoice_line_ids': [(0, 0, {
                'name': 'IC matrix line',
                'quantity': 1.0,
                'price_unit': 1000.0,
                'product_id': self.product.id,
                'account_id': account.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        source.with_company(company).action_post()
        mirror = source.eh_intercompany_mirror_id
        self.assertTrue(mirror, 'mirror must be created for %s' % case)
        self.assertEqual(mirror.state, 'draft')
        # The main company's generic chart applies a default 15% purchase
        # tax to mirrored lines; strip it so pair totals reflect the
        # intended scenario (the engine rightly flags tax-asymmetric
        # totals as amount mismatches, which is not what this matrix
        # exercises).
        mirror.sudo().invoice_line_ids.write({'tax_ids': [(6, 0, [])]})
        # Elimination periods are ledger periods. Vendor bills otherwise may
        # default their accounting date to month-end, which would correctly
        # make a one-day cutoff treat the counterpart as not yet posted.
        mirror.sudo().write({'date': day})
        if case['match'] == 'mismatch':
            mirror.sudo().invoice_line_ids.write({'price_unit': 900.0})
        mirror.sudo().with_company(mirror.company_id).action_post()

        batch = self.Batch.create({
            'company_a_id': self.company_a.id,
            'company_b_id': self.company_b.id,
            'period_from': day,
            'period_to': day,
            'elimination_company_id': self.company_elim.id,
        })
        batch.action_compute()

        # Oracle: common eliminated amount per bucket. Source stays at
        # 1,000; a mismatched mirror is 900; the engine eliminates
        # min(source, mirror).
        expected = 900.0 if case['match'] == 'mismatch' else 1000.0
        # Seller subtotal: the OUT side of the pair. The mirror is the
        # OUT side exactly when the source is a bill, and only the
        # mirror is ever edited, so the seller subtotal is 900 only for
        # (doc=bill, match=mismatch).
        seller_subtotal = (
            900.0 if (case['doc'] == 'bill' and case['match'] == 'mismatch')
            else 1000.0)
        # margin = seller subtotal - immutable posting-date cost 600 x qty 1.
        margin = round(seller_subtotal - 600.0, 2)
        unrealised = round(margin * case['fraction'], 2)

        # ---- mismatch tab ----
        if case['match'] == 'mismatch':
            self.assertEqual(
                len(batch.mismatch_ids), 1,
                'mismatch rows: %s; legs: %s' % (
                    [(m.kind, m.reason) for m in batch.mismatch_ids],
                    [(l.kind, l.debit, l.credit) for l in batch.line_ids]))
            row = batch.mismatch_ids
            self.assertEqual(row.kind, 'amount')
            self.assertAlmostEqual(row.difference, 100.00, places=2)
        else:
            self.assertFalse(
                batch.mismatch_ids,
                'unexpected mismatch rows: %s' % [
                    (m.kind, m.source_amount, m.mirror_amount, m.reason)
                    for m in batch.mismatch_ids])

        # ---- elimination legs ----
        self.assertEqual(len(batch.line_ids), 4)
        by_kind = {line.kind: line for line in batch.line_ids}
        self.assertEqual(
            set(by_kind), {'receivable', 'payable', 'revenue', 'expense'})
        # Orientation invariants: the elimination always removes the
        # recognised asset/income against the recognised liability/
        # expense, whichever side of the pair each was booked on.
        self.assertAlmostEqual(by_kind['receivable'].credit, expected,
                               places=2)
        self.assertAlmostEqual(by_kind['payable'].debit, expected,
                               places=2)
        self.assertAlmostEqual(by_kind['revenue'].debit, expected,
                               places=2)
        self.assertAlmostEqual(by_kind['expense'].credit, expected,
                               places=2)
        self.assertAlmostEqual(
            sum(batch.line_ids.mapped('debit')),
            sum(batch.line_ids.mapped('credit')), places=2,
            msg='legs must balance for %s' % case)

        # ---- unrealised profit ----
        self.assertEqual(len(batch.unrealised_line_ids), 1)
        up = batch.unrealised_line_ids
        self.assertAlmostEqual(up.margin, margin, places=2)
        up.write({
            'remaining_fraction': case['fraction'],
            'inventory_evidence_date': batch.period_to,
            'inventory_evidence_reference':
                'PAIRWISE-STOCK-%s-%s' % (batch.id, up.id),
            'inventory_evidence_reviewed': True,
        })
        self.assertAlmostEqual(up.unrealised_amount, unrealised, places=2)

        # ---- consolidation hook ----
        summary = self.Batch.eh_ic_elimination_summary(
            day, day, [self.company_a.id, self.company_b.id])
        self.assertAlmostEqual(
            summary['receivable_eliminated'], expected, places=2)
        self.assertAlmostEqual(
            summary['payable_eliminated'], expected, places=2)
        self.assertAlmostEqual(
            summary['revenue_eliminated'], expected, places=2)
        self.assertAlmostEqual(
            summary['expense_eliminated'], expected, places=2)
        self.assertAlmostEqual(
            summary['unrealised_profit'], unrealised, places=2)
        self.assertEqual(
            summary['mismatch_count'],
            1 if case['match'] == 'mismatch' else 0)

        # ---- block / override on mismatched cases ----
        if case['match'] == 'mismatch':
            with self.assertRaises(UserError):
                batch.action_post()
            batch.block_on_mismatch = False
        batch.action_post()
        self.assertEqual(batch.state, 'posted')
        self.assertBalanced(batch.move_id)
        self.assertEqual(batch.move_id.company_id, self.company_elim)
        self.assertTrue(batch.move_id.eh_sealed)

    def test_pairwise_matrix(self):
        base = date(2026, 4, 1)
        for offset, case in enumerate(pairwise_cases(AXES)):
            with self.subTest(**case):
                # Residual balance elimination intentionally includes older
                # open pairs through each cutoff. Isolate matrix cases so this
                # test remains a per-case oracle rather than accidentally
                # accumulating previous invoices into later scenarios.
                with self.env.cr.savepoint() as savepoint:
                    self._run_case(case, base + timedelta(days=offset))
                    savepoint.rollback()
