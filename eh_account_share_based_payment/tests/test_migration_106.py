# -*- encoding: utf-8 -*-
"""Fail-closed 1.0.6 activation and option-result migration regressions."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestSbpMigration106(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.0.6' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_sbp_migration_106_pre_test', path)
        cls.pre = module_from_spec(spec)
        spec.loader.exec_module(cls.pre)
        cls.expense = cls._ensure_account(
            cls.env, '6189', 'SBP 1.0.6 Migration Expense', 'expense')
        cls.reserve = cls._ensure_account(
            cls.env, '3189', 'SBP 1.0.6 Migration Reserve', 'equity')

    def _draft_plan(self, valuation=None):
        return self.env['eh.sbp.plan'].create({
            'name': '/',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'expense_account_id': self.expense.id,
            'equity_account_id': self.reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
                'valuation_id': valuation.id if valuation else False,
            })],
        })

    def test_forged_legacy_valuation_requires_current_recompute(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/',
            'pricing_model': 'black_scholes',
            'spot': 100.0,
            'strike': 100.0,
            'volatility_pct': 20.0,
            'rate_pct': 5.0,
            'term_years': 1.0,
        })
        plan = self._draft_plan(valuation=valuation)
        grant = plan.grant_ids[:1]
        self.env.flush_all()
        # Simulate both the pre-control raw result and an intermediate build
        # that tried to bless it with a plausible stored stamp.
        self.env.cr.execute(
            "UPDATE eh_sbp_valuation SET result_value = 987.65, "
            "result_provenance_version = 999 WHERE id = %s",
            (valuation.id,),
        )
        self.pre.migrate(self.env.cr, '19.0.1.0.5')
        valuation.invalidate_recordset([
            'result_value', 'result_provenance_version',
        ])
        self.assertAlmostEqual(valuation.result_value, 987.65)
        self.assertEqual(valuation.result_provenance_version, 0)
        with self.assertRaises(UserError):
            grant.action_use_valuation()
        with self.assertRaises(UserError):
            plan.action_activate()

        valuation.action_compute()
        valuation.invalidate_recordset([
            'result_value', 'result_provenance_version',
        ])
        self.assertEqual(valuation.result_provenance_version, 1)
        with self.assertRaises(UserError):
            plan.action_activate()
        grant.action_use_valuation()
        self.assertAlmostEqual(
            grant.grant_date_fair_value, valuation.result_value, places=4,
        )
        plan.action_activate()
        self.assertEqual(plan.state, 'active')

    def test_pre_control_active_plan_is_not_shape_blessed(self):
        plan = self._draft_plan()
        plan.action_activate()
        self.env.flush_all()
        self.pre.migrate(self.env.cr, '19.0.1.0.5')
        plan.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        self.assertTrue(plan.legacy_provenance_quarantined)
        with self.assertRaises(UserError):
            self.env['eh.sbp.period.run'].create({
                'plan_id': plan.id,
                'period_end': '2027-01-01',
            })
