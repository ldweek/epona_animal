# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for the webhook dispatcher on eh.report.schedule.

The HTTP POST itself is stubbed by replacing _dispatch_webhook with a
recorder so the tests run hermetically. Coverage:

* Payload shape per webhook_format (slack, teams, generic).
* HTML stripping in _strip_html collapses tags and trims length.
* Both-channel mode: an email failure does not block the webhook
  succeeding (and vice versa).
* Missing webhook_url raises UserError when channel='webhook'.
* Webhook-only channel does not require recipient emails.

The schedule needs a real eh.account.dynamic.report record because
_send_now calls _build_attachments which materialises the report; we
seed a minimal dynamic report so the call resolves without going
through the full handler stack.
"""

import json
import socket
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_dynamic_reports_pro.models import (
    report_schedule as report_schedule_module,
)


@tagged('eh_account_dynamic_reports_pro', 'integration', 'post_install', '-at_install')
class TestWebhookPayload(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Schedule = cls.env['eh.report.schedule']

    def setUp(self):
        super().setUp()
        # Bearer attachment links must use the public HTTPS origin.  Odoo's
        # test default is commonly http://localhost and is deliberately not
        # valid for external token-bearing webhook payloads.
        self.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://reports.example.test',
        )

    def _make_schedule(self, **vals):
        # The report_id is a required FK; pick any existing dynamic
        # report. Seed one with a known handler if none is present.
        report = self.env['eh.account.dynamic.report'].search([], limit=1)
        if not report:
            report = self.env['eh.account.dynamic.report'].create({
                'name': 'Webhook Test Report',
                'handler_model':
                    'eh.account.dynamic.report.handler.trial_balance',
            })
        defaults = {
            'name': 'Webhook test',
            'report_id': report.id,
            'subject': 'Test Subject',
            'body': '<p>Hello <b>world</b></p>',
            'options_json': '{}',
            'delivery_format': 'pdf',
            'recipient_emails': 'noone@example.com',
        }
        defaults.update(vals)
        return self.Schedule.create(defaults)

    # ---- payload shapes ----

    def test_slack_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
        )
        attachment_links = [
            {'name': 'tb.pdf',
             'url': 'https://example/web/content/1?download=true',
             'mimetype': 'application/pdf', 'size_bytes': 1024},
        ]
        payload = schedule._build_webhook_payload(
            subject='TB ready',
            body_text='Trial balance for April',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['text'], 'TB ready')
        self.assertEqual(len(payload['attachments']), 1)
        att = payload['attachments'][0]
        self.assertEqual(att['title'], 'tb.pdf')
        self.assertEqual(
            att['title_link'],
            'https://example/web/content/1?download=true',
        )
        self.assertEqual(att['footer'], 'ERP Heritage Accounting Suite')

    def test_slack_payload_falls_back_when_no_attachments(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
        )
        payload = schedule._build_webhook_payload(
            subject='Empty', body_text='No attachments today',
            attachment_links=[],
        )
        # Slack still gets one attachment block with the body text
        # and footer.
        self.assertEqual(len(payload['attachments']), 1)
        self.assertIn('No attachments', payload['attachments'][0]['text'])

    def test_teams_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='teams',
            webhook_url='https://outlook.office.com/webhook/...',
        )
        attachment_links = [
            {'name': 'pl.pdf', 'url': 'https://example/p/1',
             'mimetype': 'application/pdf', 'size_bytes': 2048},
            {'name': 'bs.pdf', 'url': 'https://example/p/2',
             'mimetype': 'application/pdf', 'size_bytes': 1500},
        ]
        payload = schedule._build_webhook_payload(
            subject='Reports', body_text='Period close pack',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['@type'], 'MessageCard')
        self.assertEqual(payload['summary'], 'Reports')
        self.assertEqual(payload['themeColor'], '1A2C3D')
        # Teams sections: body section + facts section.
        self.assertEqual(len(payload['sections']), 2)
        facts = payload['sections'][1]['facts']
        self.assertEqual(facts[0]['name'], 'pl.pdf')
        self.assertEqual(facts[1]['value'], 'https://example/p/2')

    def test_generic_payload_shape(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://example.com/hook',
        )
        attachment_links = [
            {'name': 'gl.pdf', 'url': 'https://example/p/3',
             'mimetype': 'application/pdf', 'size_bytes': 999},
        ]
        payload = schedule._build_webhook_payload(
            subject='GL', body_text='General ledger',
            attachment_links=attachment_links,
        )
        self.assertEqual(payload['subject'], 'GL')
        self.assertEqual(payload['body'], 'General ledger')
        self.assertEqual(payload['attachments'], attachment_links)
        self.assertEqual(payload['schedule'], schedule.name)

    # ---- HTML stripping ----

    def test_strip_html_removes_tags(self):
        text = self.Schedule._strip_html('<p>Hello <b>world</b></p>')
        self.assertEqual(text, 'Hello world')

    def test_strip_html_collapses_whitespace(self):
        text = self.Schedule._strip_html(
            '<p>One</p>\n\n  <p>Two</p>\n<br/>Three',
        )
        self.assertEqual(text, 'One Two Three')

    def test_strip_html_caps_length(self):
        long_html = '<p>' + ('x' * 5000) + '</p>'
        text = self.Schedule._strip_html(long_html)
        self.assertLessEqual(len(text), 1500)

    def test_strip_html_handles_empty(self):
        self.assertEqual(self.Schedule._strip_html(None), '')
        self.assertEqual(self.Schedule._strip_html(''), '')

    # ---- channel routing ----

    def test_webhook_only_does_not_require_emails(self):
        # Email channel raises when no recipients. Webhook channel
        # uses the URL as the destination, so it must not bounce on
        # missing recipient emails.
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://example.com/hook',
            recipient_emails=False,
        )
        # Stub the dispatcher so we don't make a real HTTP call. We
        # also stub _build_attachments to avoid pulling the real
        # rendering pipeline through the test.
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{'name': 'x.pdf', 'datas': b''}],
        ), patch.object(
            type(schedule), '_dispatch_webhook',
            return_value=None,
        ):
            schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')

    def test_webhook_url_required_when_channel_includes_webhook(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url=False,
        )
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{'name': 'x.pdf', 'datas': b''}],
        ):
            with self.assertRaises(UserError):
                schedule._send_now()

    def test_webhook_timeout_above_limit_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._make_schedule(webhook_timeout=61)

    def test_legacy_unsafe_timeout_fails_before_render_or_network(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_url='https://example.com/hook',
        )
        self.env.cr.execute(
            "ALTER TABLE eh_report_schedule DROP CONSTRAINT "
            "eh_report_schedule_bounded_webhook_timeout"
        )
        self.env.cr.execute(
            "UPDATE eh_report_schedule SET webhook_timeout = 61 "
            "WHERE id = %s",
            [schedule.id],
        )
        schedule.invalidate_recordset(['webhook_timeout'])
        with patch.object(
            type(schedule), '_build_attachments',
        ) as build, patch.object(
            type(schedule), '_dispatch_webhook',
        ) as dispatch:
            with self.assertRaises(UserError):
                schedule._send_now()
            build.assert_not_called()
            dispatch.assert_not_called()

    # ---- SSRF guard on the server-issued POST ----

    def test_dispatch_webhook_rejects_unsafe_base_url_before_token(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://webhook.example.test/hook',
        )
        AttachmentType = type(self.env['ir.attachment'])
        for base_url in ('', '/relative', 'http://reports.example.test'):
            self.env['ir.config_parameter'].sudo().set_param(
                'web.base.url', base_url,
            )
            with patch.object(
                AttachmentType, 'create',
            ) as create, patch.object(
                report_schedule_module.socket, 'getaddrinfo',
            ) as getaddrinfo:
                with self.assertRaisesRegex(UserError, 'absolute HTTPS'):
                    schedule._dispatch_webhook([{
                        'name': 'must-not-exist.pdf',
                        'datas': b'cGRm',
                        'mimetype': 'application/pdf',
                    }], '<p>x</p>')
                create.assert_not_called()
                getaddrinfo.assert_not_called()

    def test_dispatch_webhook_blocks_internal_targets(self):
        # webhook_url is user-editable (group_eh_user) but the POST is issued
        # by the system cron. The guard must refuse internal/metadata targets
        # BEFORE any network call, so a plain user cannot pivot the server
        # into the cloud metadata service or an internal admin port.
        blocked = [
            'https://169.254.169.254/latest/meta-data/',  # cloud metadata
            'https://127.0.0.1:8069/web/session',          # loopback
            'https://10.0.0.5/internal',                   # RFC 1918
            'https://[::1]:8069/',                         # ipv6 loopback
            'http://8.8.8.8/hook',                         # HTTPS required
            'https://user:secret@8.8.8.8/hook',            # credentials
            'https://8.8.8.8:0/hook',                      # invalid port
            'https://[::1',                                # malformed URL
            'file:///etc/passwd',                          # non-http scheme
            'https://metadata.google.internal/',           # metadata hostname
        ]
        for url in blocked:
            schedule = self._make_schedule(
                delivery_channel='webhook',
                webhook_format='generic',
                webhook_url=url,
            )
            with patch.object(
                report_schedule_module, '_PinnedHTTPSConnection',
            ) as connection:
                with self.assertRaises(UserError):
                    schedule._dispatch_webhook([], '<p>x</p>')
                # The guard fired before any network call was attempted.
                connection.assert_not_called()

    def test_dispatch_webhook_resolves_once_and_pins_public_address(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://webhook.example.test/hook?token=abc',
        )
        endpoint = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            ('8.8.8.8', 443),
        )
        pinned_endpoint = endpoint[:3] + (endpoint[4],)
        with patch.object(
            report_schedule_module.socket,
            'getaddrinfo',
            return_value=[endpoint],
        ) as getaddrinfo, patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection_class:
            connection = connection_class.return_value
            response = connection.getresponse.return_value
            response.status = 204
            response.read.return_value = b''

            schedule._dispatch_webhook([{
                'name': 'report.pdf',
                'datas': b'cGRm',
                'mimetype': 'application/pdf',
            }], '<p>x</p>')

            getaddrinfo.assert_called_once_with(
                'webhook.example.test',
                443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            connection_class.assert_called_once_with(
                host='webhook.example.test',
                port=443,
                endpoint=pinned_endpoint,
                timeout=15,
            )
            request_args = connection.request.call_args
            self.assertEqual(request_args.args[:2], ('POST', '/hook?token=abc'))
            connection.getresponse.assert_called_once_with()
            connection.close.assert_called_once_with()
            delivery_attachment = self.env['ir.attachment'].sudo().search([
                ('eh_report_schedule_delivery', '=', True),
                ('res_model', '=', 'eh.report.schedule'),
                ('res_id', '=', schedule.id),
            ])
            self.assertEqual(len(delivery_attachment), 1)
            self.assertTrue(delivery_attachment.access_token)
            encoded_payload = request_args.kwargs['body'].decode('utf-8')
            self.assertIn('access_token=', encoded_payload)
            self.assertIn(
                delivery_attachment.access_token,
                encoded_payload,
            )

    def test_dispatch_webhook_rejects_private_dns_answer_before_connect(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://rebind.example.test/hook',
        )
        private_answer = [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            ('10.23.45.67', 443),
        )]
        with patch.object(
            report_schedule_module.socket,
            'getaddrinfo',
            return_value=private_answer,
        ) as getaddrinfo, patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection:
            with self.assertRaises(UserError):
                schedule._dispatch_webhook([], '<p>x</p>')
            getaddrinfo.assert_called_once()
            connection.assert_not_called()

    def test_dispatch_webhook_rejects_mixed_public_private_dns_answers(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://mixed.example.test/hook',
        )
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
             ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
             ('127.0.0.1', 443)),
        ]
        with patch.object(
            report_schedule_module.socket,
            'getaddrinfo',
            return_value=answers,
        ), patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection:
            with self.assertRaises(UserError):
                schedule._dispatch_webhook([], '<p>x</p>')
            connection.assert_not_called()

    def test_dispatch_webhook_rejects_redirect_without_following(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://webhook.example.test/hook',
        )
        answer = [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            ('8.8.8.8', 443),
        )]
        with patch.object(
            report_schedule_module.socket,
            'getaddrinfo',
            return_value=answer,
        ), patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection_class:
            connection = connection_class.return_value
            response = connection.getresponse.return_value
            response.status = 302
            response.read.return_value = b''
            response.getheader.return_value = (
                'http://169.254.169.254/latest/meta-data/'
            )

            with self.assertRaisesRegex(UserError, 'Redirects are disabled'):
                schedule._dispatch_webhook([], '<p>x</p>')

            connection.request.assert_called_once()
            connection.getresponse.assert_called_once_with()
            response.getheader.assert_called_once_with('Location')
            connection.close.assert_called_once_with()

    def test_dispatch_webhook_http_failure_rolls_back_tokenized_blob(self):
        schedule = self._make_schedule(
            delivery_channel='webhook',
            webhook_format='generic',
            webhook_url='https://webhook.example.test/hook',
        )
        answer = [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            ('8.8.8.8', 443),
        )]
        Attachment = self.env['ir.attachment'].sudo()
        with patch.object(
            report_schedule_module.socket, 'getaddrinfo', return_value=answer,
        ), patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection_class:
            connection = connection_class.return_value
            response = connection.getresponse.return_value
            response.status = 503
            response.reason = 'Service Unavailable'
            response.read.return_value = b'try later'

            with self.assertRaisesRegex(UserError, 'HTTP 503'):
                schedule._dispatch_webhook([{
                    'name': 'failed-webhook.pdf',
                    'datas': b'cGRm',
                    'mimetype': 'application/pdf',
                }], '<p>x</p>')

        self.assertFalse(Attachment.search([
            ('name', '=', 'failed-webhook.pdf'),
            ('eh_report_schedule_delivery', '=', True),
            ('res_model', '=', 'eh.report.schedule'),
            ('res_id', '=', schedule.id),
        ]))

    def test_both_channel_records_partial_success(self):
        # Email succeeds, webhook fails: schedule records success but
        # the error message captures the webhook failure for ops.
        schedule = self._make_schedule(
            delivery_channel='both',
            webhook_format='slack',
            webhook_url='https://hooks.slack.com/services/T/B/X',
            recipient_emails='ops@example.com',
        )
        answer = [(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            '',
            ('8.8.8.8', 443),
        )]
        with patch.object(
            type(schedule), '_build_attachments',
            return_value=[{
                'name': 'partial-failure.pdf',
                'datas': b'cGRm',
                'mimetype': 'application/pdf',
            }],
        ), patch.object(
            type(schedule), '_dispatch_email',
            return_value=None,
        ), patch.object(
            report_schedule_module.socket, 'getaddrinfo', return_value=answer,
        ), patch.object(
            report_schedule_module, '_PinnedHTTPSConnection',
        ) as connection_class:
            connection = connection_class.return_value
            response = connection.getresponse.return_value
            response.status = 503
            response.reason = 'Service Unavailable'
            response.read.return_value = b'simulated webhook failure'
            schedule._send_now()
        self.assertEqual(schedule.last_run_status, 'success')
        self.assertIn('simulated webhook failure', schedule.last_error or '')
        self.assertFalse(self.env['ir.attachment'].sudo().search([
            ('name', '=', 'partial-failure.pdf'),
            ('eh_report_schedule_delivery', '=', True),
            ('res_model', '=', 'eh.report.schedule'),
            ('res_id', '=', schedule.id),
        ]))
