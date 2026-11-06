# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Schedule tests.

Covers recipient resolution, advance_next_run for daily / weekly /
monthly cadences, _cron_run_due picks up due schedules, error isolation
when delivery fails, and the pause / resume actions.

Email delivery itself goes through Odoo's mail.mail.  Success-path tests mock
an explicit transport acknowledgement; an unconfirmed/false return must stay
a delivery failure.
"""

import json
from datetime import date, timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestSchedule(EhAccountIntegrationTestCase):

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
        cls.schedule_user = cls.env['res.users'].create({
            'name': 'EH Schedule Functional User',
            'login': 'eh_schedule_functional_user',
            'email': 'eh-schedule-user@example.com',
            'company_id': cls.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        # A real schedule must have a non-technical creator who retains
        # explicit report access.  Creating fixtures as TransactionCase's
        # user 1 would exercise a legacy/module-data row, which is now
        # intentionally fail-closed.
        cls.Schedule = cls.env['eh.report.schedule'].with_user(
            cls.schedule_user,
        )
        # Seed at least one move so render does not return an empty payload.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )

    def _make_schedule(self, **overrides):
        vals = {
            'name': 'Monthly TB',
            'report_id': self.report.id,
            'options_json': json.dumps({
                'date': {
                    'date_from': '2026-01-01',
                    'date_to': '2026-12-31',
                },
                'company_ids': [self.company.id],
                'posted_only': True,
                'show_zero': False,
            }),
            'interval': 1,
            'interval_unit': 'month',
            'next_run': fields.Datetime.now() - timedelta(minutes=1),
            'subject': 'Test report',
            'body': '<p>Body</p>',
            'recipient_emails': 'recipient@example.com',
            'delivery_format': 'xlsx',
        }
        vals.update(overrides)
        return self.Schedule.create(vals)

    def _confirmed_mail_transport(self):
        return patch.object(
            type(self.env['mail.mail']),
            'send',
            return_value=None,
        )

    # ---- recipient resolution ----

    def test_resolve_recipient_emails_combines_all_sources(self):
        partner = self.env['res.partner'].create({
            'name': 'Recipient One', 'email': 'one@example.com',
        })
        user = self.env['res.users'].create({
            'name': 'User Two', 'login': 'sched_user_two',
            'email': 'two@example.com',
        })
        schedule = self._make_schedule(
            recipient_emails='extra@example.com,duplicate@example.com',
            recipient_partner_ids=[(6, 0, [partner.id])],
            recipient_user_ids=[(6, 0, [user.id])],
        )
        emails = schedule._resolve_recipient_emails()
        self.assertIn('one@example.com', emails)
        self.assertIn('two@example.com', emails)
        self.assertIn('extra@example.com', emails)
        self.assertIn('duplicate@example.com', emails)
        self.assertEqual(len(emails), len(set(emails)))

    def test_resolve_skips_invalid_email_fragments(self):
        schedule = self._make_schedule(
            recipient_emails=' , not-an-email, real@example.com , ',
        )
        emails = schedule._resolve_recipient_emails()
        self.assertEqual(emails, ['real@example.com'])

    def test_send_now_raises_when_no_recipients(self):
        schedule = self._make_schedule(recipient_emails=False)
        with self.assertRaises(UserError):
            schedule._send_now()

    def test_malformed_or_non_object_options_fail_when_saved(self):
        for raw_options in (
            '', '{bad', '[]', 'null', '"text"', '1', 'true',
            '{"limit": NaN}', '{"limit": Infinity}',
        ):
            with self.subTest(options_json=raw_options):
                with self.assertRaises(ValidationError):
                    self._make_schedule(options_json=raw_options)

    def test_legacy_malformed_options_still_fail_closed_at_run_time(self):
        schedule = self._make_schedule()
        self.env.cr.execute(
            "UPDATE eh_report_schedule SET options_json = '{bad' "
            "WHERE id = %s",
            [schedule.id],
        )
        schedule.invalidate_recordset(['options_json'])
        with self.assertRaises(UserError):
            schedule._parse_options()

    def test_options_reject_unknown_token_and_reversed_range_when_saved(self):
        for date_options in (
            {'date_from': 'auto_unknown', 'date_to': 'today'},
            {'date_from': '2026-08-02', 'date_to': '2026-08-01'},
        ):
            with self.subTest(date_options=date_options):
                with self.assertRaises(ValidationError):
                    self._make_schedule(options_json=json.dumps({
                        'date': date_options,
                    }))

    def test_structured_custom_period_overrides_only_json_date(self):
        schedule = self._make_schedule(
            period_preset='custom',
            period_date_from='2026-04-01',
            period_date_to='2026-06-30',
            options_json=json.dumps({
                'date': {
                    'date_from': '2020-01-01',
                    'date_to': '2020-01-31',
                },
                'posted_only': True,
                'analytic_account_ids': [42],
            }),
        )
        options = schedule._parse_options()
        self.assertEqual(options['date'], {
            'mode': 'range',
            'date_from': '2026-04-01',
            'date_to': '2026-06-30',
        })
        self.assertTrue(options['posted_only'])
        self.assertEqual(options['analytic_account_ids'], [42])

    def test_structured_previous_month_resolves_at_delivery_time(self):
        schedule = self._make_schedule(
            period_preset='previous_month',
            options_json=json.dumps({'posted_only': True}),
        )
        with patch.object(
            fields.Date,
            'context_today',
            return_value=date(2026, 8, 24),
        ):
            options = schedule._parse_options()
        self.assertEqual(options['date'], {
            'mode': 'range',
            'date_from': '2026-07-01',
            'date_to': '2026-07-31',
        })

    def test_parse_options_resolves_relative_dates_at_run_time(self):
        schedule = self._make_schedule(options_json=json.dumps({
            'date': {
                'mode': 'range',
                'date_from': 'auto_prev_month_start',
                'date_to': 'auto_prev_month_end',
            },
            'posted_only': True,
        }))
        with patch.object(
            fields.Date,
            'context_today',
            return_value=date(2026, 8, 24),
        ):
            options = schedule._parse_options()
        self.assertEqual(options['date']['date_from'], '2026-07-01')
        self.assertEqual(options['date']['date_to'], '2026-07-31')

    def test_parse_options_resolves_current_period_tokens(self):
        schedule = self._make_schedule(options_json=json.dumps({
            'date': {
                'mode': 'range',
                'date_from': 'auto_qtd',
                'date_to': 'today',
            },
        }))
        with patch.object(
            fields.Date,
            'context_today',
            return_value=date(2026, 8, 24),
        ):
            options = schedule._parse_options()
        self.assertEqual(options['date']['date_from'], '2026-07-01')
        self.assertEqual(options['date']['date_to'], '2026-08-24')

    def test_create_rejects_unknown_relative_date(self):
        with self.assertRaisesRegex(ValidationError, 'unsupported date token'):
            self._make_schedule(options_json=json.dumps({
                'date': {
                    'date_from': 'auto_unknown_period',
                    'date_to': 'today',
                },
            }))

    # ---- cadence ----

    def test_advance_next_run_monthly(self):
        schedule = self._make_schedule(
            interval=1, interval_unit='month',
            next_run=fields.Datetime.from_string('2026-06-15 12:00:00'),
        )
        schedule._advance_next_run(
            reference=fields.Datetime.from_string('2026-06-16 18:00:00'),
        )
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-07-15 12:00:00'),
        )

    def test_advance_next_run_weekly(self):
        schedule = self._make_schedule(
            interval=2, interval_unit='week',
            next_run=fields.Datetime.from_string('2026-06-15 12:00:00'),
        )
        schedule._advance_next_run(
            reference=fields.Datetime.from_string('2026-06-15 12:00:00'),
        )
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-06-29 12:00:00'),
        )

    def test_advance_next_run_daily(self):
        schedule = self._make_schedule(
            interval=3, interval_unit='day',
            next_run=fields.Datetime.from_string('2026-06-15 12:00:00'),
        )
        schedule._advance_next_run(
            reference=fields.Datetime.from_string('2026-06-15 12:00:00'),
        )
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-06-18 12:00:00'),
        )

    def test_advance_next_run_keeps_late_daily_wall_clock(self):
        schedule = self._make_schedule(
            interval=1,
            interval_unit='day',
            next_run=fields.Datetime.from_string('2026-06-15 09:37:00'),
        )
        schedule._advance_next_run(
            reference=fields.Datetime.from_string('2026-06-17 10:00:00'),
        )
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-06-18 09:37:00'),
        )

    def test_advance_next_run_preserves_month_end_anchor(self):
        schedule = self._make_schedule(
            interval=1,
            interval_unit='month',
            next_run=fields.Datetime.from_string('2026-01-31 17:45:00'),
        )
        schedule._advance_next_run(
            reference=fields.Datetime.from_string('2026-03-01 00:00:00'),
        )
        self.assertEqual(
            schedule.next_run,
            fields.Datetime.from_string('2026-03-31 17:45:00'),
        )

    # ---- send / cron ----

    def test_send_now_creates_attachment_and_records_success(self):
        schedule = self._make_schedule()
        with self._confirmed_mail_transport():
            schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertTrue(schedule.last_run)
        self.assertEqual(schedule.last_attachment_count, 1)
        self.assertFalse(schedule.last_error)

    def test_email_delivery_accepts_core_none_success_contract(self):
        schedule = self._make_schedule()
        attachment = {
            'name': 'trial_balance.xlsx',
            'datas': b'UEs=',
            'mimetype': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        }
        with patch.object(
            type(self.env['mail.mail']),
            'send',
            return_value=None,
        ) as send:
            schedule._dispatch_email([attachment], '<p>Body</p>')
        send.assert_called_once_with(raise_exception=True)

    def test_email_delivery_propagates_transport_exception(self):
        schedule = self._make_schedule()
        attachment = {
            'name': 'trial_balance.xlsx',
            'datas': b'UEs=',
            'mimetype': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        }
        Mail = self.env['mail.mail'].sudo()
        Attachment = self.env['ir.attachment'].sudo()
        mail_ids_before = set(Mail.search([]).ids)
        attachment_ids_before = set(Attachment.search([]).ids)
        with patch.object(
            type(self.env['mail.mail']),
            'send',
            side_effect=UserError('simulated SMTP failure'),
        ):
            with self.assertRaisesRegex(UserError, 'SMTP failure'):
                schedule._dispatch_email([attachment], '<p>Body</p>')
        self.assertEqual(set(Mail.search([]).ids), mail_ids_before)
        self.assertEqual(
            set(Attachment.search([]).ids), attachment_ids_before,
        )

    def test_email_failure_cleanup_keeps_webhook_success_truthful(self):
        schedule = self._make_schedule(
            delivery_channel='both',
            webhook_url='https://example.com/hook',
        )
        attachment = {
            'name': 'partial-success.xlsx',
            'datas': b'UEs=',
            'mimetype': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        }
        Mail = self.env['mail.mail'].sudo()
        Attachment = self.env['ir.attachment'].sudo()
        with patch.object(
            type(schedule), '_build_attachments', return_value=[attachment],
        ), patch.object(
            type(self.env['mail.mail']),
            'send',
            side_effect=UserError('simulated SMTP failure'),
        ), patch.object(
            type(schedule), '_dispatch_webhook', return_value=None,
        ) as webhook:
            schedule._send_now()

        webhook.assert_called_once()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertIn('SMTP failure', schedule.last_error or '')
        self.assertFalse(Mail.search([
            ('subject', '=', schedule.subject),
            ('email_to', '=', schedule.recipient_emails),
        ]))
        self.assertFalse(Attachment.search([
            ('name', '=', attachment['name']),
            ('res_model', '=', 'mail.mail'),
        ]))

    def test_email_sender_prefers_schedule_company(self):
        with self.env.cr.savepoint():
            try:
                delivery_company = self.env['res.company'].create({
                    'name': 'Scheduled Delivery Company',
                    'email': 'reports@scheduled-company.example',
                })
            except Exception as exc:
                self.skipTest(
                    f"environment cannot create a second company: {exc}"
                )
                return
        self.schedule_user.sudo().write({
            'company_ids': [(4, delivery_company.id)],
        })
        schedule = self._make_schedule(company_id=delivery_company.id)
        fake_mail = Mock()
        fake_mail.send.return_value = None
        attachment = {
            'name': 'company-sender.xlsx',
            'datas': b'UEs=',
            'mimetype': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        }
        with patch.object(
            type(self.env['mail.mail']), 'create', return_value=fake_mail,
        ) as create:
            schedule._dispatch_email([attachment], '<p>Body</p>')
        self.assertEqual(
            create.call_args.args[0]['email_from'],
            delivery_company.email,
        )

    def test_both_format_build_is_atomic_when_pdf_fails(self):
        schedule = self._make_schedule(delivery_format='both')
        report_type = type(schedule.report_id)
        with patch.object(
            report_type, 'render_xlsx', return_value=b'xlsx',
        ), patch.object(
            report_type, 'render_pdf', side_effect=UserError('pdf failed'),
        ):
            with self.assertRaisesRegex(UserError, 'pdf failed'):
                schedule._build_attachments(schedule._parse_options())

    def test_webhook_delivery_attachment_retention_is_bounded(self):
        Attachment = self.env['ir.attachment'].sudo()
        old_delivery = Attachment.create({
            'name': 'old-report.pdf',
            'datas': b'b2xk',
            'mimetype': 'application/pdf',
            'res_model': 'eh.report.schedule',
            'res_id': self._make_schedule().id,
            'eh_report_schedule_delivery': True,
        })
        fresh_delivery = Attachment.create({
            'name': 'fresh-report.pdf',
            'datas': b'ZnJlc2g=',
            'mimetype': 'application/pdf',
            'res_model': 'eh.report.schedule',
            'res_id': self._make_schedule().id,
            'eh_report_schedule_delivery': True,
        })
        unrelated = Attachment.create({
            'name': 'user-document.pdf',
            'datas': b'dXNlcg==',
            'mimetype': 'application/pdf',
            'res_model': 'eh.report.schedule',
            'res_id': self._make_schedule().id,
        })
        old_date = fields.Datetime.now() - timedelta(days=31)
        self.env.cr.execute(
            "UPDATE ir_attachment SET create_date = %s WHERE id IN %s",
            [old_date, (old_delivery.id, unrelated.id)],
        )
        Attachment._gc_eh_report_schedule_deliveries()
        self.assertFalse(old_delivery.exists())
        self.assertTrue(fresh_delivery.exists())
        self.assertTrue(unrelated.exists())

    def test_action_run_now_advances_next_run(self):
        schedule = self._make_schedule(
            interval=1, interval_unit='month',
        )
        before = schedule.next_run
        with self._confirmed_mail_transport():
            schedule.action_run_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertGreater(schedule.next_run, before)

    def test_action_run_now_keeps_future_planned_cadence(self):
        planned = fields.Datetime.now() + timedelta(days=10)
        schedule = self._make_schedule(next_run=planned)
        with self._confirmed_mail_transport():
            schedule.action_run_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertEqual(schedule.next_run, planned)

    def test_action_run_now_failure_persists_truthful_telemetry(self):
        schedule = self._make_schedule()
        schedule.sudo().write({'last_attachment_count': 3})
        planned = schedule.next_run
        with patch.object(
            type(schedule),
            '_send_now',
            side_effect=UserError('manual render failed'),
        ):
            action = schedule.action_run_now()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(schedule.last_run_status, 'error')
        self.assertTrue(schedule.last_run)
        self.assertIn('manual render failed', schedule.last_error or '')
        self.assertEqual(schedule.last_attachment_count, 0)
        self.assertGreater(schedule.next_run, planned)

    def test_action_run_now_rejects_concurrent_delivery_claim(self):
        schedule = self._make_schedule()
        with patch.object(
                type(schedule), '_eh_try_lock_delivery', return_value=False), \
                patch.object(type(schedule), '_send_now') as send_now:
            with self.assertRaisesRegex(UserError, 'already being delivered'):
                schedule.action_run_now()
        send_now.assert_not_called()

    def test_cron_skips_concurrently_claimed_schedule(self):
        schedule = self._make_schedule()
        with patch.object(
                type(schedule), '_eh_try_lock_delivery', return_value=False), \
                patch.object(type(schedule), '_send_now') as send_now:
            self.Schedule._cron_run_due(auto_commit=False)
        send_now.assert_not_called()
        schedule.invalidate_recordset()
        self.assertFalse(schedule.last_run_status)

    def test_cron_picks_up_due_schedule(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() - timedelta(hours=1),
        )
        with self._confirmed_mail_transport():
            self.Schedule._cron_run_due(auto_commit=False)
        schedule.invalidate_recordset()
        self.assertEqual(schedule.last_run_status, 'success')

    def test_cron_skips_inactive(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() - timedelta(hours=1),
            active=False,
        )
        self.Schedule._cron_run_due(auto_commit=False)
        schedule.invalidate_recordset()
        self.assertFalse(schedule.last_run_status)

    def test_cron_skips_future(self):
        schedule = self._make_schedule(
            next_run=fields.Datetime.now() + timedelta(hours=1),
        )
        self.Schedule._cron_run_due(auto_commit=False)
        schedule.invalidate_recordset()
        self.assertFalse(schedule.last_run_status)

    # ---- pause / resume ----

    def test_action_pause_and_resume(self):
        schedule = self._make_schedule()
        schedule.action_pause()
        self.assertFalse(schedule.active)
        schedule.action_resume()
        self.assertTrue(schedule.active)

    # ---- error isolation in cron ----

    def test_cron_isolates_failure(self):
        good = self._make_schedule(name='Good')
        bad = self._make_schedule(name='Bad', recipient_emails=False)
        with self._confirmed_mail_transport():
            self.Schedule._cron_run_due(auto_commit=False)
        good.invalidate_recordset()
        bad.invalidate_recordset()
        self.assertEqual(good.last_run_status, 'success')
        self.assertEqual(bad.last_run_status, 'error')
        self.assertIn('recipient', (bad.last_error or '').lower())
        # TransactionCase suppresses automatic mail tracking.  Prove the
        # persisted attempt fields use mail.thread's tracking contract; the
        # state/error assertions above prove the actual failure write.
        for field_name in (
            'last_run', 'last_run_status', 'last_error',
            'last_attachment_count',
        ):
            self.assertTrue(self.Schedule._fields[field_name].tracking)

    def test_cron_recovers_aborted_cursor_and_commits_each_outcome(self):
        due_at = fields.Datetime.now() - timedelta(hours=1)
        first = self._make_schedule(name='First', next_run=due_at)
        broken = self._make_schedule(name='Broken DB', next_run=due_at)
        last = self._make_schedule(name='Last', next_run=due_at)
        events = []

        def _send(schedule):
            events.append(('send', schedule.id))
            if schedule.id == broken.id:
                # Real PostgreSQL error: cursor stays aborted until cron's
                # per-candidate savepoint rolls back.
                schedule.env.cr.execute("SELECT 1 / 0")
            schedule.write({
                'last_run': fields.Datetime.now(),
                'last_run_status': 'success',
                'last_error': False,
                'last_attachment_count': 1,
            })

        def _commit():
            events.append(('commit', None))

        ScheduleType = type(self.Schedule)
        with mute_logger(
            'odoo.sql_db',
            'odoo.addons.eh_account_dynamic_reports_pro.models.'
            'report_schedule',
        ), patch.object(
            ScheduleType, '_send_now', _send,
        ), patch.object(
            self.env.cr, 'commit', side_effect=_commit,
        ) as commit:
            self.Schedule._cron_run_due()
            # Every cadence was advanced, including failed DB attempt; second
            # pass must not duplicate any external delivery.
            self.Schedule._cron_run_due()

        self.assertEqual(
            [event for event in events if event[0] == 'send'],
            [('send', first.id), ('send', broken.id), ('send', last.id)],
        )
        self.assertEqual(commit.call_count, 3)
        self.assertEqual(
            [event[0] for event in events],
            ['send', 'commit', 'send', 'commit', 'send', 'commit'],
        )
        (first | broken | last).invalidate_recordset()
        self.assertEqual(first.last_run_status, 'success')
        self.assertEqual(broken.last_run_status, 'error')
        self.assertIn('division by zero', broken.last_error or '')
        self.assertEqual(last.last_run_status, 'success')
        self.assertGreater(broken.next_run, due_at)
