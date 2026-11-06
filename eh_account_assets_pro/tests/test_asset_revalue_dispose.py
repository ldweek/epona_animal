# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset revaluation and disposal flows.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetRevalueDispose(EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Manager group needed for both flows.
        cls.env.user.group_ids |= cls.env.ref('account.group_account_manager')

    @staticmethod
    def _post_through(asset, cutoff):
        """Post a deterministic historical slice, independent of wall time."""
        cutoff = fields.Date.to_date(cutoff)
        asset.depreciation_line_ids.filtered(
            lambda line: line.depreciation_date <= cutoff,
        ).action_post()

    def test_dispose_with_no_proceeds_books_loss(self):
        asset = self._make_asset(
            code='DSP-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        self._post_through(asset, '2025-06-30')
        nbv_before = asset.net_book_value
        self.assertAlmostEqual(nbv_before, 6000.0, places=2)
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2025-07-15',
            'proceeds': 0.0,
        })
        wizard.action_dispose()
        self.assertEqual(asset.state, 'disposed')
        self.assertTrue(asset.disposal_move_id)
        self.assertTrue(
            asset.disposal_move_id.eh_sealed,
            "a disposal entry backs a frozen asset figure and must be sealed",
        )
        with self.assertRaises(UserError):
            asset.disposal_move_id.button_draft()
        # No remaining unposted lines.
        self.assertFalse(asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        ))
        # Disposal move references the loss account because proceeds < NBV.
        loss_lines = asset.disposal_move_id.line_ids.filtered(
            lambda l: l.account_id == self.account_disposal_loss,
        )
        self.assertEqual(len(loss_lines), 1)
        # July earns 15/31 of the 1,000 monthly charge before IAS 16.55
        # derecognition: 483.87 depreciation and 5,516.13 carrying value.
        stub = asset.depreciation_line_ids.filtered('is_event_accrual')
        self.assertEqual(len(stub), 1)
        self.assertEqual(str(stub.depreciation_date), '2025-07-15')
        self.assertAlmostEqual(stub.amount, 483.87, places=2)
        self.assertTrue(stub.is_posted)
        self.assertTrue(stub.move_id)
        self.assertAlmostEqual(loss_lines.debit, 5_516.13, places=2)
        accumulated_lines = asset.disposal_move_id.line_ids.filtered(
            lambda line: (
                line.account_id == self.account_accum_dep
                and line.debit > 0.0
            ),
        )
        asset_lines = asset.disposal_move_id.line_ids.filtered(
            lambda line: (
                line.account_id == self.account_fixed
                and line.credit > 0.0
            ),
        )
        self.assertEqual(len(accumulated_lines), 1)
        self.assertAlmostEqual(accumulated_lines.debit, 6_483.87, places=2)
        self.assertEqual(len(asset_lines), 1)
        self.assertAlmostEqual(asset_lines.credit, 12000.0, places=2)
        self.assertAlmostEqual(
            sum(asset.disposal_move_id.line_ids.mapped('debit')),
            sum(asset.disposal_move_id.line_ids.mapped('credit')),
            places=2,
        )

    def test_dispose_with_gain(self):
        asset = self._make_asset(
            code='DSP-G-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=12,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2026-04-30',
            'proceeds': 5000.0,
            'cash_account_id': self.account_cash.id,
        })
        wizard.action_dispose()
        self.assertEqual(asset.state, 'disposed')
        gain = asset.disposal_move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_disposal_gain,
        )
        loss = asset.disposal_move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_disposal_loss,
        )
        cash = asset.disposal_move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_cash,
        )
        contra = asset.disposal_move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_accum_dep,
        )
        cost = asset.disposal_move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_fixed,
        )
        self.assertEqual(len(gain), 1)
        self.assertFalse(loss)
        self.assertAlmostEqual(gain.credit, 5_000.0, places=2)
        self.assertAlmostEqual(cash.debit, 5_000.0, places=2)
        self.assertAlmostEqual(contra.debit, 12_000.0, places=2)
        self.assertAlmostEqual(cost.credit, 12_000.0, places=2)
        self.assertAlmostEqual(
            sum(asset.disposal_move_id.line_ids.mapped('debit')),
            sum(asset.disposal_move_id.line_ids.mapped('credit')),
            places=2,
        )

    def test_dispose_blocks_when_already_disposed(self):
        asset = self._make_asset(code='DSP-X-1')
        asset.action_activate()
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
        })
        wizard.action_dispose()
        # A second disposal should fail.
        wizard2 = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
        })
        with self.assertRaises(UserError):
            wizard2.action_dispose()

    def test_dispose_through_taxed_customer_invoice_uses_one_tax_source(self):
        """The invoice owns receivable and output tax; disposal clears only
        its net revenue line and stores durable sale evidence."""
        output_tax = self._ensure_account(
            self.env, '2215', 'Asset Sale Output Tax', 'liability_current',
        )
        tax_vals = {
            'name': 'Asset sale GST 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        }
        if 'country_id' in self.env['account.tax']._fields:
            fiscal_country = getattr(
                self.company, 'account_fiscal_country_id', False,
            )
            if not fiscal_country:
                fiscal_country = self.company.country_id or self.env.ref('base.us')
                self.company.sudo().country_id = fiscal_country.id
                fiscal_country = (
                    self.company.account_fiscal_country_id or fiscal_country
                )
            tax_vals['country_id'] = fiscal_country.id
        tax = self.env['account.tax'].create(tax_vals)
        (tax.invoice_repartition_line_ids
         | tax.refund_repartition_line_ids).filtered(
            lambda line: line.repartition_type == 'tax',
        ).write({'account_id': output_tax.id})

        asset = self._make_asset(
            code='DSP-INV-TAX-1',
            acquisition_cost=12_000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_b.id,
            'journal_id': self.journal_sale.id,
            'invoice_date': '2026-02-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale of fixed asset',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 1_000.0,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        invoice.action_post()
        sale_line = invoice.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product',
        )
        self.assertEqual(len(sale_line), 1)
        self.assertAlmostEqual(sale_line.balance, -1_000.0, places=2)
        self.assertAlmostEqual(invoice.amount_untaxed, 1_000.0, places=2)
        self.assertAlmostEqual(invoice.amount_tax, 100.0, places=2)
        self.assertAlmostEqual(invoice.amount_total, 1_100.0, places=2)

        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2026-02-15',
            'sale_invoice_line_id': sale_line.id,
        })
        wizard.action_dispose()

        disposal = asset.disposal_move_id
        revenue_clear = disposal.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        )
        self.assertAlmostEqual(revenue_clear.debit, 1_000.0, places=2)
        self.assertEqual(revenue_clear.partner_id, self.partner_b)
        self.assertFalse(disposal.line_ids.filtered('tax_line_id'))
        self.assertFalse(disposal.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        ))
        source_tax = invoice.line_ids.filtered(
            lambda line: line.account_id == output_tax,
        )
        self.assertEqual(len(source_tax), 1)
        self.assertAlmostEqual(source_tax.credit, 100.0, places=2)
        self.assertAlmostEqual(
            sum((invoice | disposal).line_ids.filtered(
                lambda line: line.account_id == output_tax,
            ).mapped('credit')),
            100.0,
            places=2,
        )
        self.assertEqual(asset.disposal_invoice_id, invoice)
        self.assertEqual(asset.disposal_invoice_line_id, sale_line)
        self.assertEqual(asset.disposal_partner_id, self.partner_b)
        self.assertAlmostEqual(asset.disposal_proceeds, 1_000.0, places=2)

    def test_revalue_uplift(self):
        asset = self._make_asset(
            code='RV-1',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=24,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        cost_before = asset.acquisition_cost
        nbv_before = asset.net_book_value
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 3000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        wizard.action_revalue()
        # Historical cost is NOT mutated (that corrupts the IAS 36.117
        # ceiling); the uplift lands in revaluation_adjustment and lifts NBV.
        self.assertEqual(asset.acquisition_cost, cost_before)
        self.assertAlmostEqual(asset.revaluation_adjustment, 3000.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, nbv_before + 3000.0, places=2)
        # Schedule should still have remaining unposted lines.
        unposted = asset.depreciation_line_ids.filtered(
            lambda l: not l.is_posted,
        )
        self.assertGreater(len(unposted), 0)
        # Label honesty: the uplift path books a LIVE, posted journal entry;
        # the selection label must not claim it is a preview.
        uplift_label = dict(
            wizard._fields['direction'].selection,
        )['uplift']
        self.assertNotIn(
            'Preview', uplift_label,
            "uplift label must not claim 'Preview' when it posts a live entry",
        )
        posted = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-04-30'),
        ])
        self.assertEqual(len(posted), 1, "uplift must book exactly one move")
        self.assertEqual(
            posted.state, 'posted',
            "uplift move must be posted (live entry, not a preview)",
        )
        self.assertTrue(
            posted.eh_sealed,
            "a revaluation entry backs frozen carrying values and must be sealed",
        )
        with self.assertRaises(UserError):
            posted.button_draft()
        # Uplift is Dr Asset / Cr Reserve, both for the full amount.
        self.assertAlmostEqual(
            sum(posted.line_ids.mapped('debit')),
            sum(posted.line_ids.mapped('credit')), places=2)
        reserve_cr = sum(posted.line_ids.filtered(
            lambda l: l.account_id == self.account_reval_reserve,
        ).mapped('credit'))
        self.assertAlmostEqual(reserve_cr, 3000.0, places=2)

    def test_revalue_impairment(self):
        asset = self._make_asset(
            code='RV-2',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=24,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        cost_before = asset.acquisition_cost
        nbv_before = asset.net_book_value
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'impairment',
            'amount': 1000.0,
            'counterpart_account_id': self.account_impairment.id,
        })
        wizard.action_revalue()
        # Cost preserved; the downward revaluation reduces NBV via the
        # adjustment accumulator, not by rewriting acquisition_cost.
        self.assertEqual(asset.acquisition_cost, cost_before)
        self.assertAlmostEqual(asset.revaluation_adjustment, -1000.0, places=2)
        self.assertAlmostEqual(
            asset.net_book_value, nbv_before - 1000.0, places=2)
        move = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-04-30'),
        ])
        self.assertEqual(len(move), 1)
        pl_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_impairment,
        )
        asset_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_fixed,
        )
        self.assertAlmostEqual(pl_line.debit, 1_000.0, places=2)
        self.assertAlmostEqual(asset_line.credit, 1_000.0, places=2)
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')),
            places=2,
        )

    def test_impairment_preserves_reducing_balance_consumption_pattern(self):
        asset = self._make_asset(
            code='IMP-RB-1',
            acquisition_cost=36_000.0,
            useful_life_months=36,
            method='reducing_balance',
            declining_factor=2.0,
        )
        asset.action_activate()
        first = asset.depreciation_line_ids.sorted('sequence')[0]
        first.action_post()
        self.assertAlmostEqual(first.amount, 2_000.0, places=2)
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-02-28',
            'amount': 2_000.0,
            'reason': 'Reducing-balance rebuild regression',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        })
        impairment.action_post()

        remaining = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')
        self.assertEqual(asset.method, 'reducing_balance')
        self.assertEqual(len(remaining), 35)
        expected_first = max(32_000.0 * 2.0 / 36.0, 32_000.0 / 35.0)
        self.assertAlmostEqual(remaining[0].amount, expected_first, places=2)
        self.assertNotAlmostEqual(remaining[0].amount, 32_000.0 / 35.0, places=2)
        self.assertAlmostEqual(
            first.amount + sum(remaining.mapped('amount'))
            + impairment.amount,
            36_000.0,
            places=2,
        )
        self.assertAlmostEqual(remaining[-1].remaining_value, 0.0, places=2)

    def test_revaluation_preserves_reducing_balance_consumption_pattern(self):
        asset = self._make_asset(
            code='REV-RB-1',
            acquisition_cost=36_000.0,
            useful_life_months=36,
            method='reducing_balance',
            declining_factor=2.0,
        )
        asset.action_activate()
        first = asset.depreciation_line_ids.sorted('sequence')[0]
        first.action_post()
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-02-28',
            'direction': 'uplift',
            'amount': 3_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        wizard.action_revalue()

        remaining = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')
        self.assertEqual(asset.method, 'reducing_balance')
        self.assertEqual(len(remaining), 35)
        self.assertEqual(str(remaining[0].depreciation_date), '2026-03-31')
        expected_first = max(37_000.0 * 2.0 / 36.0, 37_000.0 / 35.0)
        self.assertAlmostEqual(remaining[0].amount, expected_first, places=2)
        self.assertNotAlmostEqual(remaining[0].amount, 37_000.0 / 35.0, places=2)
        self.assertAlmostEqual(
            first.amount + sum(remaining.mapped('amount')),
            39_000.0,
            places=2,
        )
        self.assertAlmostEqual(remaining[-1].remaining_value, 0.0, places=2)

    def test_dispose_derecognises_impairment(self):
        """Disposing a previously-impaired asset must remove the accumulated
        impairment and measure gain/loss against the true carrying amount,
        not the depreciation-only book value."""
        asset = self._make_asset(
            code='DSP-IMP-1',
            in_service_date='2025-01-31',
            acquisition_cost=12000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        # Enough life remains that carrying stays well positive after a
        # 1500 impairment, so proceeds set to carrying are positive.
        self.assertGreater(asset.net_book_value, 3000.0)
        imp = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 1500.0,
            'is_reversal': False,
            'reason': 'Test impairment for disposal derecognition',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        })
        imp.action_post()
        self.assertAlmostEqual(asset.accumulated_impairment, 1500.0, places=2)
        dep_before = asset.total_depreciated
        carrying = asset.net_book_value
        april_charge = asset.depreciation_line_ids.filtered(
            lambda line: (
                not line.is_posted
                and str(line.depreciation_date) == '2026-04-30'
            ),
        ).amount
        post_event_carrying = asset.currency_id.round(
            carrying - april_charge,
        )
        # Proceeds equal to carrying after the April charge => zero gain/loss.
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2026-04-30',
            'proceeds': post_event_carrying,
            'cash_account_id': self.account_cash.id,
        })
        # The wizard must display the impairment-netted carrying amount.
        self.assertAlmostEqual(wizard.nbv, carrying, places=2)
        wizard.action_dispose()
        move = asset.disposal_move_id
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        # The contra account is debited for accumulated depreciation PLUS the
        # 1500 accumulated impairment, so nothing is stranded on it.
        contra_debit = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_accum_dep,
        ).mapped('debit'))
        self.assertAlmostEqual(
            contra_debit, dep_before + april_charge + 1500.0, places=2,
        )
        # Proceeds == carrying => no gain and no loss line.
        gl = move.line_ids.filtered(
            lambda l: l.account_id in (
                self.account_disposal_gain, self.account_disposal_loss,
            ),
        )
        self.assertFalse(gl, "no gain/loss when proceeds equal carrying")

    def test_revalue_uplift_tracks_surplus(self):
        """An uplift increments revaluation_surplus by the uplift amount, on
        top of the existing carrying-amount adjustment and reserve credit."""
        asset = self._make_asset(
            code='RV-SURP-1',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=24,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        cost_before = asset.acquisition_cost
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 3000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        wizard.action_revalue()
        # Wave 0 invariant: historical cost untouched.
        self.assertEqual(asset.acquisition_cost, cost_before)
        # New: the surplus now carries the full uplift.
        self.assertAlmostEqual(asset.revaluation_surplus, 3000.0, places=2)

    def test_revalue_downward_reverses_surplus_then_pl(self):
        """A downward revaluation first reverses the existing revaluation
        surplus (Dr reserve) and routes only the excess to P&L (IAS 16.40)."""
        asset = self._make_asset(
            code='RV-SURP-2',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=24,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        # Uplift builds a 2000 surplus.
        up = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 2000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        up.action_revalue()
        self.assertAlmostEqual(asset.revaluation_surplus, 2000.0, places=2)

        # Now revalue down by 3000: 2000 reverses the surplus (to the reserve),
        # the remaining 1000 hits the P&L impairment account.
        down = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-05-31',
            'direction': 'impairment',
            'amount': 3000.0,
            'counterpart_account_id': self.account_impairment.id,
            'revaluation_reserve_account_id': self.account_reval_reserve.id,
        })
        down.action_revalue()

        # Surplus fully consumed.
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)

        # Locate the downward move: its asset credit equals the amount.
        moves = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-05-31'),
        ])
        self.assertEqual(len(moves), 1)
        move = moves
        # Move balances.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        # 2000 debited to the revaluation reserve (surplus reversal).
        reserve_dr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_reval_reserve,
        ).mapped('debit'))
        self.assertAlmostEqual(reserve_dr, 2000.0, places=2)
        # 1000 debited to the P&L impairment account (the excess).
        pl_dr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_impairment,
        ).mapped('debit'))
        self.assertAlmostEqual(pl_dr, 1000.0, places=2)

    def test_impairment_consumes_revaluation_reserve_and_cancel_restores_it(self):
        """IAS 36.60 applies to the impairment model as well as the
        revaluation wizard: reserve is consumed first and cancellation
        restores both the ledger and the tracked surplus."""
        asset = self._make_asset(
            code='IMP-RESERVE-1',
            in_service_date='2025-01-31',
            acquisition_cost=36_000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        uplift = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 3_000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        uplift.action_revalue()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-05-31',
            'amount': 5_000.0,
            'reason': 'Reserve-first IAS 36.60 regression',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'revaluation_reserve_account_id': self.account_reval_reserve.id,
        })
        impairment.action_post()

        move = impairment.move_id
        reserve = move.line_ids.filtered(
            lambda line: line.account_id == self.account_reval_reserve,
        )
        expense = move.line_ids.filtered(
            lambda line: line.account_id == self.account_impairment,
        )
        contra = move.line_ids.filtered(
            lambda line: line.account_id == self.account_accum_dep,
        )
        self.assertAlmostEqual(reserve.debit, 3_000.0, places=2)
        self.assertAlmostEqual(expense.debit, 2_000.0, places=2)
        self.assertAlmostEqual(contra.credit, 5_000.0, places=2)
        self.assertAlmostEqual(
            impairment.revaluation_surplus_used, 3_000.0, places=2,
        )
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)

        impairment.action_cancel()
        self.assertEqual(impairment.state, 'cancelled')
        self.assertEqual(impairment.reversal_move_id.state, 'posted')
        self.assertTrue(impairment.reversal_move_id.eh_sealed)
        self.assertAlmostEqual(asset.revaluation_surplus, 3_000.0, places=2)
        self.assertAlmostEqual(asset.accumulated_impairment, 0.0, places=2)
        for line in move.line_ids:
            reversed_lines = impairment.reversal_move_id.line_ids.filtered(
                lambda candidate: candidate.account_id == line.account_id,
            )
            self.assertAlmostEqual(
                sum(reversed_lines.mapped('balance')), -line.balance, places=2,
            )

    def test_dispose_recycles_surplus_to_retained_earnings(self):
        """Disposing a revalued asset recycles the remaining revaluation
        surplus directly to retained earnings, not through P&L (IAS 16.41)."""
        asset = self._make_asset(
            code='DSP-SURP-1',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        # Uplift creates a 4000 surplus.
        up = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 4000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        up.action_revalue()
        self.assertAlmostEqual(asset.revaluation_surplus, 4000.0, places=2)

        carrying = asset.net_book_value
        may_charge = asset.depreciation_line_ids.filtered(
            lambda line: (
                not line.is_posted
                and str(line.depreciation_date) == '2026-05-31'
            ),
        ).amount
        post_event_carrying = asset.currency_id.round(carrying - may_charge)
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2026-05-31',
            'proceeds': post_event_carrying,
            'cash_account_id': self.account_cash.id,
            'revaluation_reserve_account_id': self.account_reval_reserve.id,
            'retained_earnings_account_id': self.account_retained_earnings.id,
        })
        # Wizard exposes the surplus to recycle.
        self.assertAlmostEqual(wizard.revaluation_surplus, 4000.0, places=2)
        wizard.action_dispose()
        self.assertEqual(asset.state, 'disposed')
        # Surplus zeroed after recycle.
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)

        move = asset.disposal_move_id
        # Move balances.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        # 4000 debited to the revaluation reserve.
        reserve_dr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_reval_reserve,
        ).mapped('debit'))
        self.assertAlmostEqual(reserve_dr, 4000.0, places=2)
        # 4000 credited to retained earnings (recycled within equity).
        re_cr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_retained_earnings,
        ).mapped('credit'))
        self.assertAlmostEqual(re_cr, 4000.0, places=2)
        # The recycle did NOT touch the P&L: no gain/loss line, since proceeds
        # equal the carrying amount.
        gl = move.line_ids.filtered(
            lambda l: l.account_id in (
                self.account_disposal_gain, self.account_disposal_loss,
            ),
        )
        self.assertFalse(gl, "surplus recycle must not create a P&L gain/loss")

    def test_dispose_blocks_when_surplus_needs_accounts(self):
        """A revalued asset with a surplus refuses to dispose unless both
        recycle accounts are provided."""
        asset = self._make_asset(
            code='DSP-SURP-2',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        up = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'uplift',
            'amount': 2000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        up.action_revalue()
        wizard = self.env['eh.asset.dispose.wizard'].create({
            'asset_id': asset.id,
            'disposal_date': '2026-05-31',
            'proceeds': 0.0,
        })
        with self.assertRaises(UserError):
            wizard.action_dispose()

    def test_uplift_after_downward_reverses_pl_first(self):
        """IAS 16.39: a downward revaluation with no surplus charges the full
        decrease to P&L; a later uplift must first reverse that P&L decrease
        (credit to income) before crediting any excess to the revaluation
        surplus."""
        asset = self._make_asset(
            code='RV-PL-1',
            in_service_date='2025-01-31',
            acquisition_cost=24000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        self._post_through(asset, '2026-03-31')
        # No prior surplus, so the whole 2000 decrease hits P&L.
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)
        down = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-04-30',
            'direction': 'impairment',
            'amount': 2000.0,
            'counterpart_account_id': self.account_impairment.id,
        })
        down.action_revalue()
        # The P&L decrease is now recorded so a later uplift can reverse it.
        self.assertAlmostEqual(asset.revaluation_pl_decrease, 2000.0, places=2)
        self.assertAlmostEqual(asset.revaluation_surplus, 0.0, places=2)

        # Uplift by 3000: 2000 reverses the prior P&L decrease (credit income),
        # only the remaining 1000 is credited to the revaluation surplus.
        up = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-05-31',
            'direction': 'uplift',
            'amount': 3000.0,
            'counterpart_account_id': self.account_reval_reserve.id,
            'revaluation_income_account_id': self.account_impairment.id,
        })
        up.action_revalue()
        # Prior P&L decrease fully reversed; surplus carries only the excess.
        self.assertAlmostEqual(asset.revaluation_pl_decrease, 0.0, places=2)
        self.assertAlmostEqual(asset.revaluation_surplus, 1000.0, places=2)

        move = self.env['account.move'].search([
            ('ref', '=', 'Revaluation %s' % asset.display_name),
            ('date', '=', '2026-05-31'),
        ])
        self.assertEqual(len(move), 1)
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        # 2000 credited to the P&L (income) account: the reversal of the prior
        # decrease.
        pl_cr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_impairment,
        ).mapped('credit'))
        self.assertAlmostEqual(pl_cr, 2000.0, places=2)
        # 1000 credited to the revaluation reserve: the excess above the
        # reversed decrease.
        reserve_cr = sum(move.line_ids.filtered(
            lambda l: l.account_id == self.account_reval_reserve,
        ).mapped('credit'))
        self.assertAlmostEqual(reserve_cr, 1000.0, places=2)

    def test_revalue_blocked_below_salvage(self):
        asset = self._make_asset(
            code='RV-3',
            acquisition_cost=10000.0,
            salvage_value=8000.0,
            useful_life_months=24,
        )
        asset.action_activate()
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'direction': 'impairment',
            'amount': 5000.0,
            'counterpart_account_id': self.account_impairment.id,
        })
        move_count = self.env['account.move'].search_count([])
        unposted_ids = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        ).ids
        with self.assertRaises(UserError):
            wizard.action_revalue()
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertEqual(
            asset.depreciation_line_ids.filtered(
                lambda line: not line.is_posted,
            ).ids,
            unposted_ids,
        )
        self.assertFalse(asset.revaluation_move_ids)
        self.assertFalse(wizard.processed)

    def test_fully_depreciated_asset_is_not_offered_false_revaluation_path(self):
        asset = self._make_asset(
            code='RV-FULL-1',
            acquisition_cost=1_200.0,
            useful_life_months=1,
        )
        asset.action_activate()
        asset.action_post_due_lines()
        self.assertEqual(asset.state, 'fully_depreciated')
        move_count = self.env['account.move'].search_count([])
        wizard = self.env['eh.asset.revalue.wizard'].create({
            'asset_id': asset.id,
            'revalue_date': '2026-02-28',
            'direction': 'uplift',
            'amount': 100.0,
            'counterpart_account_id': self.account_reval_reserve.id,
        })
        with self.assertRaisesRegex(UserError, 'running or paused'):
            wizard.action_revalue()
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertFalse(asset.revaluation_move_ids)
        self.assertFalse(wizard.processed)
