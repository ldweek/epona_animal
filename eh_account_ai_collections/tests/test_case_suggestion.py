# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the live collections next-action suggestion.

Each test builds a case in a specific state and asserts the
deterministic dunning ladder surfaces the right action and priority on
the case record. This proves the helper is genuinely wired live, not
just importable.
"""

from datetime import date, datetime, timedelta
import importlib.util
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.modules.module import get_module_path
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_collections.models import action as action_model
from odoo.addons.eh_account_ai_collections.models import (
    collections_case as ai_case_model,
)


@tagged('eh_account_ai_collections', 'integration', 'post_install',
        '-at_install')
class TestCaseSuggestion(EhAccountIntegrationTestCase):

    def _today(self):
        return fields.Date.context_today(self.env['res.users'])

    def _case(self, days=0, total=0.0, partner=None):
        return self.env['eh.collections.case'].create({
            'partner_id': (partner or self.partner_a).id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'days_overdue_max': days,
            'total_overdue_amount': total,
        })

    def test_active_promise_suppresses_escalation(self):
        case = self._case(days=200, total=5000.0)
        case.action_log_action(
            'call', "Promised to pay", contact_made=True,
            promise_amount=500.0, promise_date=self._today() + timedelta(days=30),
        )
        self.assertTrue(case.has_active_promise)
        self.assertEqual(case.eh_ai_suggested_action, 'monitor')
        self.assertEqual(case.eh_ai_suggested_priority, 'low')

    def test_broken_promise_escalates(self):
        case = self._case(days=90, total=3000.0)
        case.action_log_action(
            'call', "Promise lapsed",
            promise_amount=500.0, promise_date=self._today() - timedelta(days=10),
        )
        self.assertTrue(case.broken_promise)
        self.assertEqual(case.eh_ai_suggested_action, 'escalate_to_manager')
        self.assertEqual(case.eh_ai_suggested_priority, 'high')

    def test_consider_write_off_when_very_overdue(self):
        case = self._case(days=200, total=8000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'consider_write_off')
        self.assertEqual(case.eh_ai_suggested_priority, 'high')
        self.assertIn('8000.00', case.eh_ai_suggestion_rationale)

    def test_write_off_priority_uses_company_materiality(self):
        self.company.eh_ai_collections_writeoff_materiality = 1000.0
        case = self._case(days=200, total=500.0)

        self.assertEqual(case.eh_ai_suggested_action, 'consider_write_off')
        self.assertEqual(case.eh_ai_suggested_priority, 'medium')
        self.assertIn('below the company review threshold',
                      case.eh_ai_suggestion_rationale)

    def test_send_demand_letter_at_90(self):
        case = self._case(days=95, total=4000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'send_demand_letter')

    def test_agency_referral_after_letter(self):
        case = self._case(days=130, total=4000.0)
        old_timestamp = fields.Datetime.subtract(
            fields.Datetime.now(), days=31,
        )
        with patch.object(
                action_model.fields.Datetime, 'now', return_value=old_timestamp):
            self.env['eh.collections.action'].create({
                'case_id': case.id,
                'action_type': 'letter',
                'summary': "Demand letter sent",
                'contact_made': True,
            })
        case.invalidate_recordset([
            'action_ids', 'eh_ai_suggested_action',
            'eh_ai_suggestion_rationale',
        ])
        snapshot = case._eh_build_case_snapshot()
        self.assertGreaterEqual(
            snapshot.demand_letter_days_ago or -1, 30, snapshot,
        )
        self.assertEqual(
            case.eh_ai_suggested_action, 'refer_to_collections_agency',
        )

    def test_recent_demand_letter_does_not_claim_30_day_nonresponse(self):
        case = self._case(days=130, total=4000.0)
        case.action_log_action('letter', "Demand letter sent today")

        self.assertEqual(
            case.eh_ai_suggested_action, 'review_demand_letter',
        )
        self.assertNotIn('30+', case.eh_ai_suggestion_rationale)

    def test_demand_letter_at_90_is_not_sent_twice(self):
        case = self._case(days=95, total=4000.0)
        case.action_log_action('letter', "Demand letter already sent")

        self.assertEqual(case.eh_ai_suggested_action,
                         'review_demand_letter')

    def test_automated_followup_marks_demand_letter_evidence(self):
        case = self._case(days=95, total=4000.0)
        level = self.env['eh.collections.followup.level'].create({
            'name': 'AI automated final demand',
            'company_id': self.company.id,
            'sequence': 987,
            'days_overdue': 90,
            'action_type': 'activity',
            'eh_ai_is_demand_letter': True,
        })

        self.assertTrue(case._apply_followup_level(level, source='test'))
        action = case.action_ids.sorted('id', reverse=True)[:1]
        self.assertTrue(action.eh_ai_is_demand_letter)
        self.assertEqual(case._eh_build_case_snapshot().has_demand_letter,
                         True)
        self.assertEqual(case.eh_ai_suggested_action,
                         'review_demand_letter')

    def test_first_contact_email_when_no_contact(self):
        case = self._case(days=35, total=1000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'first_contact_email')

    def test_phone_call_after_contact(self):
        case = self._case(days=50, total=1000.0)
        case.action_log_action('email', "First email", contact_made=True)
        self.assertEqual(case.eh_ai_suggested_action, 'phone_call')

    def test_resolved_disputed_and_paused_cases_do_not_keep_escalating(self):
        resolved_stage = self.env['eh.collections.stage'].create({
            'name': 'AI Resolved', 'sequence': 900, 'is_resolved': True,
        })
        disputed_stage = self.env['eh.collections.stage'].create({
            'name': 'AI Disputed', 'sequence': 800, 'is_disputed': True,
        })
        resolved = self._case(
            days=200, total=5000.0,
            partner=self.env['res.partner'].create({'name': 'AI Resolved'}),
        )
        disputed = self._case(
            days=200, total=5000.0,
            partner=self.env['res.partner'].create({'name': 'AI Disputed'}),
        )
        paused = self._case(
            days=200, total=5000.0,
            partner=self.env['res.partner'].create({'name': 'AI Paused'}),
        )
        resolved.stage_id = resolved_stage
        disputed.stage_id = disputed_stage
        paused.followup_paused = True
        self.assertEqual(
            resolved.eh_ai_suggested_action, 'no_action_resolved',
        )
        self.assertEqual(disputed.eh_ai_suggested_action, 'review_dispute')
        self.assertEqual(paused.eh_ai_suggested_action, 'monitor_paused_case')

    def test_active_payment_plan_is_real_case_state(self):
        case = self._case(days=120, total=5000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'send_demand_letter')
        case.has_active_payment_plan = True
        self.assertTrue(case._eh_build_case_snapshot().has_payment_plan)
        self.assertEqual(
            case.eh_ai_suggested_action, 'monitor_payment_plan',
        )

    def test_suggestion_recomputes_on_state_change(self):
        case = self._case(days=35, total=1000.0)
        self.assertEqual(case.eh_ai_suggested_action, 'first_contact_email')
        # Logging a contact while still under 45 days drops it out of
        # every contact rule -> manual review. Non-stored field must
        # reflect this on the next read.
        case.action_log_action('email', "First email", contact_made=True)
        self.assertEqual(case.eh_ai_suggested_action, 'manual_review')

    def test_demand_letter_detected_from_action_log(self):
        case = self._case(days=130, total=4000.0)
        snap = case._eh_build_case_snapshot()
        self.assertFalse(snap.has_demand_letter)
        case.action_log_action('letter', "Demand letter")
        snap = case._eh_build_case_snapshot()
        self.assertTrue(snap.has_demand_letter)

    def test_action_and_priority_are_filterable_groupable_and_sortable(self):
        early = self._case(days=35, total=100.0)
        late = self._case(
            days=200,
            total=8000.0,
            partner=self.env['res.partner'].create({'name': 'AI Late Case'}),
        )
        self.env['eh.collections.case'].flush_model([
            'eh_ai_suggested_action', 'eh_ai_suggested_priority',
        ])

        found = self.env['eh.collections.case'].search([
            ('id', 'in', (early | late).ids),
            ('eh_ai_suggested_action', '=', 'consider_write_off'),
        ])
        self.assertEqual(found, late)
        ordered = self.env['eh.collections.case'].search(
            [('id', 'in', (early | late).ids)],
            order='eh_ai_suggested_action, id',
        )
        self.assertEqual(ordered[:1], late)
        grouped = self.env['eh.collections.case'].read_group(
            [('id', 'in', (early | late).ids)],
            ['id:count'], ['eh_ai_suggested_action'],
        )
        self.assertEqual(
            {row['eh_ai_suggested_action'] for row in grouped},
            {'first_contact_email', 'consider_write_off'},
        )

    def test_action_selection_uses_translatable_labels(self):
        field = self.env['eh.collections.case']._fields[
            'eh_ai_suggested_action'
        ]
        labels = dict(field._description_selection(self.env))
        self.assertEqual(labels['send_demand_letter'], 'Send demand letter')
        self.assertNotEqual(labels['send_demand_letter'],
                            'send_demand_letter')

    def test_stored_keys_and_live_rationale_use_distinct_computes(self):
        model_fields = self.env['eh.collections.case']._fields
        action = model_fields['eh_ai_suggested_action']
        priority = model_fields['eh_ai_suggested_priority']
        rationale = model_fields['eh_ai_suggestion_rationale']

        self.assertEqual(action.compute, priority.compute)
        self.assertNotEqual(action.compute, rationale.compute)
        self.assertTrue(action.store)
        self.assertTrue(priority.store)
        self.assertTrue(action.compute_sudo)
        self.assertTrue(priority.compute_sudo)
        self.assertFalse(rationale.store)
        self.assertFalse(rationale.compute_sudo)

    def test_reading_live_rationale_does_not_write_stored_keys(self):
        case = self._case(days=35, total=1000.0)
        stored_keys = [
            'eh_ai_suggested_action', 'eh_ai_suggested_priority',
        ]
        case.flush_recordset(stored_keys)
        self.env.cr.execute(
            "UPDATE eh_collections_case "
            "SET eh_ai_suggested_action = %s, "
            "    eh_ai_suggested_priority = %s "
            "WHERE id = %s",
            ['manual_review', 'low', case.id],
        )
        case.invalidate_recordset(
            stored_keys + ['eh_ai_suggestion_rationale'],
        )

        self.assertTrue(case.eh_ai_suggestion_rationale)
        case.flush_recordset(stored_keys)
        self.env.cr.execute(
            "SELECT eh_ai_suggested_action, eh_ai_suggested_priority "
            "FROM eh_collections_case WHERE id = %s",
            [case.id],
        )

        self.assertEqual(self.env.cr.fetchone(), ('manual_review', 'low'))

    def test_action_recency_uses_actor_civil_date(self):
        case = self._case(days=50, total=1000.0).with_context(
            tz='America/Los_Angeles',
        )
        utc_instant = datetime(2026, 1, 1, 0, 30)
        with patch.object(
            action_model.fields.Datetime, 'now', return_value=utc_instant,
        ), patch.object(
            ai_case_model.fields.Date, 'context_today',
            return_value=date(2025, 12, 31),
        ):
            case.action_log_action('email', 'Civil-date boundary')
            snapshot = case._eh_build_case_snapshot()

        self.assertEqual(snapshot.last_action_days_ago, 0)

    def test_migration_backfills_only_explicit_manual_letters(self):
        case = self._case(days=95, total=4000.0)
        letter = case.action_log_action('letter', 'Historic manual demand')
        email = case.action_log_action('email', 'Historic ordinary email')
        self.env.cr.execute(
            "UPDATE eh_collections_action "
            "SET eh_ai_is_demand_letter = FALSE WHERE id IN %s",
            [tuple((letter | email).ids)],
        )
        (letter | email).invalidate_recordset(['eh_ai_is_demand_letter'])
        path = Path(get_module_path('eh_account_ai_collections')) / (
            'migrations/19.0.1.2.0/post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_ai_collections_migration_120', path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.1.1.0')
        migration.migrate(self.env.cr, '19.0.1.1.0')
        (letter | email).invalidate_recordset(['eh_ai_is_demand_letter'])

        self.assertTrue(letter.eh_ai_is_demand_letter)
        self.assertFalse(email.eh_ai_is_demand_letter)
