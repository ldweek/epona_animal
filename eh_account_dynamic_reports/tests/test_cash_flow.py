# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Cash Flow Statement handler tests.

Covers all three activity sections, multi counterpart moves, sign flow
(inflows positive, outflows negative), opening/closing cash balances,
the Balance Check identity, exclusion of pure cash transfers, posted only,
cancelled exclusion, error handling, orchestrator wiring, XLSX export.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestCashFlowHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.cash_flow'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'cash_flow')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'cash_flow',
                'name': 'Cash Flow Statement',
                'handler_model':
                    'eh.account.dynamic.report.handler.cash_flow',
            })
        # Additional fixtures used only by Cash Flow tests.
        cls.account_fixed_asset = cls._ensure_account(
            cls.env, '1500', 'Equipment', 'asset_fixed',
        )
        cls.account_long_term_loan = cls._ensure_account(
            cls.env, '2500', 'Long Term Loan', 'liability_non_current',
        )

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _post_in_period(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str),
        )

    def _partial_reconcile(self, debit_line, credit_line, amount):
        """Create one explicit company-currency reconciliation edge."""
        return self.env['account.partial.reconcile'].create({
            'debit_move_id': debit_line.id,
            'credit_move_id': credit_line.id,
            'amount': amount,
            'debit_amount_currency': amount,
            'credit_amount_currency': amount,
        })

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _amount(line):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    def test_kwd_precision_keeps_one_fils_cash_flow(self):
        kwd = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'KWD')], limit=1,
        )
        self.assertTrue(kwd)
        lines = self.handler._render_section(
            "Operating Activities", 'operating', ('income',),
            {'income': 0.001}, section_total=0.001, show_zero=False,
            currency=kwd,
        )
        detail = next(
            line for line in lines
            if line['id'] == 'section-operating-line-income'
        )
        total = next(
            line for line in lines
            if line['id'] == 'section-operating-total'
        )
        self.assertAlmostEqual(self._amount(detail), 0.001, places=3)
        self.assertAlmostEqual(self._amount(total), 0.001, places=3)

    def test_reconciled_attributes_receipt_to_income(self):
        # Invoice (no cash line): Dr AR 1000 / Cr Revenue 1000.
        inv = self._post_in_period([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        # Receipt (cash-active): Dr Cash 1000 / Cr AR 1000.
        pay = self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_receivable, 'credit': 1000.0,
             'partner': self.partner_a},
        ], date_str='2026-06-20')
        ar = (inv.line_ids + pay.line_ids).filtered(
            lambda l: l.account_id == self.account_receivable)
        ar.reconcile()

        # Coarse direct method: cash receipt lands in Receivables.
        coarse = self.handler.compute(self.options)
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                coarse, 'section-operating-line-asset_receivable')),
            1000.0, places=2)

        # Reconciliation-accurate: attributed to Income, no Receivables row.
        fine = self.handler.compute(
            dict(self.options, cash_flow_reconciled=True))
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                fine, 'section-operating-line-income')),
            1000.0, places=2)
        self.assertIsNone(self._line_by_id(
            fine, 'section-operating-line-asset_receivable'))
        # Total cash and the balance identity are preserved.
        self.assertAlmostEqual(fine['totals']['operating'], 1000.0, places=2)
        self.assertAlmostEqual(
            fine['totals']['net_change_in_cash'], 1000.0, places=2)
        self.assertAlmostEqual(fine['totals']['balance_check'], 0.0, places=2)

    def test_reconciled_and_coarse_agree_on_net_change(self):
        # WS3: the reconciliation-accurate path only re-attributes which
        # bucket a cash movement lands in; it must never change the total
        # cash moved. Net change in cash (and the balance identity) are
        # identical whether or not the checkbox is on.
        inv = self._post_in_period([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        pay = self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_receivable, 'credit': 1000.0,
             'partner': self.partner_a},
        ], date_str='2026-06-20')
        ar = (inv.line_ids + pay.line_ids).filtered(
            lambda l: l.account_id == self.account_receivable)
        ar.reconcile()

        coarse = self.handler.compute(self.options)
        fine = self.handler.compute(
            dict(self.options, cash_flow_reconciled=True))
        self.assertAlmostEqual(
            coarse['totals']['net_change_in_cash'],
            fine['totals']['net_change_in_cash'], places=2)
        self.assertAlmostEqual(
            coarse['totals']['balance_check'],
            fine['totals']['balance_check'], places=2)
        self.assertAlmostEqual(fine['totals']['balance_check'], 0.0, places=2)
        # The meta records the chosen path so the viewer can reflect it.
        self.assertTrue(fine['meta']['reconciled'])
        self.assertFalse(coarse['meta']['reconciled'])

    def test_reconciled_partial_payment_uses_partial_amount(self):
        invoice = self._post_in_period([
            {
                'account': self.account_receivable,
                'debit': 1000.0,
                'partner': self.partner_a,
            },
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-06-01')
        receipt = self._post_in_period([
            {'account': self.account_cash, 'debit': 400.0},
            {
                'account': self.account_receivable,
                'credit': 400.0,
                'partner': self.partner_a,
            },
        ], date_str='2026-06-20')
        receivable_lines = (invoice.line_ids | receipt.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable)
        receivable_lines.reconcile()

        result = self.handler.compute(dict(
            self.options, cash_flow_reconciled=True))

        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-income')),
            400.0,
            places=2,
        )
        self.assertIsNone(self._line_by_id(
            result, 'section-operating-line-asset_receivable'))
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 400.0, places=2)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2)

    def test_reconciled_multi_invoice_uses_each_partial_allocation(self):
        other_income = self._ensure_account(
            self.env, '4098', 'Other Settlement Income', 'income_other')
        first_invoice = self._post_in_period([
            {
                'account': self.account_receivable,
                'debit': 1000.0,
                'partner': self.partner_a,
            },
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-06-01')
        second_invoice = self._post_in_period([
            {
                'account': self.account_receivable,
                'debit': 1000.0,
                'partner': self.partner_a,
            },
            {'account': other_income, 'credit': 1000.0},
        ], date_str='2026-06-02')
        receipt = self._post_in_period([
            {'account': self.account_cash, 'debit': 400.0},
            {
                'account': self.account_receivable,
                'credit': 400.0,
                'partner': self.partner_a,
            },
        ], date_str='2026-06-20')
        first_ar = first_invoice.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable)
        second_ar = second_invoice.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable)
        receipt_ar = receipt.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable)
        self._partial_reconcile(first_ar, receipt_ar, 100.0)
        self._partial_reconcile(second_ar, receipt_ar, 300.0)

        result = self.handler.compute(dict(
            self.options, cash_flow_reconciled=True))

        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-income')),
            100.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-income_other')),
            300.0,
            places=2,
        )
        # Both edges allocate one cash line; neither gross invoice nor edge
        # traversal may count that receipt twice.
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 400.0, places=2)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2)

    def test_reconciled_unapplied_receipt_share_stays_receivable(self):
        invoice = self._post_in_period([
            {
                'account': self.account_receivable,
                'debit': 400.0,
                'partner': self.partner_a,
            },
            {'account': self.account_revenue, 'credit': 400.0},
        ], date_str='2026-06-01')
        receipt = self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {
                'account': self.account_receivable,
                'credit': 1000.0,
                'partner': self.partner_a,
            },
        ], date_str='2026-06-20')
        receivable_lines = (invoice.line_ids | receipt.line_ids).filtered(
            lambda line: line.account_id == self.account_receivable)
        receivable_lines.reconcile()

        result = self.handler.compute(dict(
            self.options, cash_flow_reconciled=True))

        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-income')),
            400.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-asset_receivable')),
            600.0,
            places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 1000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2)

    def test_reconciled_future_allocation_is_unapplied_as_of_report_date(self):
        receipt = self._post_in_period([
            {'account': self.account_cash, 'debit': 600.0},
            {
                'account': self.account_receivable,
                'credit': 600.0,
                'partner': self.partner_a,
            },
        ], date_str='2026-06-20')
        future_invoice = self._post_in_period([
            {
                'account': self.account_receivable,
                'debit': 600.0,
                'partner': self.partner_a,
            },
            {'account': self.account_revenue, 'credit': 600.0},
        ], date_str='2027-01-05')
        receivable_lines = (
            receipt.line_ids | future_invoice.line_ids
        ).filtered(lambda line: line.account_id == self.account_receivable)
        receivable_lines.reconcile()

        result = self.handler.compute(dict(
            self.options, cash_flow_reconciled=True))

        self.assertIsNone(self._line_by_id(
            result, 'section-operating-line-income'))
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, 'section-operating-line-asset_receivable')),
            600.0,
            places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 600.0, places=2)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2)

    # ---- core classification ----

    def test_customer_payment_lands_in_operating_inflow(self):
        # First, create the receivable on a separate move.
        self._post_in_period([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-06-01')
        # Customer pays.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0,
             'partner': self.partner_a},
            {'account': self.account_receivable, 'credit': 1000.0,
             'partner': self.partner_a},
        ], date_str='2026-06-15')

        result = self.handler.compute(self.options)
        # Operating section has a Receivables line worth +1000 (cash inflow
        # from customer collections).
        receivables_line = self._line_by_id(
            result, 'section-operating-line-asset_receivable',
        )
        self.assertIsNotNone(receivables_line)
        self.assertAlmostEqual(self._amount(receivables_line), 1000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['operating'], 1000.0, places=2,
        )

    def test_direct_revenue_lands_in_operating(self):
        # Cash sale, no receivable.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 500.0},
            {'account': self.account_revenue, 'credit': 500.0},
        ])
        result = self.handler.compute(self.options)
        income_line = self._line_by_id(
            result, 'section-operating-line-income',
        )
        self.assertIsNotNone(income_line)
        self.assertAlmostEqual(self._amount(income_line), 500.0, places=2)

    def test_account_type_filter_targets_direct_counterparts(self):
        """The account type is activity-side, never a cash-line predicate."""
        self._post_in_period([
            {'account': self.account_cash, 'debit': 300.0},
            {'account': self.account_revenue, 'credit': 300.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 120.0},
            {'account': self.account_cash, 'credit': 120.0},
        ], date_str='2026-06-20')

        result = self.handler.compute(dict(
            self.options,
            account_type_ids=['expense'],
        ))

        expense_line = self._line_by_id(
            result, 'section-operating-line-expense')
        self.assertIsNotNone(expense_line, result)
        self.assertAlmostEqual(self._amount(expense_line), -120.0, places=2)
        self.assertIsNone(self._line_by_id(
            result, 'section-operating-line-income'))
        self.assertAlmostEqual(result['totals']['operating'], -120.0, places=2)
        # Opening/closing remain actual cash balances; the account-type
        # selector must not turn their asset_cash predicate into an impossible
        # expense-and-cash intersection.
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 180.0, places=2)

    def test_account_and_analytic_filters_target_direct_counterparts(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic accounting is not installed")
        plan = self.env['account.analytic.plan'].create({
            'name': 'Cash Flow Direct Filter Plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'Cash Flow Direct A',
            'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'Cash Flow Direct B',
            'plan_id': plan.id,
        })
        other_expense = self._ensure_account(
            self.env, '5097', 'Other Filter Expense', 'expense')

        def post_cash_expense(account, amount, analytic):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string('2026-06-15'),
                'line_ids': [
                    (0, 0, {
                        'account_id': account.id,
                        'debit': amount,
                        'analytic_distribution': {
                            str(analytic.id): 100.0,
                        },
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'credit': amount,
                    }),
                ],
            })
            move.action_post()

        post_cash_expense(self.account_expense, 120.0, analytic_a)
        post_cash_expense(self.account_expense, 30.0, analytic_b)
        post_cash_expense(other_expense, 50.0, analytic_a)

        result = self.handler.compute(dict(
            self.options,
            account_ids=[self.account_expense.id],
            analytic_account_ids=[analytic_a.id],
        ))

        expense_line = self._line_by_id(
            result, 'section-operating-line-expense')
        self.assertIsNotNone(expense_line, result)
        self.assertAlmostEqual(self._amount(expense_line), -120.0, places=2)
        # All three payments remain in the actual cash balance even though the
        # activity slice selects only one counterpart line.
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], -200.0, places=2)

    def test_account_type_filter_reaches_reconciled_traced_activity(self):
        bill = self._post_in_period([
            {'account': self.account_expense, 'debit': 90.0},
            {'account': self.account_payable, 'credit': 90.0,
             'partner': self.partner_a},
        ], date_str='2026-06-01')
        payment = self._post_in_period([
            {'account': self.account_payable, 'debit': 90.0,
             'partner': self.partner_a},
            {'account': self.account_cash, 'credit': 90.0},
        ], date_str='2026-06-20')
        payable_lines = (bill.line_ids | payment.line_ids).filtered(
            lambda line: line.account_id == self.account_payable)
        payable_lines.reconcile()

        result = self.handler.compute(dict(
            self.options,
            cash_flow_reconciled=True,
            account_type_ids=['expense'],
        ))

        expense_line = self._line_by_id(
            result, 'section-operating-line-expense')
        self.assertIsNotNone(expense_line)
        self.assertAlmostEqual(self._amount(expense_line), -90.0, places=2)
        self.assertIsNone(self._line_by_id(
            result, 'section-operating-line-liability_payable'))

    def test_account_and_analytic_plan_filters_reach_traced_activity(self):
        if 'account.analytic.plan' not in self.env:
            self.skipTest("Analytic accounting is not installed")
        selected_plan = self.env['account.analytic.plan'].create({
            'name': 'Cash Flow Traced Selected Plan',
        })
        other_plan = self.env['account.analytic.plan'].create({
            'name': 'Cash Flow Traced Other Plan',
        })
        selected_analytic = self.env['account.analytic.account'].create({
            'name': 'Cash Flow Traced Selected',
            'plan_id': selected_plan.id,
        })
        other_analytic = self.env['account.analytic.account'].create({
            'name': 'Cash Flow Traced Other',
            'plan_id': other_plan.id,
        })
        other_expense = self._ensure_account(
            self.env, '5098', 'Other Traced Expense', 'expense')

        def post_and_pay_bill(account, amount, analytic, day):
            bill = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': fields.Date.from_string('2026-06-%02d' % day),
                'line_ids': [
                    (0, 0, {
                        'account_id': account.id,
                        'debit': amount,
                        'analytic_distribution': {
                            str(analytic.id): 100.0,
                        },
                    }),
                    (0, 0, {
                        'account_id': self.account_payable.id,
                        'credit': amount,
                        'partner_id': self.partner_a.id,
                    }),
                ],
            })
            bill.action_post()
            payment = self._post_in_period([
                {
                    'account': self.account_payable,
                    'debit': amount,
                    'partner': self.partner_a,
                },
                {'account': self.account_cash, 'credit': amount},
            ], date_str='2026-07-%02d' % day)
            payable_lines = (bill.line_ids | payment.line_ids).filtered(
                lambda line: line.account_id == self.account_payable)
            payable_lines.reconcile()

        post_and_pay_bill(
            self.account_expense, 90.0, selected_analytic, 1)
        post_and_pay_bill(
            self.account_expense, 30.0, other_analytic, 2)
        post_and_pay_bill(other_expense, 40.0, selected_analytic, 3)

        result = self.handler.compute(dict(
            self.options,
            cash_flow_reconciled=True,
            account_ids=[self.account_expense.id],
            analytic_plan_ids=[selected_plan.id],
        ))

        expense_line = self._line_by_id(
            result, 'section-operating-line-expense')
        self.assertIsNotNone(expense_line)
        self.assertAlmostEqual(self._amount(expense_line), -90.0, places=2)
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], -160.0, places=2)

    def test_partner_filter_reaches_reconciled_traced_activity(self):
        def invoice_and_receive(partner, amount, day):
            invoice = self._post_in_period([
                {
                    'account': self.account_receivable,
                    'debit': amount,
                    'partner': partner,
                },
                {
                    'account': self.account_revenue,
                    'credit': amount,
                    'partner': partner,
                },
            ], date_str='2026-06-%02d' % day)
            receipt = self._post_in_period([
                {'account': self.account_cash, 'debit': amount},
                {
                    'account': self.account_receivable,
                    'credit': amount,
                    'partner': partner,
                },
            ], date_str='2026-07-%02d' % day)
            receivable_lines = (invoice.line_ids | receipt.line_ids).filtered(
                lambda line: line.account_id == self.account_receivable)
            receivable_lines.reconcile()

        invoice_and_receive(self.partner_a, 100.0, 1)
        invoice_and_receive(self.partner_b, 50.0, 2)

        result = self.handler.compute(dict(
            self.options,
            cash_flow_reconciled=True,
            partner_ids=[self.partner_a.id],
        ))

        income_line = self._line_by_id(
            result, 'section-operating-line-income')
        self.assertIsNotNone(income_line)
        self.assertAlmostEqual(self._amount(income_line), 100.0, places=2)
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 150.0, places=2)

    def test_account_type_filter_scopes_indirect_activity(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 300.0},
            {'account': self.account_revenue, 'credit': 300.0},
        ])
        self._post_in_period([
            {'account': self.account_expense, 'debit': 120.0},
            {'account': self.account_cash, 'credit': 120.0},
        ], date_str='2026-06-20')

        result = self.handler.compute(dict(
            self.options,
            cash_flow_method='indirect',
            account_type_ids=['expense'],
        ))

        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, 'indirect-pbt')),
            -120.0,
            places=2,
        )
        self.assertAlmostEqual(result['totals']['operating'], -120.0, places=2)
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 180.0, places=2)

    def test_equipment_purchase_lands_in_investing_outflow(self):
        self._post_in_period([
            {'account': self.account_fixed_asset, 'debit': 5000.0},
            {'account': self.account_cash, 'credit': 5000.0},
        ])
        result = self.handler.compute(self.options)
        fixed_line = self._line_by_id(
            result, 'section-investing-line-asset_fixed',
        )
        self.assertIsNotNone(fixed_line)
        # Cash outflow for fixed asset purchase: -5000.
        self.assertAlmostEqual(self._amount(fixed_line), -5000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['investing'], -5000.0, places=2,
        )

    def test_prepayment_is_operating_working_capital(self):
        prepayment = self._ensure_account(
            self.env, '1405', 'Supplier Prepayments', 'asset_prepayments')
        self._post_in_period([
            {'account': prepayment, 'debit': 750.0},
            {'account': self.account_cash, 'credit': 750.0},
        ])
        result = self.handler.compute(self.options)
        line = self._line_by_id(
            result, 'section-operating-line-asset_prepayments')
        self.assertIsNotNone(line)
        self.assertAlmostEqual(self._amount(line), -750.0, places=2)
        self.assertAlmostEqual(result['totals']['operating'], -750.0, places=2)
        self.assertAlmostEqual(result['totals']['investing'], 0.0, places=2)

    def test_loan_received_lands_in_financing_inflow(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 10000.0},
            {'account': self.account_long_term_loan, 'credit': 10000.0},
        ])
        result = self.handler.compute(self.options)
        loan_line = self._line_by_id(
            result, 'section-financing-line-liability_non_current',
        )
        self.assertIsNotNone(loan_line)
        self.assertAlmostEqual(self._amount(loan_line), 10000.0, places=2)
        self.assertAlmostEqual(
            result['totals']['financing'], 10000.0, places=2,
        )

    def test_equity_injection_lands_in_financing(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 20000.0},
            {'account': self.account_equity, 'credit': 20000.0},
        ])
        result = self.handler.compute(self.options)
        equity_line = self._line_by_id(
            result, 'section-financing-line-equity',
        )
        self.assertIsNotNone(equity_line)
        self.assertAlmostEqual(self._amount(equity_line), 20000.0, places=2)

    def test_multi_counterpart_move_splits_correctly(self):
        # DR Cash 1000 / CR Revenue 700 / CR Liability Current 300 (e.g. tax).
        liability_current = self._ensure_account(
            self.env, '2200', 'Tax Payable', 'liability_current',
        )
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 700.0},
            {'account': liability_current, 'credit': 300.0},
        ])
        result = self.handler.compute(self.options)
        income_line = self._line_by_id(
            result, 'section-operating-line-income',
        )
        liab_line = self._line_by_id(
            result, 'section-operating-line-liability_current',
        )
        self.assertAlmostEqual(self._amount(income_line), 700.0, places=2)
        self.assertAlmostEqual(self._amount(liab_line), 300.0, places=2)
        self.assertAlmostEqual(
            result['totals']['operating'], 1000.0, places=2,
        )

    # ---- balance check identity ----

    def test_balance_check_zero_for_simple_inflow(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )

    def test_balance_check_zero_for_complex_ledger(self):
        # Equity injection.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 5000.0},
            {'account': self.account_equity, 'credit': 5000.0},
        ])
        # Direct revenue.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 2000.0},
            {'account': self.account_revenue, 'credit': 2000.0},
        ])
        # Equipment purchase.
        self._post_in_period([
            {'account': self.account_fixed_asset, 'debit': 3000.0},
            {'account': self.account_cash, 'credit': 3000.0},
        ])
        # Vendor payment without prior payable (treat as expense direct).
        self._post_in_period([
            {'account': self.account_expense, 'debit': 500.0},
            {'account': self.account_cash, 'credit': 500.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )
        # Net change = 5000 + 2000 - 3000 - 500 = 3500.
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 3500.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance']
            - result['totals']['opening_cash_balance'],
            3500.0, places=2,
        )

    def test_pure_cash_transfer_excluded(self):
        # Two cash accounts, transfer between them.
        bank = self._ensure_account(
            self.env, '1010', 'Bank Account', 'asset_cash',
        )
        self._post_in_period([
            {'account': bank, 'debit': 500.0},
            {'account': self.account_cash, 'credit': 500.0},
        ])
        result = self.handler.compute(self.options)
        # No section should record the transfer.
        self.assertAlmostEqual(
            result['totals']['operating'], 0.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['investing'], 0.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['financing'], 0.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 0.0, places=2,
        )

    # ---- IAS 7.28 FX effect on cash ----

    def _ensure_fx_journal(self):
        """Return the company's exchange difference journal, provisioning
        one when the clean-room database has none configured."""
        fx_journal = self.company.currency_exchange_journal_id
        if not fx_journal:
            fx_journal = self.env['account.journal'].create({
                'name': 'Exchange Difference',
                'code': 'EXCD',
                'type': 'general',
                'company_id': self.company.id,
            })
            self.company.sudo().currency_exchange_journal_id = fx_journal
        return fx_journal

    def test_fx_revaluation_isolated_on_fx_effect_line(self):
        fx_journal = self._ensure_fx_journal()
        fx_gain = self._ensure_account(
            self.env, '7100', 'FX Gain', 'income_other',
        )
        # Normal operating receipt.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        # Unrealised FX revaluation of cash held, posted in the exchange
        # difference journal: Dr Cash 50 / Cr FX Gain 50.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 50.0},
                {'account': fx_gain, 'credit': 50.0},
            ],
            journal=fx_journal,
            date=fields.Date.from_string('2026-06-30'),
        )
        result = self.handler.compute(self.options)
        # The revaluation is presented on its own FX line, not as activity.
        fx_line = self._line_by_id(result, 'fx_effect_on_cash')
        self.assertIsNotNone(fx_line)
        self.assertAlmostEqual(self._amount(fx_line), 50.0, places=2)
        self.assertAlmostEqual(
            result['totals']['fx_effect_on_cash'], 50.0, places=2,
        )
        # Operating carries only the receipt; the FX gain is excluded.
        self.assertAlmostEqual(
            result['totals']['operating'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 1000.0, places=2,
        )
        # Extended identity: Closing == Opening + Net Change + FX effect.
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 1050.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )
        # The FX line sits after Net Change and before the cash balances.
        line_ids = [l['id'] for l in result['lines']]
        self.assertLess(
            line_ids.index('net_change_in_cash'),
            line_ids.index('fx_effect_on_cash'),
        )
        self.assertLess(
            line_ids.index('fx_effect_on_cash'),
            line_ids.index('closing_cash_balance'),
        )

    def test_fx_revaluation_journal_isolated_on_fx_effect_line(self):
        # Defect (B): core posts no cash line into
        # currency_exchange_journal_id on AR/AP settlement, so a real bank
        # revaluation run posted in a dedicated journal must still be
        # recognised as an exchange-rate effect. Configuring that journal
        # on eh_cash_fx_revaluation_journal_id isolates it onto the FX line
        # rather than leaking into the opening-to-closing difference.
        fx_gain = self._ensure_account(
            self.env, '7100', 'FX Gain', 'income_other',
        )
        reval_journal = self.env['account.journal'].create({
            'name': 'Cash Revaluation',
            'code': 'REVL',
            'type': 'general',
            'company_id': self.company.id,
        })
        self.company.sudo().eh_cash_fx_revaluation_journal_id = reval_journal
        # Normal operating receipt in the misc journal.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        # Bank revaluation posted in the dedicated revaluation journal:
        # Dr Cash 80 / Cr FX Gain 80.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 80.0},
                {'account': fx_gain, 'credit': 80.0},
            ],
            journal=reval_journal,
            date=fields.Date.from_string('2026-06-30'),
        )
        result = self.handler.compute(self.options)
        # Revaluation is on the FX line, not in the activity sections.
        fx_line = self._line_by_id(result, 'fx_effect_on_cash')
        self.assertIsNotNone(fx_line)
        self.assertAlmostEqual(self._amount(fx_line), 80.0, places=2)
        self.assertAlmostEqual(
            result['totals']['fx_effect_on_cash'], 80.0, places=2,
        )
        # Operating carries only the receipt; the revaluation is excluded.
        self.assertAlmostEqual(
            result['totals']['operating'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 1000.0, places=2,
        )
        # Closing reflects both postings; the identity holds via the FX line.
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 1080.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )

    def test_fx_counterpart_account_isolated_without_nominated_journal(self):
        # A stock-Odoo bank revaluation posts to an ordinary bank journal,
        # NOT the nominated exchange difference journal, and carries the
        # exchange gain/loss account as its counterpart. Detection must catch
        # it via that counterpart account alone, isolate the cash side on the
        # FX line, and keep the Balance Check at zero, with no nominated FX
        # journal configured on the company at all.
        fx_gain = self._ensure_account(
            self.env, '7100', 'FX Gain', 'income_other',
        )
        # Nominate the exchange gain/loss account on the company; leave the
        # exchange difference journal (and the revaluation journal) unset so
        # only the counterpart-account seam can pick this up.
        self.company.sudo().write({
            'currency_exchange_journal_id': False,
            'income_currency_exchange_account_id': fx_gain.id,
        })
        if 'eh_cash_fx_revaluation_journal_id' in self.company._fields:
            self.company.sudo().eh_cash_fx_revaluation_journal_id = False
        # Normal operating receipt in the misc journal.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        # Stock-style bank revaluation posted in an ordinary bank journal:
        # Dr Cash 60 / Cr FX gain account 60.
        bank_journal = self.env['account.journal'].create({
            'name': 'Ordinary Bank', 'code': 'OBNK', 'type': 'general',
            'company_id': self.company.id,
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 60.0},
                {'account': fx_gain, 'credit': 60.0},
            ],
            journal=bank_journal,
            date=fields.Date.from_string('2026-06-30'),
        )
        result = self.handler.compute(self.options)
        # Revaluation is isolated on the FX line, not the activity sections.
        fx_line = self._line_by_id(result, 'fx_effect_on_cash')
        self.assertIsNotNone(fx_line)
        self.assertAlmostEqual(self._amount(fx_line), 60.0, places=2)
        self.assertAlmostEqual(
            result['totals']['fx_effect_on_cash'], 60.0, places=2,
        )

    def test_mixed_activity_and_fx_move_splits_instead_of_whole_move_fx(self):
        fx_gain = self._ensure_account(
            self.env, '4898', 'Mixed Exchange Gain', 'income_other')
        self.company.sudo().write({
            'currency_exchange_journal_id': False,
            'income_currency_exchange_account_id': fx_gain.id,
        })
        if 'eh_cash_fx_revaluation_journal_id' in self.company._fields:
            self.company.sudo().eh_cash_fx_revaluation_journal_id = False
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 80.0},
            {'account': fx_gain, 'credit': 20.0},
        ])
        result = self.handler.compute(self.options)
        income = self._line_by_id(result, 'section-operating-line-income')
        self.assertAlmostEqual(self._amount(income), 80.0, places=2)
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, 'fx_effect_on_cash')),
            20.0, places=2)
        self.assertAlmostEqual(result['totals']['balance_check'], 0.0, places=2)
        # Operating carries only the 80 activity counterpart; the 20 FX
        # counterpart is disclosed separately while cash still moved by 100.
        self.assertAlmostEqual(
            result['totals']['operating'], 80.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 80.0, places=2,
        )
        # Closing reflects the complete mixed move; the identity holds via
        # the separately disclosed FX component.
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 100.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )

    # ---- option-filtered opening/closing (Balance Check integrity) ----

    def test_journal_filter_keeps_balance_check_zero(self):
        # Defect (A): when the report is scoped to a single journal, the
        # activity sections and FX line already measure only that journal's
        # cash movement. Opening and closing must measure the same filtered
        # subset, or the Balance Check surfaces a spurious residual.
        # Cash receipt posted in the misc journal (in scope).
        self._post_in_period([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        # Cash receipt posted in a different journal (out of scope).
        other_journal = self.env['account.journal'].create({
            'name': 'Other Cash', 'code': 'OTHR', 'type': 'general',
            'company_id': self.company.id,
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 400.0},
                {'account': self.account_revenue, 'credit': 400.0},
            ],
            journal=other_journal,
            date=fields.Date.from_string('2026-06-20'),
        )
        scoped = dict(self.options, journal_ids=[self.journal_misc.id])
        result = self.handler.compute(scoped)
        # Only the in-scope receipt drives net change and closing cash.
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 1000.0, places=2,
        )
        # The identity holds only when opening/closing honour the filter.
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )

    def test_partner_filter_targets_counterpart_and_retains_cash_balance(self):
        # Partner belongs to the classified activity line. Cash legs commonly
        # carry no partner and must still be discovered and included in the
        # actual opening/closing cash balance.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 600.0},
            {'account': self.account_revenue, 'credit': 600.0,
             'partner': self.partner_a},
        ])
        self._post_in_period([
            {'account': self.account_cash, 'debit': 300.0},
            {'account': self.account_revenue, 'credit': 300.0,
             'partner': self.partner_b},
        ], date_str='2026-06-20')
        scoped = dict(self.options, partner_ids=[self.partner_a.id])
        result = self.handler.compute(scoped)
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 600.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 900.0, places=2,
        )
        # The residual is the intentionally excluded Partner B activity, not a
        # missing or incorrectly filtered cash balance.
        self.assertAlmostEqual(
            result['totals']['balance_check'], 300.0, places=2,
        )

    # ---- configurable cash and cash equivalents ----

    def test_cash_equivalent_transfer_is_pure_and_counts_as_cash(self):
        money_market = self._ensure_account(
            self.env, '1050', 'Money Market Fund', 'asset_current',
        )
        self.company.sudo().eh_cash_equivalent_account_ids = [
            (6, 0, money_market.ids),
        ]
        # Pre-period balance held in the equivalent: part of opening cash.
        self.post_balanced_move(
            [
                {'account': money_market, 'debit': 1000.0},
                {'account': self.account_equity, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        # In-period transfer from real cash into the equivalent: a pure
        # cash movement, no activity in any section.
        self._post_in_period([
            {'account': money_market, 'debit': 500.0},
            {'account': self.account_cash, 'credit': 500.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(result['totals']['operating'], 0.0, places=2)
        self.assertAlmostEqual(result['totals']['investing'], 0.0, places=2)
        self.assertAlmostEqual(result['totals']['financing'], 0.0, places=2)
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 0.0, places=2,
        )
        # Equivalent balances count towards opening and closing cash.
        self.assertAlmostEqual(
            result['totals']['opening_cash_balance'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )

    def test_no_fx_no_equivalents_output_shape_unchanged(self):
        # Regression: an existing-style ledger with no FX postings and no
        # configured equivalents must produce the same section totals and
        # the same trailing line layout as before the FX/equivalents work.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 5000.0},
            {'account': self.account_equity, 'credit': 5000.0},
        ])
        self._post_in_period([
            {'account': self.account_cash, 'debit': 2000.0},
            {'account': self.account_revenue, 'credit': 2000.0},
        ])
        self._post_in_period([
            {'account': self.account_fixed_asset, 'debit': 3000.0},
            {'account': self.account_cash, 'credit': 3000.0},
        ])
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['operating'], 2000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['investing'], -3000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['financing'], 5000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 4000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['balance_check'], 0.0, places=2,
        )
        # No FX line, no FX totals key: payload shape unchanged.
        self.assertIsNone(self._line_by_id(result, 'fx_effect_on_cash'))
        self.assertNotIn('fx_effect_on_cash', result['totals'])
        line_ids = [l['id'] for l in result['lines']]
        self.assertEqual(line_ids[-4:], [
            'net_change_in_cash', 'opening_cash_balance',
            'closing_cash_balance', 'cash_balance_check',
        ])

    # ---- date filtering ----

    def test_out_of_period_entries_excluded_from_net_change(self):
        # Pre period equity injection contributes to opening, not net change.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_equity, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        # In period revenue.
        self._post_in_period([
            {'account': self.account_cash, 'debit': 200.0},
            {'account': self.account_revenue, 'credit': 200.0},
        ])
        result = self.handler.compute(self.options)
        # Opening = 1000, net change = 200, closing = 1200.
        self.assertAlmostEqual(
            result['totals']['opening_cash_balance'], 1000.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 200.0, places=2,
        )
        self.assertAlmostEqual(
            result['totals']['closing_cash_balance'], 1200.0, places=2,
        )

    # ---- state filtering ----

    def test_posted_only_excludes_draft(self):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-15',
            'line_ids': [
                (0, 0, {'account_id': self.account_cash.id, 'debit': 999.0}),
                (0, 0, {'account_id': self.account_revenue.id, 'credit': 999.0}),
            ],
        })
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 0.0, places=2,
        )

    def test_cancelled_excluded(self):
        move = self._post_in_period([
            {'account': self.account_cash, 'debit': 444.0},
            {'account': self.account_revenue, 'credit': 444.0},
        ])
        move.button_cancel()
        result = self.handler.compute(self.options)
        self.assertAlmostEqual(
            result['totals']['net_change_in_cash'], 0.0, places=2,
        )

    # ---- structural ----

    def test_three_section_headers_present(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        result = self.handler.compute(self.options)
        kinds = [
            (l.get('meta') or {}).get('kind')
            for l in result['lines']
        ]
        self.assertEqual(kinds.count('section_header'), 3)
        self.assertEqual(kinds.count('section_total'), 3)
        # Net Change, Opening, Closing, Balance Check.
        self.assertIn('net_change', kinds)
        self.assertIn('cash_balance', kinds)
        self.assertIn('balance_check', kinds)

    def test_zero_section_lines_hidden_by_default(self):
        # Only operating activity; investing/financing line bodies should be
        # hidden (only their headers and totals show).
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        result = self.handler.compute(self.options)
        section_lines = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('kind') == 'section_line'
        ]
        # Only the income line should appear.
        self.assertEqual(len(section_lines), 1)
        self.assertEqual(
            section_lines[0]['meta']['account_type'], 'income',
        )

    # ---- error handling ----

    def test_missing_dates_raise(self):
        bad = dict(self.options)
        bad.pop('date')
        with self.assertRaises(UserError):
            self.handler.compute(bad)

    # ---- orchestrator wiring ----

    def test_orchestrator_renders(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        result = self.report.render(self.options)
        self.assertFalse(result['from_cache'])

    def test_orchestrator_cache_hit_on_second_render(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        first = self.report.render(self.options)
        second = self.report.render(self.options)
        self.assertFalse(first['from_cache'])
        self.assertTrue(second['from_cache'])
        self.assertEqual(first['totals'], second['totals'])

    # ---- XLSX export ----

    def test_xlsx_export_renders_workbook(self):
        self._post_in_period([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ])
        content = self.report.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')
        self.assertGreater(len(content), 1000)
