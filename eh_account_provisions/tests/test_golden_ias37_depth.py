# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 37 depth examples: onerous contracts, the restructuring gate,
discount-rate governance, contingent-asset recognition and reimbursement
assets (IFRS 10/10 program, Phase 4).

Every expected amount is hand-derived from the inputs stated in the test,
with the derivation in a comment; nothing is read back from the engine to
build an expectation. Conventions inherited from models/provision.py:

* Onerous measure (IAS 37.66-69 incl. the 2020 amendment):
  net cost of fulfilling = max(cost of fulfilling - benefits expected, 0);
  provision = min(net cost, penalty of exiting) when a penalty exists,
  otherwise the net cost alone (no exit option). Rounded to company
  currency (2dp).
* The discounting and unwind engine is untouched: PV = estimate/(1+r)^n,
  compound whole-period unwinding off the day-count schedule, accretion
  capped at the undiscounted estimate.
* Reimbursements post a separate asset leg with the P&L credit on the
  provision expense account (permitted net presentation, IAS 37.54); the
  provision liability is never touched.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun directly
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_provisions', 'post_install', '-at_install')
class TestGoldenIas37Depth(EhGoldenTestCase):
    """IAS 37 depth mechanics as hand-computed worked examples."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')
        cls.finance_cost = cls._ensure_account(
            cls.env, '5700', 'Finance Cost', 'expense')
        cls.reimb_asset = cls._ensure_account(
            cls.env, '1450', 'Reimbursement Receivable', 'asset_current')
        cls.contingent_asset_acc = cls._ensure_account(
            cls.env, '1460', 'Settlement Receivable', 'asset_current')
        cls.other_income = cls._ensure_account(
            cls.env, '4900', 'Other Income', 'income_other')

    def _provision(self, **vals):
        base = {
            'name': '/', 'classification': 'provision',
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': self.finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'asset_account_id': self.contingent_asset_acc.id,
            'income_account_id': self.other_income.id,
            'reimbursement_account_id': self.reimb_asset.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        base.update(vals)
        return self.env['eh.provision'].create(base)

    # ------------------------------------------------------------------
    # onerous contracts (IAS 37.66-69)
    # ------------------------------------------------------------------

    def test_golden_onerous_exit_penalty_cheaper(self):
        """IAS 37.68: fulfil 120,000, benefits 30,000, penalty 70,000.

        net cost of fulfilling = 120,000 - 30,000 = 90,000
        provision = min(90,000, 70,000) = 70,000 (exiting is cheaper)

        best_estimate derives at 70,000 and recognition (no discounting)
        posts Dr expense 70,000 / Cr provision 70,000.
        """
        p = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=70000.0)
        self.assertAlmostEqual(p.best_estimate, 70000.00, places=2)
        self.assertAlmostEqual(p.present_value, 70000.00, places=2)
        p.action_recognise()
        self.assertEqual(p.state, 'recognised')
        self.assertEqual(len(p.move_ids), 1)
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 70000.00, 0.0),
            (self.provision_liab, 0.0, 70000.00),
        ])
        self.assertBalanced(p.move_ids)
        self.assertAlmostEqual(p.carrying_amount, 70000.00, places=2)

    def test_golden_onerous_net_fulfil_cheaper(self):
        """IAS 37.68: fulfil 120,000, benefits 60,000, penalty 70,000.

        net cost of fulfilling = 120,000 - 60,000 = 60,000 < penalty 70,000
        provision = 60,000 (fulfilling is cheaper).
        """
        p = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=60000.0,
            penalty_exit=70000.0)
        self.assertAlmostEqual(p.best_estimate, 60000.00, places=2)
        p.action_recognise()
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 60000.00, 0.0),
            (self.provision_liab, 0.0, 60000.00),
        ])
        self.assertAlmostEqual(p.carrying_amount, 60000.00, places=2)

    def test_golden_onerous_no_exit_option_unbounded(self):
        """IAS 37.68: no exit penalty (0 = no exit option) leaves the net
        cost of fulfilling uncapped: 120,000 - 30,000 = 90,000."""
        p = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=0.0)
        self.assertAlmostEqual(p.best_estimate, 90000.00, places=2)

    def test_golden_onerous_discounting_flow_unchanged(self):
        """The derived onerous measure feeds the untouched discounting and
        unwind engine.

        Measure = min(120,000 - 30,000, 70,000) = 70,000, payable
        2029-01-01, rate 6%, 3 annual periods, recognised 2026-01-01.

        PV = 70,000 / 1.06^3 = 70,000 / 1.191016 = 58,773.35

        Unwind at 2027-01-01 (span 2026-01-01 -> 2029-01-01 = 1,096 days,
        boundary 1 = +round(1096/3) = +365 days = 2027-01-01, so exactly
        one whole period is due; single compound step = simple product):

        finance cost = 58,773.35 x 0.06 = 3,526.401 -> 3,526.40
        carrying after = 58,773.35 + 3,526.40 = 62,299.75
        """
        p = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=70000.0,
            discount_rate=6.0, periods_to_settlement=3,
            expected_settlement_date='2029-01-01')
        self.assertAlmostEqual(p.present_value, 58773.35, places=2)
        with freeze_time('2026-01-01'):
            p.action_accounting_date = '2026-01-01'
            p.action_recognise()
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 58773.35, 0.0),
            (self.provision_liab, 0.0, 58773.35),
        ])
        before = p.move_ids
        with freeze_time('2027-01-01'):
            p.action_accounting_date = '2027-01-01'
            p.action_unwind()
        unwind = p.move_ids - before
        self.assertMoveLines(unwind, [
            (self.finance_cost, 3526.40, 0.0),
            (self.provision_liab, 0.0, 3526.40),
        ])
        self.assertBalanced(unwind)
        self.assertAlmostEqual(p.carrying_amount, 62299.75, places=2)
        self.assertEqual(p.unwound_periods, 1)

    def test_golden_onerous_remeasure_rederives_from_inputs(self):
        """IAS 37.59/68: remeasuring an onerous provision re-derives the
        measure from the staged revised inputs, never from a free-keyed
        figure.

        Recognised at min(120,000 - 30,000, 70,000) = 70,000. Revised
        inputs: fulfil 100,000, benefits 60,000, penalty 70,000:

        revised = min(100,000 - 60,000, 70,000) = 40,000
        delta   = 40,000 - 70,000 = -30,000 (release)

        Remeasurement entry: Dr provision 30,000 / Cr expense 30,000.
        """
        p = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=70000.0)
        p.action_recognise()
        # Without staged inputs the remeasure is refused (the manual
        # remeasure_estimate path is reserved for non-onerous/override).
        p.remeasure_estimate = 40000.0
        with self.assertRaises(UserError):
            p.action_remeasure()
        p.write({
            'remeasure_cost_fulfil': 100000.0,
            'remeasure_benefit_expected': 60000.0,
            'remeasure_penalty_exit': 70000.0,
        })
        before = p.move_ids
        p.action_remeasure()
        remeasure = p.move_ids - before
        self.assertEqual(len(remeasure), 1)
        self.assertMoveLines(remeasure, [
            (self.provision_liab, 30000.00, 0.0),
            (self.account_expense, 0.0, 30000.00),
        ])
        self.assertAlmostEqual(p.carrying_amount, 40000.00, places=2)
        self.assertAlmostEqual(p.best_estimate, 40000.00, places=2)
        # The staged inputs became the measurement inputs and were cleared.
        self.assertAlmostEqual(p.unavoidable_cost_fulfil, 100000.00, places=2)
        self.assertAlmostEqual(p.contract_benefit_expected, 60000.00, places=2)
        self.assertAlmostEqual(p.remeasure_cost_fulfil, 0.0, places=2)
        # Ledger: liability 40,000 credit; expense 70,000 - 30,000 = 40,000.
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), -40000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.account_expense), 40000.00, places=2)

    def test_golden_onerous_guardrails(self):
        """The onerous measure is mechanical, not honour-system:
        a hand-keyed estimate diverging from the derived measure is refused;
        the override needs a documented reason; raw input edits freeze after
        recognition; recognition without inputs (and no override) refuses.
        """
        # Hand-keyed estimate diverging from min(90,000, 70,000) = 70,000.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._provision(
                provision_type='onerous', best_estimate=99999.0,
                unavoidable_cost_fulfil=120000.0,
                contract_benefit_expected=30000.0,
                penalty_exit=70000.0)
        # Override without a reason is refused.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._provision(
                provision_type='onerous', best_estimate=99999.0,
                unavoidable_cost_fulfil=120000.0,
                penalty_exit=70000.0,
                onerous_override=True)
        # Override with a reason stands at the keyed estimate.
        p = self._provision(
            provision_type='onerous', best_estimate=99999.0,
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=70000.0,
            onerous_override=True,
            onerous_override_reason='Contract-specific measurement memo')
        self.assertAlmostEqual(p.best_estimate, 99999.00, places=2)
        # Recognition without inputs and without override is refused.
        empty = self._provision(provision_type='onerous',
                                best_estimate=50000.0)
        with self.assertRaises(UserError):
            empty.action_recognise()
        # Inputs freeze once recognised (write guard, not just the UI).
        engine = self._provision(
            provision_type='onerous',
            unavoidable_cost_fulfil=120000.0,
            contract_benefit_expected=30000.0,
            penalty_exit=70000.0)
        engine.action_recognise()
        with self.assertRaises(UserError):
            engine.unavoidable_cost_fulfil = 200000.0
        with self.assertRaises(UserError):
            engine.penalty_exit = 10000.0
        with self.assertRaises(UserError):
            engine.onerous_override = True

    # ------------------------------------------------------------------
    # restructuring gate (IAS 37.72, 37.80-81)
    # ------------------------------------------------------------------

    def _restructuring(self, **vals):
        base = {
            'provision_type': 'restructuring',
            'best_estimate': 75000.0,
            'restructuring_line_ids': [
                (0, 0, {'name': 'Redundancy package', 'cost_kind':
                        'termination', 'amount': 40000.0}),
                (0, 0, {'name': 'Lease break fee', 'cost_kind':
                        'contract_termination', 'amount': 25000.0}),
                (0, 0, {'name': 'Site decommissioning', 'cost_kind':
                        'other_direct', 'amount': 10000.0}),
            ],
        }
        base.update(vals)
        return self._provision(**base)

    def test_golden_restructuring_gate_blocks_until_criteria(self):
        """IAS 37.72: components 40,000 + 25,000 + 10,000 = 75,000 are not
        recognisable until the detailed formal plan and the valid
        expectation (announcement date + narrative) are attested; then the
        entry posts at exactly 75,000."""
        p = self._restructuring()
        # No checklist at all.
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)
        # Plan without the raised expectation.
        p.restructuring_plan = True
        with self.assertRaises(UserError):
            p.action_recognise()
        # Both flags but no announcement evidence.
        p.restructuring_expectation = True
        with self.assertRaises(UserError):
            p.action_recognise()
        # Complete gate: announcement date + narrative.
        p.write({
            'restructuring_announcement_date': '2026-03-01',
            'restructuring_announcement':
                'Main features announced to affected staff on 1 March.',
        })
        p.action_recognise()
        self.assertEqual(p.state, 'recognised')
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 75000.00, 0.0),
            (self.provision_liab, 0.0, 75000.00),
        ])
        self.assertAlmostEqual(p.carrying_amount, 75000.00, places=2)
        # Component lines freeze with the posted measurement.
        with self.assertRaises(UserError):
            p.restructuring_line_ids[0].amount = 50000.0
        with self.assertRaises(UserError):
            p.restructuring_line_ids[0].unlink()
        with self.assertRaises(UserError):
            self.env['eh.provision.restructuring.line'].create({
                'provision_id': p.id, 'name': 'Late addition',
                'cost_kind': 'termination', 'amount': 1000.0})

    def test_golden_restructuring_excluded_cost_never_enters_sum(self):
        """IAS 37.81: retraining is registered but excluded. A best
        estimate of 80,000 that bakes the 5,000 retraining cost in fails
        the gate (components sum to 75,000); at 75,000 it recognises.
        """
        p = self._restructuring(
            best_estimate=80000.0,
            restructuring_plan=True,
            restructuring_expectation=True,
            restructuring_announcement_date='2026-03-01',
            restructuring_announcement='Announced to affected staff.',
            restructuring_line_ids=[
                (0, 0, {'name': 'Redundancy package', 'cost_kind':
                        'termination', 'amount': 40000.0}),
                (0, 0, {'name': 'Lease break fee', 'cost_kind':
                        'contract_termination', 'amount': 25000.0}),
                (0, 0, {'name': 'Site decommissioning', 'cost_kind':
                        'other_direct', 'amount': 10000.0}),
                (0, 0, {'name': 'Retraining retained staff', 'cost_kind':
                        'retraining', 'amount': 5000.0}),
            ])
        # Only the direct components count: 40,000 + 25,000 + 10,000.
        self.assertAlmostEqual(
            p.restructuring_component_total, 75000.00, places=2)
        self.assertAlmostEqual(
            p.restructuring_excluded_total, 5000.00, places=2)
        self.assertFalse(
            p.restructuring_line_ids.filtered(
                lambda l: l.cost_kind == 'retraining').in_scope)
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)
        p.best_estimate = 75000.0
        p.action_recognise()
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 75000.00, 0.0),
            (self.provision_liab, 0.0, 75000.00),
        ])

    def test_golden_restructuring_requires_component_lines(self):
        """IAS 37.80: a restructuring provision without a component
        breakdown (or with only excluded lines) cannot be recognised."""
        p = self._provision(
            provision_type='restructuring', best_estimate=75000.0,
            restructuring_plan=True, restructuring_expectation=True,
            restructuring_announcement_date='2026-03-01',
            restructuring_announcement='Announced.')
        with self.assertRaises(UserError):
            p.action_recognise()
        p.restructuring_line_ids = [
            (0, 0, {'name': 'Marketing relaunch', 'cost_kind': 'marketing',
                    'amount': 75000.0})]
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)

    # ------------------------------------------------------------------
    # discount-rate governance (IAS 37.47)
    # ------------------------------------------------------------------

    def test_golden_rate_governance_attested_and_frozen(self):
        """The rate carries a basis attestation and a source note, both
        chatter-tracked; rate and basis freeze on posting and move only
        through the sanctioned flows."""
        fields_ = self.env['eh.provision']._fields
        # Chatter tracking is not observable in a test transaction; assert
        # the field definitions carry it.
        self.assertTrue(fields_['discount_rate'].tracking)
        self.assertTrue(fields_['rate_basis'].tracking)
        self.assertTrue(fields_['rate_source'].tracking)
        self.assertTrue(fields_['virtually_certain'].tracking)
        # Default preserves prior behaviour and IAS 37.47's prescription.
        blank = self._provision(best_estimate=1000.0)
        self.assertEqual(blank.rate_basis, 'entity_specific_pretax')
        p = self._provision(
            best_estimate=100000.0, discount_rate=6.0,
            periods_to_settlement=2,
            rate_basis='risk_free_govt',
            rate_source='2yr government bond curve, 2026-01 memo')
        # PV = 100,000 / 1.06^2 = 100,000 / 1.1236 = 88,999.64
        self.assertAlmostEqual(p.present_value, 88999.64, places=2)
        p.action_recognise()
        # Rate and its attestation are frozen after posting.
        with self.assertRaises(UserError):
            p.discount_rate = 8.0
        with self.assertRaises(UserError):
            p.rate_basis = 'other'
        self.assertEqual(p.rate_basis, 'risk_free_govt')

    # ------------------------------------------------------------------
    # contingent asset recognition (IAS 37.33-35)
    # ------------------------------------------------------------------

    def test_golden_contingent_asset_virtually_certain_recognised(self):
        """IAS 37.33: a 500,000 inflow that becomes virtually certain is
        recognised as an ASSET: Dr settlement receivable 500,000 /
        Cr other income 500,000. No provision account is touched and the
        record freezes in the Asset Recognised state."""
        p = self._provision(
            classification='contingent_asset', best_estimate=500000.0,
            virtually_certain=True)
        p.action_recognise_asset()
        self.assertEqual(p.state, 'recognised_asset')
        self.assertEqual(len(p.move_ids), 1)
        self.assertMoveLines(p.move_ids, [
            (self.contingent_asset_acc, 500000.00, 0.0),
            (self.other_income, 0.0, 500000.00),
        ])
        self.assertBalanced(p.move_ids)
        self.assertAlmostEqual(p.carrying_amount, 500000.00, places=2)
        # An asset was recognised, never a provision credit.
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_asset_acc),
            500000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.other_income), -500000.00, places=2)
        # Frozen: measurement edits and deletion are refused.
        with self.assertRaises(UserError):
            p.best_estimate = 600000.0
        with self.assertRaises(UserError):
            p.virtually_certain = False
        with self.assertRaises(UserError):
            p.unlink()
        # The provision-recognition path still refuses contingent items.
        draft = self._provision(
            classification='contingent_asset', best_estimate=100.0,
            virtually_certain=True)
        with self.assertRaises(UserError):
            draft.action_recognise()

    def test_golden_contingent_asset_disclose_only_without_flag(self):
        """IAS 37.31: without the virtually-certain attestation the
        contingent asset stays disclosure-only; both posting paths refuse
        and a raw state write is caught by the constraint."""
        p = self._provision(
            classification='contingent_asset', best_estimate=500000.0)
        with self.assertRaises(UserError):
            p.action_recognise_asset()
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)
        self.assertEqual(p.state, 'draft')
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            p.write({'state': 'recognised_asset'})
        self.env.invalidate_all()
        self.assertEqual(p.state, 'draft')
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_asset_acc), 0.0, places=2)

    def test_golden_contingent_asset_reversal(self):
        """IAS 37.35: if the inflow stops being virtually certain the
        recognised asset is derecognised against the income that carried
        it: Dr other income 500,000 / Cr receivable 500,000."""
        p = self._provision(
            classification='contingent_asset', best_estimate=500000.0,
            virtually_certain=True)
        p.action_recognise_asset()
        before = p.move_ids
        p.action_reverse()
        reversal = p.move_ids - before
        self.assertMoveLines(reversal, [
            (self.other_income, 500000.00, 0.0),
            (self.contingent_asset_acc, 0.0, 500000.00),
        ])
        self.assertEqual(p.state, 'reversed')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_asset_acc), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.other_income), 0.0, places=2)

    # ------------------------------------------------------------------
    # reimbursement assets (IAS 37.53)
    # ------------------------------------------------------------------

    def test_golden_reimbursement_separate_asset_capped(self):
        """IAS 37.53: provision 100,000; a 40,000 reimbursement posts a
        SEPARATE asset leg (Dr reimbursement receivable 40,000 / Cr
        provision expense 40,000: net P&L presentation, gross balance
        sheet). The provision carrying amount never moves and the cap at
        the carrying amount is enforced.

        Ledger after: liability 100,000 Cr (untouched), asset 40,000 Dr,
        expense net = 100,000 - 40,000 = 60,000 Dr.
        """
        p = self._provision(
            best_estimate=100000.0,
            reimbursement_partner_id=self.partner_a.id)
        p.action_recognise()
        p.reimbursement_amount = 40000.0
        before = p.move_ids
        p.action_recognise_reimbursement()
        reimb = p.move_ids - before
        self.assertEqual(len(reimb), 1)
        self.assertMoveLines(reimb, [
            (self.reimb_asset, 40000.00, 0.0),
            (self.account_expense, 0.0, 40000.00),
        ])
        self.assertBalanced(reimb)
        # Never netted: the provision liability and carrying stand whole.
        self.assertAlmostEqual(p.carrying_amount, 100000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), -100000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.reimb_asset), 40000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.account_expense), 60000.00, places=2)
        self.assertAlmostEqual(p.reimbursement_recognised, 40000.00, places=2)
        self.assertAlmostEqual(p.reimbursement_amount, 0.0, places=2)
        # Cap: 40,000 recognised + 70,000 staged = 110,000 > 100,000.
        p.reimbursement_amount = 70000.0
        with self.assertRaises(UserError):
            p.action_recognise_reimbursement()
        self.assertAlmostEqual(p.reimbursement_recognised, 40000.00, places=2)
        # Boundary: topping up to exactly the carrying amount is allowed.
        p.reimbursement_amount = 60000.0
        p.action_recognise_reimbursement()
        self.assertAlmostEqual(
            p.reimbursement_recognised, 100000.00, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.reimb_asset), 100000.00, places=2)
        # And nothing more fits under the cap.
        p.reimbursement_amount = 0.01
        with self.assertRaises(UserError):
            p.action_recognise_reimbursement()

    def test_golden_reimbursement_only_on_recognised_provision(self):
        """A reimbursement asset needs a recognised provision to cap
        against; draft provisions and contingent items refuse."""
        draft = self._provision(best_estimate=1000.0,
                                reimbursement_amount=100.0)
        with self.assertRaises(UserError):
            draft.action_recognise_reimbursement()
        self.assertFalse(draft.move_ids)
