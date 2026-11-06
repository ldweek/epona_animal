# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 15 worked examples for eh_account_revenue (Phase 4 depth).

Each test is a hand-computed worked example in the shape of the IFRS 15
illustrative material (numbers only, recomputed by hand from the inputs
stated in the test). The exact journal entry the engine posts is asserted
line by line against literal amounts; nothing is read back from the engine
under test to build an expected value.

Covered mechanics:

* Input/output measurement of progress (IFRS 15.39-45, B14-B19): the
  cost-to-cost and units-delivered drivers compute the percentage complete
  (manual entry refused), milestones/time keep a manual percentage backed
  by the mandatory basis note.
* Variable-consideration constraint reassessment (IFRS 15.56): a period-end
  review revises the estimate/constraint, reallocates through the existing
  variable-consideration flow, posts the exact catch-up and freezes itself
  as the audit trail.
* Financing re-measurement on a catch-up modification (IFRS 15.60-65): the
  time-based accretion re-runs off the revised transaction price and the
  delta posts through the same interest accrual accounts.
* Closure validation: closing blocks while obligations are open; the
  manager override releases the remaining contract liability to P&L
  (IFRS 15.B46 breakage) or to a refund liability (IFRS 15.55).

Financing convention (read from models/revenue_contract.py): for an advance
contract the revenue base is the prepaid cash accreted forward,
pv = price x (1 + rate) ^ (months / 12); with a payment date the discount
unwinds on the effective-interest day-count basis
accreted(t) = total_interest x ((1 + rate) ^ (t x years) - 1)
            / ((1 + rate) ^ years - 1)
with t the elapsed-day fraction of the contract-date -> payment-date span.
"""

from datetime import date, timedelta

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun directly
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_revenue', 'post_install', '-at_install')
class TestGoldenIfrs15(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')
        cls.refund_liab_acc = cls._ensure_account(
            cls.env, '2360', 'Refund Liability', 'liability_current')
        cls.fin_expense_acc = cls._ensure_account(
            cls.env, '5210', 'Financing Interest Expense', 'expense')

    def _contract(self, price=1000.0, obligations=None, **extra):
        obligation_vals = []
        for source in obligations or []:
            vals = dict(source)
            if (
                vals.get('satisfaction') == 'over_time'
                and 'method_basis' not in vals
            ):
                vals['method_basis'] = (
                    'Test evidence: stated progress faithfully depicts the '
                    'service transferred to date.'
                )
            obligation_vals.append(vals)
        vals = {
            'partner_id': self.partner_a.id,
            'transaction_price': price,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, o) for o in obligation_vals],
        }
        vals.update(extra)
        return self.env['eh.revenue.contract'].create(vals)

    # ------------------------------------------------------------------
    # (1) Measurement of progress (IFRS 15.39-45, B14-B19)
    # ------------------------------------------------------------------

    def test_golden_input_cost_drives_percent_and_revenue(self):
        """IFRS 15.B18-B19 cost-to-cost input method.

        Contract price 2,000, one over-time obligation (SSP 2,000, so the
        full price allocates to it), total expected cost 1,000. Costs
        incurred 400 imply 400 / 1,000 = 40.00% complete, so cumulative
        revenue is 40% x 2,000 = 800.00. Nothing billed, so:
        Dr contract asset 800 / Cr revenue 800.

        Costs then reach 650: 65.00% complete, cumulative 1,300, increment
        1,300 - 800 = 500.00: Dr contract asset 500 / Cr revenue 500.
        """
        c = self._contract(price=2000.0, obligations=[{
            'name': 'Build', 'standalone_price': 2000.0,
            'satisfaction': 'over_time',
            'progress_method': 'input_cost',
            'method_basis': 'Costs incurred track the physical build and '
                            'depict the transfer of control (IFRS 15.B18).',
            'cost_total_estimate': 1000.0,
            'cost_incurred': 400.0,
        }])
        ob = c.obligation_ids
        self.assertAlmostEqual(ob.percent_complete, 40.0, places=2)
        c.action_activate()
        c.action_recognise()
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_asset_acc, 800.0, 0.0),
            (self.account_revenue, 0.0, 800.0),
        ])
        self.assertBalanced(move)
        # Manual percentage entry is refused for the cost-driven method:
        # the recorded costs are the evidence of progress.
        with self.assertRaises(UserError):
            ob.percent_complete = 90.0
        ob.cost_incurred = 650.0
        self.assertAlmostEqual(ob.percent_complete, 65.0, places=2)
        c.action_recognise()
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_asset_acc, 500.0, 0.0),
            (self.account_revenue, 0.0, 500.0),
        ])
        self.assertAlmostEqual(c.amount_recognised, 1300.0, places=2)
        # A cost overrun caps the measure at 100%: recognised revenue can
        # never exceed the allocated price.
        ob.cost_incurred = 1500.0
        self.assertAlmostEqual(ob.percent_complete, 100.0, places=2)
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 2000.0, places=2)

    def test_golden_output_units_drives_percent_and_revenue(self):
        """IFRS 15.B15 output method, units delivered.

        Contract price 1,200, one over-time obligation, 60 units promised.
        15 delivered imply 15 / 60 = 25.00% complete, cumulative revenue
        25% x 1,200 = 300.00: Dr contract asset 300 / Cr revenue 300.
        All 60 delivered: 100%, increment 1,200 - 300 = 900.00.
        """
        c = self._contract(price=1200.0, obligations=[{
            'name': 'Deliveries', 'standalone_price': 1200.0,
            'satisfaction': 'over_time',
            'progress_method': 'output_units',
            'method_basis': 'Each delivered unit transfers control of a '
                            'distinct increment (IFRS 15.B15).',
            'units_total': 60.0,
            'units_delivered': 15.0,
        }])
        ob = c.obligation_ids
        self.assertAlmostEqual(ob.percent_complete, 25.0, places=2)
        c.action_activate()
        c.action_recognise()
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_asset_acc, 300.0, 0.0),
            (self.account_revenue, 0.0, 300.0),
        ])
        with self.assertRaises(UserError):
            ob.percent_complete = 50.0
        ob.units_delivered = 60.0
        self.assertAlmostEqual(ob.percent_complete, 100.0, places=2)
        c.action_recognise()
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_asset_acc, 900.0, 0.0),
            (self.account_revenue, 0.0, 900.0),
        ])
        self.assertAlmostEqual(c.amount_recognised, 1200.0, places=2)

    def test_over_time_requires_method_basis(self):
        # IFRS 15.B14: an over-time obligation must document why the method
        # depicts the transfer. An empty basis note is refused.
        with self.assertRaises(ValidationError):
            self._contract(price=1000.0, obligations=[{
                'name': 'Build', 'standalone_price': 1000.0,
                'satisfaction': 'over_time',
                'method_basis': '   ',
            }])

    def test_new_records_do_not_receive_migrated_basis(self):
        defaults = self.env['eh.revenue.obligation'].default_get([
            'progress_method', 'method_basis',
        ])
        self.assertEqual(defaults['progress_method'], 'output_milestones')
        self.assertFalse(defaults.get('method_basis'))

    # ------------------------------------------------------------------
    # (2) Constraint reassessment (IFRS 15.56)
    # ------------------------------------------------------------------

    def test_golden_constraint_review_lifts_included_amount(self):
        """IFRS 15.56 period-end reassessment of the constraint.

        Price 20,000, one point-in-time obligation (SSP 20,000) with a
        variable bonus estimated at 10,000 but constrained to 6,000:
        allocated = 20,000 + min(10,000, 6,000) = 26,000, recognised in
        full on satisfaction.

        The period-end review lifts the constraint to 8,000: included
        amount min(10,000, 8,000) = 8,000, allocated = 28,000, so the
        applied review trues up 28,000 - 26,000 = 2,000.00:
        Dr contract asset 2,000 / Cr revenue 2,000.
        """
        c = self._contract(price=20000.0, obligations=[{
            'name': 'Service', 'standalone_price': 20000.0,
            'variable_consideration': True,
            'variable_method': 'expected_value',
            'variable_estimate': 10000.0,
            'variable_constraint': 6000.0,
        }])
        ob = c.obligation_ids
        self.assertAlmostEqual(ob.allocated_price, 26000.0, places=2)
        c.action_activate()
        ob.satisfied = True
        c.action_recognise()
        self.assertAlmostEqual(c.amount_recognised, 26000.0, places=2)
        # The direct write path stays frozen after posting; the review is
        # the sanctioned, audit-trailed path.
        with self.assertRaises(UserError):
            ob.variable_constraint = 8000.0
        c.action_open_period_reviews()
        review = c.review_ids
        self.assertEqual(len(review), 1)
        self.assertEqual(review.state, 'draft')
        # The draft snapshots the current position and seeds the revised
        # values from it.
        self.assertAlmostEqual(review.previous_estimate, 10000.0, places=2)
        self.assertAlmostEqual(review.previous_constraint, 6000.0, places=2)
        self.assertAlmostEqual(review.new_estimate, 10000.0, places=2)
        self.assertAlmostEqual(review.new_constraint, 6000.0, places=2)
        # Re-running the helper does not duplicate the pending draft.
        c.action_open_period_reviews()
        self.assertEqual(len(c.review_ids), 1)
        review.new_constraint = 8000.0
        # Applying without a rationale is refused (the rationale is the
        # audit-trail substance the standard asks for).
        with self.assertRaises(UserError):
            review.action_apply()
        review.rationale = ('Acceptance history now makes 8,000 highly '
                            'probable not to reverse (IFRS 15.56).')
        review.review_date = c.contract_date + timedelta(days=1)
        move_count = len(c.move_ids)
        review.action_apply()
        self.assertEqual(review.state, 'applied')
        self.assertAlmostEqual(ob.variable_constraint, 8000.0, places=2)
        self.assertAlmostEqual(ob.allocated_price, 28000.0, places=2)
        self.assertAlmostEqual(c.amount_recognised, 28000.0, places=2)
        self.assertEqual(len(c.move_ids), move_count + 1)
        move = c.move_ids.sorted('id')[-1]
        self.assertEqual(move.date, review.review_date)
        self.assertMoveLines(move, [
            (self.contract_asset_acc, 2000.0, 0.0),
            (self.account_revenue, 0.0, 2000.0),
        ])
        # The applied review is frozen: edits and deletion are refused.
        with self.assertRaises(UserError):
            review.rationale = 'rewritten after the fact'
        with self.assertRaises(UserError):
            review.unlink()

    def test_period_review_requires_variable_consideration(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Plain', 'standalone_price': 1000.0}])
        c.action_activate()
        with self.assertRaises(UserError):
            c.action_open_period_reviews()

    # ------------------------------------------------------------------
    # (3) Financing re-measurement on a catch-up modification
    # ------------------------------------------------------------------

    def test_golden_financing_rerun_on_catch_up_modification(self):
        """IFRS 15.60-65: the financing computation re-runs off the revised
        transaction price when a catch-up modification changes it.

        Advance contract: the customer prepays 10,000 on 2025-01-01 for
        goods transferring 2027-01-01 (24 months, 10% effective annual).
        Module convention: pv = 10,000 x 1.1^2 = 12,100, total interest
        expense 12,100 - 10,000 = 2,100, unwinding by
        accreted(t) = 2,100 x (1.1^(2t) - 1) / (1.1^2 - 1) over the
        730-day span 2025-01-01 -> 2027-01-01.

        At 2026-01-01 the elapsed fraction is 365 / 730 = 0.5, so
        accreted = 2,100 x (1.1 - 1) / 0.21 = 2,100 x 0.1 / 0.21
                 = 1,000.00.
        Accrual entry: Dr interest expense 1,000 / Cr contract liability
        1,000.

        A catch-up modification lifts the price by 2,000 to 12,000:
        new pv = 12,000 x 1.21 = 14,520, new total interest 2,520, and the
        accreted target at the same date becomes
        2,520 x 0.1 / 0.21 = 1,200.00. The re-run posts the 200.00 delta
        through the same accounts:
        Dr interest expense 200 / Cr contract liability 200.
        """
        c = self._contract(
            price=10000.0,
            contract_date=date(2025, 1, 1),
            financing_component=True,
            financing_direction='advance',
            financing_rate=0.10,
            financing_period_months=24,
            financing_payment_date=date(2027, 1, 1),
            financing_account_id=self.fin_expense_acc.id,
            obligations=[{
                'name': 'Goods', 'standalone_price': 10000.0,
                'satisfaction': 'point_in_time'}])
        self.assertAlmostEqual(c.financing_pv, 12100.0, places=2)
        self.assertAlmostEqual(c.financing_component_amount, 2100.0,
                               places=2)
        c.action_activate()
        with freeze_time('2026-01-01'):
            # Ledger dates are action inputs, not ambient server dates.
            effective_date = date(2026, 1, 1)
            c.action_accrue_financing(accounting_date=effective_date)
            move = c.move_ids.sorted('id')[-1]
            self.assertMoveLines(move, [
                (self.fin_expense_acc, 1000.0, 0.0),
                (self.contract_liab_acc, 0.0, 1000.0),
            ])
            self.assertAlmostEqual(
                c.financing_interest_recognised, 1000.0, places=2)
            move_count = len(c.move_ids)
            c._apply_modification(
                method='catch_up', new_transaction_price=12000.0,
                description='Scope growth, remaining goods not distinct',
                effective_date=effective_date)
            self.assertAlmostEqual(c.financing_pv, 14520.0, places=2)
            self.assertEqual(len(c.move_ids), move_count + 1)
            move = c.move_ids.sorted('id')[-1]
            self.assertMoveLines(move, [
                (self.fin_expense_acc, 200.0, 0.0),
                (self.contract_liab_acc, 0.0, 200.0),
            ])
            self.assertAlmostEqual(
                c.financing_interest_recognised, 1200.0, places=2)
            self.assertEqual(c.modification_count, 1)
            for m in c.move_ids:
                self.assertBalanced(m)

    # ------------------------------------------------------------------
    # (4) Closure validation
    # ------------------------------------------------------------------

    def test_golden_closure_blocks_open_obligations(self):
        # An over-time obligation below 100% blocks closing.
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 60.0}])
        c.action_activate()
        with self.assertRaises(UserError):
            c.action_close()
        # An unsatisfied point-in-time obligation blocks closing.
        c2 = self._contract(price=500.0, obligations=[
            {'name': 'Licence', 'standalone_price': 500.0}])
        c2.action_activate()
        with self.assertRaises(UserError):
            c2.action_close()
        # Completion alone is not settlement: recognised-but-unbilled revenue
        # is a live contract asset and must remain actionable. Record billing,
        # bringing the contract position to zero, before ordinary close.
        c2.obligation_ids.satisfied = True
        c2.action_recognise()
        with self.assertRaises(UserError):
            c2.action_close()
        c2.bill_amount = 500.0
        c2.action_bill()
        c2.action_close()
        self.assertEqual(c2.state, 'done')

    def test_golden_closure_override_releases_liability_to_income(self):
        """Override close, P&L path (IFRS 15.B46 breakage pattern).

        Billed 500 ahead with no performance: contract liability 500.
        The manager closes with remainder, releasing it to profit or loss:
        Dr contract liability 500 / Cr revenue 500. The contract's net
        position ends at zero on both the ledger and the stored totals.
        """
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 0.0}])
        c.action_activate()
        c.bill_amount = 500.0
        c.action_bill()
        self.assertAlmostEqual(c.contract_liability, 500.0, places=2)
        c.close_with_remainder = True
        # The override reason is mandatory.
        with self.assertRaises(UserError):
            c.action_close()
        c.close_reason = ('Customer abandoned the project; consideration '
                          'is non-refundable under the contract terms.')
        c.action_close()
        self.assertEqual(c.state, 'done')
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_liab_acc, 500.0, 0.0),
            (self.account_revenue, 0.0, 500.0),
        ])
        self.assertAlmostEqual(c.close_released_amount, 500.0, places=2)
        self.assertAlmostEqual(c.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), 0.0, places=2)

    def test_golden_closure_override_reclassifies_to_refund(self):
        """Override close, refund path (IFRS 15.55).

        Billed 300 ahead; the unperformed balance is owed back, so the
        close reclassifies the contract liability as a refund liability:
        Dr contract liability 300 / Cr refund liability 300.
        """
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 0.0}])
        c.action_activate()
        c.bill_amount = 300.0
        c.action_bill()
        c.write({
            'close_with_remainder': True,
            'close_reason': 'Undelivered balance refundable under the '
                            'termination clause.',
            'close_release_to': 'refund',
            'refund_liability_account_id': self.refund_liab_acc.id,
        })
        c.action_close()
        self.assertEqual(c.state, 'done')
        move = c.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_liab_acc, 300.0, 0.0),
            (self.refund_liab_acc, 0.0, 300.0),
        ])
        self.assertAlmostEqual(c.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.refund_liab_acc), -300.0, places=2)

    def test_closure_override_requires_manager(self):
        c = self._contract(price=1000.0, obligations=[
            {'name': 'Build', 'standalone_price': 1000.0,
             'satisfaction': 'over_time', 'percent_complete': 0.0}])
        c.action_activate()
        c.write({'close_with_remainder': True,
                 'close_reason': 'attempted by a non-manager'})
        user = self.env['res.users'].create({
            'name': 'plain', 'login': 'rev_close_plain@test',
            'email': 'rev_close_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user).action_close()
