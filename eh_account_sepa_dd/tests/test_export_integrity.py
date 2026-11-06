# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Tamper resistance for generated PAIN.008 audit artifacts."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_dd', 'post_install', '-at_install')
class TestSepaDdExportIntegrity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        journal = cls.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', cls.env.company.id),
        ], limit=1)
        if not journal:
            journal = cls.env['account.journal'].create({
                'name': 'DD Integrity Bank',
                'code': 'DDIG',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })
        cls.batch = cls.env['eh.batch.payment'].create({
            'journal_id': journal.id,
            'batch_type': 'inbound',
            'payment_date': '2026-04-30',
        })
        cls.xml_bytes = (
            b'<Document><MsgId>MSG-DD-INTEGRITY-001</MsgId></Document>'
        )
        cls.attachment = cls.env['ir.attachment'].create({
            'name': 'dd_integrity.xml',
            'type': 'binary',
            'raw': cls.xml_bytes,
            'mimetype': 'application/xml',
            'res_model': cls.batch._name,
            'res_id': cls.batch.id,
        })
        cls.export = cls.env['eh.sepa.dd.export'].create({
            'batch_id': cls.batch.id,
            'message_id': 'MSG-DD-INTEGRITY-001',
            'transaction_count': 1,
            'control_sum': 42.0,
            'sequence_type': 'FRST',
            'local_instrument': 'CORE',
            'file_hash': cls.env['eh.sepa.dd.export'].compute_hash(
                cls.xml_bytes,
            ),
            'attachment_id': cls.attachment.id,
        })
        group = cls.env.ref('eh_account_base.group_eh_user', False)
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'DD integrity user',
                'login': 'eh_sepa_dd_integrity_user',
                'email': 'eh_sepa_dd_integrity_user@example.com',
                'company_id': cls.env.company.id,
                'company_ids': [(6, 0, cls.env.company.ids)],
                'group_ids': [(4, group.id)] if group else [],
            })
        except Exception:
            cls.plain_user = cls.env['res.users'].browse()
        auditor_group = cls.env.ref('eh_account_base.group_eh_auditor')
        cls.auditor_user = cls.env['res.users'].create({
            'name': 'SEPA DD evidence auditor',
            'login': 'eh_sepa_dd_evidence_auditor',
            'email': 'eh_sepa_dd_evidence_auditor@example.com',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, auditor_group.ids)],
        })

    def test_generator_owned_audit_identity_is_immutable(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.export.with_user(self.plain_user).write({
                'message_id': 'FORGED-DD-MSG',
                'sequence_type': 'RCUR',
                'file_hash': '0' * 64,
            })
        with self.assertRaises(AccessError):
            self.env['eh.sepa.dd.export'].with_user(
                self.plain_user,
            ).create({
                'batch_id': self.batch.id,
                'message_id': 'FORGED-DD-EXPORT',
            })
        self.assertEqual(self.export.message_id, 'MSG-DD-INTEGRITY-001')
        self.assertEqual(self.export.sequence_type, 'FRST')
        self.assertEqual(self.export.local_instrument, 'CORE')

    def test_same_sequence_different_instruments_remain_distinct(self):
        b2b_bytes = b'<Document><LclInstrm><Cd>B2B</Cd></LclInstrm></Document>'
        attachment = self.env['ir.attachment'].create({
            'name': 'dd_integrity_b2b.xml',
            'type': 'binary',
            'raw': b2b_bytes,
            'mimetype': 'application/xml',
            'res_model': self.batch._name,
            'res_id': self.batch.id,
        })
        b2b = self.env['eh.sepa.dd.export'].create({
            'batch_id': self.batch.id,
            'message_id': 'MSG-DD-INTEGRITY-B2B',
            'transaction_count': 1,
            'control_sum': 42.0,
            'sequence_type': 'FRST',
            'local_instrument': 'B2B',
            'file_hash': self.env['eh.sepa.dd.export'].compute_hash(b2b_bytes),
            'attachment_id': attachment.id,
        })
        self.export.invalidate_recordset(['state'])
        self.assertEqual(self.export.state, 'generated')
        self.assertEqual(b2b.state, 'generated')

    def test_generated_attachment_is_immutable(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user).write({
                'raw': b'<Document>substituted</Document>',
            })
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user).unlink()
        self.assertEqual(self.attachment.raw, self.xml_bytes)

    def test_hash_mismatch_blocks_button_and_direct_stream(self):
        self.attachment.sudo().write({
            'raw': b'<Document>substituted</Document>',
        })
        with self.assertRaises(UserError):
            self.export.action_download()
        with self.assertRaises(UserError):
            self.attachment._to_http_stream()
        with self.assertRaises(UserError):
            self.env['ir.binary']._record_to_stream(self.attachment, 'raw')
        with self.assertRaises(UserError):
            self.attachment.read(['raw'])
        self.assertEqual(self.export.state, 'generated')

    def test_valid_download_remains_usable(self):
        action = self.export.action_download()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(self.export.state, 'downloaded')
        stream = self.attachment._to_http_stream()
        self.assertEqual(stream.type, 'data')
        self.assertEqual(stream.read(), self.xml_bytes)

    def test_superseded_export_cannot_be_downloaded_or_served(self):
        self.export.sudo().write({'state': 'superseded'})

        with self.assertRaises(UserError):
            self.export.action_download()
        with self.assertRaises(UserError):
            self.attachment._to_http_stream()
        with self.assertRaises(UserError):
            self.attachment.read(['raw'])

        self.export.invalidate_recordset(['state'])
        self.assertEqual(self.export.state, 'superseded')

    def test_plain_user_cannot_read_bank_instruction_bytes(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.export.with_user(self.plain_user).action_download()
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user)._to_http_stream()
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user).read(['raw'])
        self.assertEqual(self.export.state, 'generated')

    def test_read_only_auditor_can_verify_and_download_evidence(self):
        action = self.export.with_user(self.auditor_user).action_download()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(self.export.state, 'downloaded')
        stream = self.attachment.with_user(
            self.auditor_user,
        )._to_http_stream()
        self.assertEqual(stream.read(), self.xml_bytes)
