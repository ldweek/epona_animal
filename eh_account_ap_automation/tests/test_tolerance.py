# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tolerance profile tests: defaulting, partner override, single default
per company constraint.
"""

from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from .common import EhApTestCase


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestTolerance(EhApTestCase):

    def test_default_profile_loaded(self):
        # Loaded by data file or by setUp.
        self.assertTrue(self.default_profile)
        self.assertTrue(self.default_profile.is_default)

    def test_get_default_returns_default(self):
        prof = self.env['eh.ap.tolerance.profile'].get_default(self.company)
        self.assertEqual(prof, self.default_profile)

    def test_only_one_default_per_company(self):
        with self.assertRaises(UserError):
            self.env['eh.ap.tolerance.profile'].create({
                'name': 'Second Default',
                'is_default': True,
                'company_id': self.company.id,
            })

    def test_partner_override_takes_priority(self):
        custom = self.env['eh.ap.tolerance.profile'].create({
            'name': 'Strict',
            'qty_tolerance_pct': 0.0,
            'price_tolerance_pct': 0.0,
        })
        self.partner_a.eh_ap_tolerance_profile_id = custom
        prof = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
            self.partner_a, company=self.company,
        )
        self.assertEqual(prof, custom)

    def test_partner_without_override_falls_back(self):
        prof = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
            self.partner_b, company=self.company,
        )
        self.assertEqual(prof, self.default_profile)

    def test_negative_tolerance_blocked(self):
        with self.assertRaises(Exception):
            self.env['eh.ap.tolerance.profile'].create({
                'name': 'Negative',
                'qty_tolerance_pct': -1.0,
            })

    def test_new_company_gets_operational_default_profile(self):
        company = self.env['res.company'].create({
            'name': 'AP Default Lifecycle Company',
            'currency_id': self.company.currency_id.id,
        })
        Profile = self.env['eh.ap.tolerance.profile'].sudo()
        defaults = Profile.with_context(active_test=False).search([
            ('company_id', '=', company.id),
            ('is_default', '=', True),
        ])
        self.assertEqual(len(defaults), 1)
        self.assertTrue(defaults.active)
        self.assertEqual(defaults.qty_tolerance_pct, 2.0)
        self.assertEqual(defaults.price_tolerance_pct, 1.0)
        self.assertEqual(defaults.amount_tolerance, 5.0)
        self.assertEqual(defaults.over_receipt_pct, 0.0)
        self.assertEqual(Profile.get_default(company), defaults)
        operator = new_test_user(
            self.env,
            login='ap_default_lifecycle_operator',
            groups='eh_account_base.group_eh_user',
            company_id=company.id,
        )
        company_as_operator = self.env['res.company'].with_user(
            operator,
        ).browse(company.id)
        resolved = self.env['eh.ap.tolerance.profile'].with_user(
            operator,
        ).with_company(company_as_operator).get_default(company_as_operator)
        self.assertEqual(resolved, defaults)

    def test_existing_company_default_seeding_is_idempotent(self):
        company = self.env['res.company'].create({
            'name': 'AP Upgrade Default Company',
            'currency_id': self.company.currency_id.id,
        })
        Profile = self.env['eh.ap.tolerance.profile'].sudo()
        Profile.with_context(active_test=False).search([
            ('company_id', '=', company.id),
            ('is_default', '=', True),
        ]).unlink()

        created = Profile._eh_ensure_company_defaults(company)
        created_again = Profile._eh_ensure_company_defaults(company)

        self.assertEqual(len(created), 1)
        self.assertFalse(created_again)
        self.assertEqual(Profile.with_context(active_test=False).search_count([
            ('company_id', '=', company.id),
            ('is_default', '=', True),
        ]), 1)
