# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression: the eh.sepa.export state machine
(generated -> downloaded -> superseded) must be unreachable by a direct
RPC/ORM write. A plain user with model write access can still not flip
state to 'downloaded' to skip action_download and its attachment check,
nor bury an audit row as 'superseded'. State advances only through the
model's own actions, which run as su.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_sepa_ct.models.sepa_export import (
    SEPA_EXPORT_CAPABILITY,
    SEPA_EXPORT_CONTEXT,
)


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaExportWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBK',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        cls.batch = cls.env['eh.batch.payment'].create({
            'journal_id': cls.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })

        cls.xml_bytes = b'<Document><MsgId>MSG-GUARD-001</MsgId></Document>'
        cls.attachment = cls.env['ir.attachment'].create({
            'name': 'guard_probe.xml',
            'type': 'binary',
            'raw': cls.xml_bytes,
            'mimetype': 'application/xml',
            'res_model': cls.batch._name,
            'res_id': cls.batch.id,
        })

        # A fresh generator-owned export is born in the initial state.
        cls.rec = cls.env['eh.sepa.export'].with_context(**{
            SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
        }).create({
            'batch_id': cls.batch.id,
            'message_id': 'MSG-GUARD-001',
            'transaction_count': 1,
            'control_sum': 25.0,
            'file_hash': cls.env['eh.sepa.export'].compute_hash(cls.xml_bytes),
            'attachment_id': cls.attachment.id,
        })

        # A plain user WITH ORM write access to the model, so the block we
        # observe is the workflow guard - not ir.model.access.
        group = cls.env.ref('eh_account_base.group_eh_user', False)
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'eh_sepa_guard_user',
                'login': 'eh_sepa_guard_user',
                'email': 'eh_sepa_guard_user@example.com',
                'group_ids': [(4, group.id)] if group else [],
            })
        except Exception:
            cls.plain_user = cls.env['res.users'].browse()
        auditor_group = cls.env.ref('eh_account_base.group_eh_auditor')
        cls.auditor_user = cls.env['res.users'].create({
            'name': 'SEPA CT evidence auditor',
            'login': 'eh_sepa_ct_evidence_auditor',
            'email': 'eh_sepa_ct_evidence_auditor@example.com',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, auditor_group.ids)],
        })

    def test_direct_state_write_blocked_for_plain_user(self):
        """A non-superuser cannot RPC-write state past action_download."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        self.assertEqual(self.rec.state, 'generated')
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({'state': 'downloaded'})
        # State is unchanged after the blocked write.
        self.assertEqual(self.rec.state, 'generated')

    def test_supersede_write_blocked_for_plain_user(self):
        """A plain user cannot bury an audit row by flipping it superseded."""
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({'state': 'superseded'})
        self.assertEqual(self.rec.state, 'generated')

    def test_action_download_moves_state_as_su(self):
        """The sanctioned action still advances the state (positive path)."""
        self.rec.action_download()
        self.assertEqual(self.rec.state, 'downloaded')
        stream = self.attachment._to_http_stream()
        self.assertEqual(stream.type, 'data')
        self.assertEqual(stream.read(), self.xml_bytes)

    def test_download_transition_takes_export_row_lock(self):
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            self.rec.action_download()
        self.assertTrue(any(
            'eh_sepa_export' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_plain_user_cannot_create_export_audit_evidence(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.env['eh.sepa.export'].with_user(self.plain_user).create({
                'batch_id': self.batch.id,
                'message_id': 'FORGED-MSG',
                'file_hash': '0' * 64,
                'attachment_id': self.attachment.id,
            })
        with self.assertRaises(AccessError):
            self.env['eh.sepa.export'].sudo().with_context(
                eh_sepa_export_capability=True,
            ).create({
                'batch_id': self.batch.id,
                'message_id': 'FORGED-SUDO-MSG',
            })

    def test_audit_identity_and_attachment_are_immutable(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({
                'message_id': 'REPLACED-MSG',
                'file_hash': '0' * 64,
            })
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user).write({
                'raw': b'<Document>substituted</Document>',
            })
        self.assertEqual(self.rec.message_id, 'MSG-GUARD-001')
        self.assertEqual(self.attachment.raw, self.xml_bytes)
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).write({
                'provenance_status': 'verified',
            })

    def test_generator_creation_is_verified(self):
        self.assertEqual(self.rec.provenance_status, 'verified')

    def test_legacy_unverified_export_is_preserved_but_quarantined(self):
        self.env.cr.execute(
            """
            SELECT message_id, file_hash, state, attachment_id,
                   generated_by_id, generated_at
              FROM eh_sepa_export
             WHERE id = %s
            """,
            (self.rec.id,),
        )
        export_before = self.env.cr.fetchone()
        self.env.cr.execute(
            """
            SELECT checksum, store_fname, db_datas, res_model, res_id
              FROM ir_attachment
             WHERE id = %s
            """,
            (self.attachment.id,),
        )
        attachment_before = self.env.cr.fetchone()

        # This is the exact label written by the upgrade migration. No other
        # legacy fact is reconstructed or mutated.
        self.env.cr.execute(
            "UPDATE eh_sepa_export "
            "SET provenance_status = 'legacy_unverified' WHERE id = %s",
            (self.rec.id,),
        )
        self.rec.invalidate_recordset()

        with self.assertRaises(UserError):
            self.rec.action_download()
        with self.assertRaises(UserError):
            self.attachment._to_http_stream()
        with self.assertRaises(UserError):
            self.env['ir.binary']._record_to_stream(self.attachment, 'raw')
        with self.assertRaises(UserError):
            self.attachment.read(['raw'])

        self.env.cr.execute(
            """
            SELECT message_id, file_hash, state, attachment_id,
                   generated_by_id, generated_at
              FROM eh_sepa_export
             WHERE id = %s
            """,
            (self.rec.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), export_before)
        self.env.cr.execute(
            """
            SELECT checksum, store_fname, db_datas, res_model, res_id
              FROM ir_attachment
             WHERE id = %s
            """,
            (self.attachment.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), attachment_before)

    def test_stored_related_company_anchor_is_immutable(self):
        """Record-rule company scope cannot be re-keyed over raw RPC."""
        with self.assertRaises(AccessError):
            self.rec.with_user(self.auditor_user).write({
                'company_id': self.env.company.id,
            })
        with self.assertRaises(AccessError):
            self.batch.with_user(self.auditor_user).write({
                'last_sepa_export_id': self.rec.id,
            })

    def test_hash_mismatch_blocks_button_and_direct_stream(self):
        # Simulate storage corruption or privileged tampering. Ordinary ORM
        # mutation is blocked above, but verification must still fail closed.
        self.attachment.sudo().write({
            'raw': b'<Document>substituted</Document>',
        })
        with self.assertRaises(UserError):
            self.rec.action_download()
        with self.assertRaises(UserError):
            self.attachment._to_http_stream()
        with self.assertRaises(UserError):
            self.attachment.read(['raw'])
        self.assertEqual(self.rec.state, 'generated')

    def test_superseded_export_cannot_be_downloaded_or_served(self):
        self.rec.sudo().with_context(**{
            SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
        }).write({'state': 'superseded'})

        with self.assertRaises(UserError):
            self.rec.action_download()
        with self.assertRaises(UserError):
            self.attachment._to_http_stream()
        with self.assertRaises(UserError):
            self.attachment.read(['raw'])

        self.rec.invalidate_recordset(['state'])
        self.assertEqual(self.rec.state, 'superseded')

    def test_plain_user_cannot_read_bank_instruction_bytes(self):
        if not self.plain_user:
            self.skipTest("No plain user available in this environment.")
        with self.assertRaises(AccessError):
            self.rec.with_user(self.plain_user).action_download()
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user)._to_http_stream()
        with self.assertRaises(AccessError):
            self.attachment.with_user(self.plain_user).read(['raw'])
        self.assertEqual(self.rec.state, 'generated')

    def test_read_only_auditor_can_verify_and_download_evidence(self):
        action = self.rec.with_user(self.auditor_user).action_download()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(self.rec.state, 'downloaded')
        stream = self.attachment.with_user(
            self.auditor_user,
        )._to_http_stream()
        self.assertEqual(stream.read(), self.xml_bytes)

    def test_send_action_attaches_exact_verified_xml(self):
        action = self.rec.action_send_confirmation()

        self.assertEqual(action['res_model'], 'mail.compose.message')
        self.assertEqual(action['target'], 'new')
        self.assertEqual(
            action['context']['default_attachment_ids'],
            [(6, 0, self.attachment.ids)],
        )

    def test_ordinary_attachment_write_skips_export_lookup(self):
        ordinary = self.env['ir.attachment'].create({
            'name': 'ordinary.txt',
            'type': 'binary',
            'raw': b'ordinary',
        })
        ExportModel = type(self.env['eh.sepa.export'])
        with patch.object(
            ExportModel,
            'search',
            side_effect=AssertionError('ordinary write queried SEPA exports'),
        ):
            with self.assertRaises(AccessError):
                ordinary.with_user(self.plain_user).write({
                    'name': 'still-ordinary.txt',
                })

    def test_attachment_marker_is_server_owned(self):
        with self.assertRaises(AccessError):
            self.env['ir.attachment'].create({
                'name': 'forged.xml',
                'type': 'binary',
                'raw': b'<forged/>',
                'eh_sepa_ct_export_evidence': True,
            })

    def test_export_views_expose_chatter_mail_and_nondraggable_state(self):
        from lxml import etree
        form = self.env.ref(
            'eh_account_sepa_ct.view_eh_sepa_export_form',
        )
        kanban = self.env.ref(
            'eh_account_sepa_ct.view_eh_sepa_export_kanban',
        )
        form_root = etree.fromstring(form.arch_db.encode())
        kanban_root = etree.fromstring(kanban.arch_db.encode())
        self.assertTrue(form_root.xpath(
            './/chatter | .//div[contains(concat(" ", '
            'normalize-space(@class), " "), " oe_chatter ")]',
        ))
        self.assertTrue(
            form_root.xpath('.//button[@name="action_send_confirmation"]'),
        )
        kanban_nodes = kanban_root.xpath('self::kanban | .//kanban')
        self.assertEqual(
            kanban_nodes[0].get('records_draggable'),
            'false',
        )
