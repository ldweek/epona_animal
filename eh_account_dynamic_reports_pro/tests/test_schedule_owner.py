# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Scheduled-report ownership and company-scope regression tests.

The vulnerability: user_id was a plain, writable Many2one and _build_attachments
bound the (root cron) render to it via with_user(). A basic accounting user
(group_eh_user has create/write on eh.report.schedule) could point user_id at a
system administrator or a better-scoped colleague, then let the hourly cron
render another company/user's financials under those elevated rights and email
them to an attacker-controlled address, defeating the company-scope clamp.

The defences exercised here are:

* _build_attachments now binds with_user() to create_uid (stamped once by the
  ORM, unforgeable over RPC), not user_id.
* user_id is fixed to create_uid. Ordinary users see only their own rows;
  company Accounting Managers may administer colleague schedules without
  changing the immutable execution identity.
* execution refuses missing, inactive, technical, delegated, or newly
  unauthorized creators before a report handler or delivery channel runs.
* scheduled render options and the report environment are narrowed to exactly
  schedule.company_id, even when create_uid is multi-company.

The test env runs as SUPERUSER (guard-exempt), so the negative assertions are
made through with_user() as freshly provisioned non-superusers; skipTest
gracefully if the environment forbids provisioning them.
"""

import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestScheduleOwner(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.report = DynRep.search([('code', '=', 'trial_balance')], limit=1)
        if not cls.report:
            cls.report = DynRep.create({
                'code': 'trial_balance',
                'name': 'Trial Balance',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        cls.Schedule = cls.env['eh.report.schedule']

        def _mk(login, group_xmlids):
            # Odoo 19 uses group_ids on res.users (the 16/17 backport transform
            # rewrites it to groups_id). Single company_id (never a two-element
            # company_ids command, which the transform mangles).
            try:
                return cls.env['res.users'].create({
                    'name': login,
                    'login': login,
                    'company_id': cls.company.id,
                    'group_ids': [(6, 0, [
                        cls.env.ref(x).id for x in group_xmlids])],
                })
            except Exception:  # pragma: no cover - hardened env may forbid it
                return None

        cls.manager = _mk('eh_sched_mgr', [
            'base.group_user', 'eh_account_base.group_eh_manager'])
        cls.plain = _mk('eh_sched_plain', [
            'base.group_user', 'eh_account_base.group_eh_user'])
        cls.other = _mk('eh_sched_other', [
            'base.group_user', 'eh_account_base.group_eh_user'])
        cls.sysadmin = _mk('eh_sched_sys', [
            'base.group_user', 'base.group_system',
            'eh_account_base.group_eh_user'])
        cls.other_company = cls.env['res.company'].create({
            'name': 'EH Schedule Other Company',
        })
        if cls.manager:
            cls.manager.sudo().write({
                'company_ids': [(6, 0, [
                    cls.company.id, cls.other_company.id,
                ])],
            })

    def _vals(self, **overrides):
        vals = {
            'name': 'Owner TB',
            'report_id': self.report.id,
            'options_json': json.dumps({'company_ids': [self.company.id]}),
            'interval': 1,
            'interval_unit': 'month',
            'next_run': fields.Datetime.now() - timedelta(minutes=1),
            'subject': 'Owner test',
            'recipient_emails': 'owner@example.com',
            'delivery_format': 'xlsx',
        }
        vals.update(overrides)
        return vals

    # ---- create/write owner guard ----

    def test_non_manager_cannot_own_another_users_schedule(self):
        if not (self.plain and self.other):
            self.skipTest("Could not provision non-manager test users.")
        with self.assertRaises(UserError):
            self.Schedule.with_user(self.plain).create(
                self._vals(user_id=self.other.id))

    def test_non_manager_cannot_repoint_owner_on_write(self):
        if not (self.plain and self.other):
            self.skipTest("Could not provision non-manager test users.")
        # Owns its own schedule (default user_id == the creator).
        schedule = self.Schedule.with_user(self.plain).create(self._vals())
        with self.assertRaises(UserError):
            schedule.with_user(self.plain).write({'user_id': self.other.id})

    def test_nobody_below_system_can_assign_to_system_admin(self):
        # A manager clears the "own only" check but must still be refused
        # when handing the schedule to a base.group_system user.
        if not (self.manager and self.sysadmin):
            self.skipTest("Could not provision manager / sysadmin test users.")
        with self.assertRaises(UserError):
            self.Schedule.with_user(self.manager).create(
                self._vals(user_id=self.sysadmin.id))

    def test_manager_cannot_split_owner_from_creator(self):
        if not (self.manager and self.other):
            self.skipTest("Could not provision manager / plain test users.")
        with self.assertRaises(UserError):
            self.Schedule.with_user(self.manager).create(
                self._vals(user_id=self.other.id))

    def test_non_manager_may_own_own_schedule(self):
        if not self.plain:
            self.skipTest("Could not provision a non-manager test user.")
        schedule = self.Schedule.with_user(self.plain).create(
            self._vals(user_id=self.plain.id))
        self.assertEqual(schedule.create_uid, self.plain)
        self.assertEqual(schedule.user_id, self.plain)

    # ---- immutable creator authorization ----

    def test_nonowner_cannot_find_edit_run_pause_resume_or_delete(self):
        if not (self.manager and self.plain):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.manager).create(self._vals())
        foreign = schedule.with_user(self.plain)

        self.assertFalse(
            self.Schedule.with_user(self.plain).search([
                ('id', '=', schedule.id),
            ]),
            "creator record rule must hide another user's schedule",
        )
        with self.assertRaises(AccessError):
            foreign.write({'subject': 'stolen'})
        with self.assertRaises(AccessError):
            foreign.action_run_now()
        with self.assertRaises(AccessError):
            foreign.action_pause()
        with self.assertRaises(AccessError):
            foreign.action_resume()
        with self.assertRaises(AccessError):
            foreign.unlink()

    def test_manager_can_find_pause_and_delete_inactive_colleague_schedule(self):
        if not (self.manager and self.plain):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.plain).create(self._vals())
        self.plain.sudo().write({'active': False})
        manager_model = self.Schedule.with_user(self.manager).with_context(
            allowed_company_ids=[self.company.id],
        )
        visible = manager_model.search([('id', '=', schedule.id)])
        self.assertEqual(visible, schedule.with_user(self.manager))
        visible.action_pause()
        self.assertFalse(visible.active)
        visible.unlink()
        self.assertFalse(schedule.exists())

    def _assert_execution_blocked(self, schedule, message_pattern):
        """Assert authorization fails before any report bytes are built."""
        rendered = []
        ReportCls = type(self.report)

        def _spy(report_self, options, use_cache=True):
            rendered.append(report_self.id)
            return b'PKmust-not-render'

        self.patch(ReportCls, 'render_xlsx', _spy)
        cron_schedule = schedule.sudo()
        with self.assertRaisesRegex(AccessError, message_pattern):
            cron_schedule._build_attachments(cron_schedule._parse_options())
        self.assertFalse(rendered)

    def test_execution_rejects_missing_creator(self):
        if not self.manager:
            self.skipTest("Could not provision manager test user.")
        schedule = self.Schedule.with_user(self.manager).create(self._vals())
        self.env.cr.execute(
            "UPDATE eh_report_schedule SET create_uid = NULL WHERE id = %s",
            [schedule.id],
        )
        schedule.invalidate_recordset(['create_uid'])

        self._assert_execution_blocked(schedule, 'no creator')

    def test_execution_rejects_inactive_creator(self):
        if not self.plain:
            self.skipTest("Could not provision plain test user.")
        schedule = self.Schedule.with_user(self.plain).create(self._vals())
        self.plain.sudo().write({'active': False})

        self._assert_execution_blocked(schedule, 'inactive')

    def test_execution_rechecks_report_access_after_group_revocation(self):
        if not self.plain:
            self.skipTest("Could not provision plain test user.")
        schedule = self.Schedule.with_user(self.plain).create(self._vals())
        eh_user = self.env.ref('eh_account_base.group_eh_user')
        self.plain.sudo().write({'group_ids': [(3, eh_user.id)]})

        self._assert_execution_blocked(schedule, 'not allowed|access')

    def test_execution_rejects_technical_superuser_creator(self):
        # Models/demo XML and careless sudo code create rows as user 1.  User
        # 1's ACL bypass is not an explicit grant and must never become the
        # authority for emailed financial data.
        schedule = self.Schedule.create(self._vals())

        self._assert_execution_blocked(schedule, 'technical superuser')

    def test_execution_rejects_legacy_delegated_owner(self):
        if not (self.manager and self.other):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.manager).create(self._vals())
        # Simulate a row written before user_id became immutable.  Do not
        # migrate its authority to either side: delegation may have been the
        # original escalation vector.
        schedule.sudo().write({'user_id': self.other.id})

        self._assert_execution_blocked(schedule, 'conflicting legacy owners')

    def test_upgrade_quarantines_unsafe_legacy_rows(self):
        if not (self.manager and self.plain and self.other):
            self.skipTest("Could not provision schedule test users.")
        valid = self.Schedule.with_user(self.manager).create(
            self._vals(name='Valid owner'),
        )
        root_created = self.Schedule.create(
            self._vals(name='Module-created legacy'),
        )
        delegated = self.Schedule.with_user(self.manager).create(
            self._vals(name='Delegated legacy'),
        )
        delegated.sudo().write({'user_id': self.other.id})
        revoked = self.Schedule.with_user(self.plain).create(
            self._vals(name='Revoked legacy'),
        )
        eh_user = self.env.ref('eh_account_base.group_eh_user')
        self.plain.sudo().write({'group_ids': [(3, eh_user.id)]})

        quarantined = set(
            self.Schedule._eh_quarantine_unsafe_schedules(),
        )

        self.assertNotIn(valid.id, quarantined)
        self.assertTrue(valid.active)
        for unsafe in (root_created, delegated, revoked):
            inspected = unsafe.sudo()
            self.assertIn(inspected.id, quarantined)
            self.assertFalse(inspected.active)
            self.assertEqual(inspected.last_run_status, 'error')
            self.assertIn(
                'Disabled during upgrade', inspected.last_error or '',
            )

        # Post-migrations may be retried after a deployment interruption.
        # Already-quarantined rows are inactive and a valid row remains safe,
        # so a second run must be a no-op.
        self.assertFalse(
            self.Schedule._eh_quarantine_unsafe_schedules(),
        )
        self.assertTrue(valid.active)

    def test_cron_records_revoked_owner_failure_without_rendering(self):
        if not self.plain:
            self.skipTest("Could not provision plain test user.")
        schedule = self.Schedule.with_user(self.plain).create(self._vals(
            name='Revoked cron owner',
            next_run=fields.Datetime.now() - timedelta(hours=1),
        ))
        eh_user = self.env.ref('eh_account_base.group_eh_user')
        self.plain.sudo().write({'group_ids': [(3, eh_user.id)]})
        rendered = []
        ReportCls = type(self.report)

        def _spy(report_self, options, use_cache=True):
            rendered.append(report_self.id)
            return b'PKmust-not-render'

        self.patch(ReportCls, 'render_xlsx', _spy)
        self.Schedule._cron_run_due(auto_commit=False)
        inspected = schedule.sudo()
        inspected.invalidate_recordset()

        self.assertEqual(inspected.last_run_status, 'error')
        self.assertTrue(inspected.last_error)
        self.assertFalse(rendered)

    # ---- render binds and scopes to create_uid/company_id ----

    def test_render_runs_as_create_uid(self):
        """Attachment rendering runs as the immutable creator."""
        if not (self.manager and self.other):
            self.skipTest("Could not provision manager / plain test users.")
        schedule = self.Schedule.with_user(self.manager).create(self._vals())
        self.assertEqual(schedule.create_uid, schedule.user_id)

        captured = {}
        ReportCls = type(self.report)
        original = ReportCls.render_xlsx

        def _spy(report_self, options, use_cache=True):
            captured['uid'] = report_self.env.uid
            return b'PKspy'

        self.patch(ReportCls, 'render_xlsx', _spy)
        schedule._build_attachments(schedule._parse_options())

        self.assertEqual(
            captured.get('uid'), schedule.create_uid.id,
            "render must bind to the immutable create_uid",
        )
        # Guard against accidentally leaving the spy patched (self.patch
        # auto-reverts, but assert the original is a real bound method).
        self.assertTrue(callable(original))

    def test_render_is_narrowed_to_schedule_company(self):
        """A multi-company creator cannot smuggle another company through
        options_json while the schedule itself belongs to this company."""
        if not self.manager:
            self.skipTest("Could not provision manager test user.")
        schedule = self.Schedule.with_user(self.manager).create(self._vals(
            company_id=self.company.id,
            options_json=json.dumps({
                'company_ids': [self.other_company.id],
                'posted_only': True,
            }),
        ))
        captured = {}
        ReportCls = type(self.report)

        def _spy(report_self, options, use_cache=True):
            captured['options_company_ids'] = options.get('company_ids')
            captured['allowed_company_ids'] = report_self.env.context.get(
                'allowed_company_ids',
            )
            captured['env_company'] = report_self.env.company.id
            captured['env_companies'] = report_self.env.companies.ids
            return b'PKspy'

        self.patch(ReportCls, 'render_xlsx', _spy)
        schedule._build_attachments(schedule._parse_options())

        expected = [self.company.id]
        self.assertEqual(captured['options_company_ids'], expected)
        self.assertEqual(captured['allowed_company_ids'], expected)
        self.assertEqual(captured['env_company'], self.company.id)
        self.assertEqual(captured['env_companies'], expected)
