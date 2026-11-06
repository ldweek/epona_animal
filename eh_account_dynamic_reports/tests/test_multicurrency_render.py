# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""End-to-end presentation-currency ownership tests.

Every assertion enters through ``eh.account.dynamic.report.render``. This
guards both sides of the handler/orchestrator contract: SQL converts each
company's ledger values before aggregation, while the orchestrator formats
the chosen currency without converting those values a second time.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestMultiCurrencyRender(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.company
        cls.currency_a = cls.company_a.currency_id
        cls.currency_b = cls.env['res.currency'].create({
            'name': 'MCR', 'symbol': 'M', 'rounding': 0.01,
        })
        cls.env['res.currency.rate'].create({
            'currency_id': cls.currency_b.id,
            'name': '2020-01-01',
            'rate': 4.0,
        })
        cls.company_b = cls.env['res.company'].create({
            'name': 'Multi Currency Render B',
            'currency_id': cls.currency_b.id,
        })
        # Company B needs an explicit dated rate for target currency A.
        # Core used to hide this missing side with 1.0; report translation now
        # requires every source company to prove its target rate at cutoff.
        cls.env['res.currency.rate'].create({
            'currency_id': cls.currency_a.id,
            'company_id': cls.company_b.id,
            'name': '2020-01-01',
            'rate': 0.25,
        })
        cls.env.user.company_ids = [(4, cls.company_b.id)]
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

        cls.journal_b = cls.env['account.journal'].create({
            'name': 'Multi Currency Journal B',
            'code': 'MCRB',
            'type': 'general',
            'company_id': cls.company_b.id,
        })
        cls.cash_b = cls.env['account.account'].create({
            'code': '1004B',
            'name': 'Cash MCR B',
            'account_type': 'asset_cash',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })
        cls.equity_b = cls.env['account.account'].create({
            'code': '3004B',
            'name': 'Equity MCR B',
            'account_type': 'equity',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })
        cls.revenue_b = cls.env['account.account'].create({
            'code': '4004B',
            'name': 'Revenue MCR B',
            'account_type': 'income',
            'company_ids': [(6, 0, [cls.company_b.id])],
        })

        opening_date = fields.Date.from_string('2025-12-15')
        period_date = fields.Date.from_string('2026-06-15')
        cls.post_balanced_move([
            {'account': cls.account_cash, 'debit': 800.0},
            {'account': cls.account_equity, 'credit': 800.0},
        ], date=opening_date)
        cls.post_balanced_move([
            {'account': cls.cash_b, 'debit': 800.0},
            {'account': cls.equity_b, 'credit': 800.0},
        ], journal=cls.journal_b, date=opening_date)
        cls.post_balanced_move([
            {'account': cls.account_cash, 'debit': 1000.0},
            {'account': cls.account_revenue, 'credit': 1000.0},
        ], date=period_date)
        cls.post_balanced_move([
            {'account': cls.cash_b, 'debit': 1000.0},
            {'account': cls.revenue_b, 'credit': 1000.0},
        ], journal=cls.journal_b, date=period_date)

        cls.reports = {}
        models = {
            'trial_balance':
                'eh.account.dynamic.report.handler.trial_balance',
            'profit_and_loss':
                'eh.account.dynamic.report.handler.profit_and_loss',
            'balance_sheet':
                'eh.account.dynamic.report.handler.balance_sheet',
            'general_ledger':
                'eh.account.dynamic.report.handler.general_ledger',
        }
        for code, model in models.items():
            report = cls.env['eh.account.dynamic.report'].search(
                [('code', '=', code)], limit=1)
            if not report:
                report = cls.env['eh.account.dynamic.report'].create({
                    'code': code,
                    'name': code.replace('_', ' ').title(),
                    'handler_model': model,
                })
            cls.reports[code] = report

    def _options(self, target, **extra):
        values = {
            'date': {
                'date_from': '2026-01-01',
                'date_to': '2026-12-31',
            },
            'company_ids': [self.company_a.id, self.company_b.id],
            'posted_only': True,
            'show_zero': False,
            'hierarchical_groups': False,
            'presentation_currency_id': target.id,
        }
        values.update(extra)
        return values

    def _converted(self, amount, company, target):
        return company.currency_id._convert(
            amount,
            target,
            company,
            fields.Date.from_string('2026-12-31'),
        )

    @staticmethod
    def _line(payload, line_id):
        return next(line for line in payload['lines'] if line['id'] == line_id)

    @staticmethod
    def _column(line, label):
        return next(
            column['value'] for column in line['columns']
            if column['expression_label'] == label
        )

    def _assert_core_reports(self, target):
        a_period = self._converted(1000.0, self.company_a, target)
        b_period = self._converted(1000.0, self.company_b, target)
        a_opening = self._converted(800.0, self.company_a, target)
        b_opening = self._converted(800.0, self.company_b, target)
        expected_period = round(a_period + b_period, 2)
        expected_assets = round(
            a_opening + b_opening + a_period + b_period, 2)

        pnl = self.reports['profit_and_loss'].render(
            self._options(target), use_cache=False)
        self.assertAlmostEqual(
            self._column(self._line(pnl, 'net_profit'), 'amount'),
            expected_period, places=2)
        self.assertEqual(pnl['currency']['id'], target.id)
        self.assertTrue(
            pnl['meta'].get('presentation_currency_converted'))

        balance_sheet = self.reports['balance_sheet'].render(
            self._options(target), use_cache=False)
        self.assertAlmostEqual(
            balance_sheet['totals']['assets'], expected_assets, places=2)
        self.assertAlmostEqual(
            balance_sheet['totals']['balance_check'], 0.0, places=2)
        self.assertEqual(balance_sheet['currency']['id'], target.id)

        trial_balance = self.reports['trial_balance'].render(
            self._options(target), use_cache=False)
        self.assertAlmostEqual(
            trial_balance['totals']['period_debit'],
            expected_period, places=2)
        self.assertAlmostEqual(
            trial_balance['totals']['opening_debit'],
            round(a_opening + b_opening, 2), places=2)
        self.assertEqual(trial_balance['currency']['id'], target.id)

        horizontal = self.reports['profit_and_loss'].render(
            self._options(target, horizontal_group_by='company'),
            use_cache=False,
        )
        net = self._line(horizontal, 'net_profit')
        company_columns = {
            column['name']: column['expression_label']
            for column in horizontal['columns']
            if column['expression_label'].startswith('group_')
        }
        self.assertAlmostEqual(
            self._column(net, company_columns[self.company_a.name]),
            a_period, places=2)
        self.assertAlmostEqual(
            self._column(net, company_columns[self.company_b.name]),
            b_period, places=2)
        self.assertAlmostEqual(
            self._column(net, 'total'), expected_period, places=2)

    def test_render_to_company_a_currency(self):
        self._assert_core_reports(self.currency_a)

    def test_render_to_company_b_currency(self):
        self._assert_core_reports(self.currency_b)

    def test_general_ledger_eager_lazy_and_expand(self):
        target = self.currency_a
        options = self._options(target)
        expected_opening = round(
            self._converted(800.0, self.company_b, target), 2)
        expected_period = round(
            self._converted(1000.0, self.company_b, target), 2)

        eager = self.reports['general_ledger'].render(
            options, use_cache=False)
        eager_lines = [
            line for line in eager['lines']
            if (line.get('meta') or {}).get('account_id') == self.cash_b.id
        ]
        opening = next(
            line for line in eager_lines
            if (line.get('meta') or {}).get('kind') == 'opening_balance')
        entry = next(
            line for line in eager_lines
            if (line.get('meta') or {}).get('kind') == 'aml')
        total = next(
            line for line in eager_lines
            if (line.get('meta') or {}).get('kind') == 'account_total')
        self.assertAlmostEqual(
            self._column(opening, 'balance'), expected_opening, places=2)
        self.assertAlmostEqual(
            self._column(entry, 'debit'), expected_period, places=2)
        self.assertAlmostEqual(
            self._column(total, 'balance'),
            expected_opening + expected_period, places=2)

        lazy_options = self._options(target, lazy_expand=True)
        lazy = self.reports['general_ledger'].render(
            lazy_options, use_cache=False)
        lazy_total = next(
            line for line in lazy['lines']
            if (line.get('meta') or {}).get('kind') == 'account_total'
            and (line.get('meta') or {}).get('account_id') == self.cash_b.id
        )
        self.assertAlmostEqual(
            self._column(lazy_total, 'balance'),
            expected_opening + expected_period, places=2)

        expanded = self.reports['general_ledger'].expand_line(
            lazy_options, 'account-%s' % self.cash_b.id,
            offset=0, limit=10,
        )
        self.assertEqual(expanded['total_count'], 1)
        child = expanded['child_lines'][0]
        self.assertAlmostEqual(
            self._column(child, 'debit'), expected_period, places=2)
        self.assertAlmostEqual(
            self._column(child, 'balance'),
            expected_opening + expected_period, places=2)
