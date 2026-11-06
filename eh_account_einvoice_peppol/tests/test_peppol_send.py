# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Integration tests for the outbound Peppol transmission flow.

Exercise action_eh_send_peppol against the manual default (queued), a
registered mock adapter (delivered with a transmission id), and an
adapter that fails (error recorded + UserError). No network.
"""

import hashlib
from unittest.mock import patch

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_einvoice_peppol.tools import access_point_registry
from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    NS_CAC,
    NS_CBC,
    render_invoice_xml,
)


class _MockAccessPoint:
    def __init__(self, config):
        self.config = config

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {'transmission_id': 'TX-1', 'status': 'delivered',
                'raw_response': 'OK'}

    def poll(self, since_iso):
        return []


class _ErrorAccessPoint:
    def __init__(self, config):
        self.config = config

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        raise access_point_registry.AccessPointError("gateway down")

    def poll(self, since_iso):
        return []


class _UnknownStatusAccessPoint(_MockAccessPoint):
    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {'transmission_id': 'TX-UNKNOWN', 'status': 'provider_magic'}


class _MissingStatusAccessPoint(_MockAccessPoint):
    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {'transmission_id': 'TX-NO-STATUS'}


class _PollingAccessPoint:
    since_calls = []
    messages = []

    def __init__(self, config):
        self.config = config

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {'transmission_id': 'POLL', 'status': 'delivered'}

    def poll(self, since_iso):
        self.since_calls.append(since_iso)
        return list(self.messages)


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class PeppolSendTest(EhAccountIntegrationTestCase):

    _TEST_ADAPTERS = {
        'eh_test_mock_ap': lambda config: _MockAccessPoint(config),
        'eh_test_err_ap': lambda config: _ErrorAccessPoint(config),
        'eh_test_poll_ap': lambda config: _PollingAccessPoint(config),
        'eh_test_unknown_ap': lambda config: _UnknownStatusAccessPoint(config),
        'eh_test_missing_status_ap':
            lambda config: _MissingStatusAccessPoint(config),
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._previous_adapters = {
            key: access_point_registry._REGISTRY.get(key)
            for key in cls._TEST_ADAPTERS
        }
        for key, factory in cls._TEST_ADAPTERS.items():
            access_point_registry.register_adapter(key, factory)

    @classmethod
    def tearDownClass(cls):
        try:
            for key, previous in cls._previous_adapters.items():
                if previous is None:
                    access_point_registry._REGISTRY.pop(key, None)
                else:
                    access_point_registry._REGISTRY[key] = previous
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        # The void-for-re-cut action is manager-gated; run the suite as an EH
        # Accounting Manager so the sanctioned void/re-send path is exercised.
        # The dedicated non-manager test uses with_user(a plain user).
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager')
        _PollingAccessPoint.since_calls = []
        _PollingAccessPoint.messages = []
        self.peppol_user = self.env['res.users'].create({
            'name': 'Peppol Send Operator',
            'login': 'eh_peppol_send_operator',
            # Odoo 16 requires a sender email before message_post; newer
            # series can derive/fallback without raising.
            'email': 'eh_peppol_send_operator@example.com',
            'company_id': self.env.company.id,
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        au = self.env.ref('base.au')
        self.env.company.write({
            'country_id': au.id, 'vat': '83914571673',
            'account_fiscal_country_id': au.id,
            'street': '1 Seller Street', 'city': 'Sydney', 'zip': '2000',
        })
        self.env.company.partner_id.write({
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '83914571673',
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Buyer Co', 'is_company': True, 'country_id': au.id,
            'vat': '53004085616',
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '53004085616',
            'street': '2 Buyer Street', 'city': 'Sydney', 'zip': '2000',
        })
        self.fiscal_position = self.env['account.fiscal.position'].create({
            'name': 'Peppol AU fixture',
            'company_id': self.env.company.id,
            'country_id': au.id,
        })
        TaxGroup = self.env['account.tax.group'].sudo()
        if 'company_id' in TaxGroup._fields:
            tax_group = TaxGroup.search([
                ('company_id', '=', self.env.company.id),
                ('country_id', '=', au.id),
            ], limit=1) or TaxGroup.create({
                'name': 'Peppol send tax fixture',
                'company_id': self.env.company.id,
                'country_id': au.id,
            })
        else:
            tax_group = TaxGroup.search([
                ('country_id', '=', au.id),
            ], limit=1) or TaxGroup.create({
                'name': 'Peppol send tax fixture',
                'country_id': au.id,
            })
        self.zero_tax = self.env['account.tax'].create({
            'name': 'Peppol zero-rated sale fixture',
            'amount': 0.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.env.company.id,
            'country_id': au.id,
            'tax_group_id': tax_group.id,
            'eh_edi_tax_category': 'Z',
        })
        if not self.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', self.env.company.id)], limit=1):
            self.env['account.journal'].create({
                'name': 'Sales', 'code': 'INV', 'type': 'sale',
                'company_id': self.env.company.id,
            })

    def _post_invoice(self):
        product = self.env['product.product'].create({
            'name': 'Test Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'fiscal_position_id': self.fiscal_position.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_origin': 'PO-SEND-001',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id, 'name': 'Consulting',
                'quantity': 10.0, 'price_unit': 100.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, self.zero_tax.ids)],
            })],
        })
        move.action_post()
        return move

    def test_send_manual_is_queued(self):
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertEqual(move.eh_peppol_ap_key, 'manual')
        self.assertTrue(move.eh_peppol_sent_at)

    def test_poll_cursor_drains_batches_without_losing_messages(self):
        self.env.company.write({
            'eh_peppol_access_point_key': 'eh_test_poll_ap',
            'eh_peppol_poll_cursor': False,
        })
        before = fields.Datetime.now()
        _PollingAccessPoint.messages = [
            {'message_id': 'IN-1', 'xml_bytes': b'<invalid-one/>'},
            {'message_id': 'IN-2', 'xml_bytes': b'<invalid-two/>'},
        ]

        self.env['eh.peppol.inbound']._cron_poll_access_points(batch_size=1)
        self.assertFalse(self.env.company.eh_peppol_poll_cursor)
        first_since = fields.Datetime.to_datetime(
            _PollingAccessPoint.since_calls[-1]
        )
        self.assertLess(first_since, before)
        self.assertEqual(self.env['eh.peppol.inbound'].search_count([
            ('transmission_id', 'in', ['IN-1', 'IN-2']),
        ]), 1)

        self.env['eh.peppol.inbound']._cron_poll_access_points(batch_size=1)
        self.env.company.invalidate_recordset(['eh_peppol_poll_cursor'])
        self.assertTrue(self.env.company.eh_peppol_poll_cursor)
        self.assertEqual(self.env['eh.peppol.inbound'].search_count([
            ('transmission_id', 'in', ['IN-1', 'IN-2']),
        ]), 2)

        completed = self.env.company.eh_peppol_poll_cursor
        self.env['eh.peppol.inbound']._cron_poll_access_points(batch_size=1)
        self.assertEqual(self.env['eh.peppol.inbound'].search_count([
            ('transmission_id', 'in', ['IN-1', 'IN-2']),
        ]), 2)
        last_since = fields.Datetime.to_datetime(
            _PollingAccessPoint.since_calls[-1]
        )
        self.assertLess(last_since, completed)

    def test_transmission_audit_fields_reject_direct_write(self):
        move = self._post_invoice()
        with self.assertRaises(AccessError):
            move.write({
                'eh_peppol_transmission_status': 'delivered',
                'eh_peppol_transmission_id': 'FORGED',
            })
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')

    def test_delivered_status_cannot_be_reset_by_direct_rpc_write(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        with self.assertRaises(AccessError):
            move.with_user(self.peppol_user).with_context(
                eh_peppol_transmission_internal=True,
            ).write({'eh_peppol_transmission_status': 'not_sent'})
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')

    def test_send_via_adapter_records_delivery(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')
        self.assertEqual(move.eh_peppol_ap_key, 'eh_test_mock_ap')
        attachment = move.eh_peppol_payload_attachment_id
        self.assertTrue(attachment)
        self.assertTrue(attachment.eh_peppol_outbound_evidence)
        self.assertEqual(
            move.eh_peppol_payload_hash,
            hashlib.sha256(attachment.raw).hexdigest(),
        )
        self.assertEqual(
            move.eh_peppol_recipient_endpoint_snapshot, '53004085616',
        )
        self.assertEqual(move.eh_peppol_recipient_scheme_snapshot, '0151')
        with self.assertRaises(AccessError):
            attachment.write({'name': 'replacement.xml'})
        with self.assertRaises(AccessError):
            attachment.sudo().unlink()

    def test_sent_invoice_and_lines_are_frozen_until_credit_note(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        invoice_line = move.invoice_line_ids

        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            move.write({'invoice_date_due': '2026-06-30'})
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            move.button_cancel()
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            invoice_line.write({'price_unit': 999.0})
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            invoice_line.with_context(
                eh_account_engine_internal=True,
            ).write({'price_unit': 998.0})
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            invoice_line.with_context(
                eh_peppol_correction_internal=True,
            ).write({'price_unit': 997.0})
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            self.env['account.move.line'].create({
                'move_id': move.id,
                'name': 'Injected line',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 1.0,
            })
        with self.assertRaisesRegex(UserError, 'already been queued or sent'):
            invoice_line.unlink()

    def test_outbound_snapshot_does_not_lock_master_data_across_network(self):
        move = self._post_invoice()
        cursor_class = type(self.env.cr)
        original_execute = cursor_class.execute
        statements = []

        def capture(cursor, query, params=None, *args, **kwargs):
            statements.append(str(query).lower())
            return original_execute(cursor, query, params, *args, **kwargs)

        with patch.object(cursor_class, 'execute', new=capture):
            move._eh_lock_peppol_outbound_snapshot()

        sql = '\n'.join(statements)
        self.assertIn('from account_move ', sql)
        self.assertIn('from account_move_line', sql)
        for table in (
                'res_company', 'res_partner', 'account_tax',
                'product_product', 'product_template', 'uom_uom',
                'res_currency'):
            self.assertNotIn('from %s' % table, sql)

    def test_confirmed_send_creates_one_linked_credit_note(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        move = self._post_invoice()
        move.action_eh_send_peppol()

        # A normal EH Accounting User owns the source-company correction;
        # manager/superuser elevation is not required for an ordinary invoice.
        action = move.with_user(
            self.peppol_user,
        ).action_eh_create_peppol_credit_note()
        credit = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(credit.move_type, 'out_refund')
        self.assertEqual(credit.reversed_entry_id, move)
        self.assertEqual(credit.invoice_origin, move.invoice_origin)
        self.assertEqual(
            move.with_user(
                self.peppol_user,
            ).action_eh_create_peppol_credit_note()['res_id'], credit.id,
        )

        credit.action_post()
        xml = etree.fromstring(
            render_invoice_xml(credit._eh_build_peppol_payload())
        )
        reference = xml.find(
            'cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID',
            {'cac': NS_CAC, 'cbc': NS_CBC},
        )
        self.assertIsNotNone(reference)
        self.assertEqual(reference.text, move.name)

    def test_unknown_adapter_status_is_quarantined_as_queued(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_unknown_ap'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-UNKNOWN')
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()

    def test_missing_adapter_status_is_quarantined_as_queued(self):
        self.env.company.eh_peppol_access_point_key = (
            'eh_test_missing_status_ap'
        )
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-NO-STATUS')

    def test_sealed_invoice_send_and_void_preserve_delivery_audit(self):
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move._eh_stamp_verified_seal()

        move.action_eh_send_peppol()
        self.assertTrue(move.eh_sealed)
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')

        move.action_eh_void_peppol_transmission()
        self.assertTrue(move.eh_sealed)
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')

    def test_sealed_invoice_transport_error_is_quarantined_with_evidence(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_err_ap'
        move = self._post_invoice()
        move._eh_stamp_verified_seal()

        result = move.action_eh_send_peppol()

        self.assertEqual(result.get('tag'), 'display_notification')
        self.assertTrue(move.eh_sealed)
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertTrue(move.eh_peppol_payload_attachment_id)
        self.assertIn(
            'gateway down', move.eh_peppol_transmission_message or '',
        )

    def test_legacy_unverified_invoice_cannot_export_or_send(self):
        move = self._post_invoice()
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE account_move
               SET eh_sealed = FALSE,
                   eh_legacy_unverified_seal = TRUE
             WHERE id = %s
            """,
            (move.id,),
        )
        move.invalidate_recordset([
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])

        with self.assertRaises(UserError):
            move.action_eh_export_peppol_xml()
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')

    def test_send_adapter_error_quarantines_exact_attempt_and_notifies(self):
        self.env.company.eh_peppol_access_point_key = 'eh_test_err_ap'
        move = self._post_invoice()
        # No raise: the uncertain outcome and exact attempted bytes survive the
        # transaction, but the move is fail-closed against an ordinary retry.
        result = move.action_eh_send_peppol()
        self.assertEqual(result.get('tag'), 'display_notification')
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.assertIn('gateway down',
                      move.eh_peppol_transmission_message or '')
        evidence = move.eh_peppol_payload_attachment_id
        self.assertTrue(evidence.eh_peppol_outbound_evidence)
        self.assertEqual(
            move.eh_peppol_payload_hash,
            hashlib.sha256(evidence.raw).hexdigest(),
        )

    def test_send_refuses_retransmission(self):
        """A second send of an already-transmitted invoice is refused, so a
        double click / retry cannot emit the legal e-invoice twice.
        """
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()
        # The recorded transmission is unchanged after the refused re-send.
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_send_manual_queued_is_not_resendable(self):
        """The manual default reaches 'queued'; that too blocks a re-send."""
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()

    def test_transport_exception_requires_manager_void_before_retry(self):
        """A lost response may hide provider acceptance; never auto-retry."""
        self.env.company.eh_peppol_access_point_key = 'eh_test_err_ap'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()
        move.action_eh_void_peppol_transmission()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')

    # ---- void / re-cut escape hatch (over-restriction refinement) ----

    def test_void_manual_queued_then_resend(self):
        """Legitimate re-cut path: the out-of-box 'manual' adapter parks the
        move at 'queued'; a manager voids that unconfirmed transmission and
        sends again once the out-of-band routing is corrected.
        """
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        voided_evidence = move.eh_peppol_payload_attachment_id
        # Void resets the unconfirmed transmission back to a re-sendable state.
        move.action_eh_void_peppol_transmission()
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')
        self.assertFalse(move.eh_peppol_transmission_id)
        self.assertFalse(move.eh_peppol_ap_key)
        self.assertFalse(move.eh_peppol_payload_attachment_id)
        self.assertTrue(voided_evidence.exists())
        self.assertTrue(voided_evidence.eh_peppol_outbound_evidence)
        move.write({'narration': 'Reviewed before Peppol re-cut'})
        # A real adapter is now configured and the corrected file is sent.
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_legacy_unpinned_transmission_cannot_be_voided_and_resent(self):
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE account_move
               SET eh_peppol_transmission_status = 'queued',
                   eh_peppol_legacy_unpinned = TRUE
             WHERE id = %s
            """,
            [move.id],
        )
        move.invalidate_recordset([
            'eh_peppol_transmission_status', 'eh_peppol_legacy_unpinned',
        ])
        with self.assertRaisesRegex(UserError, 'cannot be voided or re-sent'):
            move.action_eh_void_peppol_transmission()
        with self.assertRaises(UserError):
            move.action_eh_send_peppol()

    def test_void_refuses_delivered_transmission(self):
        """The original protection stays closed: a confirmed 'delivered'
        transmission can NEVER be voided/re-cut, so the legal e-invoice cannot
        be emitted twice via the escape hatch.
        """
        self.env.company.eh_peppol_access_point_key = 'eh_test_mock_ap'
        self.env.company.eh_peppol_ap_config = '{"api_key": "k"}'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        with self.assertRaises(UserError):
            move.action_eh_void_peppol_transmission()
        # The confirmed transmission is untouched by the refused void.
        self.assertEqual(move.eh_peppol_transmission_status, 'delivered')
        self.assertEqual(move.eh_peppol_transmission_id, 'TX-1')

    def test_void_refuses_not_sent(self):
        """A move that never left has nothing to void."""
        move = self._post_invoice()
        self.assertEqual(move.eh_peppol_transmission_status, 'not_sent')
        with self.assertRaises(UserError):
            move.action_eh_void_peppol_transmission()

    def test_non_manager_cannot_void(self):
        """The escape hatch is manager-gated: an ordinary EH accounting user
        (who can read/send moves) cannot reset a 'queued' transmission, so the
        re-send guard cannot be defeated by a non-manager.
        """
        try:
            eh_user = self.env['res.users'].create({
                'name': 'Peppol EH User',
                'login': 'eh_peppol_send_user',
                'company_id': self.env.company.id,
                'group_ids': [(6, 0, [
                    self.env.ref('eh_account_base.group_eh_user').id,
                ])],
            })
        except Exception:  # noqa: BLE001
            self.skipTest("Could not create a non-manager EH user.")
        self.env.company.eh_peppol_access_point_key = 'manual'
        move = self._post_invoice()
        move.action_eh_send_peppol()
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
        with self.assertRaises(UserError):
            move.with_user(eh_user).action_eh_void_peppol_transmission()
        # The guard held: the transmission status is unchanged.
        self.assertEqual(move.eh_peppol_transmission_status, 'queued')
