# -*- encoding: utf-8 -*-
"""Regressions for remediated medium/low IAS 8 / IAS 10 findings."""

import ast
from pathlib import Path
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_events', 'post_install', '-at_install')
class TestEventsMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def _posting_change(self, **overrides):
        values = {
            'change_type': 'error_correction',
            'change_date': '2027-06-30',
            'restatement_date': '2027-01-01',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Opening receivable correction',
                'adjustment': -125.0,
            })],
        }
        values.update(overrides)
        return self.env['eh.accounting.change'].create(values)

    def _posting_event(self, **overrides):
        values = {
            'name': 'Settlement evidence',
            'reporting_date': '2027-06-30',
            'event_date': '2027-07-15',
            'authorized_for_issue_date': '2027-09-30',
            'authorized_for_issue_by': 'Board of Directors',
            'is_adjusting': True,
            'category': 'litigation',
            'estimated_effect': 250.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        }
        values.update(overrides)
        return self.env['eh.subsequent.event'].create(values)

    def test_ias10_event_date_must_be_strictly_after_reporting_date(self):
        for event_date in ('2027-06-29', '2027-06-30'):
            with self.subTest(event_date=event_date), self.assertRaises(
                ValidationError,
            ):
                self._posting_event(event_date=event_date)
        event = self._posting_event(event_date='2027-07-01')
        self.assertEqual(str(event.event_date), '2027-07-01')
        with self.assertRaises(ValidationError):
            self._posting_event(event_date='2027-10-01')

    def test_authorising_body_and_cannot_estimate_are_distinct_evidence(self):
        event = self._posting_event(
            name='Effect unavailable',
            is_adjusting=False,
            estimation_status='cannot_estimate',
            estimated_effect=0.0,
        )
        self.assertEqual(event.authorized_for_issue_by, 'Board of Directors')
        self.assertEqual(event.estimation_status, 'cannot_estimate')
        with self.assertRaises(UserError):
            event.action_book_adjusting_entry()
        with self.assertRaises(ValidationError):
            self._posting_event(
                is_adjusting=False,
                estimation_status='cannot_estimate',
                estimated_effect=1.0,
            )

    def test_explicit_opening_date_and_equity_tag_drive_posting(self):
        change = self._posting_change()
        self.assertEqual(
            change.equity_presentation, 'retrospective_restatement',
        )
        change.action_post_restatement()
        self.assertEqual(str(change.move_id.date), '2027-01-01')
        policy = self.env['eh.accounting.change'].create({
            'change_type': 'policy_change',
        })
        estimate = self.env['eh.accounting.change'].create({
            'change_type': 'estimate_change',
        })
        self.assertEqual(
            policy.equity_presentation, 'retrospective_application',
        )
        self.assertEqual(estimate.equity_presentation, 'prospective_change')
        with self.assertRaises(ValidationError):
            self._posting_change(restatement_date='2027-07-01')

    def test_posted_copies_reset_workflow_and_posted_evidence(self):
        change = self._posting_change()
        change.action_post_restatement()
        change_copy = change.copy()
        self.assertEqual(change_copy.state, 'draft')
        self.assertFalse(change_copy.move_id)
        self.assertNotEqual(change_copy.name, change.name)

        event = self._posting_event()
        event.action_book_adjusting_entry()
        event_copy = event.copy()
        self.assertEqual(event_copy.state, 'draft')
        self.assertFalse(event_copy.move_id)
        self.assertNotEqual(event_copy.reference, event.reference)

    def test_references_use_business_year_and_independent_company_counters(self):
        company_a = self.env['res.company'].create({
            'name': 'IAS Register Company A',
        })
        company_b = self.env['res.company'].create({
            'name': 'IAS Register Company B',
        })
        event_a = self.env['eh.subsequent.event'].create({
            'name': 'Company A event',
            'company_id': company_a.id,
            'reporting_date': '2030-12-31',
            'event_date': '2031-01-02',
        })
        event_b = self.env['eh.subsequent.event'].create({
            'name': 'Company B event',
            'company_id': company_b.id,
            'reporting_date': '2030-12-31',
            'event_date': '2031-01-03',
        })
        self.assertEqual(event_a.reference, 'IAS10/2031/0001')
        self.assertEqual(event_b.reference, 'IAS10/2031/0001')
        change = self.env['eh.accounting.change'].create({
            'company_id': company_a.id,
            'change_date': '2030-09-30',
        })
        self.assertEqual(change.name, 'IAS8/2030/0001')

    def test_user_and_auditor_navigation_matches_acl(self):
        user_group = self.env.ref('eh_account_base.group_eh_user')
        auditor_group = self.env.ref('eh_account_base.group_eh_auditor')
        parent = self.env.ref('eh_account_base.menu_eh_ifrs')
        self.assertIn(auditor_group, parent.group_ids)
        for xmlid in (
            'eh_account_events.menu_eh_accounting_change',
            'eh_account_events.menu_eh_subsequent_event',
        ):
            menu = self.env.ref(xmlid)
            self.assertIn(user_group, menu.group_ids)
            self.assertIn(auditor_group, menu.group_ids)
        self.assertTrue(self.env.ref(
            'eh_account_events.view_eh_accounting_change_search',
        ))

    def test_event_descriptive_write_does_not_take_transition_lock(self):
        event = self._posting_event()
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            event.write({'description': 'Routine narrative refinement'})
        self.assertFalse(any(
            'eh_subsequent_event' in query and 'FOR UPDATE' in query
            for query in queries
        ))
        queries.clear()
        with patch.object(self.env.cr, 'execute', _record_execute):
            event.write({'event_date': '2027-07-16'})
        self.assertTrue(any(
            'eh_subsequent_event' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_listing_version_and_comparative_scope_are_truthful(self):
        root = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval(
            (root / '__manifest__.py').read_text(encoding='utf-8')
        )
        listing = (
            root / 'static' / 'description' / 'index.html'
        ).read_text(encoding='utf-8')
        self.assertTrue(manifest['version'].endswith('.1.2.10'))
        self.assertIn('v%s' % manifest['version'], listing)
        self.assertNotIn('.1.2.8', listing)
        self.assertNotIn('per-account comparative trail across prior periods',
                         listing)
        self.assertIn('does not create separate entries per prior period',
                      listing)
