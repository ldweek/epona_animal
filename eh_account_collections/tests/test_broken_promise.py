# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Broken-promise auto-escalation tests.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestBrokenPromise(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Action = cls.env['eh.collections.action']
        cls.partner_a.email = 'collections-broken-promise@example.com'

    def _make_case(self, days_overdue=45, total=500.0, partner=None):
        partner = partner or self.partner_a
        return self.Case.create({
            'partner_id': partner.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'total_overdue_amount': total,
            'days_overdue_max': days_overdue,
            'priority': '0',
        })

    def _add_promise(self, case, days_offset, amount=200.0):
        promise_date = fields.Date.context_today(self.env['res.users']) + (
            timedelta(days=days_offset)
        )
        return self.Action.create({
            'case_id': case.id,
            'action_type': 'call',
            'summary': "Promise",
            'contact_made': True,
            'promise_amount': amount,
            'promise_date': promise_date,
        })

    def test_active_promise_not_broken(self):
        case = self._make_case()
        self._add_promise(case, days_offset=7)
        case.invalidate_recordset(['has_active_promise', 'broken_promise'])
        self.assertTrue(case.has_active_promise)
        self.assertFalse(case.broken_promise)

    def test_lapsed_promise_is_broken(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-3)
        case.invalidate_recordset(['has_active_promise', 'broken_promise'])
        self.assertFalse(case.has_active_promise)
        self.assertTrue(case.broken_promise)

    def test_resolved_case_not_broken_even_if_promise_lapsed(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-3)
        resolved_stage = self.env['eh.collections.stage'].search(
            [('is_resolved', '=', True)], limit=1,
        )
        case.stage_id = resolved_stage.id
        case.invalidate_recordset(['broken_promise'])
        self.assertFalse(case.broken_promise)

    def test_handle_broken_promise_bumps_priority(self):
        case = self._make_case()
        self.assertEqual(case.priority, '0')
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        self.assertEqual(case.priority, '1')
        self.assertTrue(case.last_broken_promise_handled_at)

    def test_handle_broken_promise_moves_to_escalated_stage(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        escalated = self.env['eh.collections.stage'].search(
            [('is_escalated', '=', True)], limit=1,
        )
        self.assertTrue(escalated)
        self.assertEqual(case.stage_id, escalated)

    def test_handle_broken_promise_logs_action(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        before = len(case.action_ids)
        case.invalidate_recordset(['broken_promise'])
        case.action_handle_broken_promise()
        self.assertGreater(len(case.action_ids), before)

    def test_cron_handles_each_promise_action_once(self):
        case = self._make_case()
        self._add_promise(case, days_offset=-2)
        case.invalidate_recordset(['broken_promise'])
        # First cron pass: escalates.
        result = self.Case._cron_handle_broken_promises()
        self.assertEqual(result['escalated'], 1)
        priority_after_first = case.priority
        handled_action = case.last_broken_promise_action_id
        self.assertTrue(handled_action)
        # Time is not the identity guard. Even after the legacy timestamp is
        # aged beyond 24 hours, the exact same action is never escalated again.
        case.last_broken_promise_handled_at = (
            fields.Datetime.now() - timedelta(days=30)
        )
        result = self.Case._cron_handle_broken_promises()
        self.assertEqual(result['escalated'], 0)
        self.assertEqual(case.priority, priority_after_first)

        # A fresh, already-lapsed promise is a new commitment and is handled
        # exactly once under its own immutable action id.
        new_promise = self._add_promise(case, days_offset=-1, amount=300.0)
        result = self.Case._cron_handle_broken_promises()
        self.assertEqual(result['escalated'], 1)
        self.assertEqual(case.last_broken_promise_action_id, new_promise)
        self.assertNotEqual(new_promise, handled_action)

    def test_search_requires_amount_and_date_on_same_latest_action(self):
        case = self._make_case()
        future = fields.Date.context_today(self.env['res.users']) + timedelta(
            days=10,
        )
        self.Action.create({
            'case_id': case.id,
            'action_type': 'note',
            'summary': 'Amount only',
            'promise_amount': 100.0,
        })
        self.Action.create({
            'case_id': case.id,
            'action_type': 'note',
            'summary': 'Date only',
            'promise_date': future,
        })
        self.assertNotIn(
            case,
            self.Case.search([('has_active_promise', '=', True)]),
        )

    def test_negative_active_search_is_exact_complement(self):
        active = self._make_case()
        quiet = self._make_case(partner=self.partner_b)
        self._add_promise(active, days_offset=5)
        scope = [('id', 'in', (active | quiet).ids)]
        self.assertEqual(
            self.Case.search(scope + [('has_active_promise', '=', False)]),
            quiet,
        )
        self.assertEqual(
            self.Case.search(scope + [('has_active_promise', '!=', True)]),
            quiet,
        )

    def test_search_uses_latest_promise_identity(self):
        case = self._make_case()
        self._add_promise(case, days_offset=10)
        latest = self._add_promise(case, days_offset=-1)
        self.assertIn(
            case,
            self.Case.search([('broken_promise', '=', True)]),
        )
        self.assertNotIn(
            case,
            self.Case.search([('has_active_promise', '=', True)]),
        )
        case.invalidate_recordset()
        self.assertEqual(case.latest_promise_action_id, latest)

    def test_broken_search_query_count_is_constant(self):
        partners = self.env['res.partner'].create([
            {'name': 'Promise query budget %s' % index}
            for index in range(8)
        ])
        for partner in partners:
            case = self._make_case(partner=partner)
            self._add_promise(case, days_offset=-1)
        with self.assertQueryCount(1):
            found = self.Case.search([('broken_promise', '=', True)])
        self.assertTrue(found)

    def test_cron_limit_is_applied_after_latest_promise_filter(self):
        replacement = self._make_case()
        self._add_promise(replacement, days_offset=-10)
        self._add_promise(replacement, days_offset=10)
        broken = self._make_case(partner=self.partner_b)
        self.partner_b.email = 'broken-limit@example.com'
        self._add_promise(broken, days_offset=-1)
        result = self.Case._cron_handle_broken_promises(batch_size=1)
        self.assertEqual(result, {'escalated': 1, 'failed': 0})
        self.assertEqual(broken.last_broken_promise_action_id,
                         broken.latest_promise_action_id)
        self.assertFalse(replacement.last_broken_promise_action_id)

    def test_cron_counts_only_successful_savepoints(self):
        self.partner_b.email = False
        case = self._make_case(partner=self.partner_b)
        self._add_promise(case, days_offset=-1)
        result = self.Case._cron_handle_broken_promises(batch_size=1)
        self.assertEqual(result, {'escalated': 0, 'failed': 1})
        self.assertFalse(case.last_broken_promise_action_id)

    def test_action_button_raises_when_no_broken_promise(self):
        case = self._make_case()
        # No promises at all.
        with self.assertRaises(UserError):
            case.action_handle_broken_promise()
