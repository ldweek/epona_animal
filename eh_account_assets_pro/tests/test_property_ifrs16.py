# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Pairwise scenario matrix over the IFRS 16 lease engine axes:

    exemption x option x component-split x cadence

with schedule invariants asserted on every generated case:

* non-exempt: the liability amortises exactly to zero; on every row
  round(interest + principal) == round(payment); the sum of principal
  equals the opening liability; ROU depreciation sums exactly to the
  initial ROU value; the ROU row count matches the depreciation span
  (useful life when a purchase option is reasonably certain).
* exempt: no liability, no interest, no principal, no ROU on any row;
  every row's payment equals the contractual payment (straight-line
  expense == payment for equal fixed payments).

Axis values are chosen so every combination is IFRS-valid (short-term
cases use a 6-month base + 6-month certain extension = 12 <= 12; the
purchase option never pairs with an exemption because IFRS 16.5 forbids
it - that refusal is asserted separately in the golden file).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

from .common import EhAssetTestCase

AXES = {
    'exemption': ['none', 'short_term', 'low_value'],
    'option': ['none', 'extension', 'termination', 'purchase'],
    'split': [0.0, 20.0],
    'cadence': ['monthly', 'quarterly'],
}


@tagged('eh_golden', 'eh_account_assets_pro', 'post_install', '-at_install')
class TestPropertyIfrs16(EhGoldenTestCase, EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account_lease_expense = cls._ensure_account(
            cls.env, '5300', 'Lease Expense', 'expense',
        )

    def _normalise(self, case):
        """Substitute IFRS-invalid pairs with their nearest valid value:
        an exemption never carries a purchase option (IFRS 16.5), and a
        short-term election caps the effective term at 12 months, so
        exempt cases run on a 6-month base and their extension is 6
        months."""
        case = dict(case)
        if case['exemption'] != 'none' and case['option'] == 'purchase':
            case['option'] = 'none'
        return case

    def _build_case(self, idx, case):
        exempt = case['exemption'] != 'none'
        base_term = 6 if exempt else 12
        ext_months = 6
        vals = {
            'reference': 'PW-IFRS16-%02d' % idx,
            'term_months': base_term,
            'cadence': case['cadence'],
            'payment_timing': 'arrears',
            'payment_amount': 1_000.0,
            'incremental_borrowing_rate': 5.0,
            'exemption': case['exemption'],
            'payment_service_pct': case['split'],
            'component_allocation_note': (
                'Pairwise stand-alone-price allocation.'
                if case['split'] else False
            ),
            'lease_expense_account_id': self.account_lease_expense.id,
        }
        if case['exemption'] == 'low_value':
            vals['underlying_asset_value'] = 4_000.0
        if case['option'] == 'purchase':
            # Useful life exceeds the term by a whole number of periods
            # for both cadences (12 -> 18: +6 months = 6 monthly or 2
            # quarterly periods).
            vals['underlying_useful_life_months'] = 18
        lease = self._make_lease(**vals)
        if case['option'] == 'extension':
            self.env['eh.lease.option'].create({
                'lease_id': lease.id,
                'option_type': 'extension',
                'extension_months': ext_months,
                'reasonably_certain': True,
            })
        elif case['option'] == 'termination':
            self.env['eh.lease.option'].create({
                'lease_id': lease.id,
                'option_type': 'termination',
                'termination_penalty': 1_500.0,
                'reasonably_certain': True,
            })
        elif case['option'] == 'purchase':
            self.env['eh.lease.option'].create({
                'lease_id': lease.id,
                'option_type': 'purchase',
                'purchase_price': 2_500.0,
                'reasonably_certain': True,
            })
        return lease

    def _assert_exempt_invariants(self, lease, label):
        lines = lease.schedule_line_ids.sorted('sequence')
        months = {'monthly': 1, 'quarterly': 3}[lease.cadence]
        self.assertEqual(
            len(lines), lease.effective_term_months // months, label,
        )
        self.assertAlmostEqual(
            lease.liability_initial_value, 0.00, places=2, msg=label,
        )
        self.assertAlmostEqual(
            lease.rou_initial_value, 0.00, places=2, msg=label,
        )
        for line in lines:
            self.assertAlmostEqual(
                line.payment_amount, lease.payment_amount, places=2,
                msg=label,
            )
            self.assertAlmostEqual(line.interest, 0.00, places=2, msg=label)
            self.assertAlmostEqual(line.principal, 0.00, places=2, msg=label)
            self.assertAlmostEqual(line.rou_amount, 0.00, places=2, msg=label)
            self.assertAlmostEqual(
                line.liability_close, 0.00, places=2, msg=label,
            )

    def _assert_recognised_invariants(self, lease, label):
        lines = lease.schedule_line_ids.sorted('sequence')
        months = {'monthly': 1, 'quarterly': 3}[lease.cadence]
        payment_rows = lease.effective_term_months // months
        rou_rows = lease._rou_depreciation_months()
        self.assertEqual(len(lines), max(payment_rows, rou_rows), label)

        # Liability amortises exactly to zero, opening ties to PV.
        payment_lines = lines.filtered(lambda line: line.payment_amount > 0)
        self.assertEqual(len(payment_lines), payment_rows, label)
        self.assertAlmostEqual(
            payment_lines[0].liability_open,
            lease.liability_initial_value, places=2, msg=label,
        )
        self.assertAlmostEqual(
            payment_lines[-1].liability_close, 0.00, places=2, msg=label,
        )
        running = lease.liability_initial_value
        service_expected = lease.currency_id.round(
            lease.payment_amount
            * (lease.payment_service_pct or 0.0) / 100.0,
        )
        for line in payment_lines:
            # interest + principal == payment, and the close chains.
            self.assertAlmostEqual(
                line.interest + line.principal, line.payment_amount,
                places=2, msg=label,
            )
            self.assertAlmostEqual(
                line.liability_open, running, places=2, msg=label,
            )
            self.assertAlmostEqual(
                line.liability_close, running - line.principal,
                places=2, msg=label,
            )
            self.assertAlmostEqual(
                line.service_amount, service_expected, places=2, msg=label,
            )
            running = line.liability_close
        # Principal recovered == opening liability.
        self.assertAlmostEqual(
            sum(payment_lines.mapped('principal')),
            lease.liability_initial_value, places=2, msg=label,
        )
        # ROU depreciation sums exactly to the initial ROU.
        self.assertAlmostEqual(
            sum(lines.mapped('rou_amount')),
            lease.rou_initial_value, places=2, msg=label,
        )
        # Intervening/tail rows carry monthly ROU depreciation only.
        for line in lines.filtered(lambda row: row.payment_amount == 0):
            self.assertAlmostEqual(
                line.payment_amount, 0.00, places=2, msg=label,
            )
            self.assertAlmostEqual(line.interest, 0.00, places=2, msg=label)
            self.assertAlmostEqual(line.principal, 0.00, places=2, msg=label)
            self.assertGreater(line.rou_amount, 0.0, label)

    def test_pairwise_schedule_invariants(self):
        cases = pairwise_cases(AXES)
        self.assertGreater(len(cases), 0)
        observed_exempt_splits = set()
        for idx, raw in enumerate(cases, start=1):
            case = self._normalise(raw)
            label = 'case %s: %s' % (idx, case)
            lease = self._build_case(idx, case)
            self.assertEqual(lease.payment_service_pct, case['split'], label)
            if case['exemption'] != 'none':
                observed_exempt_splits.add(case['split'])
            lease.action_compute_schedule()
            if case['exemption'] != 'none':
                self._assert_exempt_invariants(lease, label)
            else:
                self._assert_recognised_invariants(lease, label)
        self.assertEqual(observed_exempt_splits, {0.0, 20.0})
