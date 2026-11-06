# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 19 worked examples for eh_account_employee_benefits.

Every expected amount is hand-derived from the inputs stated in the test,
with the derivation in a comment; nothing is read back from the engine to
build an expectation. Assertions are exact to company currency (2dp).

Engine conventions asserted here (from models/benefit_valuation.py):

* Net interest = discount rate x OPENING balances (IAS 19.123 simplified;
  mid-year cash-flow weighting out of scope).
* actuarial_gain_loss_dbo is LOSS POSITIVE; return_on_assets_excess is
  GAIN POSITIVE; OCI remeasurement (loss positive) = actuarial loss -
  excess return + ceiling effect change; never recycled (IAS 19.122).
* Separate balance-sheet accounts: the entry credits the DBO account by
  the obligation movement and debits the plan asset account by the asset
  movement; a funded plan pays benefits/settlements out of plan assets so
  they cancel inside the two movements and never touch employer cash.
* contributions_posted_elsewhere=True credits the contribution clearing
  account (payroll posts the cash leg), never the bank.
* Rounding: each derived monetary figure is rounded to 2dp in the order
  documented in the class docstring (rate products first, then sums).
"""

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_employee_benefits', 'post_install',
        '-at_install')
class TestGoldenIas19(EhGoldenTestCase):
    """IAS 19 worked examples: full-year rollforward, past service cost,
    asset ceiling, settlement, rollforward tie, opening chain, DC accrual,
    and the frozen/sealed/reversal guards."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.acc_service = cls._ensure_account(
            cls.env, '6201', 'DB Service Cost', 'expense')
        cls.acc_net_interest = cls._ensure_account(
            cls.env, '6211', 'DB Net Interest', 'expense')
        cls.acc_oci = cls._ensure_account(
            cls.env, '3201', 'OCI DB Remeasurements', 'equity')
        cls.acc_dbo = cls._ensure_account(
            cls.env, '2951', 'Defined Benefit Obligation',
            'liability_non_current')
        cls.acc_assets = cls._ensure_account(
            cls.env, '1951', 'Plan Assets', 'asset_non_current')
        cls.acc_contrib = cls._ensure_account(
            cls.env, '2151', 'Pension Contribution Clearing',
            'liability_current')
        cls.acc_dc_payable = cls._ensure_account(
            cls.env, '2171', 'DC Contributions Payable', 'liability_current')

    def _plan(self, **vals):
        base = {
            'name': 'Test DB Plan',
            'funded': True,
            'contributions_posted_elsewhere': True,
            'service_cost_account_id': self.acc_service.id,
            'net_interest_account_id': self.acc_net_interest.id,
            'oci_account_id': self.acc_oci.id,
            'dbo_account_id': self.acc_dbo.id,
            'plan_asset_account_id': self.acc_dbo.id,
            'contribution_account_id': self.acc_contrib.id,
            'contribution_cash_account_id': self.account_cash.id,
            'benefit_payment_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        plan = self.env['eh.benefit.plan'].create(base)
        plan.action_activate()
        return plan

    def _valuation(self, plan, **vals):
        base = {'plan_id': plan.id, 'period_end': '2026-12-31'}
        base.update(vals)
        return self.env['eh.benefit.valuation'].create(base)

    # ------------------------------------------------------------------
    # golden 1: full year, funded plan
    # ------------------------------------------------------------------
    def test_golden_full_year_funded(self):
        """Full-year funded plan at 5%.

        Inputs: opening DBO 1,000,000; opening assets 800,000; rate 5%;
        current service cost 60,000; benefits paid 30,000 (from plan
        assets, both sides); contributions 45,000; actuarial LOSS on DBO
        20,000; excess return on assets +5,000.

        Hand derivation:
          interest cost   = 5% x 1,000,000            =    50,000.00
          interest income = 5% x 800,000              =    40,000.00
          net interest    = 50,000 - 40,000           =    10,000.00
          closing DBO     = 1,000,000 + 60,000 + 50,000
                            - 30,000 + 20,000         = 1,100,000.00
          closing assets  = 800,000 + 40,000 + 45,000
                            - 30,000 + 5,000          =   860,000.00
          net liability   = 1,100,000 - 860,000       =   240,000.00
                            (opening 200,000, moved +40,000)
          OCI loss        = 20,000 - 5,000            =    15,000.00
          P&L             = 60,000 + 10,000           =    70,000.00

        Entry (separate DBO / plan asset accounts, contributions posted
        elsewhere so the credit hits the clearing account):
          Dr service cost           60,000.00
          Dr net interest           10,000.00
          Dr OCI remeasurement      15,000.00
          Dr plan assets            60,000.00  (860,000 - 800,000)
             Cr DBO liability                 100,000.00 (1,100,000 - 1,000,000)
             Cr contribution clearing          45,000.00
        Balanced: 145,000.00 = 145,000.00. The identity: delta DBO (100k)
        - delta assets (60k) = P&L (70k) + OCI (15k) - contributions (45k)
        = 40k.
        """
        plan = self._plan(name='Golden 1 plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=1000000.0, opening_assets=800000.0,
            discount_rate=5.0, current_service_cost=60000.0,
            benefits_paid=30000.0, contributions_employer=45000.0,
            actuarial_gain_loss_dbo=20000.0, return_on_assets_excess=5000.0)
        self.assertAlmostEqual(v.interest_cost, 50000.00, places=2,
                               msg='interest cost = 5% x opening DBO')
        self.assertAlmostEqual(v.interest_income, 40000.00, places=2,
                               msg='interest income = 5% x opening assets')
        self.assertAlmostEqual(v.net_interest, 10000.00, places=2)
        self.assertAlmostEqual(v.closing_dbo, 1100000.00, places=2)
        self.assertAlmostEqual(v.closing_assets, 860000.00, places=2)
        self.assertAlmostEqual(v.net_liability, 240000.00, places=2)
        self.assertAlmostEqual(v.oci_remeasurement, 15000.00, places=2,
                               msg='OCI = 20,000 loss - 5,000 excess return')
        self.assertAlmostEqual(v.pnl_total, 70000.00, places=2,
                               msg='P&L = 60,000 service + 10,000 net interest')
        v.action_post()
        self.assertEqual(v.state, 'posted')
        self.assertMoveLines(v.move_id, [
            (self.acc_service, 60000.00, 0.0),
            (self.acc_net_interest, 10000.00, 0.0),
            (self.acc_oci, 15000.00, 0.0),
            (self.acc_dbo, 60000.00, 0.0),
            (self.acc_dbo, 0.0, 100000.00),
            (self.acc_contrib, 0.0, 45000.00),
        ], msg='golden 1 full-year entry')
        self.assertBalanced(v.move_id)
        self.assertTrue(v.move_id.eh_sealed)
        # One IAS 19.63 net-position account: 100,000 credit obligation less
        # 60,000 debit plan-asset movement = 40,000 net credit movement.
        self.assertAlmostEqual(
            self.posted_balance(self.acc_dbo), -40000.00, places=2)
        # Disclosure feed reads the same numbers off the posted valuation.
        roll = plan.get_rollforward()
        self.assertEqual(len(roll['dbo']), 1)
        dbo_row = roll['dbo'][0]
        self.assertAlmostEqual(dbo_row['opening'], 1000000.00, places=2)
        self.assertAlmostEqual(dbo_row['interest_cost'], 50000.00, places=2)
        self.assertAlmostEqual(dbo_row['closing'], 1100000.00, places=2)
        asset_row = roll['assets'][0]
        self.assertAlmostEqual(asset_row['opening'], 800000.00, places=2)
        self.assertAlmostEqual(
            asset_row['contributions_employer'], 45000.00, places=2)
        self.assertAlmostEqual(asset_row['closing'], 860000.00, places=2)

    # ------------------------------------------------------------------
    # golden 2: past service cost immediately in P&L (IAS 19.103)
    # ------------------------------------------------------------------
    def test_golden_past_service_cost_immediate_pnl(self):
        """A plan amendment of 25,000 goes to P&L immediately, never OCI.

        Inputs: opening DBO 500,000; assets 400,000; rate 0; past service
        cost 25,000 and nothing else.
          closing DBO = 500,000 + 25,000 = 525,000; assets unchanged.
        Entry: Dr service cost 25,000 / Cr DBO 25,000. The OCI account
        must not move.
        """
        oci_before = self.posted_balance(self.acc_oci)
        plan = self._plan(name='Golden 2 plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=500000.0, opening_assets=400000.0,
            discount_rate=0.0, past_service_cost=25000.0)
        self.assertAlmostEqual(v.closing_dbo, 525000.00, places=2)
        self.assertAlmostEqual(v.oci_remeasurement, 0.00, places=2)
        self.assertAlmostEqual(v.pnl_total, 25000.00, places=2)
        v.action_post()
        self.assertMoveLines(v.move_id, [
            (self.acc_service, 25000.00, 0.0),
            (self.acc_dbo, 0.0, 25000.00),
        ], msg='past service cost is immediate P&L (IAS 19.103)')
        self.assertAlmostEqual(
            self.posted_balance(self.acc_oci), oci_before, places=2,
            msg='an amendment never touches OCI')

    # ------------------------------------------------------------------
    # golden 3: asset ceiling (IAS 19.64)
    # ------------------------------------------------------------------
    def test_golden_asset_ceiling(self):
        """Surplus 50,000 against a ceiling of 30,000.

        Inputs: opening DBO 400,000; assets 450,000; rate 0; no flows;
        ceiling 30,000; opening ceiling effect 0.
          surplus          = 450,000 - 400,000 = 50,000
          recognised asset = min(50,000, 30,000) = 30,000
          ceiling effect   = 50,000 - 30,000 = 20,000 (delta +20,000)
          OCI              = 0 - 0 + 20,000 = 20,000 loss
        Entry: Dr OCI 20,000 / Cr plan assets 20,000 (the allowance sits
        on the plan asset account so the recognised net position is the
        30,000 asset). No other movement: rate 0, no flows.
        """
        asset_before = self.posted_balance(self.acc_dbo)
        plan = self._plan(name='Golden 3 plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=400000.0, opening_assets=450000.0,
            discount_rate=0.0, apply_asset_ceiling=True,
            asset_ceiling=30000.0)
        self.assertAlmostEqual(v.surplus, 50000.00, places=2)
        self.assertAlmostEqual(v.recognised_asset, 30000.00, places=2)
        self.assertAlmostEqual(v.ceiling_effect, 20000.00, places=2)
        self.assertAlmostEqual(v.ceiling_effect_delta, 20000.00, places=2)
        self.assertAlmostEqual(v.oci_remeasurement, 20000.00, places=2)
        self.assertAlmostEqual(
            v.recognised_net_position, -30000.00, places=2,
            msg='negative recognised position = asset of 30,000')
        v.action_post()
        self.assertMoveLines(v.move_id, [
            (self.acc_oci, 20000.00, 0.0),
            (self.acc_dbo, 0.0, 20000.00),
        ], msg='ceiling effect routes to OCI against the plan asset account')
        self.assertAlmostEqual(
            self.posted_balance(self.acc_dbo), asset_before - 20000.00,
            places=2)

    # ------------------------------------------------------------------
    # golden 4: settlement (IAS 19.109-112)
    # ------------------------------------------------------------------
    def test_golden_settlement_gain(self):
        """Released DBO 100,000 settled for 90,000 -> gain 10,000 to P&L.

        Inputs: opening DBO 500,000; assets 400,000; rate 0; settlement
        releases 100,000 of DBO for a payment of 90,000 out of plan
        assets.
          settlement gain = 100,000 - 90,000 = 10,000 (P&L, inside
          service cost per IAS 19.8)
          closing DBO    = 500,000 - 100,000 = 400,000 (delta -100,000)
          closing assets = 400,000 - 90,000  = 310,000 (delta  -90,000)
        Entry: Dr DBO 100,000 / Cr plan assets 90,000 / Cr service cost
        10,000. Balanced: 100,000 = 100,000. P&L total = -10,000 (gain).
        """
        plan = self._plan(name='Golden 4 plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=500000.0, opening_assets=400000.0,
            discount_rate=0.0, settlement_dbo_released=100000.0,
            settlement_payment=90000.0)
        self.assertAlmostEqual(v.settlement_gain_loss, 10000.00, places=2)
        self.assertAlmostEqual(v.closing_dbo, 400000.00, places=2)
        self.assertAlmostEqual(v.closing_assets, 310000.00, places=2)
        self.assertAlmostEqual(v.pnl_total, -10000.00, places=2,
                               msg='the settlement gain is P&L income')
        v.action_post()
        self.assertMoveLines(v.move_id, [
            (self.acc_dbo, 100000.00, 0.0),
            (self.acc_dbo, 0.0, 90000.00),
            (self.acc_service, 0.0, 10000.00),
        ], msg='settlement gain = released DBO - payment, to P&L')
        self.assertBalanced(v.move_id)

    # ------------------------------------------------------------------
    # golden 5: rollforward tie constraint (the audit-proof requirement)
    # ------------------------------------------------------------------
    def test_golden_rollforward_tie_blocked(self):
        """A mis-keyed closing figure is refused.

        Derived closing DBO for these inputs is 1,100,000 (golden 1
        derivation); keying 1,100,500 breaks the tie by 500 > 0.01 and
        must raise. Same for closing assets (derived 860,000).
        """
        plan = self._plan(name='Golden 5 plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=1000000.0, opening_assets=800000.0,
            discount_rate=5.0, current_service_cost=60000.0,
            benefits_paid=30000.0, contributions_employer=45000.0,
            actuarial_gain_loss_dbo=20000.0, return_on_assets_excess=5000.0)
        # savepoint + invalidate: a failed flush must not poison the cache
        # for the next assertion.
        with self.assertRaises(ValidationError,
                               msg='mis-keyed closing DBO must be blocked'), \
                self.env.cr.savepoint():
            v.closing_dbo = 1100500.0
        v.invalidate_recordset()
        with self.assertRaises(ValidationError,
                               msg='mis-keyed closing assets must be blocked'), \
                self.env.cr.savepoint():
            v.closing_assets = 860300.0
        v.invalidate_recordset()
        # The straight-through figures still post.
        v.action_post()
        self.assertEqual(v.state, 'posted')

    # ------------------------------------------------------------------
    # golden 6: opening chain to the prior posted valuation
    # ------------------------------------------------------------------
    def test_golden_opening_chain(self):
        """Year 2 opens exactly where posted year 1 closed.

        Year 1: opening DBO 100,000 / assets 50,000, rate 0, service cost
        10,000 -> closing DBO 110,000, assets 50,000. Year 2 must default
        to those figures, a divergent keyed opening must raise, and
        reversing year 1 while year 2 is posted must be refused (the
        chain unwinds newest first).
        """
        plan = self._plan(name='Golden 6 plan')
        v1 = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=100000.0, opening_assets=50000.0,
            discount_rate=0.0, current_service_cost=10000.0)
        v1.action_post()
        v2 = self._valuation(plan, period_end='2027-12-31')
        self.assertAlmostEqual(v2.opening_dbo, 110000.00, places=2,
                               msg='opening DBO chains from year 1 closing')
        self.assertAlmostEqual(v2.opening_assets, 50000.00, places=2,
                               msg='opening assets chain from year 1 closing')
        with self.assertRaises(ValidationError,
                               msg='a broken opening chain must be blocked'), \
                self.env.cr.savepoint():
            v2.opening_dbo = 120000.0
        v2.invalidate_recordset()
        v2.current_service_cost = 5000.0
        v2.action_post()
        with self.assertRaises(UserError,
                               msg='reversal must run newest to oldest'):
            v1.action_reverse()

    def test_reversed_period_accepts_one_auditable_correction(self):
        plan = self._plan(name='Restated period plan')
        original = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=100000.0, opening_assets=50000.0,
            current_service_cost=10000.0,
        )
        original.action_post()
        original_move = original.move_id
        original.action_reverse()

        corrected = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=100000.0, opening_assets=50000.0,
            current_service_cost=12000.0,
        )
        self.assertEqual(corrected.supersedes_id, original)
        corrected.action_post()
        self.assertEqual(original.state, 'reversed')
        self.assertEqual(original.move_id, original_move)
        self.assertEqual(original.superseded_by_ids, corrected)
        self.assertNotEqual(corrected.move_id, original_move)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._valuation(
                plan, period_end='2026-12-31',
                opening_dbo=100000.0, opening_assets=50000.0,
            )

        following = self._valuation(plan, period_end='2027-12-31')
        self.assertAlmostEqual(
            following.opening_dbo, corrected.closing_dbo, places=2,
        )

    # ------------------------------------------------------------------
    # golden 7: DC accrual (IAS 19.51)
    # ------------------------------------------------------------------
    def test_golden_dc_accrual(self):
        """DC contribution of 10,000: Dr expense / Cr payable, sealed."""
        accrual = self.env['eh.benefit.dc.accrual'].create({
            'period_date': '2026-06-30',
            'amount': 10000.0,
            'expense_account_id': self.acc_service.id,
            'payable_account_id': self.acc_dc_payable.id,
            'journal_id': self.journal_misc.id,
        })
        accrual.action_post()
        self.assertEqual(accrual.state, 'posted')
        self.assertMoveLines(accrual.move_id, [
            (self.acc_service, 10000.00, 0.0),
            (self.acc_dc_payable, 0.0, 10000.00),
        ], msg='IAS 19.51: expense and liability when service is rendered')
        self.assertTrue(accrual.move_id.eh_sealed)
        with self.assertRaises(AccessError):
            accrual.move_id.write({'eh_benefit_provenance_version': 0})
        # Reversal restores both accounts.
        accrual.action_reverse()
        self.assertEqual(accrual.state, 'reversed')
        self.assertEqual(accrual.reversal_date, accrual.move_id.date)
        reversal = accrual.move_ids - accrual.move_id
        self.assertEqual(reversal.date, accrual.move_id.date)
        self.assertEqual(reversal.reversed_entry_id, accrual.move_id)
        self.assertEqual(accrual.move_id.eh_benefit_provenance_version, 1)
        self.assertEqual(reversal.eh_benefit_provenance_version, 1)
        with self.assertRaises(AccessError):
            reversal.write({'reversed_entry_id': False})
        self.assertMoveLines(reversal, [
            (self.acc_service, 0.0, 10000.00),
            (self.acc_dc_payable, 10000.00, 0.0),
        ], msg='DC reversal mirrors the accrual')

    def test_dc_primary_move_cannot_masquerade_as_reversal(self):
        accrual = self.env['eh.benefit.dc.accrual'].create({
            'period_date': '2026-06-30',
            'amount': 1000.0,
            'expense_account_id': self.acc_service.id,
            'payable_account_id': self.acc_dc_payable.id,
            'journal_id': self.journal_misc.id,
        })
        accrual.action_post()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET reversed_entry_id = id WHERE id = %s",
            (accrual.move_id.id,),
        )
        accrual.move_id.invalidate_recordset(['reversed_entry_id'])
        with self.assertRaises(UserError):
            accrual.action_reverse()

    def test_db_primary_move_cannot_masquerade_as_reversal(self):
        plan = self._plan(name='Primary edge guard plan')
        valuation = self._valuation(
            plan,
            period_end='2026-12-31',
            opening_dbo=100000.0,
            current_service_cost=10000.0,
        )
        valuation.action_post()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET reversed_entry_id = id WHERE id = %s",
            (valuation.move_id.id,),
        )
        valuation.move_id.invalidate_recordset(['reversed_entry_id'])
        with self.assertRaises(UserError):
            valuation.action_reverse()

    # ------------------------------------------------------------------
    # guards: frozen after post, sealed move, state machine reversal
    # ------------------------------------------------------------------
    def test_golden_frozen_and_reversal_path(self):
        """Posted valuations freeze; the wizardless reversal path is the
        only unwind.

        Reuses the golden 1 figures. After posting: editing an input or
        re-keying the state raises; resetting the sealed move to draft
        raises; action_reverse posts the exact mirror (assert line by
        line) and nets every account back to its pre-posting balance.
        """
        balances_before = {
            acc.id: self.posted_balance(acc)
            for acc in (self.acc_service, self.acc_net_interest,
                        self.acc_oci, self.acc_dbo, self.acc_assets,
                        self.acc_contrib)}
        plan = self._plan(name='Guard plan')
        v = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=1000000.0, opening_assets=800000.0,
            discount_rate=5.0, current_service_cost=60000.0,
            benefits_paid=30000.0, contributions_employer=45000.0,
            actuarial_gain_loss_dbo=20000.0, return_on_assets_excess=5000.0)
        v.action_post()
        with self.assertRaises(UserError,
                               msg='inputs must freeze after posting'):
            v.current_service_cost = 61000.0
        # State re-key by a non-superuser is refused by eh.workflow.guard;
        # that boundary is covered in test_workflow_guard with a low-privilege
        # user. This golden runs as superuser (trusted), where the sealed move
        # below is the binding protection on the figures.
        with self.assertRaises(UserError,
                               msg='the sealed move blocks the standard '
                                   'reset/reversal path'):
            v.move_id.button_draft()
        with self.assertRaises(UserError,
                               msg='posted valuations cannot be deleted'):
            v.unlink()
        # Plan structure freezes once a valuation has posted.
        with self.assertRaises(UserError,
                               msg='plan accounts freeze after posting'):
            plan.dbo_account_id = self.acc_dc_payable.id
        # The sanctioned reversal: mirror of golden 1's six legs.
        v.action_reverse()
        self.assertEqual(v.state, 'reversed')
        self.assertEqual(v.reversal_date, v.move_id.date)
        reversal = v.move_ids - v.move_id
        self.assertEqual(reversal.date, v.move_id.date)
        self.assertEqual(reversal.reversed_entry_id, v.move_id)
        self.assertEqual(v.move_id.eh_benefit_provenance_version, 1)
        self.assertEqual(reversal.eh_benefit_provenance_version, 1)
        with self.assertRaises(AccessError):
            reversal.write({'reversed_entry_id': False})
        self.assertMoveLines(reversal, [
            (self.acc_service, 0.0, 60000.00),
            (self.acc_net_interest, 0.0, 10000.00),
            (self.acc_oci, 0.0, 15000.00),
            (self.acc_dbo, 0.0, 60000.00),
            (self.acc_dbo, 100000.00, 0.0),
            (self.acc_contrib, 45000.00, 0.0),
        ], msg='reversal mirrors the valuation entry')
        for acc in (self.acc_service, self.acc_net_interest, self.acc_oci,
                    self.acc_dbo, self.acc_assets, self.acc_contrib):
            self.assertAlmostEqual(
                self.posted_balance(acc), balances_before[acc.id], places=2,
                msg='reversal nets %s back to zero movement' % acc.code)

    def test_reversal_refuses_date_before_original(self):
        plan = self._plan(name='Exact reversal date plan')
        valuation = self._valuation(
            plan, period_end='2026-12-31',
            opening_dbo=100000.0, current_service_cost=10000.0,
        )
        valuation.action_post()
        valuation.reversal_date = '2026-12-30'
        with self.assertRaises(UserError):
            valuation.action_reverse()
        valuation.invalidate_recordset(['state', 'move_ids'])
        self.assertEqual(valuation.state, 'posted')
        self.assertEqual(valuation.move_ids, valuation.move_id)

    def test_server_enforces_dc_and_db_account_classifications(self):
        bank_journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'bank'),
        ], limit=1)
        if not bank_journal:
            bank_journal = self._ensure_journal(
                self.env, self.company, 'bank', 'EBBK', 'Benefits Bank',
                default_account=self.account_cash,
            )
        with self.assertRaises(ValidationError):
            self.env['eh.benefit.dc.accrual'].create({
                'period_date': '2026-12-31', 'amount': 1.0,
                'journal_id': bank_journal.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.benefit.dc.accrual'].create({
                'period_date': '2026-12-31', 'amount': 1.0,
                'expense_account_id': self.account_cash.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.benefit.dc.accrual'].create({
                'period_date': '2026-12-31', 'amount': 1.0,
                'payable_account_id': self.acc_service.id,
            })

        invalid_plan_values = (
            {'journal_id': bank_journal.id},
            {'service_cost_account_id': self.account_cash.id},
            {'net_interest_account_id': self.account_cash.id},
            {'oci_account_id': self.acc_service.id},
            {'dbo_account_id': self.acc_service.id},
            {'plan_asset_account_id': self.acc_service.id},
            {'contribution_account_id': self.acc_service.id},
            {'benefit_payment_account_id': self.acc_service.id},
        )
        for values in invalid_plan_values:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.env['eh.benefit.plan'].create({
                    'name': 'Invalid account classification', **values,
                })
    def test_tracking_on_audit_fields(self):
        """The audit trail relies on field-level tracking (assert the
        field attribute, not chatter message counts)."""
        Valuation = self.env['eh.benefit.valuation']
        for fname in ('state', 'discount_rate', 'opening_dbo',
                      'current_service_cost', 'actuarial_gain_loss_dbo',
                      'closing_dbo'):
            self.assertTrue(
                Valuation._fields[fname].tracking,
                msg='eh.benefit.valuation.%s must be tracked' % fname)
        Plan = self.env['eh.benefit.plan']
        for fname in ('state', 'funded', 'dbo_account_id',
                      'oci_account_id'):
            self.assertTrue(
                Plan._fields[fname].tracking,
                msg='eh.benefit.plan.%s must be tracked' % fname)
