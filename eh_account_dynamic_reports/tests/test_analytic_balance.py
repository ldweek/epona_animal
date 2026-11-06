# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Analytic Balance handler tests.

Regression cover for the LATERAL-join rewrite: PostgreSQL rejects a
SUM() that contains a set-returning function, so the original
`SUM(... jsonb_each_text(...) ...)` blew up at execute time. The fix
expands the distribution via CROSS JOIN LATERAL so the aggregate
operates on plain column references.
"""

from unittest.mock import patch

import odoo
from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'post_install', '-at_install')
class TestAnalyticBalanceHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.analytic_balance'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'analytic_balance')], limit=1,
        )

        if 'account.analytic.plan' not in cls.env:
            cls.has_analytic = False
            return
        cls.has_analytic = True

        cls.plan = cls.env['account.analytic.plan'].create({'name': 'Test Plan'})
        cls.analytic_a = cls.env['account.analytic.account'].create({
            'name': 'Analytic A',
            'plan_id': cls.plan.id,
        })
        cls.analytic_b = cls.env['account.analytic.account'].create({
            'name': 'Analytic B',
            'plan_id': cls.plan.id,
        })

        # 60/40 split on a revenue line, full allocation on an expense line.
        cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-06-15'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_revenue.id,
                    'credit': 1000.0,
                    'name': 'rev split 60/40',
                    'analytic_distribution': {
                        str(cls.analytic_a.id): 60.0,
                        str(cls.analytic_b.id): 40.0,
                    },
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'debit': 1000.0,
                    'name': 'cash leg',
                }),
            ],
        }).action_post()

        cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
            'date': fields.Date.from_string('2026-07-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': cls.account_expense.id,
                    'debit': 250.0,
                    'name': 'expense full',
                    'analytic_distribution': {
                        str(cls.analytic_a.id): 100.0,
                    },
                }),
                (0, 0, {
                    'account_id': cls.account_cash.id,
                    'credit': 250.0,
                    'name': 'cash leg',
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

    def test_compute_does_not_crash_with_distribution(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        payload = self.handler.compute(self.options)
        self.assertIn('lines', payload)
        self.assertIn('columns', payload)
        self.assertEqual(len(payload['columns']), 2)

    def test_kwd_precision_keeps_one_fils_analytic_total(self):
        kwd = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'KWD')], limit=1,
        )
        self.assertTrue(kwd)
        section = self.handler._section_total_line(
            "KWD Total", 0.001, 'kwd', currency=kwd,
        )
        computed = self.handler._computed_line(
            'kwd-total', "KWD Net", 0.001, currency=kwd,
        )
        self.assertAlmostEqual(
            section['columns'][0]['value'], 0.001, places=3,
        )
        self.assertAlmostEqual(
            computed['columns'][0]['value'], 0.001, places=3,
        )

    def test_allocation_weights_apply(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        payload = self.handler.compute(self.options)
        # Ledger convention is retained: A receives -600 revenue and +250
        # expense, while B receives -400 revenue and no expense. Deliberately
        # distinct net amounts ensure the allocation assertion is non-vacuous.
        rows_by_id = {}
        for line in payload['lines']:
            meta = line.get('meta') or {}
            if meta.get('kind'):
                continue
            aid = meta.get('analytic_account_id')
            if aid:
                rows_by_id[aid] = line['columns'][0]['value']
        self.assertAlmostEqual(rows_by_id.get(self.analytic_a.id, 0.0), -350.0, places=2)
        self.assertAlmostEqual(rows_by_id.get(self.analytic_b.id, 0.0), -400.0, places=2)

    def test_account_type_filter_intersects_analytic_scope(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")

        def amounts_for(account_type):
            payload = self.handler.compute(dict(
                self.options,
                account_type_ids=[account_type],
            ))
            return {
                (line.get('meta') or {}).get('analytic_account_id'):
                    line['columns'][0]['value']
                for line in payload['lines']
                if (line.get('meta') or {}).get('analytic_account_id')
            }

        income = amounts_for('income')
        expense = amounts_for('expense')
        self.assertAlmostEqual(income[self.analytic_a.id], -600.0, places=2)
        self.assertAlmostEqual(income[self.analytic_b.id], -400.0, places=2)
        self.assertAlmostEqual(expense[self.analytic_a.id], 250.0, places=2)
        self.assertNotIn(self.analytic_b.id, expense)

    def test_visible_analytic_account_and_plan_filters_are_applied(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        by_account = self.handler.compute(dict(
            self.options,
            analytic_account_ids=[self.analytic_b.id],
        ))
        account_ids = {
            (line.get('meta') or {}).get('analytic_account_id')
            for line in by_account['lines']
            if (line.get('meta') or {}).get('analytic_account_id')
        }
        self.assertEqual(account_ids, {self.analytic_b.id})

        other_plan = self.env['account.analytic.plan'].create({
            'name': 'Filtered Other Plan',
        })
        by_plan = self.handler.compute(dict(
            self.options,
            analytic_plan_ids=[other_plan.id],
        ))
        self.assertFalse([
            line for line in by_plan['lines']
            if (line.get('meta') or {}).get('analytic_account_id')
        ])

    def test_inaccessible_analytic_rows_and_amounts_are_omitted(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        module = (
            'odoo.addons.eh_account_dynamic_reports.models.'
            'analytic_balance._readable'
        )
        with patch(module, side_effect=[
            self.analytic_a,
            self.plan,
        ]):
            rows = self.handler._fetch_analytic_rows(
                [self.company.id], '2026-01-01', '2026-12-31', True, {},
            )
        ids = {row['analytic_id'] for row in rows}
        self.assertIn(self.analytic_a.id, ids)
        self.assertNotIn(self.analytic_b.id, ids)

    def test_cross_plan_composite_key_splits_without_dividing_percentage(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        second_plan = self.env['account.analytic.plan'].create({
            'name': 'Second Test Plan',
        })
        analytic_c = self.env['account.analytic.account'].create({
            'name': 'Analytic C',
            'plan_id': second_plan.id,
        })
        composite_key = f'{self.analytic_a.id},{analytic_c.id}'
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.from_string('2026-08-01'),
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'debit': 400.0,
                    'name': 'cross-plan composite allocation',
                    'analytic_distribution': {
                        (
                            composite_key
                            if odoo.release.version_info[0] >= 17
                            else str(self.analytic_a.id)
                        ): 25.0,
                    },
                }),
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'credit': 400.0,
                    'name': 'cash leg',
                }),
            ],
        })
        move.action_post()
        if odoo.release.version_info[0] == 16:
            analytic_line = move.line_ids.filtered(
                lambda line: line.account_id == self.account_expense,
            )
            self.env.cr.execute(
                "UPDATE account_move_line "
                "SET analytic_distribution = jsonb_build_object(%s, %s) "
                "WHERE id = %s",
                [composite_key, 25.0, analytic_line.id],
            )

        rows = self.handler._fetch_analytic_rows(
            [self.company.id], '2026-08-01', '2026-08-01', True, {},
        )
        amounts = {row['analytic_id']: row['amount'] for row in rows}
        # A cross-plan key means both analytic dimensions receive the same
        # 25% allocation. Splitting the key must not divide that value by two.
        self.assertAlmostEqual(
            abs(amounts[self.analytic_a.id]), 100.0, places=2,
        )
        self.assertAlmostEqual(
            abs(amounts[analytic_c.id]), 100.0, places=2,
        )
        self.assertEqual(
            amounts[self.analytic_a.id], amounts[analytic_c.id],
        )

    def test_render_via_orchestrator(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        if not self.report:
            self.skipTest("Analytic balance report record not seeded.")
        payload = self.report.render(self.options)
        self.assertIn('lines', payload)
        self.assertIn('execution_id', payload)

    def test_pdf_export_renders(self):
        if not self.has_analytic:
            self.skipTest("Analytic addon not installed in this build.")
        if not self.report:
            self.skipTest("Analytic balance report record not seeded.")
        content = self.report.render_pdf(self.options)
        self.assertIsInstance(content, (bytes, bytearray))
        self.assertGreater(len(content), 100)
