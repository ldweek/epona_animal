# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the Year End medium/low audit tranche."""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_year_end.models.year_end_sequence import (
    ensure_company_sequence,
    next_company_reference,
)


@tagged(
    'eh_account_year_end', 'eh_year_end_medium_low', 'integration',
    'post_install', '-at_install',
)
class TestYearEndMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.Map = cls.env['eh.aoci.reserve.map']
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)
        cls.retained = cls._ensure_account(
            cls.env, '3197', 'Year End Backlog Retained Earnings', 'equity',
        )
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    def _run(self, env=None, **overrides):
        env = env or self.env
        values = {
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': env.company.id,
            'journal_id': self.journal_misc.with_env(env).id,
            'retained_earnings_account_id': self.retained.with_env(env).id,
            'lock_after_post': False,
            'no_lock_reason': 'focused backlog regression',
        }
        values.update(overrides)
        return env['eh.year.end.run'].create(values)

    def _post_pl(self, amount=100.0, on_date=None):
        return self.post_balanced_move([
            {'account': self.account_revenue, 'credit': amount},
            {'account': self.account_cash, 'debit': amount},
        ], date=on_date or date(2026, 6, 30))

    def test_three_decimal_residual_and_exact_reversal_legs(self):
        self.company.currency_id.sudo().write({'rounding': 0.001})
        self.assertEqual(self.company.currency_id.decimal_places, 3)
        self._post_pl(amount=0.004)

        run = self._run()
        run.action_compute()
        revenue = run.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        )
        self.assertEqual(len(revenue), 1)
        self.assertAlmostEqual(revenue.income_balance, 0.004, places=3)
        run.action_post()
        close_by_account = {
            line.account_id.id: (line.debit, line.credit)
            for line in run.move_id.line_ids
        }
        run.action_reverse()
        reverse_by_account = {
            line.account_id.id: (line.debit, line.credit)
            for line in run.reversal_move_id.line_ids
        }
        self.assertEqual(set(close_by_account), set(reverse_by_account))
        for account_id, (debit, credit) in close_by_account.items():
            reverse_debit, reverse_credit = reverse_by_account[account_id]
            self.assertAlmostEqual(reverse_debit, credit, places=3)
            self.assertAlmostEqual(reverse_credit, debit, places=3)
        self.assertEqual(run.reversal_move_id.date, date(2027, 1, 1))

    def test_configured_fiscal_window_and_onchange(self):
        transient = self.Run.new({
            'company_id': self.company.id,
            'fiscal_year_end': self.fy_end,
        })
        transient._onchange_fiscal_year_end_derive_start()
        self.assertEqual(transient.fiscal_year_start, self.fy_start)

        with self.assertRaisesRegex(ValidationError, 'configured fiscal year'):
            self._run(fiscal_year_start=date(2026, 2, 1))

        self.company.sudo().write({
            'fiscalyear_last_day': 30,
            'fiscalyear_last_month': '6',
        })
        non_calendar = self._run(
            fiscal_year_start=date(2025, 7, 1),
            fiscal_year_end=date(2026, 6, 30),
        )
        self.assertEqual(non_calendar.fiscal_year_start, date(2025, 7, 1))

    def test_computed_reference_fields_are_frozen_with_precise_error(self):
        self._post_pl()
        run = self._run()
        run.action_compute()
        with self.assertRaisesRegex(UserError, 'journal_id.*computed'):
            run.write({'journal_id': self.journal_misc.id})
        with self.assertRaisesRegex(
            UserError, 'retained_earnings_account_id.*computed',
        ):
            run.write({
                'retained_earnings_account_id': self.retained.id,
            })

    def test_prior_period_pl_must_be_closed_account_by_account(self):
        self._post_pl(amount=80.0, on_date=date(2025, 6, 30))
        self._post_pl(amount=100.0)
        run = self._run()
        run.action_compute()
        with self.assertRaisesRegex(UserError, 'before fiscal year start'):
            run.action_post()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.move_id)

    def test_oci_period_balances_are_aggregated_without_orm_line_search(self):
        flow = self._ensure_account(
            self.env, '3997', 'Backlog OCI Flow', 'equity',
        )
        reserve = self._ensure_account(
            self.env, '3998', 'Backlog AOCI Reserve', 'equity',
        )
        self.Map.create({
            'company_id': self.company.id,
            'kind': 'other',
            'source_account_ids': [(6, 0, flow.ids)],
            'reserve_account_id': reserve.id,
        })
        self.post_balanced_move([
            {'account': flow, 'credit': 125.0},
            {'account': self.account_cash, 'debit': 125.0},
        ], date=date(2026, 7, 1))
        run = self._run()

        LineClass = type(self.env['account.move.line'])
        with patch.object(
            LineClass, 'search',
            side_effect=AssertionError(
                'OCI aggregation must not materialize journal items',
            ),
        ):
            balances = run._fetch_period_balances(flow)
        self.assertAlmostEqual(balances[flow.id], -125.0)

    def test_branch_scope_is_complete_explicit_and_not_double_closable(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('Shared accounting-branch scope starts in Odoo 18.')
        branch = self.env['res.company'].create({
            'name': 'Year End Backlog Branch',
            'parent_id': self.company.id,
        })
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        branch_journal = self._ensure_journal(
            branch_env, branch, 'general', 'YEML', 'Year End Branch Journal',
        )
        branch_env['account.move'].create({
            'move_type': 'entry',
            'company_id': branch.id,
            'journal_id': branch_journal.id,
            'date': date(2026, 5, 1),
            'line_ids': [
                (0, 0, {
                    'name': 'Branch revenue',
                    'account_id': self.account_revenue.id,
                    'credit': 900.0,
                }),
                (0, 0, {
                    'name': 'Branch cash',
                    'account_id': self.account_cash.id,
                    'debit': 900.0,
                }),
            ],
        }).action_post()

        restricted_user = new_test_user(
            self.env,
            login='year_end_backlog_branch_restricted',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
        )
        restricted = self._run()
        with self.assertRaisesRegex(AccessError, 'complete branch subtree'):
            restricted.with_user(restricted_user).with_context(
                allowed_company_ids=[self.company.id],
            ).action_compute()
        restricted.unlink()

        root_env = self.env['eh.year.end.run'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(self.company).env
        root_run = self._run(env=root_env)
        self.assertEqual(
            set(root_run._source_companies().ids),
            {self.company.id, branch.id},
        )
        root_run.action_compute()
        self.assertAlmostEqual(root_run.net_profit, 900.0)
        root_run.action_post()
        branch_run = self._run(
            env=branch_env,
            journal_id=branch_journal.id,
        )
        branch_run.action_compute()
        with self.assertRaisesRegex(UserError, 'overlapping company/branch'):
            branch_run.action_post()

    def test_parent_hard_or_fiscal_lock_blocks_branch_close(self):
        if 'company_ids' not in self.env['account.account']._fields:
            self.skipTest('Inherited shared-chart locks start in Odoo 18.')
        branch = self.env['res.company'].create({
            'name': 'Year End Locked Branch',
            'parent_id': self.company.id,
        })
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        journal = self._ensure_journal(
            branch_env, branch, 'general', 'YELK', 'Locked Branch Journal',
        )
        move = branch_env['account.move'].create({
            'move_type': 'entry',
            'company_id': branch.id,
            'journal_id': journal.id,
            'date': date(2026, 4, 1),
            'line_ids': [
                (0, 0, {
                    'name': 'Locked branch revenue',
                    'account_id': self.account_revenue.id,
                    'credit': 70.0,
                }),
                (0, 0, {
                    'name': 'Locked branch cash',
                    'account_id': self.account_cash.id,
                    'debit': 70.0,
                }),
            ],
        })
        move.action_post()
        run = self._run(env=branch_env, journal_id=journal.id)
        run.action_compute()
        lock_field = (
            'hard_lock_date'
            if 'hard_lock_date' in self.company._fields
            else 'fiscalyear_lock_date'
        )
        self.company.sudo().write({lock_field: self.fy_end})
        with self.assertRaisesRegex(UserError, 'inherited parent-company'):
            run.action_post()

    def test_account_label_is_company_scoped_and_stable(self):
        self._post_pl()
        run = self._run()
        original_code = self.account_revenue.with_company(self.company).code
        run.action_compute()
        revenue_line = run.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        )
        self.assertEqual(revenue_line.source_account_label, original_code)
        self.account_revenue.with_company(self.company).code = '40997'
        run.action_post()
        closing_leg = run.move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue,
        )
        self.assertIn(original_code, closing_leg.name)
        self.assertNotIn('40997', closing_leg.name)

    def test_plain_accountant_can_compute_cancel_and_return_to_draft(self):
        self._post_pl()
        plain = new_test_user(
            self.env,
            login='year_end_backlog_plain',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
        )
        run = self._run()
        acting = run.with_user(plain)
        acting.action_compute()
        self.assertEqual(run.state, 'computed')
        acting.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        acting.action_set_to_draft()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids)

    def test_sequence_uses_exact_company_and_fiscal_end_year(self):
        company = self.env['res.company'].create({
            'name': 'Year End Sequence Company',
        })
        sequence = ensure_company_sequence(self.env, company)
        self.assertEqual(sequence.company_id, company)
        self.assertTrue(sequence.use_date_range)
        first_2026 = next_company_reference(
            self.env, company, date(2026, 6, 30),
        )
        first_2027 = next_company_reference(
            self.env, company, date(2027, 6, 30),
        )
        self.assertRegex(first_2026, r'^YE/2026/\d{4}$')
        self.assertRegex(first_2027, r'^YE/2027/\d{4}$')

    def test_transition_checks_source_once_and_create_avoids_company_lock(self):
        self._post_pl()
        run = self._run()
        plain = new_test_user(
            self.env,
            login='year_end_backlog_source_count',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
        )
        LineClass = type(self.env['account.move.line'])
        real_count = LineClass.search_count
        count_calls = []

        def recording_count(records, domain, *args, **kwargs):
            count_calls.append(tuple(domain))
            return real_count(records, domain, *args, **kwargs)

        with patch.object(LineClass, 'search_count', recording_count):
            run.with_user(plain).action_compute()
        self.assertEqual(len(count_calls), 2)

        statements = []
        real_execute = self.env.cr.execute

        def recording_execute(query, *args, **kwargs):
            statements.append(str(query).upper())
            return real_execute(query, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', recording_execute):
            self.Run.create({
                'fiscal_year_start': date(2027, 1, 1),
                'fiscal_year_end': date(2027, 12, 31),
                'company_id': self.company.id,
                'journal_id': self.journal_misc.id,
                'retained_earnings_account_id': self.retained.id,
            })
        self.assertFalse(any(
            'RES_COMPANY' in query and 'FOR UPDATE' in query
            for query in statements
        ))

    def test_aoci_picker_domains_and_line_display_name_are_company_aware(self):
        reserve_field = self.Map._fields['reserve_account_id']
        source_field = self.Map._fields['source_account_ids']
        account_company_field = (
            'company_ids'
            if 'company_ids' in self.env['account.account']._fields
            else 'company_id'
        )
        self.assertTrue(reserve_field.check_company)
        self.assertTrue(source_field.check_company)
        self.assertIn(account_company_field, reserve_field.domain)
        self.assertIn(account_company_field, source_field.domain)
        self.assertEqual(
            self.env['eh.year.end.line']._rec_name,
            'account_id',
        )
