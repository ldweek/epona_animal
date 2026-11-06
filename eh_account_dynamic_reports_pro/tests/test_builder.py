# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Custom report builder tests.

Covers the model (constraints, lifecycle), the formula evaluator
(security rejections, arithmetic correctness, missing identifiers), the
publish flow (registers a dynamic.report record), and end to end render
through the orchestrator (account aggregates and formula lines compose).
"""

import base64
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from odoo.addons.eh_account_dynamic_reports_pro.models.report_builder import (
    safe_eval_formula, FormulaError,
)
from odoo.addons.eh_account_dynamic_reports_pro.hooks import uninstall_hook


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestFormulaEvaluator(EhAccountIntegrationTestCase):

    def test_simple_arithmetic(self):
        self.assertAlmostEqual(safe_eval_formula("1 + 2", {}), 3.0)
        self.assertAlmostEqual(safe_eval_formula("10 - 3", {}), 7.0)
        self.assertAlmostEqual(safe_eval_formula("4 * 5", {}), 20.0)
        self.assertAlmostEqual(safe_eval_formula("9 / 2", {}), 4.5)

    def test_parentheses_and_precedence(self):
        self.assertAlmostEqual(
            safe_eval_formula("(1 + 2) * 3", {}), 9.0,
        )
        self.assertAlmostEqual(
            safe_eval_formula("1 + 2 * 3", {}), 7.0,
        )

    def test_unary_negation(self):
        self.assertAlmostEqual(safe_eval_formula("-5 + 10", {}), 5.0)
        self.assertAlmostEqual(safe_eval_formula("-(2 + 3)", {}), -5.0)

    def test_identifier_lookup(self):
        self.assertAlmostEqual(
            safe_eval_formula("a + b", {'a': 10, 'b': 20}), 30.0,
        )

    def test_missing_identifier_fails_closed(self):
        with self.assertRaisesRegex(FormulaError, 'Unknown.*b'):
            safe_eval_formula("a + b", {'a': 10})

    def test_division_by_zero_fails_closed(self):
        with self.assertRaisesRegex(FormulaError, 'divides by zero'):
            safe_eval_formula("10 / 0", {})

    def test_non_finite_constant_and_result_fail_closed(self):
        with self.assertRaisesRegex(FormulaError, 'finite numbers'):
            safe_eval_formula("1e309", {})
        with self.assertRaisesRegex(FormulaError, 'finite numbers'):
            safe_eval_formula("large * 10", {'large': 1e308})
        with self.assertRaisesRegex(FormulaError, 'finite numbers'):
            safe_eval_formula("value", {'value': float('inf')})

    def test_empty_formula_returns_zero(self):
        self.assertAlmostEqual(safe_eval_formula("", {}), 0.0)
        self.assertAlmostEqual(safe_eval_formula("   ", {}), 0.0)

    def test_function_call_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("abs(-5)", {})

    def test_attribute_access_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a.b", {})

    def test_string_constant_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("'hello'", {})

    def test_boolean_constant_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("True + 1", {})

    def test_subscript_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a[0]", {})

    def test_comprehension_rejected(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("[x for x in range(5)]", {})

    def test_syntax_error_raises(self):
        with self.assertRaises(FormulaError):
            safe_eval_formula("a +", {})


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestBuilderModel(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Builder = cls.env['eh.report.builder']
        cls.Line = cls.env['eh.report.builder.line']

    def _make_builder(self, code='my_report', name='My Report', lines=None):
        builder = self.Builder.create({
            'code': code,
            'name': name,
            'line_ids': [(0, 0, line) for line in (lines or [])],
        })
        return builder

    def test_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_builder(code='Bad-Code')
        with self.assertRaises(UserError):
            self._make_builder(code='123_starts_with_number')
        # valid codes pass
        self._make_builder(code='good_code_one', name='one')

    def test_unique_code_constraint(self):
        self._make_builder(code='unique_one')
        with self.assertRaises(Exception):
            self._make_builder(code='unique_one', name='dup')

    def test_line_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_builder(code='lc_test', lines=[
                {'name': 'L1', 'code': 'BAD-CODE', 'line_type': 'section_header'},
            ])

    def test_duplicate_line_code_within_builder_rejected(self):
        with self.assertRaises(UserError):
            self._make_builder(code='dup_lines', lines=[
                {'name': 'L1', 'code': 'shared', 'line_type': 'section_header'},
                {'name': 'L2', 'code': 'shared', 'line_type': 'section_header'},
            ])

    def test_action_publish_creates_dynamic_report(self):
        builder = self._make_builder(code='pub_one', name='Publishable')
        self.assertFalse(builder.is_published)
        builder.action_publish()
        self.assertTrue(builder.is_published)
        self.assertTrue(builder.published_report_id)
        self.assertEqual(builder.published_report_id.code, 'pub_one')
        self.assertEqual(
            builder.published_report_id.handler_model,
            'eh.account.dynamic.report.handler.builder',
        )
        self.assertEqual(
            builder.published_report_id.company_id, builder.company_id,
        )

    def test_publish_code_collision_is_friendly_and_atomic(self):
        builder = self._make_builder(
            code='existing_report_code', name='Conflicting Builder',
        )
        existing = self.env['eh.account.dynamic.report'].create({
            'code': builder.code,
            'name': 'Existing Standard Definition',
            'handler_model':
                'eh.account.dynamic.report.handler.trial_balance',
        })
        with self.assertRaisesRegex(UserError, 'already used'):
            builder.action_publish()
        self.assertFalse(builder.is_published)
        self.assertFalse(builder.published_report_id)
        self.assertTrue(existing.exists())

    def test_duplicate_allocates_next_available_code(self):
        builder = self._make_builder(code='collision_safe_copy')
        first_action = builder.action_duplicate()
        first = self.env['eh.report.builder'].browse(first_action['res_id'])
        second_action = builder.action_duplicate()
        second = self.env['eh.report.builder'].browse(second_action['res_id'])
        self.assertEqual(first.code, 'collision_safe_copy_copy')
        self.assertEqual(second.code, 'collision_safe_copy_copy_2')
        self.assertFalse(first.is_published)
        self.assertFalse(second.is_published)

    def test_published_definition_is_hidden_from_other_company(self):
        builder = self._make_builder(code='company_private_report')
        builder.action_publish()
        other_company = self.env['res.company'].create({
            'name': 'Other Builder Company',
        })
        other_user = new_test_user(
            self.env,
            login='other_builder_company_user',
            groups='base.group_user,eh_account_base.group_eh_user',
            company_id=other_company.id,
        )
        visible = self.env['eh.account.dynamic.report'].with_user(
            other_user,
        ).search([('code', '=', builder.code)])
        self.assertFalse(visible)

    def test_action_unpublish_deactivates(self):
        builder = self._make_builder(code='pub_two', name='Pub Two')
        builder.action_publish()
        report = builder.published_report_id
        builder.action_unpublish()
        self.assertFalse(builder.is_published)
        self.assertFalse(report.active)

    def test_archiving_published_builder_hides_dynamic_report(self):
        builder = self._make_builder(code='archive_published')
        builder.action_publish()
        report = builder.published_report_id
        self.assertTrue(report.active)
        builder.active = False
        self.assertFalse(report.active)
        builder.active = True
        self.assertTrue(report.active)
        builder.action_unpublish()
        builder.active = False
        builder.active = True
        self.assertFalse(report.active)

    def test_publish_idempotent_updates_existing_record(self):
        builder = self._make_builder(code='pub_three', name='Original')
        builder.action_publish()
        report_id = builder.published_report_id.id
        builder.write({
            'name': 'Updated',
            'description': 'Updated listing description',
            'sequence': 77,
        })
        self.assertEqual(builder.published_report_id.name, 'Updated')
        self.assertEqual(
            builder.published_report_id.description,
            'Updated listing description',
        )
        self.assertEqual(builder.published_report_id.sequence, 77)
        builder.action_publish()
        # Same report id, name updated.
        self.assertEqual(builder.published_report_id.id, report_id)
        self.assertEqual(builder.published_report_id.name, 'Updated')

    def test_published_code_is_immutable(self):
        builder = self._make_builder(code='stable_code', name='Stable')
        builder.action_publish()
        with self.assertRaisesRegex(UserError, 'code is immutable'):
            builder.write({'code': 'broken_new_code'})
        self.assertEqual(builder.code, 'stable_code')
        self.assertEqual(builder.published_report_id.code, 'stable_code')

    def test_public_write_cannot_forge_publish_lifecycle(self):
        builder = self._make_builder(code='server_lifecycle')
        with self.assertRaisesRegex(UserError, 'controlled by Publish'):
            builder.write({'is_published': True})
        self.assertFalse(builder.is_published)

    def test_unlink_removes_published_definition(self):
        builder = self._make_builder(code='delete_published')
        builder.action_publish()
        report = builder.published_report_id
        builder.unlink()
        self.assertFalse(report.exists())

    def test_uninstall_hook_removes_runtime_report_definitions(self):
        first = self._make_builder(code='uninstall_env_contract')
        first.action_publish()
        first_report = first.published_report_id
        Attachment = self.env['ir.attachment'].sudo()
        delivery = Attachment.create({
            'name': 'temporary-schedule-delivery.pdf',
            'datas': base64.b64encode(b'delivery'),
            'eh_report_schedule_delivery': True,
        })
        unrelated = Attachment.create({
            'name': 'unrelated.pdf',
            'datas': base64.b64encode(b'preserve'),
        })
        uninstall_hook(self.env)
        self.assertFalse(first_report.exists())
        self.assertFalse(delivery.exists())
        self.assertTrue(unrelated.exists())

        second = self._make_builder(code='uninstall_cr_contract')
        second.action_publish()
        second_report = second.published_report_id
        uninstall_hook(self.env.cr, self.env.registry)
        self.assertFalse(second_report.exists())

    def test_publish_rejects_forward_or_unknown_formula_dependency(self):
        builder = self._make_builder(code='bad_dependency', lines=[
            {
                'sequence': 10,
                'name': 'Bad total',
                'code': 'total',
                'line_type': 'formula',
                'formula': 'later + typo',
            },
            {
                'sequence': 20,
                'name': 'Later value',
                'code': 'later',
                'line_type': 'account_aggregate',
                'account_scope': 'types',
                'account_types': 'income',
            },
        ])
        with self.assertRaisesRegex(UserError, 'later line codes'):
            builder.action_publish()

    def test_publish_rejects_disallowed_formula_operator(self):
        builder = self._make_builder(code='bad_operator', lines=[
            {
                'sequence': 10,
                'name': 'Revenue',
                'code': 'revenue',
                'line_type': 'account_aggregate',
                'account_scope': 'types',
                'account_types': 'income',
            },
            {
                'sequence': 20,
                'name': 'Squared revenue',
                'code': 'squared_revenue',
                'line_type': 'formula',
                'formula': 'revenue ** 2',
            },
        ])
        with self.assertRaisesRegex(UserError, 'disallowed construct'):
            builder.action_publish()

    def test_publish_allows_runtime_dependent_division(self):
        builder = self._make_builder(code='valid_division', lines=[
            {
                'sequence': 10, 'name': 'Profit', 'code': 'profit',
                'line_type': 'account_aggregate',
                'account_scope': 'types', 'account_types': 'income',
            },
            {
                'sequence': 20, 'name': 'Revenue', 'code': 'revenue',
                'line_type': 'account_aggregate',
                'account_scope': 'types', 'account_types': 'income',
            },
            {
                'sequence': 30, 'name': 'COGS', 'code': 'cogs',
                'line_type': 'account_aggregate',
                'account_scope': 'types',
                'account_types': 'expense_direct_cost',
            },
            {
                'sequence': 40, 'name': 'Margin', 'code': 'margin',
                'line_type': 'formula',
                'formula': 'profit / (revenue - cogs)',
            },
        ])
        self.assertTrue(builder.action_publish())

    def test_publish_rejects_incomplete_or_unknown_aggregate_selector(self):
        missing = self._make_builder(code='missing_selector', lines=[{
            'name': 'Missing selector',
            'code': 'missing',
            'line_type': 'account_aggregate',
            'account_scope': 'types',
            'account_types': '',
        }])
        with self.assertRaisesRegex(UserError, 'at least one account type'):
            missing.action_publish()

        unknown = self._make_builder(code='unknown_selector', lines=[{
            'name': 'Typo selector',
            'code': 'typo',
            'line_type': 'account_aggregate',
            'account_scope': 'types',
            'account_types': 'expnese',
        }])
        with self.assertRaisesRegex(UserError, 'unknown account types'):
            unknown.action_publish()

        self.assertTrue(self.Line._fields['sign'].required)
        self.assertTrue(self.Line._fields['account_scope'].required)

    def test_formula_figure_type_controls_cell_and_total_semantics(self):
        builder = self._make_builder(code='ratio_builder', lines=[{
            'sequence': 10,
            'name': 'Margin',
            'code': 'margin',
            'line_type': 'formula',
            'formula': '1 / 4',
            'figure_type': 'percentage',
        }])
        builder.action_publish()
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ].with_context(eh_report_code=builder.code)
        payload = handler.compute({
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'primary_company_id': self.company.id,
            'posted_only': True,
            'show_zero': True,
        })
        margin = next(
            line for line in payload['lines']
            if (line.get('meta') or {}).get('builder_line_code') == 'margin'
        )
        self.assertEqual(margin['columns'][0]['value'], 0.25)
        self.assertEqual(margin['columns'][0]['figure_type'], 'percentage')
        self.assertEqual(
            payload['meta']['total_figure_types']['margin'], 'percentage',
        )

    def test_account_code_prefix_wildcards_are_literal(self):
        handler = self.env['eh.account.dynamic.report.handler.builder']
        self.assertEqual(
            handler._escape_like_prefix('10%_\\'), '10\\%\\_\\\\',
        )

    def test_builder_payload_preserves_currency_precision(self):
        builder = self._make_builder(code='precise_builder', lines=[{
            'sequence': 10,
            'name': 'Precise amount',
            'code': 'precise_amount',
            'line_type': 'account_aggregate',
            'account_scope': 'types',
            'account_types': 'income',
        }])
        line = builder.line_ids
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ]
        rendered = handler._render_account_aggregate(line, 1.234)
        self.assertEqual(rendered['columns'][0]['value'], 1.234)
        self.assertEqual(
            handler._compute_totals({'precise_amount': 1.234}),
            {'precise_amount': 1.234},
        )

    def test_published_builder_rejects_dependency_breaking_edit(self):
        builder = self._make_builder(code='edit_dependency', lines=[
            {
                'sequence': 10,
                'name': 'Revenue',
                'code': 'revenue',
                'line_type': 'account_aggregate',
                'account_scope': 'types',
                'account_types': 'income',
            },
            {
                'sequence': 20,
                'name': 'Total',
                'code': 'total',
                'line_type': 'formula',
                'formula': 'revenue',
            },
        ])
        builder.action_publish()
        total = builder.line_ids.filtered(lambda line: line.code == 'total')
        with self.assertRaisesRegex(UserError, 'unknown or later'):
            total.write({'formula': 'missing_code'})

    def test_action_open_viewer_requires_published(self):
        builder = self._make_builder(code='not_pub', name='Not Pub')
        with self.assertRaises(UserError):
            builder.action_open_viewer()

    def test_action_open_viewer_returns_client_action(self):
        builder = self._make_builder(code='pub_view', name='Pub View')
        builder.action_publish()
        action = builder.action_open_viewer()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'eh_account_dynamic_report')
        self.assertEqual(action['context']['report_code'], 'pub_view')


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestBuilderRender(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Builder = cls.env['eh.report.builder']
        # Seed activity in the period.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': 300.0},
                {'account': cls.account_cash, 'credit': 300.0},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _make_pl_builder(self):
        builder = self.Builder.create({
            'code': 'simple_pl',
            'name': 'Simple P&L',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Income',
                    'line_type': 'section_header',
                }),
                (0, 0, {
                    'sequence': 20,
                    'name': 'Revenue',
                    'code': 'revenue',
                    'line_type': 'account_aggregate',
                    'account_scope': 'types',
                    'account_types': 'income,income_other',
                    'sign': '-',  # income is credit, flip to positive
                }),
                (0, 0, {
                    'sequence': 30,
                    'name': 'Expenses',
                    'line_type': 'section_header',
                }),
                (0, 0, {
                    'sequence': 40,
                    'name': 'Operating Expenses',
                    'code': 'opex',
                    'line_type': 'account_aggregate',
                    'account_scope': 'types',
                    'account_types': 'expense,expense_direct_cost',
                    'sign': '+',
                }),
                (0, 0, {
                    'sequence': 50,
                    'name': 'Net Profit',
                    'code': 'net_profit',
                    'line_type': 'formula',
                    'formula': 'revenue - opex',
                    'is_section_total': True,
                    'level': 0,
                }),
            ],
        })
        builder.action_publish()
        return builder

    def test_render_through_orchestrator(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        self.assertFalse(result['from_cache'])
        self.assertGreater(len(result['lines']), 0)

    def test_account_aggregate_values(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        # Find the revenue line by builder line code in meta.
        revenue_line = next(
            (l for l in result['lines']
             if (l.get('meta') or {}).get('builder_line_code') == 'revenue'),
            None,
        )
        self.assertIsNotNone(revenue_line)
        amount_cell = next(
            c for c in revenue_line['columns']
            if c['expression_label'] == 'amount'
        )
        # Revenue 1000 with sign flip = +1000.
        self.assertAlmostEqual(amount_cell['value'], 1000.0, places=2)

    def test_formula_line_combines_others(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        net_profit_line = next(
            (l for l in result['lines']
             if (l.get('meta') or {}).get('builder_line_code') == 'net_profit'),
            None,
        )
        self.assertIsNotNone(net_profit_line)
        amount_cell = next(
            c for c in net_profit_line['columns']
            if c['expression_label'] == 'amount'
        )
        # 1000 revenue - 300 opex = 700.
        self.assertAlmostEqual(amount_cell['value'], 700.0, places=2)

    def test_totals_dict_includes_named_lines(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        totals = result['totals']
        self.assertIn('revenue', totals)
        self.assertIn('opex', totals)
        self.assertIn('net_profit', totals)

    def test_show_zero_hides_zero_aggregates(self):
        # No revenue activity for a different builder.
        empty_builder = self.Builder.create({
            'code': 'empty_pl',
            'name': 'Empty PL',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Empty Account',
                    'code': 'empty',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': '9999',
                    'sign': '+',
                }),
            ],
        })
        empty_builder.action_publish()
        result = empty_builder.published_report_id.render({
            **self.options, 'show_zero': False,
        })
        empty_line = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'empty'
        ]
        self.assertEqual(empty_line, [])
        # With show_zero true the line appears.
        result2 = empty_builder.published_report_id.render({
            **self.options, 'show_zero': True,
        })
        empty_line2 = [
            l for l in result2['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'empty'
        ]
        self.assertEqual(len(empty_line2), 1)

    def test_section_header_appears(self):
        builder = self._make_pl_builder()
        result = builder.published_report_id.render(self.options)
        headers = [
            l for l in result['lines']
            if (l.get('meta') or {}).get('kind') == 'section_header'
        ]
        self.assertGreaterEqual(len(headers), 2)

    def test_xlsx_export_works(self):
        builder = self._make_pl_builder()
        content = builder.published_report_id.render_xlsx(self.options)
        self.assertEqual(content[:2], b'PK')

    def test_account_codes_aggregate(self):
        builder = self.Builder.create({
            'code': 'codes_test',
            'name': 'Codes Test',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Cash and Receivables',
                    'code': 'cash_ar',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': '1',  # both 1000 and 1100 start with 1
                    'sign': '+',
                }),
            ],
        })
        builder.action_publish()
        original_execute = MoveLineQuery.execute
        calls = []

        def counted(query):
            calls.append(1)
            return original_execute(query)

        with patch.object(MoveLineQuery, 'execute', counted):
            result = builder.published_report_id.render(self.options)
        self.assertEqual(calls, [1])
        line = next(
            l for l in result['lines']
            if (l.get('meta') or {}).get('builder_line_code') == 'cash_ar'
        )
        amount = next(
            c['value'] for c in line['columns']
            if c['expression_label'] == 'amount'
        )
        # Cash debited 1000, then credited 300 = 700 closing on cash account.
        self.assertAlmostEqual(amount, 700.0, places=2)

    def test_account_aggregate_basis_distinguishes_movement_and_closing(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200.0},
                {'account': self.account_equity, 'credit': 200.0},
            ],
            date=fields.Date.from_string('2025-12-31'),
        )
        builder = self.Builder.create({
            'code': 'aggregate_basis_test',
            'name': 'Aggregate Basis Test',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Cash movement',
                    'code': 'cash_movement',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': self.account_cash.code,
                    'aggregate_basis': 'period',
                    'sign': '+',
                }),
                (0, 0, {
                    'sequence': 20,
                    'name': 'Cash closing balance',
                    'code': 'cash_closing',
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': self.account_cash.code,
                    'aggregate_basis': 'closing',
                    'sign': '+',
                }),
            ],
        })
        builder.action_publish()
        original_execute = MoveLineQuery.execute
        basis_calls = []

        def counted_basis(query):
            basis_calls.append(1)
            return original_execute(query)

        with patch.object(MoveLineQuery, 'execute', counted_basis):
            result = builder.published_report_id.render(self.options)
        self.assertEqual(basis_calls, [1])
        values = {
            (line.get('meta') or {}).get('builder_line_code'):
            next(col['value'] for col in line['columns']
                 if col['expression_label'] == 'amount')
            for line in result['lines']
            if (line.get('meta') or {}).get('builder_line_code')
        }
        self.assertAlmostEqual(values['cash_movement'], 700.0, places=2)
        self.assertAlmostEqual(values['cash_closing'], 900.0, places=2)

        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ].with_context(eh_report_code=builder.code)
        closing_line = builder.line_ids.filtered(
            lambda line: line.code == 'cash_closing'
        )
        action = handler.get_drilldown_action(
            self.options,
            'line-%s' % closing_line.id,
        )
        drilldown = self.env['account.move.line'].search(action['domain'])
        self.assertIn(
            fields.Date.from_string('2025-12-31'),
            drilldown.mapped('date'),
        )

    def test_all_aggregate_lines_share_one_account_balance_query(self):
        builder = self.Builder.create({
            'code': 'batched_aggregates',
            'name': 'Batched Aggregates',
            'line_ids': [
                (0, 0, {
                    'sequence': index * 10,
                    'name': 'Scope %s' % index,
                    'code': 'scope_%s' % index,
                    'line_type': 'account_aggregate',
                    'account_scope': 'codes',
                    'account_codes': '%04d' % index,
                    'sign': '+',
                })
                for index in range(1, 61)
            ],
        })
        builder.action_publish()
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ].with_context(eh_report_code=builder.code)
        original_execute = MoveLineQuery.execute
        calls = []
        built_queries = []

        def counted(query):
            calls.append(1)
            built_queries.append(query.build())
            return original_execute(query)

        with patch.object(MoveLineQuery, 'execute', counted):
            result = handler.compute(dict(self.options, show_zero=True))
        self.assertEqual(len(calls), 1)
        self.assertIn('LIKE', built_queries[0].code)
        self.assertIn(' OR ', built_queries[0].code)
        self.assertIn('0001%', built_queries[0].params)
        self.assertIn('0060%', built_queries[0].params)
        self.assertEqual(
            len([
                line for line in result['lines']
                if (line.get('meta') or {}).get('kind')
                == 'account_aggregate'
            ]),
            60,
        )

    def test_legacy_empty_aggregate_scope_never_scans_ledger(self):
        builder = self.Builder.create({
            'code': 'legacy_empty_scope',
            'name': 'Legacy Empty Scope',
            'line_ids': [(0, 0, {
                'sequence': 10,
                'name': 'Malformed legacy aggregate',
                'code': 'malformed',
                'line_type': 'account_aggregate',
                'account_scope': 'types',
                'account_types': 'income',
                'sign': '+',
            })],
        })
        line = builder.line_ids
        # Reproduce a pre-validation/direct-SQL legacy row. Current ORM
        # correctly rejects this shape, but runtime must remain bounded when
        # an upgraded database contains it.
        self.env.cr.execute(
            "UPDATE eh_report_builder_line SET account_types = '' "
            "WHERE id = %s",
            [line.id],
        )
        line.invalidate_recordset(['account_types'])
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ].with_context(eh_report_code=builder.code)

        with patch.object(
            MoveLineQuery, 'execute',
            side_effect=AssertionError('empty scope must not query ledger'),
        ):
            values = handler._evaluate_account_aggregates(
                line,
                options=self.options,
                date_from=fields.Date.from_string('2026-01-01'),
                date_to=fields.Date.from_string('2026-12-31'),
                company_ids=[self.company.id],
                posted_only=True,
            )
        self.assertEqual(values, {line.id: 0.0})

    def test_monetary_formula_uses_company_currency_rounding_contract(self):
        builder = self.Builder.create({
            'code': 'formula_rounding',
            'name': 'Formula Rounding',
            'line_ids': [
                (0, 0, {
                    'sequence': 10,
                    'name': 'Revenue',
                    'code': 'revenue',
                    'line_type': 'account_aggregate',
                    'account_scope': 'types',
                    'account_types': 'income,income_other',
                    'sign': '-',
                }),
                (0, 0, {
                    'sequence': 20,
                    'name': 'One Third',
                    'code': 'one_third',
                    'line_type': 'formula',
                    'formula': 'revenue / 3',
                    'figure_type': 'monetary',
                }),
            ],
        })
        builder.action_publish()
        result = builder.published_report_id.render(dict(
            self.options, use_cache=False,
        ))
        formula = next(
            line for line in result['lines']
            if (line.get('meta') or {}).get('builder_line_code')
            == 'one_third'
        )
        value = next(
            column['value'] for column in formula['columns']
            if column['expression_label'] == 'amount'
        )
        self.assertEqual(
            value, self.company.currency_id.round(1000.0 / 3.0),
        )

    def test_analytic_filter_and_drilldown_use_identical_scope(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Builder Plan',
        })
        selected = self.env['account.analytic.account'].create({
            'name': 'Selected',
            'plan_id': plan.id,
            'company_id': self.company.id,
        })
        excluded = self.env['account.analytic.account'].create({
            'name': 'Excluded',
            'plan_id': plan.id,
            'company_id': self.company.id,
        })

        def post_expense(amount, analytic):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-08-01',
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_expense.id,
                        'debit': amount,
                        'analytic_distribution': {str(analytic.id): 100.0},
                    }),
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'credit': amount,
                    }),
                ],
            })
            move.action_post()
            return move.line_ids.filtered(
                lambda item: item.account_id == self.account_expense,
            ).ensure_one()

        selected_line = post_expense(120.0, selected)
        excluded_line = post_expense(340.0, excluded)
        builder = self._make_pl_builder()
        options = dict(
            self.options,
            analytic_account_ids=[selected.id],
            use_cache=False,
        )
        result = builder.published_report_id.render(options)
        expense_row = next(
            line for line in result['lines']
            if (line.get('meta') or {}).get('builder_line_code') == 'opex'
        )
        amount = next(
            column['value'] for column in expense_row['columns']
            if column['expression_label'] == 'amount'
        )
        self.assertAlmostEqual(amount, 120.0, places=2)

        action = builder.published_report_id.get_drilldown_for_line(
            dict(options, _eh_column_expression='amount'),
            expense_row['id'],
        )
        ids = set(self.env['account.move.line'].search(action['domain']).ids)
        self.assertIn(selected_line.id, ids)
        self.assertNotIn(excluded_line.id, ids)

    def test_multi_company_target_currency_fails_before_ledger_query(self):
        builder = self._make_pl_builder()
        other_company = self.env['res.company'].create({
            'name': 'Builder Same Currency Company',
            'currency_id': self.company.currency_id.id,
        })
        target_currency = self.env['res.currency'].create({
            'name': 'BTX',
            'symbol': 'BTX',
            'rounding': 0.01,
        })
        handler = self.env[
            'eh.account.dynamic.report.handler.builder'
        ].with_context(eh_report_code=builder.code)

        with patch.object(
            MoveLineQuery, 'execute',
            side_effect=AssertionError('ledger query must not execute'),
        ), self.assertRaisesRegex(UserError, 'Select one company'):
            handler.compute(dict(
                self.options,
                company_ids=[self.company.id, other_company.id],
                presentation_currency_id=target_currency.id,
            ))
