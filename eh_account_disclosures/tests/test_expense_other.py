# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: 'Other Expenses' (account_type 'expense_other') must be part of
the P&L expense aggregation in both disclosure engines.

'expense_other' is a first-class Odoo 19 P&L expense account type (IAS 1 Other
Expenses). Two engines here bucket ledger lines by account_type:

* IFRS 8 segment ledger tie-out (segment._derive_ledger_amounts) - the 'books'
  benchmark a reported segment result is tied out against.
* IFRS 12 summarised-subsidiary information (entity_interest.
  _apply_consolidation_figures) - the disclosed summarised_profit of a
  subsidiary with material NCI.

Both previously omitted 'expense_other', so other-expense accounts (bank
charges, FX losses, donations) were silently excluded and the disclosed profit
/ segment result was overstated. These tests assert the omitted type is now
captured.

'expense_other' only exists as an account_type on Odoo 19+; on 16/17/18 the
value is not in the account.account selection, so each test skips there (the
constant simply carries a harmless value that never matches an account). This
keeps the suite cross-version safe while proving the Odoo 19 behaviour.
"""

from types import SimpleNamespace

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


def _has_expense_other(env):
    """True when 'expense_other' is a valid account.account account_type on
    this Odoo series (19+). Used to gate the tests off on 16/17/18."""
    selection = dict(
        env['account.account']._fields['account_type'].selection)
    return 'expense_other' in selection


class _FakeConsolRunLines(object):
    """Minimal stand-in for a consolidation run's line_ids recordset,
    supporting only the .filtered() / .mapped() / iteration that
    _apply_consolidation_figures uses. Lets the IFRS 12 aggregation be
    exercised without the soft-dependency consolidation engine installed."""

    def __init__(self, lines):
        self._lines = list(lines)

    def filtered(self, predicate):
        return _FakeConsolRunLines(
            [line for line in self._lines if predicate(line)])

    def mapped(self, field_name):
        return [getattr(line, field_name) for line in self._lines]

    def __iter__(self):
        return iter(self._lines)


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestSegmentLedgerExpenseOther(EhAccountIntegrationTestCase):
    """IFRS 8 segment ledger tie-out captures 'Other Expenses' postings."""

    def _make_analytic(self, name):
        plan = self.env['account.analytic.plan'].create({'name': name})
        return self.env['account.analytic.account'].create({
            'name': name, 'plan_id': plan.id})

    def test_segment_ledger_result_includes_expense_other(self):
        if not _has_expense_other(self.env):
            self.skipTest(
                "account_type 'expense_other' absent on this Odoo series")
        today = fields.Date.context_today(self.env.user)
        analytic = self._make_analytic('Ops Segment')
        other_expense = self._ensure_account(
            self.env, '5900', 'Other Expenses', 'expense_other')

        # One balanced entry tagged to the segment: 500 revenue and 80 of
        # 'expense_other', so the ledger segment result is 500 - 80 = 420.
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': today,
            'ref': 'SEG-OTHER-EXP',
            'line_ids': [
                (0, 0, {
                    'name': 'Revenue',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0, 'credit': 500.0,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
                (0, 0, {
                    'name': 'Other expense',
                    'account_id': other_expense.id,
                    'debit': 80.0, 'credit': 0.0,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
                (0, 0, {
                    'name': 'Receivable',
                    'account_id': self.account_receivable.id,
                    'debit': 420.0, 'credit': 0.0,
                }),
            ],
        })
        move.action_post()

        report = self.env['eh.segment.report'].create({
            'period_end': today,
            'entity_revenue': 500.0,
            'segment_ids': [
                (0, 0, {'name': 'Ops', 'revenue': 500.0, 'result': 420.0,
                        'analytic_account_id': analytic.id}),
            ],
        })
        line = report.segment_ids
        # Ledger revenue is the income magnitude only (500).
        self.assertAlmostEqual(line.ledger_revenue, 500.0, places=2)
        # Ledger result nets the 80 of 'expense_other'; without the fix the
        # other-expense line is never fetched and result would read 500.
        self.assertAlmostEqual(line.ledger_result, 420.0, places=2)
        # Entered result 420 therefore ties to the books.
        self.assertAlmostEqual(line.result_residual, 0.0, places=2)
        self.assertTrue(line.result_tied)


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestEntityInterestExpenseOther(EhAccountIntegrationTestCase):
    """IFRS 12 summarised-subsidiary profit captures 'Other Expenses'."""

    def test_summarised_profit_includes_expense_other(self):
        if not _has_expense_other(self.env):
            self.skipTest(
                "account_type 'expense_other' absent on this Odoo series")
        currency = self.env.company.currency_id
        interest = self.env['eh.entity.interest'].create({
            'name': 'Sub Co', 'interest_type': 'subsidiary'})

        # A subsidiary consolidation-run member: income 500,000 (credit-
        # negative), ordinary expense 300,000 and 80,000 of 'expense_other'
        # (both debit-positive). Correct summarised profit is 120,000; dropping
        # the other-expense line would overstate it to 200,000.
        member = SimpleNamespace(ownership_pct=60.0)

        def _line(account_type, amount):
            return SimpleNamespace(
                member_id=member, kind='subsidiary_balance',
                account_id=SimpleNamespace(account_type=account_type),
                amount=amount)

        run = SimpleNamespace(
            id=999001,
            display_name='Fake Consol Run 2025',
            presentation_currency_id=currency,
            line_ids=_FakeConsolRunLines([
                _line('income', -500000.0),
                _line('expense', 300000.0),
                _line('expense_other', 80000.0),
            ]),
        )

        interest._apply_consolidation_figures(run, member)

        self.assertAlmostEqual(
            interest.summarised_revenue, 500000.0, places=2)
        # Profit = -(income + expense) = -(-500000 + 380000) = 120000; the
        # 80,000 of 'expense_other' is now subtracted.
        self.assertAlmostEqual(
            interest.summarised_profit, 120000.0, places=2)
        self.assertEqual(interest.ownership_pct, 60.0)
