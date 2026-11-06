# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Headless-browser proof for the functional close-workflow tour."""

from datetime import date

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_close_workflow', 'post_install', '-at_install')
class TestCloseWorkflowTour(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        admin = cls.env.ref('base.user_admin')
        admin.group_ids |= cls.env.ref('eh_account_base.group_eh_manager')
        checklist = cls.env['eh.close.checklist'].create({
            'name': 'Close Tour Checklist',
            'code': 'close_tour_checklist',
            'ledger_lock_policy': 'signoff_only',
            'task_template_ids': [(0, 0, {
                'name': 'Reconcile the tour bank',
                'responsible_role': 'accountant',
            })],
        })
        cls.close_run = checklist.action_create_run(
            name='Close Workflow Tour Run',
            period_from=date(2025, 7, 1),
            period_to=date(2025, 7, 31),
        )

    def test_close_workflow_tour_navigates_real_controls(self):
        self.start_tour(
            '/odoo/action-eh_account_close_workflow.action_eh_close_run',
            'eh_close_setup_tour',
            login='admin',
        )
        self.close_run.invalidate_recordset(['state', 'prepared_by_id'])
        self.assertEqual(self.close_run.state, 'in_progress')
        self.assertEqual(
            self.close_run.prepared_by_id, self.env.ref('base.user_admin'),
        )
        self.assertFalse(self.close_run.task_ids.blocked_reason)
