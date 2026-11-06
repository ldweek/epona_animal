# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 37 worked examples for eh_account_provisions.

Each test is a hand-computed worked example in the shape of the IAS 37
illustrative material (numbers only, recomputed by hand from the inputs
stated in the test). The exact journal entry the engine posts is asserted
line by line against literal amounts; nothing is read back from the engine
under test to build an expected value.

Unwind convention implemented by eh.provision.action_unwind (read from
models/provision.py):

* COMPOUND accretion. For each whole period that has fallen due, the step
  is interest_k = carrying x rate and the carrying amount is raised by that
  step before the next one. Each due boundary posts its own currency-rounded
  journal entry on that boundary date, preserving catch-up evidence. For a
  single period, simple and compound coincide.
* Calendar schedule. The accounting date and expected settlement date derive
  annual anniversary periods plus one final partial-year stub. Only boundaries
  reached by the explicit unwind effective date accrete; there is no
  intra-period day-level proration, and a repeat run inside the same period is
  refused. Legacy/API annual counts without a date are materialised as an
  explicit settlement date at recognition.
* Accretion is capped so the carrying amount never exceeds the
  undiscounted best estimate.
"""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun directly
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_provisions', 'post_install', '-at_install')
class TestGoldenIas37(EhGoldenTestCase):
    """IAS 37 worked examples: discounted recognition, discount unwinding,
    remeasurement of the estimate, and the contingent-liability guard."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')
        cls.finance_cost = cls._ensure_account(
            cls.env, '5700', 'Finance Cost', 'expense')

    def _provision(self, **vals):
        base = {
            'name': '/', 'classification': 'provision',
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': self.finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        base.update(vals)
        return self.env['eh.provision'].create(base)

    def _recognised_and_unwound_once(self):
        """Shared path for examples 2 and 3.

        Best estimate 1,000,000 payable 2029-01-01, rate 6%, 3 annual
        periods, recognised 2026-01-01 and unwound once at 2027-01-01.

        Schedule derivation: span 2026-01-01 -> 2029-01-01 = 1,096 days
        (2028 is a leap year). Boundary 1 = recognition +
        round(1096 x 1/3) = +365 days = 2027-01-01, so at 2027-01-01
        exactly one whole period has fallen due.
        """
        p = self._provision(
            best_estimate=1000000.0, discount_rate=6.0,
            periods_to_settlement=3,
            expected_settlement_date='2029-01-01')
        with freeze_time('2026-01-01'):
            p.action_accounting_date = '2026-01-01'
            p.action_recognise()
        with freeze_time('2027-01-01'):
            p.action_accounting_date = '2027-01-01'
            p.action_unwind()
        return p

    def test_golden_discounted_initial_recognition(self):
        """IAS 37.36/45: a best estimate of 1,000,000 payable in 3 years,
        discounted at a pre-tax rate of 6%, is recognised at present value.

        PV = 1,000,000 / 1.06^3 = 1,000,000 / 1.191016 = 839,619.28.

        Recognition entry: Dr expense 839,619.28 / Cr provision liability
        839,619.28.
        """
        p = self._provision(best_estimate=1000000.0, discount_rate=6.0,
                            periods_to_settlement=3)
        self.assertAlmostEqual(p.present_value, 839619.28, places=2)
        p.action_recognise()
        self.assertEqual(p.state, 'recognised')
        self.assertEqual(len(p.move_ids), 1)
        self.assertMoveLines(p.move_ids, [
            (self.account_expense, 839619.28, 0.0),
            (self.provision_liab, 0.0, 839619.28),
        ])
        self.assertBalanced(p.move_ids)
        self.assertAlmostEqual(p.carrying_amount, 839619.28, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), -839619.28, places=2)

    def test_golden_unwind_one_period(self):
        """IAS 37.60: after one year the discount unwinds as a finance cost.

        Carrying at recognition = 839,619.28 (the posted, currency-rounded
        present value). One whole period due at 6%; for a single period the
        module's compound step equals the simple product:

        finance cost = 839,619.28 x 0.06 = 50,377.1568 -> 50,377.16
        carrying after = 839,619.28 + 50,377.16 = 889,996.44

        Unwind entry: Dr finance cost 50,377.16 / Cr provision 50,377.16.
        """
        p = self._recognised_and_unwound_once()
        recognition, unwind = None, None
        for move in p.move_ids:
            if move.date == date(2026, 1, 1):
                recognition = move
            elif move.date == date(2027, 1, 1):
                unwind = move
        self.assertTrue(recognition and unwind)
        self.assertMoveLines(unwind, [
            (self.finance_cost, 50377.16, 0.0),
            (self.provision_liab, 0.0, 50377.16),
        ])
        self.assertBalanced(unwind)
        self.assertAlmostEqual(p.carrying_amount, 889996.44, places=2)
        self.assertEqual(p.unwound_periods, 1)
        # The anchor advances to the period boundary just recognised.
        self.assertEqual(p.last_unwind_date, date(2027, 1, 1))
        self.assertAlmostEqual(
            self.posted_balance(self.finance_cost), 50377.16, places=2)
        # Liability = 839,619.28 + 50,377.16 = 889,996.44 credit.
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), -889996.44, places=2)

    def test_golden_remeasure_decrease_releases_to_pnl(self):
        """IAS 37.59: the provision is reviewed and adjusted to the current
        best estimate; a decrease is written back to profit or loss against
        the original expense account.

        Starting point (example 2): carrying 889,996.44, 1 of 3 periods
        unwound, 2 remaining. Revised undiscounted estimate 786,520.00:

        revised PV = 786,520.00 / 1.06^2 = 786,520.00 / 1.1236 = 700,000.00
        release    = 889,996.44 - 700,000.00 = 189,996.44

        Remeasurement entry (decrease): Dr provision 189,996.44 /
        Cr expense 189,996.44. Carrying after = 700,000.00.
        """
        p = self._recognised_and_unwound_once()
        before = p.move_ids
        with freeze_time('2027-01-01'):
            p.remeasure_estimate = 786520.00
            p.write({
                'remeasure_discount_rate': 6.0,
                'remeasure_rate_basis': 'risk_free_govt',
                'remeasure_rate_source': 'Current reporting-date curve',
            })
            p.action_accounting_date = '2027-01-01'
            p.action_remeasure()
        remeasure = p.move_ids - before
        self.assertEqual(len(remeasure), 1)
        self.assertMoveLines(remeasure, [
            (self.provision_liab, 189996.44, 0.0),
            (self.account_expense, 0.0, 189996.44),
        ])
        self.assertBalanced(remeasure)
        self.assertAlmostEqual(p.carrying_amount, 700000.00, places=2)
        # The stored best estimate tracks the revision so later unwinding
        # accretes toward the revised undiscounted figure.
        self.assertAlmostEqual(p.best_estimate, 786520.00, places=2)
        # Liability balance equals the revised carrying amount.
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), -700000.00, places=2)
        # Expense net of the writeback: 839,619.28 - 189,996.44 = 649,622.84.
        self.assertAlmostEqual(
            self.posted_balance(self.account_expense), 649622.84, places=2)

    def test_golden_contingent_liability_disclosure_only(self):
        """IAS 37.27-28: a contingent liability is disclosed, never
        recognised. It is still measured for the disclosure note
        (PV = 1,000,000 / 1.06^3 = 839,619.28) but posting is refused and
        no journal entry may exist.
        """
        p = self._provision(
            classification='contingent_liability',
            best_estimate=1000000.0, discount_rate=6.0,
            periods_to_settlement=3)
        self.assertAlmostEqual(p.present_value, 839619.28, places=2)
        with self.assertRaises(UserError):
            p.action_recognise()
        self.assertFalse(p.move_ids)
        self.assertEqual(p.state, 'draft')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        # Forcing the state by raw ORM write is caught by the constraint.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            p.write({'state': 'recognised'})
        self.env.invalidate_all()
        self.assertEqual(p.state, 'draft')
        # Nothing reached the ledger.
        self.assertAlmostEqual(
            self.posted_balance(self.provision_liab), 0.0, places=2)
