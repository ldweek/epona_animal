# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 9 expected credit loss tests."""

import importlib.util
import inspect
from pathlib import Path

from psycopg2 import IntegrityError

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'integration', 'post_install', '-at_install')
class TestEcl(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5290', 'Impairment Loss', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1290', 'Loss Allowance', 'asset_current')

    def _run(self, reporting_date='2026-06-30', opening=0.0, buckets=None):
        return self.env['eh.ecl.run'].create({
            'reporting_date': reporting_date,
            'reversal_date': reporting_date,
            'opening_allowance': opening,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in (buckets or [])],
        })

    def _matrix(self):
        return [
            {'name': 'Current', 'days_from': 0, 'days_to': 30,
             'loss_rate': 1.0},
            {'name': '31-90', 'days_from': 31, 'days_to': 90,
             'loss_rate': 5.0, 'stage': '2'},
            {'name': '90+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3'},
        ]

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_ecl_compute(self):
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        # 1000 x 25% = 250.
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)

    def test_post_increase(self):
        run = self._run(opening=0.0, buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertAlmostEqual(self._bal(self.impairment), 250.0, places=2)
        self.assertAlmostEqual(self._bal(self.allowance), -250.0, places=2)

    def test_movement_from_opening(self):
        run = self._run(opening=100.0, buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        # Closing 250, opening 100 -> movement 150.
        self.assertAlmostEqual(run.movement, 150.0, places=2)
        run.action_post()
        self.assertAlmostEqual(self._bal(self.impairment), 150.0, places=2)

    def test_decrease_reverses(self):
        run = self._run(opening=300.0, buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        # Closing 250, opening 300 -> movement -50 -> release.
        self.assertAlmostEqual(run.movement, -50.0, places=2)
        run.action_post()
        self.assertAlmostEqual(self._bal(self.allowance), 50.0, places=2)
        self.assertAlmostEqual(self._bal(self.impairment), -50.0, places=2)

    def test_populate_from_receivables(self):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-01',
            'invoice_date_due': '2026-01-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale', 'quantity': 1, 'price_unit': 1000.0,
                'account_id': self.account_revenue.id})],
        })
        inv.action_post()
        run = self._run(reporting_date='2026-06-30', buckets=self._matrix())
        run.action_populate_from_receivables()
        # Due 2026-01-15, reported 2026-06-30 -> well over 90 days.
        old = run.bucket_ids.filtered(lambda b: b.name == '90+')
        self.assertAlmostEqual(old.gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)

    def test_framework_population_api_is_not_shadowed(self):
        base_populate = getattr(models.BaseModel, '_populate', None)
        populate = getattr(self.env['eh.ecl.run'], '_populate', None)
        if base_populate is None:
            # Odoo 19 removed this framework hook.  Keep the assertion so the
            # common implementation cannot accidentally reintroduce the old
            # collision even when this suite runs only on 19.0.
            self.assertIsNone(populate)
            return

        self.assertEqual(
            tuple(inspect.signature(populate).parameters),
            ('size',),
        )
        self.assertFalse(populate('small'))

    def test_default_get_accepts_old_and_new_framework_keywords(self):
        Run = self.env['eh.ecl.run']
        self.assertIn('name', Run.default_get(fields=['name']))
        self.assertIn('name', Run.default_get(fields_list=['name']))

    def test_post_requires_manager(self):
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'ecl_plain@test', 'email': 'ecl_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            run.with_user(user).action_post()

    def test_cancel_requires_manager(self):
        run = self._run(buckets=self._matrix())
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'ecl_cancel@test',
            'email': 'ecl_cancel@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            run.with_user(user).action_cancel()
        # A manager can cancel.
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')

    def test_opening_rolls_from_prior_run(self):
        # Post a first period so it becomes the roll-forward source.
        prior = self._run(reporting_date='2026-03-31', opening=0.0,
                          buckets=self._matrix())
        prior.bucket_ids.filtered(
            lambda b: b.name == '90+').gross_carrying = 1000.0
        prior.action_compute()
        prior.action_post()
        self.assertAlmostEqual(prior.closing_allowance, 250.0, places=2)
        # A later run in the same company with no opening supplied must
        # default its opening allowance from the prior posted run's closing.
        later = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in self._matrix()],
        })
        self.assertAlmostEqual(later.opening_allowance, 250.0, places=2)
        self.assertAlmostEqual(later.rolled_opening_allowance, 250.0, places=2)
        # The explicit roll-forward action produces the same figure.
        later.opening_allowance = 0.0
        later.action_roll_forward_opening()
        self.assertAlmostEqual(later.opening_allowance, 250.0, places=2)

    def test_opening_ledger_tie_out_flag(self):
        # Post a run so the loss-allowance ledger carries a balance.
        prior = self._run(reporting_date='2026-03-31', opening=0.0,
                          buckets=self._matrix())
        prior.bucket_ids.filtered(
            lambda b: b.name == '90+').gross_carrying = 1000.0
        prior.action_compute()
        prior.action_post()
        # A new run whose opening matches the ledger ties out.
        good = self._run(reporting_date='2026-06-30', opening=250.0,
                         buckets=self._matrix())
        self.assertAlmostEqual(good.ledger_allowance, 250.0, places=2)
        self.assertTrue(good.opening_ties_out)
        # A keyed opening that disagrees with the ledger is flagged.
        bad = self.env['eh.ecl.run'].new({
            'reporting_date': '2026-06-30',
            'opening_allowance': 900.0,
            'loss_allowance_account_id': self.allowance.id,
        })
        bad._compute_roll_forward()
        self.assertFalse(bad.opening_ties_out)

    def test_matrix_frozen_after_post(self):
        run = self._run(buckets=self._matrix())
        bucket = run.bucket_ids.filtered(lambda b: b.name == '90+')
        bucket.gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            bucket.gross_carrying = 2000.0

    def test_discounting_reduces_ecl(self):
        run = self._run(buckets=[
            {'name': '90+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3',
             'discount_rate': 10.0, 'periods_to_recovery': 2},
        ])
        bucket = run.bucket_ids
        bucket.gross_carrying = 1000.0
        # Undiscounted: 1000 x 25% = 250.
        self.assertAlmostEqual(bucket.ecl_undiscounted, 250.0, places=2)
        # PV factor 1/(1.1)^2 = 1/1.21 -> ecl = 250 / 1.21.
        self.assertAlmostEqual(bucket.ecl, 250.0 / 1.21, places=2)
        self.assertLess(bucket.ecl, bucket.ecl_undiscounted)
        # Closing allowance flows from the discounted figure.
        self.assertAlmostEqual(run.closing_allowance, 250.0 / 1.21, places=2)

    def test_stage_from_ageing(self):
        run = self._run(buckets=self._matrix())
        # Wipe the seeded stages so the derivation is what we observe.
        run.bucket_ids.write({'stage': '1'})
        run.action_stage_from_ageing()
        by_name = {b.name: b.stage for b in run.bucket_ids}
        self.assertEqual(by_name['Current'], '1')   # 0-30
        self.assertEqual(by_name['31-90'], '2')     # 31-90
        self.assertEqual(by_name['90+'], '3')       # 91+

    def test_no_discount_equals_undiscounted(self):
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        for bucket in run.bucket_ids:
            self.assertEqual(bucket.discount_rate, 0.0)
            self.assertAlmostEqual(
                bucket.ecl, bucket.ecl_undiscounted, places=2)
        # And the closing allowance is unchanged from the legacy figure.
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)

    def test_reverse(self):
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)

    def test_reversed_run_is_not_next_period_opening(self):
        prior = self._run(
            reporting_date='2026-03-31', opening=0.0,
            buckets=self._matrix(),
        )
        prior.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1000.0
        prior.action_compute()
        prior.action_post()
        prior.action_reverse()

        later = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, values) for values in self._matrix()],
        })
        self.assertEqual(later.opening_allowance, 0.0)
        self.assertEqual(later.rolled_opening_allowance, 0.0)
        self.assertEqual(later.ledger_allowance, 0.0)

    def test_zero_movement_posts_snapshot_and_same_date_correction(self):
        prior = self._run(
            reporting_date='2026-03-31', opening=0.0,
            buckets=self._matrix(),
        )
        prior.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1000.0
        prior.action_compute()
        prior.action_post()

        steady = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'reversal_date': '2026-06-30',
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, values) for values in self._matrix()],
        })
        steady.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1000.0
        self.assertEqual(steady.opening_allowance, 250.0)
        self.assertEqual(steady.closing_allowance, 250.0)
        self.assertEqual(steady.movement, 0.0)
        steady.action_compute()
        steady.action_post()
        self.assertEqual(steady.state, 'posted')
        self.assertTrue(steady.zero_movement_posted)
        self.assertFalse(steady.move_id)
        self.assertEqual(steady.recon_ids.filtered(
            lambda line: line.stage == '3',
        ).closing, 250.0)

        following = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-09-30',
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, values) for values in self._matrix()],
        })
        self.assertEqual(following._prior_run(), steady)
        self.assertEqual(following.opening_allowance, 250.0)
        following.unlink()

        # A zero-movement snapshot reverses without inventing a journal, and
        # the partial live-date index then permits one corrected same-date run.
        steady.action_reverse()
        self.assertEqual(steady.state, 'reversed')
        self.assertTrue(steady.zero_movement_posted)
        self.assertFalse(steady.reversal_move_id)
        correction = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, values) for values in self._matrix()],
        })
        correction.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1200.0
        correction.action_compute()
        correction.action_post()
        self.assertEqual(correction.state, 'posted')
        self.assertFalse(correction.zero_movement_posted)
        self.assertTrue(correction.move_id)
        self.assertTrue(steady.exists())

        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            self._run(
                reporting_date='2026-06-30', opening=250.0,
                buckets=self._matrix(),
            )

    def test_deferred_tax_uses_latest_active_posted_allowance_only(self):
        posted = self._run(
            reporting_date='2026-03-31', opening=0.0,
            buckets=self._matrix(),
        )
        posted.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1000.0
        posted.action_compute()
        posted.action_post()

        draft = self._run(
            reporting_date='2026-06-30', opening=250.0,
            buckets=self._matrix(),
        )
        draft.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 4000.0
        cancelled = self._run(
            reporting_date='2026-07-31', opening=250.0,
            buckets=self._matrix(),
        )
        cancelled.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 3000.0
        cancelled.action_cancel()
        reversed_run = self._run(
            reporting_date='2026-08-31', opening=250.0,
            buckets=self._matrix(),
        )
        reversed_run.bucket_ids.filtered(
            lambda bucket: bucket.name == '90+',
        ).gross_carrying = 1200.0
        reversed_run.action_compute()
        reversed_run.action_post()
        reversed_run.action_reverse()

        rows = (posted | draft | cancelled | reversed_run) \
            .eh_deferred_tax_temp_diffs(fields.Date.to_date('2026-09-30'))
        self.assertEqual(len(rows), 1)
        self.assertIn(posted.display_name, rows[0]['name'])
        self.assertEqual(rows[0]['tax_base'], 250.0)

    def test_126_migration_installs_partial_live_date_unique_index(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.6' / 'pre-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_ecl_migration_126', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        schema = 'eh_ecl_126_%d' % self.env.company.id

        class RollbackMigrationProbe(Exception):
            pass

        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('CREATE SCHEMA %s' % schema)
                self.env.cr.execute('SET LOCAL search_path TO %s' % schema)
                self.env.cr.execute(
                    "CREATE TABLE eh_ecl_run ("
                    "id integer PRIMARY KEY, company_id integer NOT NULL, "
                    "reporting_date date NOT NULL, state varchar NOT NULL, "
                    "CONSTRAINT legacy_ecl_company_date_uniq "
                    "UNIQUE (company_id, reporting_date))"
                )
                self.env.cr.execute(
                    "INSERT INTO eh_ecl_run VALUES "
                    "(1, 7, '2026-06-30', 'posted')"
                )
                migration.migrate(self.env.cr, '19.0.1.2.5')
                self.env.cr.execute(
                    "INSERT INTO eh_ecl_run VALUES "
                    "(2, 7, '2026-06-30', 'reversed')"
                )
                with self.assertRaises(IntegrityError), \
                        mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                    self.env.cr.execute(
                        "INSERT INTO eh_ecl_run VALUES "
                        "(3, 7, '2026-06-30', 'draft')"
                    )
                self.env.cr.execute(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'eh_ecl_run_live_date_uniq'"
                )
                indexdef = self.env.cr.fetchone()[0]
                self.assertIn('WHERE', indexdef)
                for live_state in ('draft', 'computed', 'posted'):
                    self.assertIn(live_state, indexdef)
                raise RollbackMigrationProbe()
        except RollbackMigrationProbe:
            pass

    # ---- general (3-stage) model ----

    def test_simplified_is_default(self):
        run = self._run(buckets=self._matrix())
        self.assertEqual(run.measurement_approach, 'simplified')

    def test_ecl_general_stage1_uses_12m_pd(self):
        run = self._run(buckets=[
            {'name': 'S1', 'days_from': 0, 'days_to': 30, 'loss_rate': 1.0,
             'stage': '1', 'exposure_at_default': 10000.0,
             'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0},
        ])
        run.measurement_approach = 'general'
        bucket = run.bucket_ids
        # Stage 1: 10000 x 40% x 2% (12m PD) = 80.
        self.assertAlmostEqual(bucket.ecl_general, 80.0, places=2)
        self.assertAlmostEqual(bucket.ecl_effective, 80.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 80.0, places=2)

    def test_ecl_general_stage2_uses_lifetime_pd(self):
        run = self._run(buckets=[
            {'name': 'S2', 'days_from': 31, 'days_to': 90, 'loss_rate': 5.0,
             'stage': '2', 'exposure_at_default': 10000.0,
             'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0},
        ])
        run.measurement_approach = 'general'
        bucket = run.bucket_ids
        # Stage 2: 10000 x 40% x 20% (lifetime PD) = 800.
        self.assertAlmostEqual(bucket.ecl_general, 800.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 800.0, places=2)

    def test_ecl_general_stage3_uses_lifetime_pd(self):
        run = self._run(buckets=[
            {'name': 'S3', 'days_from': 91, 'days_to': 0, 'loss_rate': 25.0,
             'stage': '3', 'exposure_at_default': 5000.0,
             'lgd': 60.0, 'pd_12m': 3.0, 'pd_lifetime': 100.0},
        ])
        run.measurement_approach = 'general'
        bucket = run.bucket_ids
        # Stage 3: 5000 x 60% x 100% (lifetime PD) = 3000.
        self.assertAlmostEqual(bucket.ecl_general, 3000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 3000.0, places=2)

    def test_ecl_general_uses_pv_factor(self):
        run = self._run(buckets=[
            {'name': 'S2', 'days_from': 31, 'days_to': 90, 'loss_rate': 5.0,
             'stage': '2', 'exposure_at_default': 10000.0,
             'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0,
             'discount_rate': 10.0, 'periods_to_recovery': 2},
        ])
        run.measurement_approach = 'general'
        bucket = run.bucket_ids
        # 800 discounted by 1/(1.1)^2 = 800 / 1.21.
        self.assertAlmostEqual(bucket.ecl_general, 800.0 / 1.21, places=2)

    def test_general_posts_balanced_movement(self):
        run = self._run(opening=0.0, buckets=[
            {'name': 'S2', 'days_from': 31, 'days_to': 90, 'loss_rate': 5.0,
             'stage': '2', 'exposure_at_default': 10000.0,
             'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0},
        ])
        run.measurement_approach = 'general'
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertAlmostEqual(self._bal(self.impairment), 800.0, places=2)
        self.assertAlmostEqual(self._bal(self.allowance), -800.0, places=2)
        # Balanced by construction.
        lines = run.move_id.line_ids
        self.assertAlmostEqual(
            sum(lines.mapped('debit')), sum(lines.mapped('credit')), places=2)

    def test_stage_auto_and_action(self):
        run = self._run(buckets=[
            {'name': 'clean', 'days_from': 0, 'days_to': 30, 'loss_rate': 1.0,
             'stage': '1'},
            {'name': 'sicr', 'days_from': 31, 'days_to': 90, 'loss_rate': 5.0,
             'stage': '1', 'sicr': True},
            {'name': 'impaired', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '1', 'credit_impaired': True},
        ])
        run.measurement_approach = 'general'
        by_name = {b.name: b for b in run.bucket_ids}
        self.assertEqual(by_name['clean'].stage_auto, '1')
        self.assertEqual(by_name['sicr'].stage_auto, '2')
        self.assertEqual(by_name['impaired'].stage_auto, '3')
        # Credit-impaired takes precedence over SICR.
        by_name['sicr'].credit_impaired = True
        self.assertEqual(by_name['sicr'].stage_auto, '3')
        by_name['sicr'].credit_impaired = False
        run.action_stage_from_risk()
        self.assertEqual(by_name['clean'].stage, '1')
        self.assertEqual(by_name['sicr'].stage, '2')
        self.assertEqual(by_name['impaired'].stage, '3')

    # ---- posted-run control hole ----

    def test_posted_run_input_frozen_at_orm(self):
        # A posted run's measurement input is frozen at the ORM write layer:
        # a raw write to opening_allowance is blocked, not just the UI.
        run = self._run(opening=100.0, buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        with self.assertRaises(UserError):
            run.write({'opening_allowance': 500.0})
        with self.assertRaises(UserError):
            run.write({'reporting_date': '2026-12-31'})

    def test_posted_run_cannot_unlink(self):
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.unlink()

    def test_posted_run_bucket_cannot_unlink_or_add(self):
        run = self._run(buckets=self._matrix())
        bucket = run.bucket_ids.filtered(lambda b: b.name == '90+')
        bucket.gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        # A bucket cannot be deleted from a posted run.
        with self.assertRaises(UserError):
            bucket.unlink()
        # A bucket cannot be added to a posted run.
        with self.assertRaises(UserError):
            self.env['eh.ecl.bucket'].create({
                'run_id': run.id, 'name': 'extra',
                'days_from': 200, 'days_to': 0, 'loss_rate': 50.0})

    def test_bucket_cannot_move_into_posted_run(self):
        """Re-pointing an existing bucket from a draft run into a posted run
        would recompute the posted run's closing allowance behind the freeze;
        the source-parent guard alone does not catch a move INTO a posted run.
        """
        posted = self._run(buckets=self._matrix())
        posted.bucket_ids.filtered(
            lambda b: b.name == '90+').gross_carrying = 1000.0
        posted.action_compute()
        posted.action_post()
        self.assertEqual(posted.state, 'posted')
        before = posted.closing_allowance
        # A distinct reporting date to avoid any per-period run collision.
        draft = self._run(reporting_date='2025-06-30', buckets=[
            {'name': 'x', 'days_from': 0, 'days_to': 30, 'loss_rate': 2.0}])
        bucket = draft.bucket_ids[0]
        with self.assertRaises(UserError):
            bucket.write({'run_id': posted.id})
        posted.invalidate_recordset(['closing_allowance'])
        self.assertAlmostEqual(posted.closing_allowance, before, places=2)

    def test_state_writes_still_work_after_guard(self):
        # The freeze must not block the legitimate post -> reverse flow, whose
        # action methods write state + audit stamps + move links only.
        run = self._run(buckets=self._matrix())
        run.bucket_ids.filtered(lambda b: b.name == '90+').gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)

    def test_simplified_ignores_general_inputs(self):
        # A run left in simplified mode must be byte-identical to legacy even
        # when general-model inputs are populated on the buckets.
        run = self._run(buckets=[
            {'name': '90+', 'days_from': 91, 'days_to': 0, 'loss_rate': 25.0,
             'stage': '3', 'gross_carrying': 1000.0,
             'exposure_at_default': 99999.0, 'lgd': 90.0,
             'pd_12m': 50.0, 'pd_lifetime': 100.0},
        ])
        self.assertEqual(run.measurement_approach, 'simplified')
        bucket = run.bucket_ids
        self.assertAlmostEqual(bucket.ecl_effective, bucket.ecl, places=2)
        # Simplified matrix figure: 1000 x 25% = 250, untouched by EAD/PD/LGD.
        self.assertAlmostEqual(run.closing_allowance, 250.0, places=2)
