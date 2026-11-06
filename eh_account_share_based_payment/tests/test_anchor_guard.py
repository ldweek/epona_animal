# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""recognised_cumulative is a posted-figure anchor, not a free field.

eh.sbp.plan.recognised_cumulative is the cumulative IFRS 2 expense (and, for
a cash-settled plan, the IFRS 2.45 liability carrying amount) that the period
engine trues up against: the next run charges ``target - recognised_cumulative``.
A ``readonly`` widget does not stop an ORM/RPC write, so a low-privilege user
with model write access could restate the anchor and make the next manager-run
double-charge the expense (or suppress a legitimate true-up), with no audit
trail. recognised_cumulative is therefore guarded by eh.workflow.guard.

Two directions are asserted:

* a non-superuser user cannot RPC-write the anchor (the guard fires only when
  env.su is False, so the write must be attempted with_user a normal user);
* the sanctioned action paths (period-run Post, Cancel acceleration, cash
  Settle) still set the anchor when run by a real, non-superuser manager -
  the writes were routed through su, so the guard retrofit does not lock the
  authorised user out of the legitimate flow.
"""

from ast import literal_eval
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 use freezegun directly.
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestRecognisedCumulativeAnchorGuard(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The default test env runs as superuser; add the manager group so the
        # superuser-driven setup can activate plans and post runs.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.sbp_expense = cls._ensure_account(
            cls.env, '6150', 'Share-based Payment Expense', 'expense')
        cls.sbp_reserve = cls._ensure_account(
            cls.env, '3150', 'SBP Equity Reserve', 'equity')
        cls.sbp_liability = cls._ensure_account(
            cls.env, '2350', 'SBP Liability', 'liability_current')

        cls._user_error = None
        # A non-superuser with only the EH *user* group: has model write
        # access (perm_write=1) but is not a manager, so it is the exact actor
        # the anchor guard must stop from restating the figure.
        cls.normal_user = cls._make_user(
            'sbp_anchor_user', 'eh_account_base.group_eh_user')
        # A non-superuser *manager*: authorised to run the actions. Used to
        # prove the sudo-routed action writes are not refused by the guard.
        cls.manager_user = cls._make_user(
            'sbp_anchor_mgr', 'eh_account_base.group_eh_manager')

    @classmethod
    def _make_user(cls, login, group_xmlid):
        try:
            return cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': login,
                'login': login,
                # A sender address so Odoo 16 can post the tracking chatter the
                # actions raise (17+ tolerate a missing author email).
                'email': '%s@example.com' % login,
                'company_id': cls.company.id,
                'group_ids': [(6, 0, [cls.env.ref(group_xmlid).id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls._user_error = exc
            return cls.env['res.users']

    def _require(self, user):
        if not user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _draft_equity_plan(self, **vals):
        base = {
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'expense_account_id': self.sbp_expense.id,
            'equity_account_id': self.sbp_reserve.id,
            'liability_account_id': self.sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
            })],
        }
        base.update(vals)
        return self.env['eh.sbp.plan'].create(base)

    def _equity_plan(self, **vals):
        plan = self._draft_equity_plan(**vals)
        plan.action_activate()
        return plan

    # ------------------------------------------------------------------
    # negative: the anchor cannot be restated by a direct write
    # ------------------------------------------------------------------
    def test_anchor_write_refused_for_low_privilege_user(self):
        """After a run posts, a non-superuser cannot RPC-write the anchor."""
        self._require(self.normal_user)
        plan = self._equity_plan()
        # Post a run so the anchor carries a real booked figure:
        # 300 x 10.00 x 12/36 = 1,000.00.
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01'})
        run.action_post()
        booked = plan.recognised_cumulative
        self.assertAlmostEqual(booked, 1000.00, places=2,
                               msg='the posted run must book 1,000.00')

        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write(
                {'recognised_cumulative': 0.0})

        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertAlmostEqual(
            plan.recognised_cumulative, booked, places=2,
            msg='the booked anchor must be unchanged after a refused write')

    def test_anchor_write_refused_even_on_draft_plan(self):
        """The guard is unconditional: even before any run posts, a
        low-privilege user cannot seed the anchor by a direct write."""
        self._require(self.normal_user)
        plan = self._equity_plan()
        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write(
                {'recognised_cumulative': 5000.0})

    def test_stored_outputs_and_related_anchors_are_not_rpc_writable(self):
        self._require(self.normal_user)
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        for vals in (
            {'vesting_end_date': '2099-12-31'},
            {'currency_id': self.company.currency_id.id},
            {'measurement_currency_id': self.company.currency_id.id},
        ):
            with self.assertRaises(AccessError):
                plan.with_user(self.normal_user).write(vals)
        for vals in (
            {'vested_fraction': 1.0},
            {'cumulative_target': 999999.0},
            {'prior_recognised': 999999.0},
            {'period_charge': 999999.0},
            {'company_id': self.company.id},
            {'currency_id': self.company.currency_id.id},
            {'plan_settlement': 'cash'},
        ):
            with self.assertRaises(AccessError):
                run.with_user(self.normal_user).write(vals)

    def test_move_provenance_rejects_forgeable_context_and_repoint(self):
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        run.action_post()
        move = run.move_id
        self.assertEqual(move.eh_sbp_plan_id, plan)
        self.assertEqual(move.eh_sbp_provenance_version, 1)
        with self.assertRaises(AccessError):
            move.with_context(
                eh_sbp_engine_capability=True,
            ).write({'eh_sbp_plan_id': False})
        with self.assertRaises(AccessError):
            move.write({'eh_sbp_provenance_version': 0})
        self.assertEqual(move.eh_sbp_plan_id, plan)

    def test_child_reparent_and_retroactive_modification_are_frozen(self):
        posted = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': posted.id, 'period_end': '2027-01-01',
        })
        run.action_post()
        draft = self.env['eh.sbp.plan'].create({
            'name': '/', 'grant_date': '2026-01-01',
        })
        grant = self.env['eh.sbp.grant'].create({
            'plan_id': draft.id,
            'partner_id': self.partner_a.id,
            'instruments_granted': 10,
            'grant_date_fair_value': 1.0,
        })
        with self.assertRaises(UserError):
            grant.write({'plan_id': posted.id})
        with self.assertRaises(UserError):
            self.env['eh.sbp.modification'].create({
                'plan_id': posted.id,
                'name': 'Backdated rewrite',
                'date': '2026-06-01',
                'incremental_fv': 1.0,
            })

    def test_activation_freezes_award_terms_before_first_run(self):
        """Activation, not first posting, is the approval boundary."""
        self._require(self.normal_user)
        plan = self._draft_equity_plan(
            graded_vesting=True,
            tranche_ids=[(0, 0, {
                'name': 'Only tranche',
                'portion_pct': 100.0,
                'vesting_end_date': '2029-01-01',
                'fair_value': 10.0,
            })],
        )
        plan.action_activate()
        grant = plan.grant_ids[:1]
        tranche = plan.tranche_ids[:1]
        actor_plan = plan.with_user(self.normal_user)
        actor_grant = grant.with_user(self.normal_user)
        actor_tranche = tranche.with_user(self.normal_user)

        for values in (
            {'condition_kind': 'market'},
            {'instrument_desc': 'substituted instrument'},
            {'journal_id': self.journal_misc.id},
            {'expense_account_id': self.sbp_expense.id},
        ):
            with self.subTest(plan_values=values), self.assertRaises(UserError):
                actor_plan.write(values)
        for values in (
            {'partner_id': self.partner_b.id},
            {'instruments_granted': 9999},
            {'grant_date_fair_value': 9999.0},
            {'exercise_price': 9999.0},
        ):
            with self.subTest(grant_values=values), self.assertRaises(UserError):
                actor_grant.write(values)
        with self.assertRaises(UserError):
            actor_tranche.write({'portion_pct': 50.0})
        with self.assertRaises(UserError):
            self.env['eh.sbp.grant'].with_user(self.normal_user).create({
                'plan_id': plan.id,
                'partner_id': self.partner_b.id,
                'instruments_granted': 1,
                'grant_date_fair_value': 1.0,
            })
        with self.assertRaises(UserError):
            actor_grant.unlink()
        with self.assertRaises(UserError):
            actor_tranche.unlink()

        # IFRS 2 estimate/outcome inputs remain deliberately policy-editable.
        actor_grant.write({'expected_forfeiture_pct': 12.5})
        grant.invalidate_recordset(['expected_forfeiture_pct'])
        self.assertAlmostEqual(grant.expected_forfeiture_pct, 12.5)

    def test_ordinary_writer_cannot_inject_active_plan_modification(self):
        self._require(self.normal_user)
        plan = self._equity_plan()
        with self.assertRaises(AccessError):
            self.env['eh.sbp.modification'].with_user(
                self.normal_user,
            ).create({
                'plan_id': plan.id,
                'name': 'Unapproved basis substitution',
                'date': '2026-06-01',
                'incremental_fv': 100.0,
            })

    def test_active_plan_view_routes_ifrs2_true_up_inputs(self):
        """Active estimates stay editable while award terms look frozen."""
        view = self.env.ref(
            'eh_account_share_based_payment.view_eh_sbp_plan_form',
        )
        arch = etree.fromstring(view.arch_db.encode('utf-8'))
        expected = arch.xpath(
            "//page[@name='grants']//*[self::list or self::tree]/field"
            "[@name='expected_forfeiture_pct']",
        )
        terms = arch.xpath(
            "//page[@name='grants']//*[self::list or self::tree]/field"
            "[@name='grant_date_fair_value']",
        )
        self.assertEqual(len(expected), 1)
        self.assertEqual(len(terms), 1)
        expected_readonly = expected[0].get('readonly')
        terms_readonly = terms[0].get('readonly')
        if expected_readonly is not None:
            self.assertNotIn("parent.state != 'draft'", expected_readonly)
            self.assertIn(
                "parent.state in ('settled', 'cancelled')",
                expected_readonly,
            )
            self.assertEqual(
                terms_readonly, "parent.state != 'draft'",
            )
        else:
            # Odoo 16-17 express modifiers in the legacy ``attrs`` domain.
            expected_domain = literal_eval(
                expected[0].get('attrs', '{}'),
            ).get('readonly', [])
            terms_domain = literal_eval(
                terms[0].get('attrs', '{}'),
            ).get('readonly', [])
            self.assertNotIn(
                ('parent.state', '!=', 'draft'), expected_domain,
            )
            self.assertIn(
                ('parent.state', 'in', ['settled', 'cancelled']),
                expected_domain,
            )
            self.assertEqual(
                terms_domain, [('parent.state', '!=', 'draft')],
            )
        guidance = arch.xpath(
            "//page[@name='grants']//div[contains(@class, 'alert-info')]",
        )
        self.assertEqual(len(guidance), 1)

    def test_active_linked_valuation_evidence_is_immutable(self):
        self._require(self.normal_user)
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/',
            'pricing_model': 'black_scholes',
            'spot': 100.0,
            'strike': 100.0,
            'volatility_pct': 20.0,
            'rate_pct': 5.0,
            'term_years': 1.0,
        })
        valuation.action_compute()
        plan = self._draft_equity_plan(grant_ids=[(0, 0, {
            'partner_id': self.partner_a.id,
            'instruments_granted': 300,
            'grant_date_fair_value': valuation.result_value,
            'valuation_id': valuation.id,
        })])
        plan.action_activate()
        grant = plan.grant_ids[:1]
        original_result = valuation.result_value
        original_fair_value = grant.grant_date_fair_value

        with self.assertRaises(UserError):
            valuation.with_user(self.normal_user).write({'spot': 125.0})
        with self.assertRaises(UserError):
            valuation.action_compute()
        with self.assertRaises(UserError):
            valuation.with_user(self.normal_user).unlink()

        valuation.invalidate_recordset()
        grant.invalidate_recordset(['valuation_id', 'grant_date_fair_value'])
        self.assertTrue(valuation.exists())
        self.assertEqual(grant.valuation_id, valuation)
        self.assertAlmostEqual(valuation.result_value, original_result)
        self.assertAlmostEqual(grant.grant_date_fair_value, original_fair_value)

    def test_activation_reproves_linked_valuation_after_draft_edits(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/',
            'pricing_model': 'black_scholes',
            'spot': 100.0,
            'strike': 100.0,
            'volatility_pct': 20.0,
            'rate_pct': 5.0,
            'term_years': 1.0,
        })
        valuation.action_compute()
        plan = self._draft_equity_plan(grant_ids=[(0, 0, {
            'partner_id': self.partner_a.id,
            'instruments_granted': 300,
            'valuation_id': valuation.id,
        })])
        grant = plan.grant_ids[:1]
        grant.action_use_valuation()
        copied_value = grant.grant_date_fair_value

        # A draft valuation may still be edited, but the edit invalidates its
        # result and activation must not freeze the formerly copied amount.
        valuation.write({'spot': 125.0})
        self.assertEqual(valuation.result_provenance_version, 0)
        with self.assertRaises(UserError):
            plan.action_activate()
        self.assertEqual(plan.state, 'draft')

        # Recomputing proves the new result, not the old grant snapshot.
        valuation.action_compute()
        self.assertNotEqual(valuation.result_value, copied_value)
        with self.assertRaises(UserError):
            plan.action_activate()

        # Pulling the result and then manually substituting another amount is
        # equally rejected while the evidence link remains attached.
        grant.action_use_valuation()
        grant.write({
            'grant_date_fair_value':
                valuation.result_value + self.company.currency_id.rounding,
        })
        with self.assertRaises(UserError):
            plan.action_activate()

        grant.action_use_valuation()
        plan.action_activate()
        self.assertEqual(plan.state, 'active')

    def test_currency_switch_invalidates_valuation_and_cannot_relabel_plan(self):
        """A valuation number and approved terms retain their denomination."""
        old_currency = self.company.currency_id
        new_currency = self.env.ref(
            'base.EUR' if old_currency != self.env.ref('base.EUR')
            else 'base.USD'
        )
        if not new_currency.active:
            new_currency.sudo().write({'active': True})
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
        })
        valuation.action_compute()
        old_result = valuation.result_value
        self.assertEqual(valuation.result_currency_id, old_currency)

        # No plan exists yet and no AML has been posted, so core permits the
        # company-currency change. The SBP hook serializes it against compute
        # and invalidates every unconsumed valuation result.
        self.company.write({'currency_id': new_currency.id})
        self.company.invalidate_recordset(['currency_id'])
        valuation.invalidate_recordset([
            'result_value', 'result_provenance_version',
            'result_currency_id',
        ])
        self.assertEqual(self.company.currency_id, new_currency)
        self.assertEqual(valuation.result_value, 0.0)
        self.assertEqual(valuation.result_provenance_version, 0)
        self.assertFalse(valuation.result_currency_id)

        # Recreate the exact stale pre-fix graph through SQL: a positive,
        # current-version result still denominated in the old currency. Both
        # consumption and activation must inspect the durable currency anchor,
        # not merely the company and numeric equality.
        self.env.cr.execute(
            "UPDATE eh_sbp_valuation SET result_value = %s, "
            "result_provenance_version = 1, result_currency_id = %s "
            "WHERE id = %s",
            (old_result, old_currency.id, valuation.id),
        )
        valuation.invalidate_recordset([
            'result_value', 'result_provenance_version',
            'result_currency_id',
        ])
        plan = self._draft_equity_plan(grant_ids=[(0, 0, {
            'partner_id': self.partner_a.id,
            'instruments_granted': 300,
            'grant_date_fair_value': old_result,
            'valuation_id': valuation.id,
        })])
        grant = plan.grant_ids[:1]
        with self.assertRaises(UserError):
            grant.action_use_valuation()
        with self.assertRaises(UserError):
            plan.action_activate()
        self.assertEqual(plan.state, 'draft')

        valuation.action_compute()
        self.assertEqual(valuation.result_currency_id, new_currency)
        grant.action_use_valuation()
        plan.action_activate()
        self.assertEqual(plan.measurement_currency_id, new_currency)

        # Activation stamps the plan's own durable currency evidence. Even
        # before its first run creates an AML, company currency can no longer
        # change and raw RPC cannot substitute the snapshot.
        with self.assertRaises(UserError):
            self.company.write({'currency_id': old_currency.id})
        with self.assertRaises(AccessError):
            plan.with_user(self.normal_user).write({
                'measurement_currency_id': old_currency.id,
            })
        self.company.invalidate_recordset(['currency_id'])
        plan.invalidate_recordset(['measurement_currency_id', 'currency_id'])
        self.assertEqual(self.company.currency_id, new_currency)
        self.assertEqual(plan.measurement_currency_id, new_currency)
        self.assertEqual(plan.currency_id, new_currency)

    def test_result_currency_is_server_owned(self):
        self._require(self.normal_user)
        with self.assertRaises(AccessError):
            self.env['eh.sbp.valuation'].with_user(self.normal_user).create({
                'name': '/', 'spot': 100.0, 'strike': 100.0,
                'result_currency_id': self.company.currency_id.id,
            })
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'spot': 100.0, 'strike': 100.0,
        })
        with self.assertRaises(AccessError):
            valuation.with_user(self.normal_user).write({
                'result_currency_id': self.company.currency_id.id,
            })

    def test_non_finite_valuation_evidence_is_never_consumed(self):
        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
        })
        valuation.action_compute()
        plan = self._draft_equity_plan(grant_ids=[(0, 0, {
            'partner_id': self.partner_a.id,
            'instruments_granted': 300,
            'valuation_id': valuation.id,
        })])
        grant = plan.grant_ids[:1]

        with self.assertRaises(ValidationError):
            valuation.write({'spot': float('nan')})
        with self.assertRaises(ValidationError):
            grant.write({'grant_date_fair_value': float('nan')})

        for literal in ('NaN', 'Infinity'):
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE eh_sbp_valuation SET result_value = %s::double "
                "precision, result_provenance_version = 1 WHERE id = %s",
                (literal, valuation.id),
            )
            self.env.cr.execute(
                "UPDATE eh_sbp_grant SET grant_date_fair_value = "
                "%s::double precision WHERE id = %s",
                (literal, grant.id),
            )
            valuation.invalidate_recordset([
                'result_value', 'result_provenance_version',
            ])
            grant.invalidate_recordset(['grant_date_fair_value'])
            with self.subTest(literal=literal), self.assertRaises(UserError):
                grant.action_use_valuation()
            with self.subTest(literal=literal), self.assertRaises(UserError):
                plan.action_activate()
            self.assertEqual(plan.state, 'draft')

    def test_every_accounting_input_rejects_non_finite_rpc_values(self):
        """Reject NaN/Infinity before Float/Integer precision conversion."""
        plan = self._draft_equity_plan(
            graded_vesting=True,
            tranche_ids=[(0, 0, {
                'name': 'Finite tranche',
                'portion_pct': 100.0,
                'vesting_end_date': '2029-01-01',
                'fair_value': 10.0,
            })],
        )
        grant = plan.grant_ids[:1]
        tranche = plan.tranche_ids[:1]
        for field_name in (
            'vesting_years', 'vesting_months', 'settlement_amount',
        ):
            with self.subTest(model='plan', field=field_name), \
                    self.assertRaises(ValidationError):
                plan.write({field_name: float('nan')})
        for field_name in (
            'instruments_granted', 'grant_date_fair_value', 'exercise_price',
            'expected_forfeiture_pct', 'actual_vested_qty', 'exercised_qty',
            'expired_qty',
        ):
            with self.subTest(model='grant', field=field_name), \
                    self.assertRaises(ValidationError):
                grant.write({field_name: float('inf')})
        for field_name in ('portion_pct', 'fair_value'):
            with self.subTest(model='tranche', field=field_name), \
                    self.assertRaises(ValidationError):
                tranche.write({field_name: float('nan')})

        valuation = self.env['eh.sbp.valuation'].create({
            'name': '/', 'pricing_model': 'black_scholes',
            'spot': 100.0, 'strike': 100.0, 'volatility_pct': 20.0,
            'rate_pct': 5.0, 'term_years': 1.0,
        })
        for field_name in (
            'spot', 'strike', 'volatility_pct', 'rate_pct', 'term_years',
            'dividend_yield_pct', 'steps',
        ):
            with self.subTest(model='valuation', field=field_name), \
                    self.assertRaises(ValidationError):
                valuation.write({field_name: float('nan')})

        active = self._equity_plan()
        with self.assertRaises(ValidationError):
            self.env['eh.sbp.modification'].create({
                'plan_id': active.id,
                'name': 'Non-finite modification',
                'date': '2026-06-01',
                'incremental_fv': float('nan'),
            })
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': active.id,
            'period_end': '2027-01-01',
        })
        with self.assertRaises(ValidationError):
            run.write({'current_fair_value': float('inf')})

    def test_actions_fail_closed_for_legacy_non_finite_graphs(self):
        """Action-time graph validation catches direct-SQL legacy values."""
        manual = self._draft_equity_plan()
        graded = self._draft_equity_plan(
            graded_vesting=True,
            tranche_ids=[(0, 0, {
                'name': 'Legacy tranche',
                'portion_pct': 100.0,
                'vesting_end_date': '2029-01-01',
                'fair_value': 10.0,
            })],
        )
        modified = self._equity_plan()
        modification = self.env['eh.sbp.modification'].create({
            'plan_id': modified.id,
            'name': 'Legacy modification',
            'date': '2026-06-01',
            'incremental_fv': 1.0,
        })
        cash_run_plan = self._equity_plan(settlement='cash')
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': cash_run_plan.id,
            'period_end': '2027-01-01',
            'current_fair_value': 12.0,
        })
        cash_settle = self._equity_plan(
            settlement='cash', settlement_amount=1200.0,
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_grant SET expected_forfeiture_pct = "
            "'NaN'::double precision WHERE plan_id = %s",
            (manual.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan_tranche SET portion_pct = "
            "'Infinity'::double precision WHERE plan_id = %s",
            (graded.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_modification SET incremental_fv = "
            "'NaN'::double precision WHERE id = %s",
            (modification.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_period_run SET current_fair_value = "
            "'Infinity'::double precision WHERE id = %s",
            (run.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET settlement_amount = "
            "'NaN'::double precision WHERE id = %s",
            (cash_settle.id,),
        )
        manual.grant_ids.invalidate_recordset(['expected_forfeiture_pct'])
        graded.tranche_ids.invalidate_recordset(['portion_pct'])
        modification.invalidate_recordset(['incremental_fv'])
        run.invalidate_recordset(['current_fair_value'])
        cash_settle.invalidate_recordset(['settlement_amount'])

        with self.assertRaises(UserError):
            manual.action_activate()
        with self.assertRaises(UserError):
            graded.action_activate()
        with self.assertRaises(UserError):
            modified.action_cancel()
        with self.assertRaises(UserError):
            run.action_post()
        with self.assertRaises(UserError):
            cash_settle.action_settle()
        self.assertEqual(manual.state, 'draft')
        self.assertEqual(graded.state, 'draft')
        self.assertEqual(modified.state, 'active')
        self.assertEqual(run.state, 'draft')
        self.assertEqual(cash_settle.state, 'active')
        self.assertFalse(self.env['account.move'].search([
            ('eh_sbp_plan_id', 'in', [
                modified.id, cash_run_plan.id, cash_settle.id,
            ]),
        ]))

    def test_foreign_plan_id_is_checked_before_run_creation(self):
        self._require(self.normal_user)
        foreign_company = self.env['res.company'].create({
            'name': 'Hidden SBP Company',
        })
        foreign_plan = self.env['eh.sbp.plan'].sudo().with_company(
            foreign_company,
        ).create({
            'name': '/',
            'company_id': foreign_company.id,
            'grant_date': '2026-01-01',
        })
        with self.assertRaises(AccessError):
            self.env['eh.sbp.period.run'].with_user(
                self.normal_user,
            ).create({
                'plan_id': foreign_plan.id,
                'period_end': '2027-01-01',
            })

    def test_post_recomputes_under_run_and_plan_row_locks(self):
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            run.action_post()
        self.assertTrue(any(
            'eh_sbp_period_run' in query and 'FOR UPDATE' in query
            for query in queries
        ))
        self.assertTrue(any(
            'eh_sbp_plan' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_multi_company_plan_mutation_locks_all_companies_before_plans(self):
        """Cross-company recordsets cannot alternate company/plan locks."""
        foreign_company = self.env['res.company'].create({
            'name': 'SBP Lock Order Company',
        })
        # Create company B's plan first, then company A's plan. Plan-id order is
        # therefore B -> A while company-id order is A -> B: the exact crossing
        # order that deadlocked when write/unlink locked one plan at a time.
        foreign_plan = self.env['eh.sbp.plan'].sudo().with_company(
            foreign_company,
        ).create({
            'name': '/', 'company_id': foreign_company.id,
            'grant_date': '2026-01-01',
        })
        local_plan = self.env['eh.sbp.plan'].create({
            'name': '/', 'grant_date': '2026-01-01',
        })
        plans = foreign_plan | local_plan
        real_execute = self.env.cr.execute

        def _assert_global_order(operation, expect_company=True):
            queries = []

            def _record_execute(*args, **kwargs):
                queries.append(str(args[0]))
                return real_execute(*args, **kwargs)

            with patch.object(self.env.cr, 'execute', _record_execute):
                operation()
            company_locks = [
                index for index, query in enumerate(queries)
                if 'res_company' in query and 'FOR UPDATE' in query
            ]
            plan_locks = [
                index for index, query in enumerate(queries)
                if 'eh_sbp_plan' in query and 'FOR UPDATE' in query
            ]
            self.assertEqual(len(company_locks), 1 if expect_company else 0)
            self.assertEqual(len(plan_locks), 1)
            if expect_company:
                self.assertLess(company_locks[0], plan_locks[0])

        _assert_global_order(
            lambda: plans.write({'notes': 'lock ordered'}),
            expect_company=False,
        )
        _assert_global_order(plans.unlink)

    def test_journal_and_account_classes_are_server_enforced(self):
        bank_journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'bank'),
        ], limit=1)
        if not bank_journal:
            bank_journal = self._ensure_journal(
                self.env, self.company, 'bank', 'SBBK', 'SBP Bank',
                default_account=self.account_cash,
            )
        invalid_values = (
            {'journal_id': bank_journal.id},
            {'expense_account_id': self.account_cash.id},
            {'equity_account_id': self.sbp_expense.id},
            {'liability_account_id': self.sbp_reserve.id},
            {'settlement_account_id': self.sbp_expense.id},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.env['eh.sbp.plan'].create({
                    'name': '/', 'grant_date': '2026-01-01', **values,
                })

        plan = self._equity_plan()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET expense_account_id = %s WHERE id = %s",
            (self.account_cash.id, plan.id),
        )
        plan.invalidate_recordset(['expense_account_id'])
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        with self.assertRaises(ValidationError):
            run.action_post()

    def test_quarantined_plan_blocks_accounting_actions(self):
        self._require(self.normal_user)
        plan = self._equity_plan()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET legacy_provenance_quarantined = TRUE, "
            "legacy_provenance_quarantine_note = 'test quarantine' "
            "WHERE id = %s",
            (plan.id,),
        )
        plan.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        with self.assertRaises(UserError):
            self.env['eh.sbp.period.run'].create({
                'plan_id': plan.id, 'period_end': '2027-01-01',
            })
        with self.assertRaises(UserError):
            plan.grant_ids.write({'expected_forfeiture_pct': 20.0})
        with self.assertRaises(UserError):
            plan.with_user(self.normal_user).write({
                'legacy_provenance_review_note':
                    'Unprivileged attempt to attest legacy evidence.',
            })
        with self.assertRaises(UserError):
            plan.with_user(self.normal_user).action_review_legacy_provenance()

    def test_late_run_failure_rolls_back_gl_outputs_and_anchor(self):
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        RunModel = type(run)
        real_write = RunModel.write

        def _fail_final_state(records, values):
            if values.get('state') == 'posted':
                raise UserError("simulated late run-state failure")
            return real_write(records, values)

        raised = False
        try:
            with patch.object(RunModel, 'write', _fail_final_state):
                run.action_post()
        except UserError:
            raised = True
        self.assertTrue(raised)
        plan.invalidate_recordset(['recognised_cumulative', 'state'])
        run.invalidate_recordset()
        moves = self.env['account.move'].search([
            ('eh_sbp_plan_id', '=', plan.id),
        ])
        self.assertFalse(moves)
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.move_id)
        self.assertEqual(run.period_charge, 0.0)
        self.assertEqual(plan.recognised_cumulative, 0.0)

    def test_late_cancel_failure_rolls_back_gl_anchor_and_state(self):
        plan = self._equity_plan()
        PlanModel = type(plan)
        real_engine_write = PlanModel._eh_engine_write

        def _fail_final_state(records, values):
            if values.get('state') == 'cancelled':
                raise UserError("simulated late cancellation failure")
            return real_engine_write(records, values)

        raised = False
        try:
            with patch.object(
                    PlanModel, '_eh_engine_write', _fail_final_state):
                plan.action_cancel()
        except UserError:
            raised = True
        self.assertTrue(raised)
        plan.invalidate_recordset()
        moves = self.env['account.move'].search([
            ('eh_sbp_plan_id', '=', plan.id),
        ])
        self.assertFalse(moves)
        self.assertEqual(plan.state, 'active')
        self.assertEqual(plan.recognised_cumulative, 0.0)

    def test_late_cash_settle_failure_rolls_back_both_moves(self):
        plan = self._equity_plan(
            settlement='cash', settlement_amount=1200.0,
        )
        PlanModel = type(plan)
        real_engine_write = PlanModel._eh_engine_write

        def _fail_final_state(records, values):
            if values.get('state') == 'settled':
                raise UserError("simulated late settlement failure")
            return real_engine_write(records, values)

        raised = False
        try:
            with patch.object(
                    PlanModel, '_eh_engine_write', _fail_final_state):
                plan.action_settle()
        except UserError:
            raised = True
        self.assertTrue(raised)
        plan.invalidate_recordset()
        moves = self.env['account.move'].search([
            ('eh_sbp_plan_id', '=', plan.id),
        ])
        self.assertFalse(moves)
        self.assertEqual(plan.state, 'active')
        self.assertEqual(plan.recognised_cumulative, 0.0)
        self.assertEqual(plan.settlement_amount, 1200.0)

    def test_cancelled_plan_freezes_estimates_and_modification_graph(self):
        plan = self._equity_plan()
        modification = self.env['eh.sbp.modification'].create({
            'plan_id': plan.id,
            'name': 'Approved modification',
            'date': '2026-06-01',
            'incremental_fv': 1.0,
        })
        plan.action_cancel()
        grant = plan.grant_ids[:1]
        for values in (
            {'expected_forfeiture_pct': 50.0},
            {'forfeited': True},
            {'condition_failed': True},
            {'vesting_finalised': True},
            {'actual_vested_qty': 1},
        ):
            with self.subTest(grant_values=values), self.assertRaises(UserError):
                grant.write(values)
        with self.assertRaises(UserError):
            self.env['eh.sbp.modification'].create({
                'plan_id': plan.id,
                'name': 'Post-cancel injection',
                'date': '2026-07-01',
                'incremental_fv': 99.0,
            })
        with self.assertRaises(UserError):
            modification.write({'incremental_fv': 99.0})
        target = self._equity_plan()
        with self.assertRaises(UserError):
            modification.write({'plan_id': target.id})
        with self.assertRaises(UserError):
            modification.unlink()

    def test_settled_plan_freezes_measurement_but_keeps_disclosure_updates(self):
        plan = self._equity_plan(
            settlement='cash', settlement_amount=1200.0,
        )
        plan.action_settle()
        grant = plan.grant_ids[:1]
        for values in (
            {'expected_forfeiture_pct': 50.0},
            {'forfeited': True},
            {'condition_failed': True},
            {'vesting_finalised': True},
            {'actual_vested_qty': 1},
        ):
            with self.subTest(grant_values=values), self.assertRaises(UserError):
                grant.write(values)
        with self.assertRaises(UserError):
            plan.write({'settlement_amount': 999.0})
        self.assertEqual(plan.settlement_amount, 1200.0)

        grant.write({
            'exercised_qty': 10,
            'expired_qty': 5,
            'notes': 'Terminal disclosure update',
        })
        self.assertEqual(grant.exercised_qty, 10)
        self.assertEqual(grant.expired_qty, 5)

    # ------------------------------------------------------------------
    # positive: the sanctioned actions still set the anchor as a
    # real (non-superuser) manager
    # ------------------------------------------------------------------
    def test_run_post_sets_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01'})
        run.with_user(self.manager_user).action_post()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertAlmostEqual(
            plan.recognised_cumulative, 1000.00, places=2,
            msg='a non-superuser manager post must set the anchor via su')

    def test_cancel_acceleration_sets_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan()
        # Cancellation accelerates the full unrecognised balance immediately
        # (IFRS 2.28(a)): 300 x 10.00 = 3,000.00.
        plan.with_user(self.manager_user).action_cancel()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertEqual(plan.state, 'cancelled')
        self.assertAlmostEqual(
            plan.recognised_cumulative, 3000.00, places=2,
            msg='cancellation acceleration must set the anchor via su')

    def test_cash_settle_clears_anchor_as_non_superuser_manager(self):
        self._require(self.manager_user)
        plan = self._equity_plan(
            settlement='cash', settlement_amount=1200.0)
        # Post a cash run so a liability is carried:
        # 300 x 12.00 x 12/36 = 1,200.00.
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
            'current_fair_value': 12.0})
        run.action_post()
        self.assertAlmostEqual(plan.recognised_cumulative, 1200.00, places=2)
        # Settle trues the liability to the settlement amount and pays it,
        # then zeroes the anchor (IFRS 2.30) - all under su.
        plan.settlement_date = '2027-01-02'
        with freeze_time('2027-01-02'):
            plan.with_user(self.manager_user).action_settle()
        plan.invalidate_recordset(['recognised_cumulative'])
        self.assertEqual(plan.state, 'settled')
        self.assertAlmostEqual(
            plan.recognised_cumulative, 0.0, places=2,
            msg='cash settlement must clear the anchor via su')
