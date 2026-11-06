# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression coverage for Odoo 19's ``expense_other`` account type.

Odoo 16--18 do not expose this selection value.  Keeping it in SQL ``IN``
tuples remains safe there; integration assertions run only when the account
type exists.  One isolated posting proves every dynamic-report consumer sees
the same Other Expense instead of silently dropping it.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


def _has_expense_other(env):
    field = env['account.account']._fields['account_type']
    return 'expense_other' in dict(field._description_selection(env))


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestExpenseOtherReports(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pnl = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss']
        cls.balance_sheet = cls.env[
            'eh.account.dynamic.report.handler.balance_sheet']
        cls.executive = cls.env[
            'eh.account.dynamic.report.handler.executive_summary']
        cls.cash_flow = cls.env[
            'eh.account.dynamic.report.handler.cash_flow']
        cls.analytic = cls.env[
            'eh.account.dynamic.report.handler.analytic_balance']
        cls.has_expense_other = _has_expense_other(cls.env)
        cls.account_other_expense = None
        cls.analytic_account = None
        if not cls.has_expense_other:
            return

        cls.account_other_expense = cls._ensure_account(
            cls.env, '5901', 'Other Expenses', 'expense_other')

        analytic_distribution = False
        if 'account.analytic.plan' in cls.env:
            plan = cls.env['account.analytic.plan'].create({
                'name': 'Other Expense Plan',
            })
            cls.analytic_account = cls.env[
                'account.analytic.account'].create({
                    'name': 'Other Expense Centre',
                    'plan_id': plan.id,
                })
            analytic_distribution = {
                str(cls.analytic_account.id): 100.0,
            }

        expense_vals = {
            'account_id': cls.account_other_expense.id,
            'debit': 150.0,
            'name': 'Odoo 19 other expense',
        }
        if analytic_distribution:
            expense_vals['analytic_distribution'] = analytic_distribution
        cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, expense_vals),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'credit': 150.0,
                    'name': 'Cash payment',
                }),
            ],
        }).action_post()

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    @staticmethod
    def _line(payload, line_id):
        return next(
            (line for line in payload['lines'] if line['id'] == line_id),
            None,
        )

    @staticmethod
    def _amount(line):
        return next(
            (column['value'] for column in line['columns']
             if column['expression_label'] == 'amount'),
            None,
        )

    def _require_expense_other(self):
        if not self.has_expense_other:
            self.skipTest("'expense_other' account type not on this Odoo")

    def test_classification_contract_is_version_safe(self):
        """Unknown tuple value is inert on 16--18, active on 19+."""
        self.assertIn('expense_other', self.pnl.EXPENSE_TYPES)
        self.assertIn('expense_other', self.pnl.OPERATING_EXPENSE_TYPES)
        self.assertIn('expense_other', self.balance_sheet.EXPENSE_TYPES)
        self.assertIn('expense_other', self.executive.EXPENSE_TYPES)
        self.assertIn(
            'expense_other', self.executive.OPERATING_EXPENSE_TYPES)
        self.assertIn('expense_other', self.cash_flow.OPERATING_TYPES)
        self.assertIn('expense_other', self.cash_flow.PL_TYPES)
        self.assertEqual(
            self.pnl._expand_account_sign(
                self.account_other_expense or self.account_expense),
            1,
        )

        if not self.has_expense_other:
            payload = self.pnl.compute(self.options)
            self.assertEqual(payload['totals']['expenses'], 0.0)

    def test_profit_and_loss_includes_other_expense_in_both_layouts(self):
        self._require_expense_other()

        by_nature = self.pnl.compute(self.options)
        line_id = 'account-%d' % self.account_other_expense.id
        self.assertIsNotNone(self._line(by_nature, line_id))
        self.assertAlmostEqual(
            by_nature['totals']['expenses'], 150.0, places=2)
        self.assertAlmostEqual(
            by_nature['totals']['net_profit'], -150.0, places=2)

        by_function = self.pnl.compute(dict(
            self.options, pnl_presentation='by_function'))
        self.assertIsNotNone(self._line(by_function, line_id))
        self.assertAlmostEqual(
            self._amount(self._line(
                by_function, 'section-operating_expenses-total')),
            150.0, places=2,
        )
        self.assertAlmostEqual(
            by_function['totals']['net_profit'], -150.0, places=2)

    def test_balance_sheet_and_executive_profit_include_other_expense(self):
        self._require_expense_other()

        balance_sheet = self.balance_sheet.compute(self.options)
        self.assertAlmostEqual(
            balance_sheet['totals']['current_year_earnings'],
            -150.0, places=2,
        )
        self.assertAlmostEqual(
            balance_sheet['totals']['balance_check'], 0.0, places=2)

        executive = self.executive.compute(self.options)
        net = self._line(executive, 'exec-net_profit')
        operating = self._line(executive, 'exec-operating_profit')
        self.assertIsNotNone(net)
        self.assertIsNotNone(operating)
        self.assertAlmostEqual(
            net['columns'][0]['value'], -150.0, places=2)
        self.assertAlmostEqual(
            operating['columns'][0]['value'], -150.0, places=2,
        )

    def test_cash_flow_classifies_other_expense_and_indirect_profit(self):
        self._require_expense_other()

        direct = self.cash_flow.compute(self.options)
        other_line = self._line(
            direct, 'section-operating-line-expense_other')
        self.assertIsNotNone(other_line)
        self.assertEqual(other_line['name'], 'Other Expenses')
        self.assertAlmostEqual(self._amount(other_line), -150.0, places=2)
        self.assertAlmostEqual(direct['totals']['operating'], -150.0, places=2)
        self.assertAlmostEqual(
            direct['totals']['balance_check'], 0.0, places=2)

        indirect = self.cash_flow.compute(dict(
            self.options, cash_flow_method='indirect'))
        self.assertAlmostEqual(
            self._amount(self._line(indirect, 'indirect-pbt')),
            -150.0, places=2,
        )
        self.assertAlmostEqual(
            indirect['totals']['operating'], -150.0, places=2)

    def test_analytic_default_scope_includes_other_expense(self):
        self._require_expense_other()
        if not self.analytic_account:
            self.skipTest("Analytic addon not installed in this build")

        payload = self.analytic.compute(self.options)
        analytic_line = self._line(
            payload, 'analytic-%d' % self.analytic_account.id)
        self.assertIsNotNone(analytic_line)
        # Sign convention belongs to Analytic Balance; this regression only
        # locks inclusion and allocated magnitude.
        self.assertAlmostEqual(abs(self._amount(analytic_line)), 150.0, places=2)
