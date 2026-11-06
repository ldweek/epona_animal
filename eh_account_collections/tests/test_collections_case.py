# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Collections case tests.

Covers auto-create idempotency, refresh of existing cases, the
one-open-case-per-partner constraint, escalation, resolution, and the
overdue aggregation math.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestAutoCreate(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.today = fields.Date.context_today(cls.env['res.users'])
        # Most aggregation tests exercise ledger math independent of the
        # grace policy. A dedicated test below pins the configured threshold.
        cls.company.eh_collections_grace_days = 0

    def _post_overdue_invoice(self, partner, amount, days_overdue):
        """Post a balanced entry that creates an overdue receivable."""
        post_date = self.today - timedelta(days=days_overdue + 30)
        due_date = self.today - timedelta(days=days_overdue)
        return self.post_balanced_move(
            [
                {
                    'account': self.account_receivable,
                    'debit': amount,
                    'partner': partner,
                    'date_maturity': due_date,
                },
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=post_date,
        )

    def test_auto_create_creates_case_for_overdue_partner(self):
        self._post_overdue_invoice(self.partner_a, 500.0, days_overdue=15)
        result = self.Case.auto_create_cases([self.company.id])
        self.assertGreaterEqual(result['created'], 1)
        case = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertTrue(case)
        self.assertAlmostEqual(case.total_overdue_amount, 500.0, places=2)
        self.assertEqual(case.days_overdue_max, 15)

    def test_auto_create_is_idempotent(self):
        self._post_overdue_invoice(self.partner_a, 500.0, days_overdue=10)
        first = self.Case.auto_create_cases([self.company.id])
        second = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['created'], 0)

    def test_auto_create_batches_multiple_partners(self):
        """Multiple overdue partners are handled with one existence query
        and a bulk create, not one search per partner. A re-run refreshes
        them all via the batched lookup and creates none."""
        partners = [self.partner_a, self.partner_b]
        for i in range(3):
            partners.append(self.env['res.partner'].create({
                'name': 'Overdue Partner %d' % i,
            }))
        for partner in partners:
            self._post_overdue_invoice(partner, 100.0, days_overdue=10)
        first = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(first['created'], len(partners))
        open_cases = self.Case.search([
            ('company_id', '=', self.company.id),
            ('is_resolved', '=', False),
        ])
        self.assertEqual(len(open_cases), len(partners))
        second = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['refreshed'], len(partners))
        self.assertGreaterEqual(second['refreshed'], 1)
        cases = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(cases), 1)

    def test_auto_create_refresh_updates_totals(self):
        self._post_overdue_invoice(self.partner_a, 200.0, days_overdue=10)
        self.Case.auto_create_cases([self.company.id])
        case = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
        ], limit=1)
        self.assertAlmostEqual(case.total_overdue_amount, 200.0, places=2)
        # Add more overdue activity.
        self._post_overdue_invoice(self.partner_a, 300.0, days_overdue=20)
        self.Case.auto_create_cases([self.company.id])
        case.invalidate_recordset()
        self.assertAlmostEqual(case.total_overdue_amount, 500.0, places=2)
        self.assertEqual(case.days_overdue_max, 20)

    def test_partner_with_no_overdue_does_not_get_case(self):
        result = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(result['created'], 0)

    def test_public_auto_create_denies_company_ids_before_raw_sql(self):
        foreign = self.env['res.company'].create({
            'name': 'Collections Foreign Scope'})
        user = self.env['res.users'].create({
            'name': 'Collections Scoped User',
            'login': 'collections_scope_user',
            'company_id': self.company.id,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        Case = self.Case.with_user(user).with_company(self.company).with_context(
            allowed_company_ids=[self.company.id])
        errors = []
        with patch.object(type(self.Case), '_fetch_overdue_aggregation') as raw:
            for requested in ([foreign.id], [2_147_483_647]):
                with self.assertRaises(AccessError) as denied:
                    Case.auto_create_cases(requested)
                errors.append(str(denied.exception))
        raw.assert_not_called()
        self.assertEqual(errors[0], errors[1],
                         "denial must not reveal whether a company id exists")

    def test_partner_with_only_not_yet_due_does_not_get_case(self):
        # Future due date.
        future_due = self.today + timedelta(days=30)
        self.post_balanced_move(
            [
                {
                    'account': self.account_receivable,
                    'debit': 100.0,
                    'partner': self.partner_a,
                    'date_maturity': future_due,
                },
                {'account': self.account_revenue, 'credit': 100.0},
            ],
            date=self.today,
        )
        result = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(result['created'], 0)

    def test_auto_create_honours_company_grace_days(self):
        self.company.eh_collections_grace_days = 14
        self._post_overdue_invoice(self.partner_a, 100.0, days_overdue=10)
        result = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(result['created'], 0)
        self._post_overdue_invoice(self.partner_b, 200.0, days_overdue=15)
        result = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(result['created'], 1)
        self.assertEqual(
            self.Case.search([('is_resolved', '=', False)]).partner_id,
            self.partner_b,
        )

    def test_auto_create_duplicate_rows_are_savepoint_isolated(self):
        row = {
            'company_id': self.company.id,
            'partner_id': self.partner_a.id,
            'total_amount': 100.0,
            'oldest_due_date': self.today - timedelta(days=20),
            'max_days_overdue': 20,
        }
        with patch.object(
            type(self.Case),
            '_fetch_overdue_aggregation',
            return_value=[row, dict(row, total_amount=125.0)],
        ):
            result = self.Case.auto_create_cases([self.company.id])
        self.assertEqual(result, {'created': 1, 'refreshed': 1})
        cases = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
            ('is_resolved', '=', False),
        ])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases.total_overdue_amount, 125.0)

    def test_interactive_auto_create_returns_notification(self):
        with patch.object(
            type(self.Case),
            'auto_create_cases',
            return_value={'created': 2, 'refreshed': 3},
        ):
            action = self.Case.action_auto_create_cases()
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'display_notification')
        self.assertIn('2', action['params']['message'])
        self.assertIn('3', action['params']['message'])

    def test_resolved_case_is_left_alone(self):
        self._post_overdue_invoice(self.partner_a, 100.0, days_overdue=10)
        self.Case.auto_create_cases([self.company.id])
        case = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
        ], limit=1)
        case.action_resolve()
        # Add new overdue for the same partner.
        self._post_overdue_invoice(self.partner_a, 250.0, days_overdue=15)
        self.Case.auto_create_cases([self.company.id])
        # Original resolved case unchanged.
        case.invalidate_recordset()
        self.assertTrue(case.is_resolved)
        # A new open case exists for the partner.
        open_cases = self.Case.search([
            ('partner_id', '=', self.partner_a.id),
            ('is_resolved', '=', False),
        ])
        self.assertEqual(len(open_cases), 1)


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestCaseLifecycle(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Stage = cls.env['eh.collections.stage']

    def _make_case(self, partner=None, **kwargs):
        partner = partner or self.partner_a
        vals = {
            'partner_id': partner.id,
            **kwargs,
        }
        return self.Case.create(vals)

    def test_default_stage_assigned(self):
        case = self._make_case()
        self.assertTrue(case.stage_id)
        self.assertFalse(case.is_resolved)

    def test_action_resolve_moves_to_resolved_stage(self):
        case = self._make_case()
        case.action_resolve()
        self.assertTrue(case.is_resolved)
        self.assertTrue(case.stage_id.is_resolved)

    def test_action_escalate_increments_priority(self):
        case = self._make_case()
        # Default priority is '1' (Medium).
        self.assertEqual(case.priority, '1')
        case.action_escalate()
        self.assertEqual(case.priority, '2')
        case.action_escalate()
        self.assertEqual(case.priority, '3')
        case.action_escalate()
        # Capped at 3.
        self.assertEqual(case.priority, '3')

    def test_only_one_open_case_per_partner_company(self):
        self._make_case(partner=self.partner_a)
        with self.assertRaises(Exception):
            self._make_case(partner=self.partner_a)

    def test_resolved_case_allows_new_open_case_for_same_partner(self):
        first = self._make_case(partner=self.partner_a)
        first.action_resolve()
        second = self._make_case(partner=self.partner_a)
        self.assertNotEqual(first.id, second.id)
        self.assertFalse(second.is_resolved)

    def test_child_contact_is_canonicalised_to_commercial_partner(self):
        parent = self.env['res.partner'].create({'name': 'Commercial Entity'})
        child = self.env['res.partner'].create({
            'name': 'Accounts Payable Contact',
            'parent_id': parent.id,
            'type': 'contact',
        })
        case = self._make_case(partner=child)
        self.assertEqual(case.partner_id, parent)
        with self.assertRaises(Exception):
            self._make_case(partner=parent)

    def test_exclude_definition_matches_postgresql_catalog(self):
        definition = (
            'EXCLUDE USING gist (partner_id WITH =, company_id WITH =) '
            'WHERE ((NOT is_resolved))'
        )
        self.env.cr.execute("""
            SELECT pg_get_constraintdef(constraint_rec.oid),
                   description.description
              FROM pg_constraint AS constraint_rec
              JOIN pg_class AS table_rec
                ON table_rec.oid = constraint_rec.conrelid
         LEFT JOIN pg_description AS description
                ON description.objoid = constraint_rec.oid
             WHERE table_rec.relname = 'eh_collections_case'
               AND constraint_rec.conname =
                   'eh_collections_case_open_case_per_partner_company'
        """)
        catalog_definition, declared_definition = self.env.cr.fetchone()
        self.assertEqual(catalog_definition, definition)
        self.assertEqual(declared_definition, definition)


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestCasePromise(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']

    def test_compute_promise_picks_latest(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        future_date = fields.Date.context_today(self.env['res.users']) + timedelta(days=15)
        case.action_log_action(
            'call', 'First call',
            promise_amount=200.0, promise_date=future_date,
        )
        case.action_log_action(
            'email', 'Follow up',
            promise_amount=300.0, promise_date=future_date,
        )
        case.invalidate_recordset()
        # Latest action's promise applies.
        self.assertAlmostEqual(case.promised_amount, 300.0, places=2)
        self.assertTrue(case.has_active_promise)

    def test_promise_in_past_is_not_active(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        past_date = fields.Date.context_today(self.env['res.users']) - timedelta(days=15)
        case.action_log_action(
            'call', 'Old promise',
            promise_amount=200.0, promise_date=past_date,
        )
        case.invalidate_recordset()
        self.assertFalse(case.has_active_promise)

    def test_no_promise_means_zero_amount(self):
        case = self.Case.create({'partner_id': self.partner_a.id})
        case.action_log_action('note', 'No promise yet')
        case.invalidate_recordset()
        self.assertAlmostEqual(case.promised_amount, 0.0, places=2)
        self.assertFalse(case.has_active_promise)
