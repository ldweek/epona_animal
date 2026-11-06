# -*- encoding: utf-8 -*-
"""Focused regressions for the verified medium/low borrowing-cost backlog."""

from datetime import date
from pathlib import Path
import re
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_borrowing_costs', 'integration', 'post_install',
        '-at_install')
class TestBorrowingCostMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )
        cls.asset = cls._ensure_account(
            cls.env, '1688', 'Backlog Asset Under Construction',
            'asset_non_current',
        )
        cls.interest = cls._ensure_account(
            cls.env, '5688', 'Backlog Interest Expense', 'expense',
        )
        cls.other_expense = cls._ensure_account(
            cls.env, '5689', 'Backlog Other Interest Expense',
            'expense_other',
        )
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Borrowing Cost Preparer',
            'login': 'borrowing_cost_preparer',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _rec(self, **values):
        vals = {
            'qualifying_asset': 'Backlog qualifying asset',
            'asset_account_id': self.asset.id,
            'borrowing_cost_account_id': self.interest.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(values)
        return self.env['eh.borrowing.cost'].create(vals)

    def _arch(self, xmlid):
        return etree.fromstring(self.env.ref(xmlid).arch_db.encode())

    def test_explicit_zero_actual_cost_is_a_real_cap(self):
        capped = self._rec(
            specific_borrowing_cost=850.0,
            actual_borrowing_cost=0.0,
        )
        self.assertTrue(capped.apply_actual_borrowing_cost_cap)
        self.assertEqual(capped.uncapped_amount, 850.0)
        self.assertEqual(capped.capitalisable, 0.0)

        not_yet_known = self._rec(specific_borrowing_cost=850.0)
        self.assertFalse(not_yet_known.apply_actual_borrowing_cost_cap)
        self.assertEqual(not_yet_known.capitalisable, 850.0)
        not_yet_known.write({'actual_borrowing_cost': 0.0})
        self.assertTrue(not_yet_known.apply_actual_borrowing_cost_cap)
        self.assertEqual(not_yet_known.capitalisable, 0.0)

    def test_invalid_period_and_negative_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, 'start'):
            self._rec(
                period_start=date(2026, 2, 1),
                period_end=date(2026, 1, 31),
            )
        for field_name in (
            'specific_borrowing_cost', 'temporary_investment_income',
            'general_expenditure', 'capitalisation_rate',
            'actual_borrowing_cost',
        ):
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValidationError, 'negative'):
                    self._rec(**{field_name: -1.0})
        record = self._rec()
        with self.assertRaisesRegex(ValidationError, 'cannot be negative'):
            self.env['eh.borrowing.cost.line'].create({
                'borrowing_cost_id': record.id,
                'date': date(2026, 1, 1),
                'amount': -1.0,
            })

    def test_move_provenance_default_context_is_not_forgeable(self):
        record = self._rec()
        with self.assertRaisesRegex(AccessError, 'engine-owned'):
            self.env['account.move'].with_context(
                default_eh_borrowing_cost_id=record.id,
            ).create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
            })

    def test_undated_and_dated_general_expenditure_are_exclusive(self):
        with self.assertRaisesRegex(ValidationError, 'either'):
            self._rec(
                general_expenditure=1000.0,
                expenditure_line_ids=[(0, 0, {
                    'date': date(2026, 1, 1), 'amount': 1000.0,
                })],
            )
        undated = self._rec(general_expenditure=1000.0)
        with self.assertRaisesRegex(ValidationError, 'either'):
            self.env['eh.borrowing.cost.line'].create({
                'borrowing_cost_id': undated.id,
                'date': date(2026, 1, 1),
                'amount': 1000.0,
            })
        dated = self._rec(expenditure_line_ids=[(0, 0, {
            'date': date(2026, 1, 1), 'amount': 1000.0,
        })])
        with self.assertRaisesRegex(ValidationError, 'either'):
            dated.general_expenditure = 1000.0

    def test_reversal_uses_reviewed_open_date(self):
        record = self._rec(
            specific_borrowing_cost=850.0,
            period_end=date(2026, 1, 31),
            accounting_date=date(2026, 2, 1),
        )
        record.action_capitalise()
        original_lock = self.company.fiscalyear_lock_date
        try:
            self.company.fiscalyear_lock_date = date(2026, 2, 28)
            record.reversal_date = date(2026, 2, 28)
            with self.assertRaisesRegex(UserError, 'explicit open reversal'):
                record.action_reverse()
            self.assertEqual(record.state, 'capitalised')
            self.assertFalse(record.reversal_move_id)

            record.reversal_date = date(2026, 3, 1)
            record.action_reverse()
            self.assertEqual(record.reversal_move_id.date, date(2026, 3, 1))
            self.assertEqual(record.posted_reversal_date, date(2026, 3, 1))
        finally:
            self.company.fiscalyear_lock_date = original_lock

    def test_search_menu_and_lifecycle_buttons_match_acl(self):
        search = self._arch(
            'eh_account_borrowing_costs.view_eh_borrowing_cost_search'
        )
        for field_name in (
            'state', 'company_id', 'period_start', 'period_end',
        ):
            self.assertTrue(search.xpath(".//field[@name='%s']" % field_name))
        for filter_name in (
            'draft', 'capitalised', 'reversed', 'cancelled',
            'integrity_review', 'group_company', 'group_period',
        ):
            self.assertTrue(search.xpath(
                ".//filter[@name='%s']" % filter_name
            ))
        action = self.env.ref(
            'eh_account_borrowing_costs.action_eh_borrowing_cost'
        )
        self.assertEqual(
            action.search_view_id,
            self.env.ref(
                'eh_account_borrowing_costs.view_eh_borrowing_cost_search'
            ),
        )
        menu = self.env.ref(
            'eh_account_borrowing_costs.menu_eh_borrowing_cost'
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_user'), menu.group_ids
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_auditor'), menu.group_ids
        )
        form = self._arch(
            'eh_account_borrowing_costs.view_eh_borrowing_cost_form'
        )
        for action_name in (
            'action_capitalise', 'action_cancel', 'action_reverse',
            'action_reset_to_draft',
        ):
            button = form.xpath(".//button[@name='%s']" % action_name)[0]
            self.assertEqual(
                button.get('groups'), 'eh_account_base.group_eh_manager'
            )

    def test_all_lifecycle_manager_gates_raise_access_error(self):
        draft = self._rec(specific_borrowing_cost=850.0)
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).action_capitalise()
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).action_cancel()
        draft.action_capitalise()
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).action_reverse()
        draft.action_reverse()
        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).action_reset_to_draft()

    def test_reference_counter_is_company_and_period_year_scoped(self):
        company_a = self.env['res.company'].create({
            'name': 'Borrowing Sequence A',
        })
        company_b = self.env['res.company'].create({
            'name': 'Borrowing Sequence B',
        })
        first_a = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'A 2024',
            'company_id': company_a.id,
            'period_end': date(2024, 12, 31),
        })
        second_a = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'A 2024 second',
            'company_id': company_a.id,
            'period_end': date(2024, 12, 31),
        })
        next_year_a = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'A 2025',
            'company_id': company_a.id,
            'period_end': date(2025, 1, 31),
        })
        first_b = self.env['eh.borrowing.cost'].create({
            'qualifying_asset': 'B 2025',
            'company_id': company_b.id,
            'period_end': date(2025, 1, 31),
        })
        self.assertEqual(
            first_a.name, 'BORR/%s/2024/0001' % company_a.id
        )
        self.assertEqual(
            second_a.name, 'BORR/%s/2024/0002' % company_a.id
        )
        self.assertEqual(
            next_year_a.name, 'BORR/%s/2025/0001' % company_a.id
        )
        self.assertEqual(
            first_b.name, 'BORR/%s/2025/0001' % company_b.id
        )

    def test_account_and_journal_domains_match_core_branch_policy(self):
        fields_ = self.env['eh.borrowing.cost']._fields
        self.assertIn('parent_of', str(fields_['journal_id'].domain))
        self.assertIn('parent_of', str(fields_['asset_account_id'].domain))
        self.assertIn('parent_of', str(
            fields_['borrowing_cost_account_id'].domain
        ))
        self.assertIn('expense_other', str(
            fields_['borrowing_cost_account_id'].domain
        ))
        record = self._rec(
            specific_borrowing_cost=850.0,
            borrowing_cost_account_id=self.other_expense.id,
        )
        record.action_capitalise()
        self.assertEqual(record.move_id.state, 'posted')

    def test_cancelled_period_can_be_reopened_without_evidence_loss(self):
        record = self._rec()
        record.action_cancel()
        self.assertEqual(record.state, 'cancelled')
        record.action_reset_to_draft()
        self.assertEqual(record.state, 'draft')
        self.assertFalse(record.move_ids)

    def test_weighting_uses_inclusive_calendar_days(self):
        full_year = self._rec(
            capitalisation_rate=10.0,
            capitalisation_rate_basis='period',
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[(0, 0, {
                'date': date(2026, 1, 1), 'amount': 36500.0,
            })],
        )
        final_day = self._rec(
            capitalisation_rate=10.0,
            capitalisation_rate_basis='period',
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[(0, 0, {
                'date': date(2026, 12, 31), 'amount': 36500.0,
            })],
        )
        self.assertEqual(full_year.weighted_average_base, 36500.0)
        self.assertEqual(final_day.weighted_average_base, 100.0)

    def test_narrative_chatter_and_activity_do_not_take_transition_lock(self):
        record = self._rec()
        queries = []
        real_execute = self.env.cr.execute

        def _capture(query, *args, **kwargs):
            queries.append(str(query))
            return real_execute(query, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', _capture):
            record.write({'notes': 'Narrative-only update'})
            record.message_post(body='Review note without a state transition')
            record.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=self.env.user.id,
                summary='Review supporting schedule',
            )
        self.assertFalse(any(
            'eh_borrowing_cost' in query and 'FOR UPDATE' in query.upper()
            for query in queries
        ))

    def test_listing_matches_current_version_and_reversal_workflow(self):
        module_root = Path(__file__).parents[1]
        listing = (module_root / 'static/description/index.html').read_text()
        manifest = (module_root / '__manifest__.py').read_text()
        version_match = re.search(r"'version': '([^']+)'", manifest)
        self.assertIsNotNone(version_match)
        self.assertIn('v%s' % version_match.group(1), listing)
        self.assertNotIn('It does not itself reverse a posted entry', listing)
        self.assertIn('explicit open accounting date', listing)
