# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Follow-up engine tests.

Verifies level selection, apply semantics (counter increment, action
log row, level advance), pause behaviour, and the cron's per-case
isolation.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestFollowupEngine(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Level = cls.env['eh.collections.followup.level']

        # Three levels: 7d email, 30d email + activity, 60d terminal escalate.
        # Mail templates from the default data are present at install; we
        # reuse them so we exercise the real send path.
        cls.tpl_l1 = cls.env.ref(
            'eh_account_collections.email_template_eh_dunning_friendly',
        )
        cls.tpl_l2 = cls.env.ref(
            'eh_account_collections.email_template_eh_dunning_firm',
        )
        cls.partner_a.email = 'collections-followup-a@example.com'

        # Discard demo levels for this company so test creates a clean
        # ladder against a known shape.
        cls.Level.search([
            ('company_id', '=', cls.env.company.id),
        ]).unlink()

        cls.level_1 = cls.Level.create({
            'name': 'Test L1',
            'sequence': 10,
            'days_overdue': 7,
            'action_type': 'email',
            'mail_template_id': cls.tpl_l1.id,
            'delay_days': 14,
            'company_id': cls.env.company.id,
        })
        cls.level_2 = cls.Level.create({
            'name': 'Test L2',
            'sequence': 20,
            'days_overdue': 30,
            'action_type': 'email',
            'mail_template_id': cls.tpl_l2.id,
            'delay_days': 21,
            'company_id': cls.env.company.id,
        })
        cls.level_3 = cls.Level.create({
            'name': 'Test L3 terminal',
            'sequence': 30,
            'days_overdue': 60,
            'action_type': 'escalate',
            'escalate_to_stage_id': cls.env.ref(
                'eh_account_collections.stage_escalated',
            ).id,
            'is_terminal': True,
            'company_id': cls.env.company.id,
        })

        cls.case = cls.Case.create({
            'partner_id': cls.partner_a.id,
            'company_id': cls.env.company.id,
            'days_overdue_max': 10,
            'oldest_overdue_date': fields.Date.context_today(cls.env['res.users']),
            'total_overdue_amount': 5000.0,
        })

    # ---- level selection ----

    def test_next_level_below_threshold_returns_empty(self):
        """A case at 5 days overdue is below the 7-day L1 threshold."""
        self.case.days_overdue_max = 5
        self.assertFalse(self.case._next_followup_level())

    def test_next_level_first_run_picks_l1(self):
        self.case.days_overdue_max = 10
        chosen = self.case._next_followup_level()
        self.assertEqual(chosen, self.level_1)

    def test_first_run_picks_highest_applicable_level(self):
        self.case.days_overdue_max = 65
        self.assertEqual(self.case._next_followup_level(), self.level_3)

    def test_next_level_advances_after_l1(self):
        self.case.days_overdue_max = 35
        self.case.current_followup_level_id = self.level_1.id
        chosen = self.case._next_followup_level()
        self.assertEqual(chosen, self.level_2)

    def test_next_level_skips_below_threshold(self):
        """Case at 35 days never gets L3 (threshold 60)."""
        self.case.days_overdue_max = 35
        self.case.current_followup_level_id = self.level_2.id
        self.assertFalse(self.case._next_followup_level())

    def test_archived_current_level_does_not_stall_ladder(self):
        self.case.days_overdue_max = 65
        self.case.current_followup_level_id = self.level_1
        self.level_1.active = False
        self.assertEqual(self.case._next_followup_level(), self.level_3)

    def test_terminal_current_level_stops_even_if_later_rung_is_added(self):
        self.Level.create({
            'name': 'Must not follow terminal',
            'sequence': 40,
            'days_overdue': 70,
            'action_type': 'email',
            'mail_template_id': self.tpl_l1.id,
            'company_id': self.env.company.id,
        })
        self.case.days_overdue_max = 90
        self.case.current_followup_level_id = self.level_3
        self.assertFalse(self.case._next_followup_level())

    def test_paused_case_returns_empty(self):
        self.case.days_overdue_max = 35
        self.case.followup_paused = True
        self.assertFalse(self.case._next_followup_level())

    def test_resolved_case_returns_empty(self):
        self.case.days_overdue_max = 35
        resolved = self.env['eh.collections.stage'].search(
            [('is_resolved', '=', True)], limit=1,
        )
        self.case.stage_id = resolved.id
        self.assertFalse(self.case._next_followup_level())

    # ---- apply semantics ----

    def test_apply_increments_counter_and_advances(self):
        before = self.case.followup_count
        self.case._apply_followup_level(self.level_1, source='test')
        self.case.invalidate_recordset()
        self.assertEqual(self.case.followup_count, before + 1)
        self.assertEqual(self.case.current_followup_level_id, self.level_1)
        self.assertTrue(self.case.last_followup_sent_at)
        self.assertEqual(
            self.case.next_followup_date,
            self.case.last_followup_sent_at.date()
            + timedelta(days=self.level_1.delay_days),
        )
        # An action row must have been logged.
        action = self.case.action_ids.filtered(
            lambda a: 'Test L1' in a.summary,
        )
        self.assertTrue(action)
        self.assertFalse(
            action.contact_made,
            "queued email is not evidence that the partner was reached",
        )

    def test_email_without_recipient_does_not_send_log_or_advance(self):
        self.partner_b.email = False
        case = self.Case.create({
            'partner_id': self.partner_b.id,
            'company_id': self.env.company.id,
            'days_overdue_max': 10,
            'total_overdue_amount': 1000.0,
        })
        before_actions = len(case.action_ids)
        before_mails = self.env['mail.mail'].search_count([
            ('model', '=', case._name),
            ('res_id', '=', case.id),
        ])

        with self.assertRaisesRegex(UserError, 'no valid email address'):
            case._apply_followup_level(self.level_1, source='test')

        case.invalidate_recordset([
            'current_followup_level_id', 'followup_count',
            'last_followup_sent_at', 'next_followup_date',
        ])
        self.assertFalse(case.current_followup_level_id)
        self.assertEqual(case.followup_count, 0)
        self.assertFalse(case.last_followup_sent_at)
        self.assertEqual(len(case.action_ids), before_actions)
        self.assertEqual(
            self.env['mail.mail'].search_count([
                ('model', '=', case._name),
                ('res_id', '=', case.id),
            ]),
            before_mails,
        )

    def test_apply_email_only_no_activity_scheduled(self):
        before_activities = len(self.case.activity_ids)
        self.case._apply_followup_level(self.level_1, source='test')
        self.assertEqual(len(self.case.activity_ids), before_activities)

    def test_apply_escalate_moves_stage(self):
        # Set a non-escalated stage first.
        new_stage = self.env['eh.collections.stage'].search(
            [('is_default', '=', True)], limit=1,
        )
        self.case.stage_id = new_stage.id
        self.case.days_overdue_max = 60
        self.case.current_followup_level_id = self.level_2.id
        self.case._apply_followup_level(self.level_3, source='test')
        self.case.invalidate_recordset()
        self.assertEqual(
            self.case.stage_id,
            self.env.ref('eh_account_collections.stage_escalated'),
        )
        self.assertFalse(self.case.next_followup_date)

    # ---- manual trigger ----

    def test_action_send_followup_now_below_threshold_raises(self):
        self.case.days_overdue_max = 5
        with self.assertRaises(UserError):
            self.case.action_send_followup_now()

    def test_action_send_followup_now_advances(self):
        self.case.days_overdue_max = 10
        before = self.case.followup_count
        self.case.action_send_followup_now()
        self.case.invalidate_recordset()
        self.assertEqual(self.case.followup_count, before + 1)
        self.assertEqual(self.case.current_followup_level_id, self.level_1)

    # ---- cron ----

    def test_cron_isolates_failures(self):
        """A case that throws inside _apply_followup_level must not stop
        the cron from processing the next case in the batch.
        """
        # The second case has no valid email, so _apply_followup_level raises
        # *inside* its savepoint. The valid first case must still advance.
        self.partner_b.email = False
        failing_case = self.Case.create({
            'partner_id': self.partner_b.id,
            'company_id': self.env.company.id,
            'days_overdue_max': 10,
            'oldest_overdue_date': fields.Date.context_today(self.env['res.users']),
            'total_overdue_amount': 1000.0,
        })
        self.Case._cron_send_followups()
        self.case.invalidate_recordset()
        failing_case.invalidate_recordset()
        self.assertEqual(self.case.followup_count, 1)
        self.assertEqual(failing_case.followup_count, 0)

    # ---- constraints ----

    def test_email_level_without_template_rejects(self):
        with self.assertRaises(ValidationError):
            self.Level.create({
                'name': 'No template',
                'sequence': 99,
                'days_overdue': 100,
                'action_type': 'email',
                'mail_template_id': False,
                'company_id': self.env.company.id,
            })

    def test_escalation_level_without_target_rejects(self):
        with self.assertRaises(ValidationError):
            self.Level.create({
                'name': 'Unsafe escalation',
                'sequence': 100,
                'days_overdue': 100,
                'action_type': 'escalate',
                'company_id': self.env.company.id,
            })
