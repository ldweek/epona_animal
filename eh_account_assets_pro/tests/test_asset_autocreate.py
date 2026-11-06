# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Automatic asset creation from posted vendor bills."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_assets_pro.tests.common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetAutoCreate(EhAssetTestCase):

    def _multi_line_bill(self, amount_a, amount_b):
        product_a = self.env['product.product'].create({
            'name': 'Capital item A',
        })
        product_b = self.env['product.product'].create({
            'name': 'Capital item B',
        })
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'journal_id': self.journal_purchase.id,
            'invoice_date': '2026-03-15',
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'Capital purchase A',
                    'product_id': product_a.id,
                    'account_id': self.account_fixed.id,
                    'price_unit': amount_a,
                    'quantity': 1,
                    'tax_ids': [(6, 0, [])],
                }),
                (0, 0, {
                    'name': 'Capital purchase B',
                    'product_id': product_b.id,
                    'account_id': self.account_fixed.id,
                    'price_unit': amount_b,
                    'quantity': 1,
                    'tax_ids': [(6, 0, [])],
                }),
            ],
        })
        return bill, product_a, product_b

    def _partial_refund(self, bill, product, amount, invoice_date):
        refund = self.env['account.move'].create({
            'move_type': 'in_refund',
            'reversed_entry_id': bill.id,
            'partner_id': bill.partner_id.id,
            'journal_id': self.journal_purchase.id,
            'invoice_date': invoice_date,
            'date': invoice_date,
            'invoice_line_ids': [(0, 0, {
                'name': 'Capital purchase A',
                'product_id': product.id,
                'account_id': self.account_fixed.id,
                'price_unit': amount,
                'quantity': 1,
                'tax_ids': [(6, 0, [])],
            })],
        })
        refund.action_post()
        return refund

    def _bill(self, amount):
        return self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-03-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Capital purchase',
                'account_id': self.account_fixed.id,
                'price_unit': amount,
                'quantity': 1,
                'tax_ids': [(6, 0, [])],
            })],
        })

    def test_autocreate_draft_asset_from_bill(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'draft'
        self.asset_category.prorata_mode = 'half'
        bill = self._bill(12000.0)

        bill.action_post()

        line = bill.invoice_line_ids
        self.assertTrue(line.eh_asset_id)
        asset = line.eh_asset_id
        self.assertEqual(asset.state, 'draft')
        self.assertEqual(asset.category_id, self.asset_category)
        self.assertEqual(asset.invoice_id, bill)
        self.assertAlmostEqual(asset.acquisition_cost, 12000.0, places=2)
        self.assertEqual(asset.prorata_mode, 'half')

    def test_autocreate_validate_starts_asset(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'validate'
        bill = self._bill(6000.0)

        bill.action_post()

        asset = bill.invoice_line_ids.eh_asset_id
        self.assertTrue(asset)
        self.assertEqual(asset.state, 'running')
        self.assertTrue(asset.depreciation_line_ids)

    def test_no_asset_when_account_untagged(self):
        bill = self._bill(5000.0)

        bill.action_post()

        self.assertFalse(bill.invoice_line_ids.eh_asset_id)

    def test_repost_does_not_duplicate(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'draft'
        bill = self._bill(9000.0)
        bill.action_post()
        bill.button_draft()
        bill.action_post()

        assets = self.env['eh.asset'].search([('invoice_id', '=', bill.id)])
        self.assertEqual(len(assets), 1)

    def test_partial_refund_quarantines_only_matched_asset_and_can_discard_draft(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'draft'
        self.asset_category.prorata_mode = 'half'
        bill, product_a, _product_b = self._multi_line_bill(1_000.0, 2_000.0)
        bill.action_post()
        lines = bill.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product',
        )
        asset_a = lines.filtered(
            lambda line: line.product_id == product_a,
        ).eh_asset_id
        asset_b = (lines - lines.filtered(
            lambda line: line.product_id == product_a,
        )).eh_asset_id
        self.assertTrue(asset_a and asset_b)
        self.assertEqual(asset_a.prorata_mode, 'half')
        self.assertEqual(asset_b.prorata_mode, 'half')

        refund = self._partial_refund(
            bill, product_a, 1_000.0, '2026-04-15',
        )

        self.assertTrue(asset_a.origin_source_quarantined)
        self.assertEqual(asset_a.origin_reversal_move_id, refund)
        self.assertEqual(asset_a.state, 'draft')
        self.assertFalse(asset_b.origin_source_quarantined)
        self.assertFalse(asset_b.origin_reversal_move_id)
        asset_b.action_activate()
        self.assertEqual(asset_b.state, 'running')

        source_line_a = asset_a.invoice_line_id
        asset_a_id = asset_a.id
        asset_a.action_discard_reversed_origin_draft()
        self.assertFalse(self.env['eh.asset'].browse(asset_a_id).exists())
        self.assertFalse(source_line_a.eh_asset_id)
        self.assertTrue(asset_b.exists())

    def test_partial_refund_with_asset_ledger_requires_correction_resolution(self):
        self.account_fixed.eh_asset_category_id = self.asset_category.id
        self.account_fixed.eh_asset_auto = 'validate'
        bill, product_a, _product_b = self._multi_line_bill(12_000.0, 6_000.0)
        bill.action_post()
        source_line = bill.invoice_line_ids.filtered(
            lambda line: line.product_id == product_a,
        )
        asset = source_line.eh_asset_id
        other = (bill.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product',
        ) - source_line).eh_asset_id
        self.assertEqual(asset.state, 'running')
        first = asset.depreciation_line_ids.sorted('sequence')[0]
        first.action_post()
        refund = self._partial_refund(
            bill, product_a, 12_000.0, '2026-04-15',
        )

        self.assertTrue(asset.origin_source_quarantined)
        self.assertEqual(asset.origin_reversal_move_id, refund)
        self.assertEqual(asset.state, 'running')
        self.assertFalse(other.origin_source_quarantined)
        with self.assertRaisesRegex(UserError, 'origin bill'):
            asset.depreciation_line_ids.filtered(
                lambda line: not line.is_posted,
            ).sorted('sequence')[0].action_post()
        with self.assertRaisesRegex(UserError, 'ledger evidence'):
            asset.action_discard_reversed_origin_draft()

        correction = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-04-15',
            'ref': 'Correction supporting retained asset',
            'line_ids': [
                (0, 0, {
                    'name': 'Restore asset accounting basis',
                    'account_id': self.account_fixed.id,
                    'debit': 12_000.0,
                }),
                (0, 0, {
                    'name': 'Correction counterpart',
                    'account_id': self.account_equity.id,
                    'credit': 12_000.0,
                }),
            ],
        })
        correction.action_post()
        asset.write({
            'origin_correction_move_id': correction.id,
            'origin_resolution_note': (
                'Vendor refund was followed by a separately approved asset '
                'capitalisation correction.'
            ),
        })
        asset.action_resolve_reversed_origin_after_correction()

        self.assertFalse(asset.origin_source_quarantined)
        self.assertEqual(asset.origin_correction_move_id, correction)
        self.assertTrue(asset.origin_quarantine_resolved_at)
        self.assertEqual(asset.origin_quarantine_resolved_by_id, self.env.user)
        next_line = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')[0]
        next_line.action_post()
        self.assertTrue(next_line.is_posted)
        self.assertEqual(next_line.move_id.state, 'posted')
