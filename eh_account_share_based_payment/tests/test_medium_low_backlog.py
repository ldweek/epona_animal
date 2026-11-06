# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the revalidated Share-Based Payment backlog."""

import math
from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 use freezegun directly.
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_share_based_payment.models import sbp_plan


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestShareBasedPaymentMediumLow(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.sbp_expense = cls._ensure_account(
            cls.env, '6150', 'Share-based Payment Expense', 'expense')
        cls.sbp_reserve = cls._ensure_account(
            cls.env, '3150', 'SBP Equity Reserve', 'equity')
        cls.sbp_liability = cls._ensure_account(
            cls.env, '2350', 'SBP Liability', 'liability_current')
        cls.normal_user = cls.env['res.users'].with_context(
            mail_create_nosubscribe=True,
            no_reset_password=True,
        ).create({
            'name': 'SBP backlog actor',
            'login': 'sbp_backlog_actor',
            'email': 'sbp_backlog_actor@example.com',
            'company_id': cls.company.id,
            'group_ids': [(6, 0, [cls.env.ref(
                'eh_account_base.group_eh_user').id])],
        })
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.0.11' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_sbp_migration_1011_post_test', migration_path,
        )
        cls.post_migration_1011 = module_from_spec(spec)
        spec.loader.exec_module(cls.post_migration_1011)

    def _draft_plan(self, grants=None, **values):
        grants = grants or [{
            'partner_id': self.partner_a.id,
            'instruments_granted': 100,
            'grant_date_fair_value': 10.0,
        }]
        base = {
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 2,
            'vesting_months': 0,
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'liability_account_id': self.sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, grant) for grant in grants],
        }
        base.update(values)
        return self.env['eh.sbp.plan'].create(base)

    def _plan(self, grants=None, **values):
        plan = self._draft_plan(grants=grants, **values)
        plan.action_activate()
        return plan

    def _run(self, plan, period_end, current_fair_value=0.0, **values):
        run_values = {
            'plan_id': plan.id,
            'period_end': period_end,
            'current_fair_value': current_fair_value,
        }
        run_values.update(values)
        run = self.env['eh.sbp.period.run'].create(run_values)
        run.action_post()
        return run

    def test_zero_strike_black_scholes_is_discounted_share_value(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/',
            'pricing_model': 'black_scholes',
            'spot': 100.0,
            'strike': 0.0,
            'volatility_pct': 20.0,
            'rate_pct': 5.0,
            'term_years': 1.0,
            'dividend_yield_pct': 2.0,
        })
        valuation.action_compute()
        self.assertAlmostEqual(
            valuation.result_value,
            100.0 * math.exp(-0.02),
            places=4,
        )

    def test_invalid_crr_probability_fails_instead_of_clamping(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/',
            'pricing_model': 'binomial',
            'spot': 100.0,
            'strike': 100.0,
            'volatility_pct': 1.0,
            'rate_pct': 100.0,
            'term_years': 1.0,
            'steps': 1,
        })
        with self.assertRaises(ValidationError):
            valuation.action_compute()
        self.assertEqual(valuation.result_value, 0.0)

    def test_non_anniversary_service_fraction_accrues_partial_month(self):
        plan = self._plan(
            grants=[{
                'partner_id': self.partner_a.id,
                'instruments_granted': 120,
                'grant_date_fair_value': 12.0,
            }],
            grant_date='2026-01-31',
            vesting_years=1,
        )
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2026-02-15',
        })
        run.action_compute()
        expected_fraction = (15.0 / 28.0) / 12.0
        self.assertAlmostEqual(
            run.vested_fraction, expected_fraction, places=6,
        )
        self.assertAlmostEqual(
            run.cumulative_target,
            self.company.currency_id.round(120 * 12.0 * expected_fraction),
            places=2,
        )

    def test_graded_modification_uses_each_tranche_remaining_period(self):
        plan = self._plan(
            graded_vesting=True,
            grants=[{
                'partner_id': self.partner_a.id,
                'instruments_granted': 100,
            }],
            tranche_ids=[
                (0, 0, {
                    'name': 'Year 1', 'portion_pct': 50.0,
                    'vesting_end_date': '2027-01-01', 'fair_value': 10.0,
                }),
                (0, 0, {
                    'name': 'Year 2', 'portion_pct': 50.0,
                    'vesting_end_date': '2028-01-01', 'fair_value': 10.0,
                }),
            ],
        )
        self.env['eh.sbp.modification'].create({
            'plan_id': plan.id,
            'name': 'Replacement benefit',
            'date': '2026-07-01',
            'incremental_fv': 4.0,
        })
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2027-01-01',
        })
        run.action_compute()
        # Base 750; modification 100*4*(50%*1 + 50%*6/18) = 266.67.
        self.assertAlmostEqual(run.cumulative_target, 1016.67, places=2)

    def test_exercise_and_expiry_cannot_exceed_actual_vested(self):
        grant = self._plan().grant_ids
        with self.assertRaises(ValidationError):
            grant.write({
                'vesting_finalised': True,
                'actual_vested_qty': 60,
                'exercised_qty': 61,
            })
        grant.write({
            'vesting_finalised': True,
            'actual_vested_qty': 60,
            'exercised_qty': 40,
            'expired_qty': 20,
        })
        self.assertEqual(grant.forfeited_qty, 40)
        self.assertEqual(grant.outstanding_qty, 0)

    def test_ifrs_245_disclosure_has_all_weighted_metrics(self):
        plan = self._plan(grants=[
            {
                'partner_id': self.partner_a.id,
                'instruments_granted': 100,
                'grant_date_fair_value': 10.0,
                'exercise_price': 10.0,
            },
            {
                'partner_id': self.partner_b.id,
                'instruments_granted': 100,
                'grant_date_fair_value': 10.0,
                'exercise_price': 20.0,
            },
        ])
        plan.disclosure_date = date(2029, 1, 1)
        plan.grant_ids[0].write({
            'vesting_finalised': True,
            'actual_vested_qty': 80,
            'exercised_qty': 20,
            'expired_qty': 10,
            'exercisable_qty': 30,
            'exercise_date_share_price': 25.0,
            'contractual_expiry_date': date(2030, 1, 1),
        })
        plan.grant_ids[1].write({
            'vesting_finalised': True,
            'actual_vested_qty': 100,
            'exercisable_qty': 50,
            'contractual_expiry_date': date(2032, 1, 1),
        })
        self.assertEqual(plan.exercisable_total, 80)
        self.assertAlmostEqual(plan.waep_granted, 15.0, places=4)
        self.assertAlmostEqual(plan.waep_forfeited, 10.0, places=4)
        self.assertAlmostEqual(plan.waep_exercised, 10.0, places=4)
        self.assertAlmostEqual(plan.waep_expired, 10.0, places=4)
        self.assertAlmostEqual(plan.waep_outstanding, 16.6667, places=4)
        self.assertAlmostEqual(plan.waep_exercisable, 16.25, places=4)
        self.assertEqual(plan.exercise_price_min, 10.0)
        self.assertEqual(plan.exercise_price_max, 20.0)
        self.assertAlmostEqual(plan.wa_share_price_at_exercise, 25.0)
        expected_life = (
            50 * ((date(2030, 1, 1) - date(2029, 1, 1)).days / 365.25)
            + 100 * ((date(2032, 1, 1) - date(2029, 1, 1)).days / 365.25)
        ) / 150
        self.assertAlmostEqual(
            plan.wa_remaining_contractual_life_years,
            expected_life,
            places=4,
        )

    def test_terminal_actions_use_explicit_effective_dates(self):
        cancelled = self._plan(vesting_years=3)
        cancelled.cancellation_date = date(2027, 6, 15)
        with freeze_time('2027-08-01'):
            cancelled.action_cancel()
        self.assertTrue(cancelled.move_ids)
        self.assertEqual(set(cancelled.move_ids.mapped('date')), {
            date(2027, 6, 15),
        })

        settled = self._plan(vesting_years=1)
        settled.settlement_date = date(2027, 2, 1)
        with freeze_time('2027-08-01'):
            settled.action_settle()
        self.assertTrue(settled.move_ids)
        self.assertEqual(set(settled.move_ids.mapped('date')), {
            date(2027, 2, 1),
        })

    def test_post_vesting_forfeiture_cannot_reverse_vested_service(self):
        grant = self._plan(vesting_years=1).grant_ids
        grant.write({
            'vesting_finalised': True,
            'actual_vested_qty': 100,
        })
        with freeze_time('2027-02-01'), self.assertRaises(UserError):
            grant.write({'forfeited': True})
        self.assertEqual(grant._expected_to_vest(), 100.0)

    def test_plain_plan_note_write_does_not_lock_company_row(self):
        plan = self._draft_plan()
        with patch.object(
                sbp_plan,
                '_lock_sbp_companies',
                side_effect=AssertionError("plain write locked company")):
            plan.write({'notes': 'Disclosure narrative only'})
        self.assertEqual(plan.notes, 'Disclosure narrative only')

    def test_cancellation_payment_splits_equity_and_excess_expense(self):
        plan = self._plan(vesting_years=3)
        plan.write({
            'cancellation_date': date(2027, 6, 30),
            'cancellation_payment_amount': 1200.0,
            'cancellation_repurchase_fair_value': 900.0,
        })
        with freeze_time('2027-06-30'):
            plan.action_cancel()
        payment = plan.move_ids.filtered(lambda move: any(
            line.account_id == self.account_cash
            and line.credit == 1200.0
            for line in move.line_ids
        ))
        self.assertEqual(len(payment), 1)
        self.assertMoveLines(payment, [
            (self.sbp_reserve, 900.0, 0.0),
            (self.sbp_expense, 300.0, 0.0),
            (self.account_cash, 0.0, 1200.0),
        ])

    def test_replacement_award_is_a_modification_not_cancellation(self):
        plan = self._plan(vesting_years=3)
        self.env['eh.sbp.modification'].create({
            'plan_id': plan.id,
            'name': 'Replacement award',
            'date': '2027-01-01',
            'modification_kind': 'replacement',
            'replacement_award_reference': 'NEW-AWARD-2027',
            'incremental_fv': 2.0,
        })
        plan.cancellation_date = date(2027, 6, 30)
        with freeze_time('2027-06-30'), self.assertRaises(UserError):
            plan.action_cancel()
        self.assertEqual(plan.state, 'active')

    def test_equity_to_cash_change_transfers_reserve_and_remeasures(self):
        plan = self._plan(vesting_years=2)
        self._run(plan, '2027-01-01')
        self.assertEqual(plan.recognised_cumulative, 500.0)
        modification = self.env['eh.sbp.modification'].create({
            'plan_id': plan.id,
            'name': 'Cash alternative elected',
            'date': '2027-06-01',
            'modification_kind': 'equity_to_cash',
            'incremental_fv': 0.0,
            'cash_fair_value': 12.0,
        })
        with freeze_time('2027-06-01'):
            modification.action_apply_classification_change()
        self.assertTrue(modification.applied)
        self.assertEqual(plan.settlement, 'cash')
        self.assertAlmostEqual(plan.recognised_cumulative, 850.0, places=2)
        self.assertMoveLines(modification.transfer_move_id, [
            (self.sbp_reserve, 500.0, 0.0),
            (self.sbp_expense, 350.0, 0.0),
            (self.sbp_liability, 0.0, 850.0),
        ])

    def test_company_currency_sudo_is_preserved_without_sbp_data(self):
        other_company = self.env['res.company'].create({
            'name': 'SBP-empty currency company',
        })
        currency = self.env.ref(
            'base.EUR'
            if other_company.currency_id != self.env.ref('base.EUR')
            else 'base.USD'
        )
        other_company.with_user(self.normal_user).sudo().write({
            'currency_id': currency.id,
        })
        self.assertEqual(other_company.currency_id, currency)

    def test_compute_button_is_manager_only(self):
        arch = etree.fromstring(self.env.ref(
            'eh_account_share_based_payment.view_eh_sbp_period_run_form'
        ).arch_db.encode())
        compute = arch.xpath("//button[@name='action_compute']")
        self.assertEqual(len(compute), 1)
        self.assertEqual(
            compute[0].get('groups'),
            'eh_account_base.group_eh_manager',
        )

    def test_tranche_value_subviews_carry_company_domain_context(self):
        arches = [
            etree.fromstring(self.env.ref(xmlid).arch_db.encode())
            for xmlid in (
                'eh_account_share_based_payment.view_eh_sbp_plan_form',
                'eh_account_share_based_payment.view_eh_sbp_period_run_form',
            )
        ]
        for arch in arches:
            company_fields = arch.xpath(
                "//field[@name='tranche_value_ids']/*"
                "[self::list or self::tree]/field[@name='company_id']"
            )
            self.assertEqual(len(company_fields), 1)
            # Odoo 16 represents a hidden list column as ``invisible``;
            # later series retain ``column_invisible``.
            self.assertIn(
                '1',
                (
                    company_fields[0].get('column_invisible'),
                    company_fields[0].get('invisible'),
                ),
            )

    def test_zero_charge_run_posts_subledger_checkpoint_without_move(self):
        plan = self._plan(vesting_years=1)
        first = self._run(plan, '2027-01-01')
        self.assertTrue(first.move_id)
        zero = self._run(plan, '2027-02-01')
        self.assertEqual(zero.state, 'posted')
        self.assertTrue(zero.is_zero_charge)
        self.assertFalse(zero.move_id)
        self.assertEqual(zero.cumulative_target, zero.prior_recognised)

    def test_cash_graded_run_uses_tranche_specific_current_values(self):
        plan = self._plan(
            settlement='cash',
            graded_vesting=True,
            grants=[{
                'partner_id': self.partner_a.id,
                'instruments_granted': 100,
            }],
            tranche_ids=[
                (0, 0, {
                    'name': 'Year 1', 'portion_pct': 50.0,
                    'vesting_end_date': '2027-01-01', 'fair_value': 0.0,
                }),
                (0, 0, {
                    'name': 'Year 2', 'portion_pct': 50.0,
                    'vesting_end_date': '2028-01-01', 'fair_value': 0.0,
                }),
            ],
        )
        tranches = plan.tranche_ids.sorted('vesting_end_date')
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2027-01-01',
            'tranche_value_ids': [
                (0, 0, {
                    'tranche_id': tranches[0].id,
                    'current_fair_value': 10.0,
                }),
                (0, 0, {
                    'tranche_id': tranches[1].id,
                    'current_fair_value': 20.0,
                }),
            ],
        })
        run.action_compute()
        # T1: 100*50%*10*1 = 500; T2: 100*50%*20*1/2 = 500.
        self.assertAlmostEqual(run.cumulative_target, 1000.0, places=2)

    def test_expense_other_is_a_valid_sbp_expense_classification(self):
        other_expense = self._ensure_account(
            self.env, '6160', 'Other SBP Expense', 'expense_other')
        plan = self._draft_plan(expense_account_id=other_expense.id)
        plan._check_accounting_configuration()
        self.assertEqual(plan.expense_account_id, other_expense)

    def test_terminal_date_migration_uses_only_linked_posted_evidence(self):
        cancelled = self._plan(vesting_years=3)
        cancelled.cancellation_date = date(2027, 6, 15)
        with freeze_time('2027-06-15'):
            cancelled.action_cancel()
        settled = self._plan(vesting_years=1)
        settled.settlement_date = date(2027, 2, 1)
        with freeze_time('2027-02-01'):
            settled.action_settle()
        no_evidence = self._draft_plan()

        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET cancellation_date = NULL "
            "WHERE id = %s",
            (cancelled.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET settlement_date = NULL WHERE id = %s",
            (settled.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET state = 'cancelled', "
            "cancellation_date = NULL WHERE id = %s",
            (no_evidence.id,),
        )
        self.post_migration_1011.migrate(self.env.cr, '19.0.1.0.10')
        self.post_migration_1011.migrate(self.env.cr, '19.0.1.0.10')
        cancelled.invalidate_recordset(['cancellation_date'])
        settled.invalidate_recordset(['settlement_date'])
        no_evidence.invalidate_recordset(['state', 'cancellation_date'])

        self.assertEqual(cancelled.cancellation_date, date(2027, 6, 15))
        self.assertEqual(settled.settlement_date, date(2027, 2, 1))
        self.assertFalse(no_evidence.cancellation_date)
