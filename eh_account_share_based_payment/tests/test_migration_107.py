# -*- encoding: utf-8 -*-
"""Seeded 1.0.7 measurement-evidence upgrade regressions."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestSbpMigration107(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.0.7' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_sbp_migration_107_pre_test', path)
        cls.pre = module_from_spec(spec)
        spec.loader.exec_module(cls.pre)
        cls.expense = cls._ensure_account(
            cls.env, '6190', 'SBP 1.0.7 Migration Expense', 'expense')
        cls.reserve = cls._ensure_account(
            cls.env, '3190', 'SBP 1.0.7 Migration Reserve', 'equity')

    def _approved_plan(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
        })
        valuation.action_compute()
        plan = self.env['eh.sbp.plan'].create({
            'name': '/', 'grant_date': '2026-01-01', 'vesting_years': 3,
            'expense_account_id': self.expense.id,
            'equity_account_id': self.reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'valuation_id': valuation.id,
            })],
        })
        plan.grant_ids.action_use_valuation()
        plan.action_activate()
        return plan

    def _approved_manual_plan(self, graded=False):
        values = {
            'name': '/', 'grant_date': '2026-01-01', 'vesting_years': 3,
            'expense_account_id': self.expense.id,
            'equity_account_id': self.reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
            })],
        }
        if graded:
            values.update({
                'graded_vesting': True,
                'tranche_ids': [(0, 0, {
                    'name': 'Legacy tranche',
                    'portion_pct': 100.0,
                    'vesting_end_date': '2029-01-01',
                    'fair_value': 10.0,
                })],
            })
        plan = self.env['eh.sbp.plan'].create(values)
        plan.action_activate()
        return plan

    def test_intermediate_approved_mismatch_is_quarantined(self):
        bad = self._approved_plan()
        nan = self._approved_plan()
        infinity = self._approved_plan()
        linked_input = self._approved_plan()
        manual = self._approved_manual_plan()
        graded = self._approved_manual_plan(graded=True)
        modified = self._approved_manual_plan()
        plan_anchor = self._approved_manual_plan()
        run_output = self._approved_manual_plan()
        currency_anchor = self._approved_manual_plan()
        good = self._approved_plan()
        good_manual = self._approved_manual_plan()
        modification = self.env['eh.sbp.modification'].create({
            'plan_id': modified.id,
            'name': 'Intermediate modification',
            'date': '2026-06-01',
            'incremental_fv': 1.0,
        })
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': run_output.id,
            'period_end': '2027-01-01',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_grant SET grant_date_fair_value = "
            "grant_date_fair_value + 1 WHERE plan_id = %s",
            (bad.id,),
        )
        for plan, literal in ((nan, 'NaN'), (infinity, 'Infinity')):
            self.env.cr.execute(
                "UPDATE eh_sbp_valuation valuation SET result_value = "
                "%s::double precision FROM eh_sbp_grant grant_row "
                "WHERE grant_row.plan_id = %s "
                "AND valuation.id = grant_row.valuation_id",
                (literal, plan.id),
            )
            self.env.cr.execute(
                "UPDATE eh_sbp_grant SET grant_date_fair_value = "
                "%s::double precision WHERE plan_id = %s",
                (literal, plan.id),
            )
        self.env.cr.execute(
            "UPDATE eh_sbp_valuation valuation SET spot = "
            "'NaN'::double precision FROM eh_sbp_grant grant_row "
            "WHERE grant_row.plan_id = %s "
            "AND valuation.id = grant_row.valuation_id",
            (linked_input.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_grant SET expected_forfeiture_pct = "
            "'NaN'::double precision WHERE plan_id = %s",
            (manual.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan_tranche SET fair_value = "
            "'Infinity'::double precision WHERE plan_id = %s",
            (graded.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_modification SET incremental_fv = "
            "'NaN'::double precision WHERE id = %s",
            (modification.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET settlement_amount = "
            "'-Infinity'::double precision WHERE id = %s",
            (plan_anchor.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_period_run SET current_fair_value = "
            "'Infinity'::double precision, cumulative_target = "
            "'NaN'::double precision WHERE id = %s",
            (run.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_grant SET currency_id = NULL WHERE plan_id = %s",
            (currency_anchor.id,),
        )

        self.pre.migrate(self.env.cr, '19.0.1.0.6')
        plans = (
            bad | nan | infinity | linked_input | manual | graded | modified
            | plan_anchor | run_output | currency_anchor | good | good_manual
        )
        plans.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        for plan in (
            bad, nan, infinity, linked_input, manual, graded, modified,
            plan_anchor, run_output, currency_anchor,
        ):
            with self.subTest(plan=plan.display_name):
                self.assertTrue(plan.legacy_provenance_quarantined)
        self.assertFalse(good.legacy_provenance_quarantined)
        self.assertFalse(good_manual.legacy_provenance_quarantined)
        with self.assertRaises(UserError):
            bad.action_cancel()
