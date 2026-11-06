# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Fail-closed company-currency policy for Assets Pro subledgers."""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'post_install', '-at_install')
class TestCurrencyIntegrity(EhAssetTestCase):

    def _other_currency(self):
        currency = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
        ], limit=1)
        self.assertTrue(currency, "test database needs a second currency")
        return currency

    def test_new_asset_and_lease_must_use_company_currency(self):
        other = self._other_currency()
        with self.assertRaises(ValidationError):
            self._make_asset(
                code='FX-ASSET-CREATE-BLOCK',
                currency_id=other.id,
            )
        with self.assertRaises(ValidationError):
            self._make_lease(
                reference='FX-LEASE-CREATE-BLOCK',
                currency_id=other.id,
            )
        with self.assertRaises(ValidationError):
            self.env['eh.asset.cgu'].create({
                'name': 'FX-CGU-CREATE-BLOCK',
                'company_id': self.company.id,
                'currency_id': other.id,
            })

    def test_unposted_legacy_mismatch_can_be_corrected(self):
        other = self._other_currency()
        asset = self._make_asset(code='FX-ASSET-DRAFT-CORRECT')
        lease = self._make_lease(reference='FX-LEASE-DRAFT-CORRECT')
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'FX-CGU-DRAFT-CORRECT',
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
        })
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE eh_asset SET currency_id = %s WHERE id = %s',
            (other.id, asset.id),
        )
        self.env.cr.execute(
            'UPDATE eh_lease_contract SET currency_id = %s WHERE id = %s',
            (other.id, lease.id),
        )
        self.env.cr.execute(
            'UPDATE eh_asset_cgu SET currency_id = %s WHERE id = %s',
            (other.id, cgu.id),
        )
        asset.invalidate_recordset([
            'currency_id', 'currency_mismatch_quarantined',
        ])
        lease.invalidate_recordset([
            'currency_id', 'currency_mismatch_quarantined',
        ])
        cgu.invalidate_recordset(['currency_id'])
        self.assertEqual(asset.currency_id, other)
        self.assertEqual(lease.currency_id, other)
        self.assertEqual(cgu.currency_id, other)
        self.assertFalse(asset.currency_mismatch_quarantined)
        self.assertFalse(lease.currency_mismatch_quarantined)
        self.assertFalse(cgu.currency_mismatch_quarantined)
        with self.assertRaises(UserError):
            cgu.action_test_now()

        asset.write({'currency_id': self.company.currency_id.id})
        lease.write({'currency_id': self.company.currency_id.id})
        cgu.write({'currency_id': self.company.currency_id.id})
        self.assertEqual(asset.currency_id, self.company.currency_id)
        self.assertEqual(lease.currency_id, self.company.currency_id)
        self.assertEqual(cgu.currency_id, self.company.currency_id)

    def test_posted_mismatch_is_visible_read_only_and_cannot_post_more(self):
        other = self._other_currency()
        asset = self._make_asset(
            code='FX-ASSET-QUARANTINE',
            in_service_date='2025-01-31',
            useful_life_months=36,
        )
        asset.action_activate()
        first_asset_line = asset.depreciation_line_ids.sorted(
            'sequence',
        )[:1]
        first_asset_line.action_post()
        next_asset_line = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        ).sorted('sequence')[:1]
        self.assertTrue(next_asset_line)

        lease = self._make_lease(
            reference='FX-LEASE-QUARANTINE',
            commencement_date='2025-01-31',
            term_months=36,
        )
        lease.action_activate()
        next_lease_line = lease.schedule_line_ids.sorted('sequence')[:1]
        self.assertTrue(lease.opening_move_id)

        cgu_asset = self._make_asset(
            code='FX-CGU-EVIDENCE-ASSET',
            in_service_date='2025-01-31',
            useful_life_months=36,
        )
        cgu_asset.action_activate()
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'FX-CGU-QUARANTINE',
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
        })
        cgu_asset.write({'cgu_id': cgu.id})
        cgu_impairment = self.env['eh.asset.impairment'].create({
            'asset_id': cgu_asset.id,
            'cgu_id': cgu.id,
            'impairment_date': '2026-03-31',
            'amount': 100.0,
            'reason': 'CGU currency quarantine evidence fixture',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        })
        cgu_impairment.action_post()
        self.assertEqual(cgu_impairment.state, 'posted')

        # Matching company/currency identity is frozen as soon as ledger
        # evidence exists, even for a no-op direct write.
        with self.assertRaises(UserError):
            asset.write({'currency_id': self.company.currency_id.id})
        with self.assertRaises(UserError):
            lease.write({'currency_id': self.company.currency_id.id})

        self.env.flush_all()
        note = 'Upgrade audit fixture: history retained, record quarantined.'
        self.env.cr.execute(
            'UPDATE eh_asset '
            'SET currency_id = %s, currency_mismatch_quarantined = TRUE, '
            'currency_mismatch_note = %s WHERE id = %s',
            (other.id, note, asset.id),
        )
        self.env.cr.execute(
            'UPDATE eh_lease_contract '
            'SET currency_id = %s, currency_mismatch_quarantined = TRUE, '
            'currency_mismatch_note = %s WHERE id = %s',
            (other.id, note, lease.id),
        )
        self.env.cr.execute(
            'UPDATE eh_asset_cgu '
            'SET currency_id = %s, currency_mismatch_quarantined = TRUE, '
            'currency_mismatch_note = %s WHERE id = %s',
            (other.id, note, cgu.id),
        )
        asset.invalidate_recordset([
            'currency_id', 'currency_mismatch_quarantined',
            'currency_mismatch_note',
        ])
        lease.invalidate_recordset([
            'currency_id', 'currency_mismatch_quarantined',
            'currency_mismatch_note',
        ])
        cgu.invalidate_recordset([
            'currency_id', 'currency_mismatch_quarantined',
            'currency_mismatch_note',
        ])

        self.assertTrue(asset.currency_mismatch_quarantined)
        self.assertTrue(lease.currency_mismatch_quarantined)
        self.assertTrue(cgu.currency_mismatch_quarantined)
        self.assertEqual(asset.currency_mismatch_note, note)
        self.assertEqual(lease.currency_mismatch_note, note)
        self.assertEqual(cgu.currency_mismatch_note, note)
        with self.assertRaises(UserError):
            asset.write({'code': 'FORBIDDEN'})
        with self.assertRaises(UserError):
            lease.write({'reference': 'FORBIDDEN'})
        with self.assertRaises(UserError):
            cgu.write({'fair_value': 999.0})
        with self.assertRaises(UserError):
            self.env['eh.asset.cgu.cashflow'].create({
                'cgu_id': cgu.id,
                'period': 1,
                'amount': 1000.0,
            })
        with self.assertRaises(UserError):
            cgu.action_test_now()
        with self.assertRaises(UserError):
            cgu.unlink()

        move_count = self.env['account.move'].search_count([])
        with self.assertRaises(UserError):
            next_asset_line.action_post()
        with self.assertRaises(UserError):
            next_lease_line.action_post()
        self.assertEqual(
            self.env['account.move'].search_count([]), move_count,
        )
        self.assertEqual(first_asset_line.move_id.state, 'posted')
        self.assertEqual(lease.opening_move_id.state, 'posted')
        self.assertEqual(cgu_impairment.move_id.state, 'posted')
