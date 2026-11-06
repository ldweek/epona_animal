# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared test base for the assets and leases module.

Extends EhAccountIntegrationTestCase from eh_account_base with the
account types and journal needed for asset and lease posting.
"""

from odoo import fields
from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


class EhAssetTestCase(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Posting depreciation, impairment, lease, and low-value pool
        # entries is gated to EH accounting managers (segregation of
        # duties). The test user runs those posting paths, so it must be
        # a manager; individual gate tests exercise the blocked path with
        # a dedicated non-manager user (see _make_non_manager_user).
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

        cls.account_fixed = cls._ensure_account(
            cls.env, '1500', 'Fixed Assets', 'asset_fixed',
        )
        cls.account_accum_dep = cls._ensure_account(
            cls.env, '1510', 'Accumulated Depreciation', 'asset_fixed',
        )
        cls.account_dep_expense = cls._ensure_account(
            cls.env, '5100', 'Depreciation Expense', 'expense_depreciation',
        )
        cls.account_disposal_gain = cls._ensure_account(
            cls.env, '4900', 'Gain on Disposal', 'income_other',
        )
        cls.account_disposal_loss = cls._ensure_account(
            cls.env, '5900', 'Loss on Disposal', 'expense',
        )
        cls.account_impairment = cls._ensure_account(
            cls.env, '5910', 'Impairment Loss', 'expense',
        )
        cls.account_reval_reserve = cls._ensure_account(
            cls.env, '3910', 'Revaluation Reserve', 'equity',
        )
        cls.account_retained_earnings = cls._ensure_account(
            cls.env, '3900', 'Retained Earnings', 'equity',
        )

        # Lease specific
        cls.account_rou = cls._ensure_account(
            cls.env, '1520', 'ROU Asset', 'asset_fixed',
        )
        cls.account_rou_accum = cls._ensure_account(
            cls.env, '1530', 'ROU Accumulated Depreciation', 'asset_fixed',
        )
        cls.account_lease_liability = cls._ensure_account(
            cls.env, '2200', 'Lease Liability', 'liability_current',
        )
        cls.account_interest = cls._ensure_account(
            cls.env, '5200', 'Interest Expense', 'expense',
        )
        cls.account_rou_dep = cls._ensure_account(
            cls.env, '5110', 'ROU Depreciation', 'expense_depreciation',
        )
        cls.account_termination_pl = cls._ensure_account(
            cls.env, '5920', 'Lease Termination P/L', 'expense',
        )

        cls.asset_category = cls.env['eh.asset.category'].create({
            'name': 'IT Hardware',
            'code': 'ITHW',
            'method': 'straight_line',
            'useful_life_months': 36,
            'salvage_rate': 0.0,
            'prorate_first_period': False,
            'asset_account_id': cls.account_fixed.id,
            'depreciation_account_id': cls.account_dep_expense.id,
            'accumulated_depreciation_account_id': cls.account_accum_dep.id,
            'disposal_gain_account_id': cls.account_disposal_gain.id,
            'disposal_loss_account_id': cls.account_disposal_loss.id,
            'journal_id': cls.journal_misc.id,
        })

    @classmethod
    def _make_asset(cls, **overrides):
        vals = {
            'name': '/',
            'code': 'IT-0001',
            'category_id': cls.asset_category.id,
            'partner_id': cls.partner_a.id,
            'acquisition_date': '2026-01-01',
            'in_service_date': '2026-01-31',
            'acquisition_cost': 36000.0,
            'salvage_value': 0.0,
            'method': 'straight_line',
            'useful_life_months': 36,
            'prorate_first_period': False,
            'asset_account_id': cls.account_fixed.id,
            'depreciation_account_id': cls.account_dep_expense.id,
            'accumulated_depreciation_account_id': cls.account_accum_dep.id,
            'disposal_gain_account_id': cls.account_disposal_gain.id,
            'disposal_loss_account_id': cls.account_disposal_loss.id,
            'journal_id': cls.journal_misc.id,
        }
        vals.update(overrides)
        # Keep fixtures that override an older service date economically
        # coherent: an asset cannot enter service before it is acquired.
        if ('in_service_date' in overrides
                and 'acquisition_date' not in overrides
                and fields.Date.to_date(vals['in_service_date'])
                < fields.Date.to_date(vals['acquisition_date'])):
            vals['acquisition_date'] = vals['in_service_date']
        return cls.env['eh.asset'].create(vals)

    def _make_non_manager_user(self):
        """Create a plain accounting user in group_eh_user but NOT in
        group_eh_manager, for segregation-of-duties gate tests."""
        group_user = self.env.ref('eh_account_base.group_eh_user')
        group_manager = self.env.ref('eh_account_base.group_eh_manager')
        user = self.env['res.users'].create({
            'name': 'Assets Clerk',
            'login': 'assets_clerk_%s' % self.env.cr.dbname,
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id])],
            'group_ids': [(6, 0, [group_user.id])],
        })
        self.assertFalse(user.has_group('eh_account_base.group_eh_manager'))
        self.assertNotIn(group_manager, user.group_ids)
        return user

    @classmethod
    def _make_lease(cls, **overrides):
        vals = {
            'name': '/',
            'reference': 'TEST-LSE-1',
            'lessor_id': cls.partner_b.id,
            'commencement_date': '2026-01-31',
            'term_months': 36,
            'cadence': 'monthly',
            'payment_timing': 'arrears',
            'payment_amount': 1000.0,
            'incremental_borrowing_rate': 6.0,
            'rou_asset_account_id': cls.account_rou.id,
            'lease_liability_account_id': cls.account_lease_liability.id,
            'interest_expense_account_id': cls.account_interest.id,
            'rou_depreciation_account_id': cls.account_rou_dep.id,
            'rou_accumulated_depreciation_account_id': cls.account_rou_accum.id,
            'cash_account_id': cls.account_cash.id,
            'journal_id': cls.journal_misc.id,
        }
        vals.update(overrides)
        return cls.env['eh.lease.contract'].create(vals)
