# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario tests for the IFRS 15 revenue engine.

All-pairs sweep over the engine's scenario axes (satisfaction timing x
progress measurement method x variable consideration x financing
component), asserting engine invariants rather than hand-picked amounts:

* cumulative recognised (cash-basis) revenue never exceeds the allocated
  transaction price;
* the contract asset and contract liability are never negative and never
  both positive (the net position is one-sided by construction);
* every posted entry balances;
* driver-measured methods (costs incurred, units delivered) derive the
  stored percentage exactly from their drivers.

Golden worked examples with exact journal-entry assertions live in
test_golden_ifrs15.py; these tests catch the interaction bugs a fixed
example cannot.
"""

from dateutil.relativedelta import relativedelta

from odoo.fields import Date

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_revenue', 'post_install', '-at_install')
class TestPropertyRevenue(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')
        cls.fin_expense_acc = cls._ensure_account(
            cls.env, '5215', 'Financing Interest', 'expense')

    AXES = {
        'satisfaction': ['point_in_time', 'over_time'],
        'progress_method': ['output_milestones', 'output_units',
                            'input_cost', 'input_time'],
        'variable': ['none', 'constrained'],
        'financing': ['none', 'advance'],
    }

    def _build_case(self, case, idx):
        ob_vals = {
            'name': 'OB %s' % idx,
            'standalone_price': 1000.0,
            'satisfaction': case['satisfaction'],
            'progress_method': case['progress_method'],
            'method_basis': 'Pairwise case %s: method depicts the transfer '
                            'of control for this scenario.' % idx,
        }
        if case['variable'] == 'constrained':
            # Estimate 200 constrained to 120: only 120 joins the price.
            ob_vals.update({
                'variable_consideration': True,
                'variable_method': 'expected_value',
                'variable_estimate': 200.0,
                'variable_constraint': 120.0,
            })
        if case['progress_method'] == 'input_cost':
            ob_vals['cost_total_estimate'] = 1000.0
        elif case['progress_method'] == 'output_units':
            ob_vals['units_total'] = 10.0
        contract_vals = {
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, ob_vals)],
        }
        if case['financing'] == 'advance':
            today = Date.context_today(self.env.user)
            contract_vals.update({
                'contract_date': today,
                'financing_component': True,
                'financing_direction': 'advance',
                'financing_rate': 0.10,
                'financing_period_months': 12,
                'financing_payment_date': today + relativedelta(months=12),
                'financing_account_id': self.fin_expense_acc.id,
            })
        contract = self.env['eh.revenue.contract'].create(contract_vals)
        contract.action_activate()
        ob = contract.obligation_ids
        if case['satisfaction'] == 'point_in_time':
            ob.satisfied = True
        else:
            method = case['progress_method']
            if method == 'input_cost':
                ob.cost_incurred = 500.0
            elif method == 'output_units':
                ob.units_delivered = 5.0
            else:
                ob.percent_complete = 50.0
        currency = contract.currency_id
        if currency.compare_amounts(
                sum(contract.obligation_ids.mapped('to_recognise')),
                0.0) != 0:
            accounting_date = (
                contract.financing_payment_date
                if case['financing'] == 'advance'
                else None
            )
            contract.action_recognise(accounting_date=accounting_date)
        return contract

    def _assert_invariants(self, contract, case, label):
        allocated_total = sum(
            contract.obligation_ids.mapped('allocated_price'))
        # Cash-basis recognised revenue can never exceed the allocated
        # transaction price.
        recognition_cap = (
            contract.financing_pv
            if case['financing'] == 'advance'
            else allocated_total
        )
        self.assertLessEqual(
            contract.amount_recognised, recognition_cap + 0.005,
            'recognised above allocated for %s' % label)
        # The net contract position is one-sided and never negative.
        self.assertGreaterEqual(
            contract.contract_asset, -0.005,
            'negative contract asset for %s' % label)
        self.assertGreaterEqual(
            contract.contract_liability, -0.005,
            'negative contract liability for %s' % label)
        self.assertTrue(
            contract.contract_asset <= 0.005
            or contract.contract_liability <= 0.005,
            'asset and liability both positive for %s' % label)
        # Every posted entry balances.
        for move in contract.move_ids:
            self.assertBalanced(move)
        # Driver-measured methods derive the stored percentage exactly.
        ob = contract.obligation_ids
        if (case['satisfaction'] == 'over_time'
                and case['progress_method'] in ('input_cost',
                                                'output_units')):
            self.assertAlmostEqual(
                ob.percent_complete, 50.0, places=2,
                msg='driver percent wrong for %s' % label)

    def test_pairwise_matrix(self):
        for idx, case in enumerate(pairwise_cases(self.AXES)):
            label = repr(case)
            contract = self._build_case(case, idx)
            self._assert_invariants(contract, case, label)
