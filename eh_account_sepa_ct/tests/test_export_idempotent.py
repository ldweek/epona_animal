# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Idempotency of the SEPA Credit Transfer export.

The PAIN.001 IS the instruction the bank executes. Regenerating it for the
same posted batch mints a fresh MsgId but keeps the SAME EndToEndIds and
amounts, so the bank treats the two files as independent instructions and,
if both are submitted, pays every supplier twice. These tests prove that a
second export of an already-exported batch is refused, that a genuine re-cut
is only reachable through the explicit, audited void action, and that voiding
requires the manager group.
"""

from datetime import date
from decimal import Decimal

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaExportIdempotent(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Batch = cls.env['eh.batch.payment']

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
        cls.originator_bank = cls.env['res.partner.bank'].create({
            'acc_number': 'DE89370400440532013000',
            'partner_id': cls.company.partner_id.id,
            'company_id': cls.company.id,
        })
        cls.bank_journal.bank_account_id = cls.originator_bank

        cls.env['eh.sepa.originator'].create({
            'journal_id': cls.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'DE89370400440532013000',
            'bic': 'DEUTDEFFXXX',
            'segregation_policy': 'three_person',
        })

        cls.creditor_bank = cls._create_partner_bank_as_validator({
            'acc_number': 'FR1420041010050500013M02606',
            'partner_id': cls.partner_a.id,
            'allow_out_payment': True,
        })

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.bank_journal.currency_id = cls.eur
        cls.env['res.currency.rate'].create({
            'currency_id': cls.eur.id,
            'company_id': cls.company.id,
            'rate': 1.0,
            'name': '2026-01-01',
        })

        # The class user is the confirmer (maker); promote to manager so
        # confirm/post pass their own guards.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )

    def _make_manager(self, login):
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_manager',
            ).id)],
        })

    def _posted_batch(self, poster, payment_values=None):
        """Confirm (as the class user) then post (as `poster`) a one-line
        outbound batch, returning it posted and ready to export."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'sepa_ct_enabled': True,
            'payment_date': '2026-04-30',
        })
        values = {
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'INV-001',
            'currency_id': self.eur.id,
            'partner_bank_id': self.creditor_bank.id,
        }
        values.update(payment_values or {})
        self.env['account.payment'].create(values)
        batch.action_confirm()

        batch.with_user(poster).action_post()
        self.assertEqual(batch.state, 'posted')
        return batch

    def test_reexport_is_blocked(self):
        """A second Export click on an already-exported batch is refused,
        so no duplicate PAIN.001 (a second bank instruction) is minted."""
        poster = self._make_manager('eh_ct_idem_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter')

        batch.with_user(exporter).action_export_sepa_ct()
        self.assertEqual(len(batch.sepa_export_ids), 1)
        first = batch.sepa_export_ids
        self.assertEqual(first.state, 'generated')

        # Second export attempt on the same posted batch: blocked.
        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()
        # No new export row was created; the first is untouched.
        self.assertEqual(len(batch.sepa_export_ids), 1)
        self.assertEqual(batch.sepa_export_ids.state, 'generated')

    def test_reexport_blocked_even_after_download(self):
        """Downloading the file does not unlock a re-export: a downloaded
        file is an even stronger 'already sent' signal."""
        poster = self._make_manager('eh_ct_idem_poster2')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter2')

        batch.with_user(exporter).action_export_sepa_ct()
        batch.sepa_export_ids.with_user(exporter).action_download()
        self.assertEqual(batch.sepa_export_ids.state, 'downloaded')

        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()
        self.assertEqual(len(batch.sepa_export_ids), 1)

    def test_recut_after_explicit_void(self):
        """The legitimate re-cut path: void the prior (never-sent) file, then
        export again. The prior row is superseded and exactly one file is
        active afterwards."""
        poster = self._make_manager('eh_ct_idem_poster3')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter3')

        batch.with_user(exporter).action_export_sepa_ct()
        first = batch.sepa_export_ids
        self.assertEqual(len(first), 1)

        # Explicitly void the prior file (it was never submitted).
        first.with_user(exporter).action_void_for_recut()
        self.assertEqual(first.state, 'superseded')

        # Re-export now succeeds because no active file remains.
        batch.with_user(exporter).action_export_sepa_ct()
        active = batch.sepa_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertEqual(len(batch.sepa_export_ids), 2)
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active, first)

    def test_void_requires_manager(self):
        """Voiding a SEPA export (which unlocks a re-cut) is a manager-only
        act; an ordinary accounting user is refused."""
        poster = self._make_manager('eh_ct_idem_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_ct_idem_exporter4')
        batch.with_user(exporter).action_export_sepa_ct()
        export = batch.sepa_export_ids

        member = self.env['res.users'].create({
            'name': 'eh_ct_idem_member',
            'login': 'eh_ct_idem_member',
            'email': 'eh_ct_idem_member@example.com',
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })
        if not member:
            self.skipTest("Could not provision a non-manager user.")
        with self.assertRaises(UserError):
            export.with_user(member).action_void_for_recut()
        self.assertEqual(export.state, 'generated')

    def test_confirmation_requires_explicit_trusted_bank_target(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'sepa_ct_enabled': True,
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 25.0,
            'date': '2026-04-15',
            'currency_id': self.eur.id,
            'partner_bank_id': False,
        })

        with self.assertRaises(UserError):
            batch.action_confirm()

        batch.invalidate_recordset(['state', 'sepa_ct_source_fingerprint'])
        self.assertEqual(batch.state, 'draft')
        self.assertFalse(batch.sepa_ct_source_fingerprint)

    def test_bank_drift_after_approval_blocks_cut(self):
        poster = self._make_manager('eh_ct_drift_poster')
        batch = self._posted_batch(poster)
        approved = batch.sepa_ct_source_fingerprint
        self.assertTrue(approved)
        self._write_partner_bank_as_validator(self.creditor_bank, {
            'allow_out_payment': False,
            'acc_number': 'DE89370400440532013000',
        })
        self._write_partner_bank_as_validator(
            self.creditor_bank, {'allow_out_payment': True},
        )
        exporter = self._make_manager('eh_ct_drift_exporter')

        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()

        self.assertFalse(batch.sepa_export_ids)
        with self.assertRaises(AccessError):
            batch.with_user(exporter).with_context(
                eh_authorized_sepa_ct_source_fingerprint=True,
            ).write({
                'sepa_ct_source_fingerprint': '0' * 64,
            })

        batch.sepa_ct_reapproval_reason = (
            'Supplier supplied a replacement IBAN and a manager verified the '
            'signed bank-change evidence.'
        )
        batch.with_user(exporter).action_reapprove_sepa_ct_sources()
        self.assertEqual(len(batch.sepa_ct_source_approval_ids), 2)
        self.assertEqual(batch.sepa_ct_source_approved_by_id, exporter)

        final_exporter = self._make_manager('eh_ct_drift_final_exporter')
        batch.with_user(final_exporter).action_export_sepa_ct()
        self.assertEqual(len(batch.sepa_export_ids), 1)

    def test_execution_offset_is_applied_to_requested_execution_date(self):
        originator = self.env['eh.sepa.originator'].search([
            ('journal_id', '=', self.bank_journal.id),
        ], limit=1)
        originator.requested_execution_offset_days = 2
        poster = self._make_manager('eh_ct_offset_poster')
        batch = self._posted_batch(poster)

        payload = batch._eh_build_pain_001_payload(originator)

        self.assertEqual(
            payload['payments'][0]['requested_execution_date'],
            max(
                fields.Date.context_today(batch),
                date(2026, 4, 30),
                date(2026, 4, 15),
            ) + date.resolution * 2,
        )

    def test_execution_date_respects_latest_payment_date(self):
        originator = self.env['eh.sepa.originator'].search([
            ('journal_id', '=', self.bank_journal.id),
        ], limit=1)
        originator.requested_execution_offset_days = 1
        latest = fields.Date.context_today(self.Batch) + date.resolution * 10
        poster = self._make_manager('eh_ct_latest_date_poster')
        batch = self._posted_batch(poster, {'date': latest})

        payload = batch._eh_build_pain_001_payload(originator)

        self.assertEqual(
            payload['payments'][0]['requested_execution_date'],
            latest + date.resolution,
        )

    def test_originator_does_not_opt_ordinary_batch_into_sepa(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'sepa_ct_enabled': False,
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 10.0,
            'date': '2026-04-15',
            'currency_id': self.eur.id,
            'partner_bank_id': self.creditor_bank.id,
        })

        batch.action_confirm()

        self.assertEqual(batch.state, 'confirmed')
        self.assertFalse(batch.sepa_ct_source_fingerprint)

    def test_originator_options_and_structured_reference_reach_real_payload(self):
        originator = self.env['eh.sepa.originator'].search([
            ('journal_id', '=', self.bank_journal.id),
        ], limit=1)
        originator.write({
            'charge_bearer': 'SHAR',
            'instruction_priority': 'HIGH',
            'batch_booking': True,
        })
        poster = self._make_manager('eh_ct_options_poster')
        batch = self._posted_batch(poster, {
            'sepa_ct_structured_reference': 'RF18539007547034',
        })

        payment_block = batch._eh_build_pain_001_payload(originator)[
            'payments'
        ][0]

        self.assertEqual(payment_block['charge_bearer'], 'SHAR')
        self.assertEqual(payment_block['instruction_priority'], 'HIGH')
        self.assertTrue(payment_block['batch_booking'])
        self.assertEqual(
            payment_block['transactions'][0]['structured_reference'],
            'RF18539007547034',
        )

    def test_real_posted_payment_xml_ties_to_gl_amount(self):
        from lxml import etree
        poster = self._make_manager('eh_ct_real_gl_poster')
        exporter = self._make_manager('eh_ct_real_gl_exporter')
        batch = self._posted_batch(poster)

        batch.with_user(exporter).action_export_sepa_ct()

        payment = batch.payment_ids
        self.assertEqual(payment.move_id.state, 'posted')
        xml_bytes = batch.sepa_export_ids.attachment_id.raw
        root = etree.fromstring(xml_bytes)
        namespace = root.nsmap[None]
        amount = root.find('.//{%s}InstdAmt' % namespace)
        self.assertEqual(Decimal(amount.text), Decimal('100.00'))
        self.assertEqual(
            Decimal(str(batch.sepa_export_ids.control_sum)),
            Decimal(str(payment.amount)),
        )

    def test_execution_offset_drift_after_approval_blocks_cut(self):
        originator = self.env['eh.sepa.originator'].search([
            ('journal_id', '=', self.bank_journal.id),
        ], limit=1)
        originator.requested_execution_offset_days = 1
        poster = self._make_manager('eh_ct_offset_drift_poster')
        batch = self._posted_batch(poster)
        approved = batch.sepa_ct_source_fingerprint
        originator.requested_execution_offset_days = 2
        exporter = self._make_manager('eh_ct_offset_drift_exporter')

        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_ct()

        self.assertEqual(batch.sepa_ct_source_fingerprint, approved)
        self.assertFalse(batch.sepa_export_ids)

    def test_bank_target_requires_trust_owner_activity_and_company(self):
        def payment_for(partner, bank):
            batch = self.Batch.create({
                'journal_id': self.bank_journal.id,
                'batch_type': 'outbound',
                'sepa_ct_enabled': True,
                'payment_date': '2026-04-30',
            })
            payment = self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'partner_id': partner.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': 10.0,
                'date': '2026-04-15',
                'currency_id': self.eur.id,
                'partner_bank_id': bank.id,
            })
            return batch, payment

        untrusted = self._create_partner_bank_as_validator({
            'acc_number': 'NL91ABNA0417164300',
            'partner_id': self.partner_a.id,
            'allow_out_payment': True,
        })
        batch, payment = payment_for(self.partner_a, untrusted)
        self._write_partner_bank_as_validator(
            untrusted, {'allow_out_payment': False},
        )
        with self.assertRaises(UserError):
            batch._eh_assert_ct_bank_target(payment)

        archived = self._create_partner_bank_as_validator({
            'acc_number': 'ES9121000418450200051332',
            'partner_id': self.partner_a.id,
            'allow_out_payment': True,
        })
        batch, payment = payment_for(self.partner_a, archived)
        archived.active = False
        with self.assertRaises(UserError):
            batch._eh_assert_ct_bank_target(payment)

        wrong_owner = self._create_partner_bank_as_validator({
            'acc_number': 'IT60X0542811101000000123456',
            'partner_id': self.partner_a.id,
            'allow_out_payment': True,
        })
        batch, payment = payment_for(self.partner_a, wrong_owner)
        self._write_partner_bank_as_validator(wrong_owner, {
            'allow_out_payment': False,
            'partner_id': self.partner_b.id,
        })
        self._write_partner_bank_as_validator(
            wrong_owner, {'allow_out_payment': True},
        )
        with self.assertRaises(UserError):
            batch._eh_assert_ct_bank_target(payment)

        foreign_company = self.env['res.company'].create({
            'name': 'SEPA Foreign Bank Owner',
            'currency_id': self.eur.id,
        })
        foreign_partner = self.env['res.partner'].create({
            'name': 'SEPA Foreign Supplier',
            'company_id': foreign_company.id,
        })
        foreign_bank = self._create_partner_bank_as_validator({
            'acc_number': 'BE68539007547034',
            'partner_id': foreign_partner.id,
            'allow_out_payment': True,
        })
        with self.assertRaises(UserError):
            # Core check_company normally rejects this even earlier; the
            # exporter repeats the invariant for legacy/sudo-imported rows.
            payment_for(foreign_partner, foreign_bank)
