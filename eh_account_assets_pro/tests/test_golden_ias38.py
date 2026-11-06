# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IAS 38 intangible-asset guardrails and the IAS 36.10 annual-test cron.

1. Indefinite-life intangibles (IAS 38.107-108): amortisation is
   prohibited - schedule generation is blocked, hand-keyed depreciation
   lines are blocked, and the asset activates schedule-less; it joins
   the annual impairment-test population instead.
2. Development-cost capitalisation gate (IAS 38.57): an intangible
   flagged dev_cost_capitalisation cannot leave draft until all six
   criteria are ticked; otherwise the module's guidance is to expense.
3. Annual-test enforcement (IAS 36.10): once the company's annual test
   month is reached, goodwill and indefinite-life intangibles with no
   test evidence in the current fiscal year are flagged
   annual_test_overdue and receive a to-do activity; a CGU test or a
   posted impairment event clears the flag.
"""

from datetime import date

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .common import EhAssetTestCase


@tagged('eh_golden', 'eh_account_assets_pro', 'post_install', '-at_install')
class TestGoldenIas38(EhGoldenTestCase, EhAssetTestCase):

    def _intangible(self, code, **overrides):
        vals = {
            'code': code,
            'asset_class': 'intangible',
            'acquisition_cost': 50_000.0,
            'salvage_value': 0.0,
            'method': 'straight_line',
            'useful_life_months': 60,
            'prorate_first_period': False,
        }
        vals.update(overrides)
        return self._make_asset(**vals)

    # ------------------------------------------------------------------
    # 1. Indefinite life: no amortisation (IAS 38.107)
    # ------------------------------------------------------------------
    def test_indefinite_life_requires_intangible_class(self):
        with self.assertRaises(ValidationError):
            self._make_asset(
                code='IAS38-TANG',
                asset_class='tangible',
                is_indefinite_life=True,
            )

    def test_indefinite_blocks_schedule_and_amortisation(self):
        asset = self._intangible('IAS38-IND', is_indefinite_life=True)
        # Schedule generation is blocked outright.
        with self.assertRaises(UserError):
            asset.action_compute_schedule()
        # Hand-keyed amortisation lines are blocked at the line model,
        # covering imports and code paths, not only the form.
        with self.assertRaises(ValidationError):
            self.env['eh.asset.depreciation.line'].create({
                'asset_id': asset.id,
                'sequence': 1,
                'depreciation_date': '2026-01-31',
                'amount': 100.0,
            })
        # Activation succeeds schedule-less: the asset runs (so it can
        # be impaired, revalued, disposed) but never amortises.
        asset.action_activate()
        self.assertEqual(asset.state, 'running')
        self.assertFalse(asset.depreciation_line_ids)
        self.assertAlmostEqual(asset.net_book_value, 50_000.00, places=2)

    def test_indefinite_flag_blocked_when_schedule_exists(self):
        asset = self._intangible('IAS38-FIN')
        asset.action_compute_schedule()
        self.assertTrue(asset.depreciation_line_ids)
        with self.assertRaises(ValidationError):
            asset.is_indefinite_life = True

    # ------------------------------------------------------------------
    # 2. Development-cost gate (IAS 38.57)
    # ------------------------------------------------------------------
    def test_dev_gate_requires_intangible_class(self):
        with self.assertRaises(ValidationError):
            self._make_asset(
                code='IAS38-DEVT',
                asset_class='tangible',
                dev_cost_capitalisation=True,
            )

    def test_dev_gate_blocks_activation_until_all_six_ticked(self):
        asset = self._intangible(
            'IAS38-DEV',
            dev_cost_capitalisation=True,
            dev_technical_feasibility=True,
            dev_intention_complete=True,
            dev_ability_use_sell=True,
            dev_probable_benefits=True,
            dev_resources_available=True,
            # dev_reliable_measurement deliberately missing
        )
        asset.action_compute_schedule()
        with self.assertRaises(UserError):
            asset.action_activate()
        self.assertEqual(asset.state, 'draft')
        # Ticking the last criterion opens the gate.
        asset.dev_reliable_measurement = True
        asset.action_activate()
        self.assertEqual(asset.state, 'running')

    # ------------------------------------------------------------------
    # 3. Annual-test cron (IAS 36.10)
    # ------------------------------------------------------------------
    def _fy_anchor_dates(self):
        """Dates inside the CURRENT fiscal year (default: calendar year
        of today), so evidence written 'now' (CGU tests, impairment
        posts) falls in the same year the cron inspects."""
        today = fields.Date.context_today(self.env['eh.asset'])
        after_trigger = date(today.year, 12, 15)   # month 12 = default
        before_trigger = date(today.year, 1, 15)
        return today, before_trigger, after_trigger

    def test_cron_flags_overdue_and_schedules_activity(self):
        today, before_trigger, after_trigger = self._fy_anchor_dates()
        goodwill = self._intangible(
            'IAS38-GW', is_goodwill=True, is_indefinite_life=True,
        )
        goodwill.action_activate()
        indefinite = self._intangible(
            'IAS38-BRAND', is_indefinite_life=True,
        )
        indefinite.action_activate()
        # A plain finite-life intangible is NOT in the population.
        finite = self._intangible('IAS38-SW')
        finite.action_compute_schedule()
        finite.action_activate()

        Asset = self.env['eh.asset']
        # Before the annual test month: nothing flagged.
        Asset._cron_ias36_annual_test(as_of=before_trigger)
        self.assertFalse(goodwill.annual_test_overdue)
        self.assertFalse(indefinite.annual_test_overdue)

        # After the annual test month with no test in the fiscal year:
        # both population assets flag; the finite one never does.
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        self.assertTrue(goodwill.annual_test_overdue)
        self.assertTrue(indefinite.annual_test_overdue)
        self.assertFalse(finite.annual_test_overdue)
        acts = self.env['mail.activity'].search([
            ('res_model', '=', 'eh.asset'),
            ('res_id', '=', goodwill.id),
        ])
        self.assertEqual(len(acts), 1)
        # Second pass does not duplicate the activity.
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        acts = self.env['mail.activity'].search([
            ('res_model', '=', 'eh.asset'),
            ('res_id', '=', goodwill.id),
        ])
        self.assertEqual(len(acts), 1)

    def test_cgu_test_clears_overdue_flag(self):
        today, _before, after_trigger = self._fy_anchor_dates()
        goodwill = self._intangible(
            'IAS38-GW2', is_goodwill=True, is_indefinite_life=True,
        )
        goodwill.action_activate()
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Annual test unit',
            # Recoverable far above carrying: the test passes, which is
            # still full test evidence under IAS 36.10.
            'fair_value': 500_000.0,
            'member_ids': [(6, 0, [goodwill.id])],
        })
        Asset = self.env['eh.asset']
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        self.assertTrue(goodwill.annual_test_overdue)

        cgu.action_test_now()
        self.assertEqual(cgu.last_test_result, 'passed')
        self.assertFalse(goodwill.annual_test_overdue)
        # The cron agrees on its next pass: last_test_date (today) sits
        # inside the same fiscal year as the trigger date.
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        self.assertFalse(goodwill.annual_test_overdue)

    def test_posted_impairment_clears_overdue_flag(self):
        today, _before, after_trigger = self._fy_anchor_dates()
        brand = self._intangible(
            'IAS38-BRD2', is_indefinite_life=True,
        )
        brand.action_activate()
        Asset = self.env['eh.asset']
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        self.assertTrue(brand.annual_test_overdue)

        imp = self.env['eh.asset.impairment'].create({
            'asset_id': brand.id,
            'impairment_date': today,
            'amount': 5_000.0,
            'is_reversal': False,
            'recoverable_amount': 45_000.0,
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'reason': 'Annual test: carrying above recoverable amount',
        })
        imp.action_post()
        self.assertFalse(brand.annual_test_overdue)
        self.assertAlmostEqual(
            brand.recoverable_amount_latest, 45_000.00, places=2,
        )
        Asset._cron_ias36_annual_test(as_of=after_trigger)
        self.assertFalse(brand.annual_test_overdue)
