# -*- encoding: utf-8 -*-
"""Seeded 1.0.8-to-1.0.9 quarantine remediation regression."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestSbpMigration109(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.0.9' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_sbp_migration_109_pre_test', path)
        cls.pre = module_from_spec(spec)
        spec.loader.exec_module(cls.pre)
        cls.expense = cls._ensure_account(
            cls.env, '6192', 'SBP 1.0.9 Migration Expense', 'expense')
        cls.reserve = cls._ensure_account(
            cls.env, '3192', 'SBP 1.0.9 Migration Reserve', 'equity')

    def _active_linked_plan(self):
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
                'grant_date_fair_value': valuation.result_value,
                'valuation_id': valuation.id,
            })],
        })
        plan.grant_ids.action_use_valuation()
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        run.action_post()
        return plan, valuation, run

    def test_broken_108_upgrade_preserves_loss_and_releases_future_only(self):
        plan, valuation, old_run = self._active_linked_plan()
        copied_fair_value = plan.grant_ids.grant_date_fair_value
        anchor = plan.recognised_cumulative
        old_move = old_run.move_id

        # Exact persisted shape left by original 1.0.8: current consumed
        # result destroyed, no backup, every approved plan quarantined.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET measurement_currency_id = NULL, "
            "legacy_provenance_quarantined = TRUE, "
            "legacy_provenance_quarantine_note = %s WHERE id = %s",
            ('Original 1.0.8 blanket quarantine.', plan.id),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_valuation SET result_value = 0, "
            "result_provenance_version = 0, result_currency_id = NULL, "
            "legacy_result_value = NULL, "
            "legacy_result_provenance_version = 0, "
            "legacy_result_currency_id = NULL, "
            "legacy_result_unverified = FALSE, "
            "legacy_result_quarantine_note = NULL WHERE id = %s",
            (valuation.id,),
        )
        self.pre.migrate(self.env.cr, '19.0.1.0.8')
        # Idempotence: second upgrade pass must neither reconstruct evidence
        # nor duplicate/change its loss classification.
        self.pre.migrate(self.env.cr, '19.0.1.0.8')
        plan.invalidate_recordset([
            'measurement_currency_id', 'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        valuation.invalidate_recordset([
            'result_value', 'result_provenance_version',
            'result_currency_id', 'legacy_result_value',
            'legacy_result_unverified', 'legacy_result_quarantine_note',
        ])
        old_move.invalidate_recordset([
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])

        self.assertTrue(plan.legacy_provenance_quarantined)
        self.assertEqual(valuation.result_value, 0.0)
        self.assertFalse(valuation.legacy_result_value)
        self.assertTrue(valuation.legacy_result_unverified)
        self.assertIn(
            'no result was reconstructed',
            valuation.legacy_result_quarantine_note,
        )
        self.assertAlmostEqual(
            plan.grant_ids.grant_date_fair_value, copied_fair_value,
        )
        self.assertTrue(old_move.eh_legacy_unverified_seal)
        self.assertFalse(old_move.eh_sealed)
        with self.assertRaises(UserError):
            valuation.action_compute()
        with self.assertRaises(UserError):
            self.env['eh.sbp.period.run'].create({
                'plan_id': plan.id, 'period_end': '2028-01-01',
            })

        plan.write({
            'legacy_provenance_review_currency_id':
                plan.company_id.currency_id.id,
            'legacy_provenance_review_anchor': anchor,
            'legacy_provenance_review_anchor_confirmed': True,
            'legacy_provenance_review_note': (
                'Agreed award workbook currency, copied grant fair value, '
                'period run and cumulative reserve to general ledger.'
            ),
        })
        plan.action_review_legacy_provenance()
        self.assertFalse(plan.legacy_provenance_quarantined)
        self.assertEqual(plan.measurement_currency_id, plan.currency_id)
        self.assertEqual(plan.legacy_provenance_review_move_count, 1)
        self.assertEqual(
            plan.legacy_provenance_review_unverified_move_count, 1,
        )
        self.assertIn(
            '"eh_legacy_unverified_seal":true',
            plan.legacy_provenance_review_snapshot,
        )
        with self.assertRaises(AccessError):
            plan.write({
                'legacy_provenance_review_note':
                    'Attempt to rewrite frozen review evidence.',
            })

        new_run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2028-01-01',
        })
        new_run.action_post()
        self.assertTrue(new_run.move_id.eh_sealed)
        self.assertFalse(new_run.move_id.eh_legacy_unverified_seal)
        self.assertEqual(new_run.move_id.eh_sbp_provenance_version, 1)
        old_move.invalidate_recordset([
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])
        self.assertFalse(old_move.eh_sealed)
        self.assertTrue(old_move.eh_legacy_unverified_seal)
        self.assertAlmostEqual(
            plan.grant_ids.grant_date_fair_value, copied_fair_value,
        )
