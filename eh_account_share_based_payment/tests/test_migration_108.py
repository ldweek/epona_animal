# -*- encoding: utf-8 -*-
"""Seeded 1.0.8 durable-currency provenance upgrade regressions."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestSbpMigration108(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.0.8' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_sbp_migration_108_pre_test', path)
        cls.pre = module_from_spec(spec)
        spec.loader.exec_module(cls.pre)
        cls.expense = cls._ensure_account(
            cls.env, '6191', 'SBP 1.0.8 Migration Expense', 'expense')
        cls.reserve = cls._ensure_account(
            cls.env, '3191', 'SBP 1.0.8 Migration Reserve', 'equity')

    def _valuation(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
        })
        valuation.action_compute()
        return valuation

    def _plan(self, valuation=None, activate=False):
        grant_values = {
            'partner_id': self.partner_a.id,
            'instruments_granted': 300,
            'grant_date_fair_value': 10.0,
        }
        if valuation:
            grant_values.update({
                'valuation_id': valuation.id,
                'grant_date_fair_value': valuation.result_value,
            })
        plan = self.env['eh.sbp.plan'].create({
            'name': '/', 'grant_date': '2026-01-01', 'vesting_years': 3,
            'expense_account_id': self.expense.id,
            'equity_account_id': self.reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, grant_values)],
        })
        if activate:
            if valuation:
                plan.grant_ids.action_use_valuation()
            plan.action_activate()
        return plan

    def test_results_preserved_and_only_unconsumed_current_result_reset(self):
        linked_valuation = self._valuation()
        standalone_valuation = self._valuation()
        linked_result = linked_valuation.result_value
        standalone_result = standalone_valuation.result_value
        linked_active = self._plan(linked_valuation, activate=True)
        manual_active = self._plan(activate=True)
        manual_draft = self._plan()
        all_plans = linked_active | manual_active | manual_draft
        all_valuations = linked_valuation | standalone_valuation

        # Seed the exact pre-1.0.8 shape. The columns exist in the current test
        # registry, but an upgraded 1.0.7 database had neither durable anchor.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET measurement_currency_id = NULL "
            "WHERE id IN %s",
            (tuple(all_plans.ids),),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_valuation SET result_currency_id = NULL "
            "WHERE id IN %s",
            (tuple(all_valuations.ids),),
        )

        self.pre.migrate(self.env.cr, '19.0.1.0.7')
        all_plans.invalidate_recordset([
            'measurement_currency_id', 'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        all_valuations.invalidate_recordset([
            'result_value', 'result_provenance_version',
            'result_currency_id', 'legacy_result_value',
            'legacy_result_provenance_version',
            'legacy_result_currency_id', 'legacy_result_unverified',
            'legacy_result_quarantine_note',
        ])

        self.assertTrue(linked_active.exists())
        self.assertTrue(manual_active.exists())
        self.assertTrue(manual_draft.exists())
        self.assertTrue(linked_active.legacy_provenance_quarantined)
        self.assertTrue(manual_active.legacy_provenance_quarantined)
        self.assertFalse(manual_draft.legacy_provenance_quarantined)
        self.assertAlmostEqual(linked_valuation.result_value, linked_result)
        self.assertEqual(linked_valuation.result_provenance_version, 0)
        self.assertFalse(linked_valuation.result_currency_id)
        self.assertTrue(linked_valuation.legacy_result_unverified)
        self.assertAlmostEqual(
            linked_valuation.legacy_result_value, linked_result,
        )
        self.assertEqual(standalone_valuation.result_value, 0.0)
        self.assertEqual(standalone_valuation.result_provenance_version, 0)
        self.assertFalse(standalone_valuation.result_currency_id)
        self.assertTrue(standalone_valuation.legacy_result_unverified)
        self.assertAlmostEqual(
            standalone_valuation.legacy_result_value, standalone_result,
        )
        with self.assertRaises(UserError):
            linked_active.action_cancel()
        with self.assertRaises(UserError):
            manual_active.action_cancel()

        # The migration is idempotent and must not duplicate its audit note.
        note = linked_active.legacy_provenance_quarantine_note
        self.pre.migrate(self.env.cr, '19.0.1.0.7')
        linked_active.invalidate_recordset([
            'legacy_provenance_quarantine_note',
        ])
        self.assertEqual(linked_active.legacy_provenance_quarantine_note, note)

        # Consumed evidence cannot be recomputed or overwritten. Controlled
        # review attests exact currency/anchor and snapshots all source/result
        # rows, after which only future accounting uses current provenance.
        with self.assertRaises(UserError):
            linked_valuation.action_compute()
        with self.assertRaises(UserError):
            linked_active.grant_ids.write({'expected_forfeiture_pct': 5.0})
        linked_active.write({
            'legacy_provenance_review_currency_id':
                linked_active.company_id.currency_id.id,
            'legacy_provenance_review_anchor': 0.0,
            'legacy_provenance_review_anchor_confirmed': True,
            'legacy_provenance_review_note': (
                'Reviewed original valuation workbook, award terms and zero '
                'opening general-ledger anchor.'
            ),
        })
        linked_active.action_review_legacy_provenance()
        self.assertFalse(linked_active.legacy_provenance_quarantined)
        self.assertTrue(linked_active.legacy_provenance_reviewed_at)
        self.assertEqual(
            linked_active.legacy_provenance_reviewed_by_id, self.env.user,
        )
        self.assertEqual(len(linked_active.legacy_provenance_review_digest), 64)
        self.assertIn(
            'eh.sbp.legacy-review.v1',
            linked_active.legacy_provenance_review_snapshot,
        )
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': linked_active.id,
            'period_end': '2027-01-01',
        })
        run.action_post()
        self.assertTrue(run.move_id.eh_sealed)
        self.assertFalse(run.move_id.eh_legacy_unverified_seal)
        self.assertEqual(run.move_id.eh_sbp_provenance_version, 1)
        self.assertAlmostEqual(
            linked_valuation.legacy_result_value, linked_result,
        )

        # Draft manual terms remain reviewable and can acquire a new, current
        # measurement-currency stamp only through the activation action.
        manual_draft.action_activate()
        self.assertEqual(
            manual_draft.measurement_currency_id,
            manual_draft.company_id.currency_id,
        )
