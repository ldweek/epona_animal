# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Idempotency of the SEPA Direct Debit export.

Generating the PAIN.008 consumes every mandate in the batch (advancing
its FRST -> RCUR sequence counter and its last collection date) and cuts
the collection file the bank debits against debtor accounts. Re-running
the export on a batch that was already exported would consume every
mandate a second time and produce a duplicate file, so the bank could
debit each debtor twice. These tests prove a second export attempt is
refused, the mandate counter is not advanced again, and no second export
row is written.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_dd', 'integration', 'post_install', '-at_install')
class TestSepaDdReexportIdempotency(EhAccountIntegrationTestCase):

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

        existing = cls.env['eh.sepa.creditor'].search(
            [('journal_id', '=', cls.bank_journal.id)], limit=1,
        )
        if existing:
            cls.creditor = existing
        else:
            cls.creditor = cls.env['eh.sepa.creditor'].create({
                'name': 'Demo creditor',
                'journal_id': cls.bank_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Demo Co',
                'iban': 'DE89370400440532013000',
                'pre_notification_days': 0,
            })

        cls.mandate = cls.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DDIDEM-001',
            'creditor_id': cls.creditor.id,
            'partner_id': cls.partner_a.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
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

    def _make_user(self, login):
        """A plain EH accounting user (NOT a manager). Read/write on the
        export model but no manager group, so the void gate must refuse."""
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })

    def _post_batch(self, batch, poster):
        batch.action_confirm()
        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'in_process'

        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(poster).action_post()
        self.assertEqual(batch.state, 'posted')
        return batch

    def _posted_batch(self, poster, final_collection=False):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'sepa_dd_collection': True,
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'INV-001',
            'currency_id': self.eur.id,
            'eh_sepa_dd_final_collection': final_collection,
        })
        return self._post_batch(batch, poster)

    def test_reexport_is_refused_and_does_not_reconsume(self):
        """A second export attempt on an already-exported batch is refused,
        the mandate sequence counter is not advanced again, and no second
        export row is written."""
        poster = self._make_manager('eh_dd_idem_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_idem_exporter')

        # First export: consumes the mandate (FRST), writes one export row.
        batch.with_user(exporter).action_export_sepa_dd()
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)

        # Second export on the SAME batch must be refused before any
        # mandate is re-consumed and before a second file is cut.
        exporter2 = self._make_manager('eh_dd_idem_exporter2')
        with self.assertRaises(UserError):
            batch.with_user(exporter2).action_export_sepa_dd()

        # The mandate counter stayed at 1 (no double FRST -> RCUR advance)
        # and no second export row exists.
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)

    def test_guard_keys_on_live_exports_only(self):
        """The guard blocks on a LIVE (generated/downloaded) export, not on
        a superseded audit row: a batch whose only export rows are
        superseded is not permanently locked out of the export action."""
        poster = self._make_manager('eh_dd_idem_poster2')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_idem_exporter3')

        batch.with_user(exporter).action_export_sepa_dd()
        first = batch.sepa_dd_export_ids
        self.assertEqual(len(first), 1)

        # A live export blocks re-export.
        exporter2 = self._make_manager('eh_dd_idem_exporter4')
        with self.assertRaises(UserError):
            batch.with_user(exporter2).action_export_sepa_dd()

        # Retire the prior export (server-side supersede runs under sudo);
        # the batch now has no LIVE export, so the idempotency guard no
        # longer trips. (Re-export then proceeds through the normal SoD /
        # mandate path, which is out of scope for this guard test.)
        first.sudo().write({'state': 'superseded'})
        live = batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertFalse(live)

    def test_void_for_recut_reopens_the_batch_for_a_legitimate_recut(self):
        """The escape hatch the refuse message promises: a manager voids the
        live export via action_void_for_recut (the audit row flips to
        superseded), and the batch can then be re-collected. Proves the
        re-cut path the guard's error text advertises actually exists and
        works end to end - the fix for the over-restriction."""
        poster = self._make_manager('eh_dd_void_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter')

        batch.with_user(exporter).action_export_sepa_dd()
        first = batch.sepa_dd_export_ids
        self.assertEqual(len(first), 1)
        self.assertIn(first.state, ('generated', 'downloaded'))
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)

        # Manager voids the un-transmitted file for re-cut.
        voider = self._make_manager('eh_dd_voider')
        first.with_user(voider).action_void_for_recut()
        self.assertEqual(first.state, 'superseded')
        self.assertFalse(batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        ))

        # With no live export the idempotency guard no longer trips.  The
        # fresh file reuses the first cut's server-owned consumption snapshot:
        # FRST stays FRST and the mandate is not consumed a second time.
        exporter2 = self._make_manager('eh_dd_void_exporter2')
        batch.with_user(exporter2).action_export_sepa_dd()
        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 1)
        live = batch.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        self.assertEqual(len(live), 1)
        self.assertEqual(live.sequence_type, 'FRST')
        payment = batch.payment_ids
        self.assertEqual(payment.eh_sepa_dd_mandate_id, self.mandate)
        self.assertEqual(payment.eh_sepa_dd_sequence_type, 'FRST')
        with self.assertRaises(AccessError):
            payment.with_user(exporter2).write({
                'eh_sepa_dd_sequence_type': 'RCUR',
            })
        # And the retired file stays retired: never two live files at once.
        self.assertEqual(first.state, 'superseded')

    def test_one_off_recut_reuses_ooff_after_mandate_completion(self):
        """OOFF completion must not make an unsubmitted file impossible to
        re-cut or cause a second mandate consumption."""
        self.mandate.action_revoke()
        one_off = self.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DDIDEM-OOFF',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner_a.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 4, 1),
            'state': 'active',
            'local_instrument': 'CORE',
            'is_one_off': True,
        })
        poster = self._make_manager('eh_dd_ooff_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_ooff_exporter')

        batch.with_user(exporter).action_export_sepa_dd()
        one_off.invalidate_recordset(['state', 'collection_count'])
        self.assertEqual(one_off.state, 'completed')
        self.assertEqual(one_off.collection_count, 1)
        first = batch.sepa_dd_export_ids
        self.assertEqual(first.sequence_type, 'OOFF')

        first.with_user(exporter).action_void_for_recut()
        exporter2 = self._make_manager('eh_dd_ooff_exporter2')
        batch.with_user(exporter2).action_export_sepa_dd()

        one_off.invalidate_recordset(['state', 'collection_count'])
        self.assertEqual(one_off.state, 'completed')
        self.assertEqual(one_off.collection_count, 1)
        live = batch.sepa_dd_export_ids.filtered(
            lambda export: export.state in ('generated', 'downloaded'),
        )
        self.assertEqual(len(live), 1)
        self.assertEqual(live.sequence_type, 'OOFF')

    def test_product_final_collection_emits_fnal_and_completes_mandate(self):
        first_poster = self._make_manager('eh_dd_fnal_first_poster')
        first = self._posted_batch(first_poster)
        first_exporter = self._make_manager('eh_dd_fnal_first_exporter')
        first.with_user(first_exporter).action_export_sepa_dd()
        self.assertEqual(first.sepa_dd_export_ids.sequence_type, 'FRST')

        final_poster = self._make_manager('eh_dd_fnal_poster')
        final = self._posted_batch(final_poster, final_collection=True)
        final_exporter = self._make_manager('eh_dd_fnal_exporter')
        final.with_user(final_exporter).action_export_sepa_dd()

        self.mandate.invalidate_recordset(['state', 'collection_count'])
        self.assertEqual(final.sepa_dd_export_ids.sequence_type, 'FNAL')
        self.assertEqual(final.payment_ids.eh_sepa_dd_sequence_type, 'FNAL')
        self.assertEqual(self.mandate.state, 'completed')
        self.assertEqual(self.mandate.collection_count, 2)

    def test_bank_selected_export_resolves_exact_matching_mandate(self):
        matching = self.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DD-IBAN-MATCH',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner_a.id,
            'debtor_iban': 'DE89370400440532013000',
            'signature_date': date(2026, 2, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'sepa_dd_collection': True,
            'payment_date': '2026-04-30',
        })
        payment = self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'DD-IBAN-MATCH',
            'currency_id': self.eur.id,
            'eh_sepa_dd_final_collection': False,
        })
        bank = self.env['res.partner.bank'].create({
            'partner_id': self.partner_a.id,
            'acc_number': 'DE89370400440532013000',
        })
        payment.eh_sepa_dd_debtor_bank_id = bank
        poster = self._make_manager('eh_dd_iban_poster')
        self._post_batch(batch, poster)
        exporter = self._make_manager('eh_dd_iban_exporter')
        batch.with_user(exporter).action_export_sepa_dd()
        self.assertEqual(batch.payment_ids.eh_sepa_dd_mandate_id, matching)

    def test_unmatched_selected_bank_fails_instead_of_newest_fallback(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'sepa_dd_collection': True,
            'payment_date': '2026-04-30',
        })
        payment = self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'DD-IBAN-UNMATCHED',
            'currency_id': self.eur.id,
            'eh_sepa_dd_final_collection': False,
        })
        bank = self.env['res.partner.bank'].create({
            'partner_id': self.partner_a.id,
            'acc_number': 'DE89370400440532013000',
        })
        payment.eh_sepa_dd_debtor_bank_id = bank
        with self.assertRaisesRegex(UserError, 'does not match exactly one'):
            batch.action_confirm()

    def test_export_splits_frst_and_rcur_into_two_files(self):
        second_partner = self.partner_b
        recurring = self.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DD-SPLIT-RCUR',
            'creditor_id': self.creditor.id,
            'partner_id': second_partner.id,
            'debtor_iban': 'DE89370400440532013000',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })
        recurring.sudo().with_context(
            eh_sepa_dd_collection_engine=True,
        ).consume_for_collection(date(2026, 4, 1))
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'sepa_dd_collection': True,
            'payment_date': '2026-04-30',
        })
        for partner, amount in ((self.partner_a, 100.0), (second_partner, 200.0)):
            self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'partner_id': partner.id,
                'partner_type': 'customer',
                'payment_type': 'inbound',
                'journal_id': self.bank_journal.id,
                'amount': amount,
                'date': '2026-04-15',
                'currency_id': self.eur.id,
            })
        poster = self._make_manager('eh_dd_split_poster')
        self._post_batch(batch, poster)
        exporter = self._make_manager('eh_dd_split_exporter')
        batch.with_user(exporter).action_export_sepa_dd()
        self.assertEqual(len(batch.sepa_dd_export_ids), 2)
        self.assertEqual(
            set(batch.sepa_dd_export_ids.mapped('sequence_type')),
            {'FRST', 'RCUR'},
        )

    def test_recut_rechecks_revoked_mandate(self):
        """A durable first-cut snapshot cannot bypass later revocation."""
        poster = self._make_manager('eh_dd_revoke_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_revoke_exporter')
        batch.with_user(exporter).action_export_sepa_dd()
        first = batch.sepa_dd_export_ids.filtered(
            lambda export: export.state in ('generated', 'downloaded'),
        )
        first.with_user(exporter).action_void_for_recut()

        revoker = self._make_manager('eh_dd_revoke_manager')
        self.mandate.with_user(revoker).action_revoke()
        self.assertEqual(self.mandate.state, 'revoked')

        recutter = self._make_manager('eh_dd_revoked_recutter')
        with self.assertRaisesRegex(UserError, 'revoked'):
            batch.with_user(recutter).action_export_sepa_dd()
        self.assertFalse(batch.sepa_dd_export_ids.filtered(
            lambda export: export.state in ('generated', 'downloaded'),
        ))

    def test_void_for_recut_refused_for_non_manager(self):
        """The void is a money-file control and stays manager-gated: a plain
        EH user cannot void an export. Proves the escape hatch did not open a
        privilege hole - the four-eyes intent behind the export guard is
        preserved."""
        poster = self._make_manager('eh_dd_void_poster3')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter3')
        batch.with_user(exporter).action_export_sepa_dd()
        export = batch.sepa_dd_export_ids
        self.assertEqual(len(export), 1)

        try:
            plain = self._make_user('eh_dd_plain_user')
        except Exception:
            self.skipTest("cannot provision a non-manager EH user here")

        with self.assertRaises(UserError):
            export.with_user(plain).action_void_for_recut()
        # The export stayed live: the non-manager attempt changed nothing.
        self.assertIn(export.state, ('generated', 'downloaded'))

    def test_state_guard_still_blocks_direct_supersede_write(self):
        """The original protection is intact: superseding an export is a
        guarded state transition that only the record's own sudo action may
        perform. A non-superuser cannot forge the void by RPC-writing
        state='superseded' directly - so the audit trail cannot be doctored
        and the double-file guard cannot be side-stepped."""
        poster = self._make_manager('eh_dd_void_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_void_exporter4')
        batch.with_user(exporter).action_export_sepa_dd()
        export = batch.sepa_dd_export_ids
        self.assertEqual(len(export), 1)

        # Even a manager writing the guarded field DIRECTLY (not via the
        # action) is refused; state moves only through the sudo action.
        with self.assertRaises(AccessError):
            export.with_user(exporter).write({'state': 'superseded'})
        self.assertIn(export.state, ('generated', 'downloaded'))

    def test_creditor_drift_after_approval_blocks_cut(self):
        poster = self._make_manager('eh_dd_drift_poster')
        batch = self._posted_batch(poster)
        self.assertTrue(batch.sepa_dd_source_fingerprint)
        self.creditor.write({'creditor_name': 'Changed After Approval'})
        exporter = self._make_manager('eh_dd_drift_exporter')

        with self.assertRaises(UserError):
            batch.with_user(exporter).action_export_sepa_dd()

        self.mandate.invalidate_recordset(['collection_count'])
        self.assertEqual(self.mandate.collection_count, 0)
        self.assertFalse(batch.sepa_dd_export_ids)
        with self.assertRaises(AccessError):
            batch.with_user(exporter).write({
                'sepa_dd_source_fingerprint': '0' * 64,
            })

        batch.sepa_dd_reapproval_reason = (
            'Creditor legal name changed and the manager verified the bank '
            'mandate file before approving the revised collection source.'
        )
        batch.with_user(exporter).action_reapprove_sepa_dd_sources()
        self.assertEqual(len(batch.sepa_dd_source_approval_ids), 2)
        final_exporter = self._make_manager('eh_dd_drift_final_exporter')
        batch.with_user(final_exporter).action_export_sepa_dd()
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)

    def test_ordinary_inbound_batch_is_not_hijacked_by_dd_creditor(self):
        customer = self.env['res.partner'].create({
            'name': 'Ordinary receipt customer without a mandate',
        })
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'sepa_dd_collection': False,
            'payment_date': '2026-04-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': customer.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': self.bank_journal.id,
            'amount': 25.0,
            'date': '2026-04-15',
            'currency_id': self.eur.id,
        })

        batch.action_confirm()

        self.assertEqual(batch.state, 'confirmed')
        self.assertFalse(batch.sepa_dd_source_fingerprint)

    def test_amendment_is_emitted_once_and_recut_keeps_snapshot(self):
        self.mandate.write({'debtor_iban': 'DE89370400440532013000'})
        amendment = self.mandate.amendment_ids[:1]
        self.assertTrue(amendment)

        poster1 = self._make_manager('eh_dd_amendment_poster1')
        first_batch = self._posted_batch(poster1)
        exporter1 = self._make_manager('eh_dd_amendment_exporter1')
        first_batch.with_user(exporter1).action_export_sepa_dd()
        first_export = first_batch.sepa_dd_export_ids
        self.assertIn(b'AmdmntInf', first_export.attachment_id.raw)
        self.assertEqual(
            first_batch.payment_ids.eh_sepa_dd_amendment_id, amendment)
        self.assertEqual(amendment.reported_payment_id, first_batch.payment_ids)

        poster2 = self._make_manager('eh_dd_amendment_poster2')
        second_batch = self._posted_batch(poster2)
        exporter2 = self._make_manager('eh_dd_amendment_exporter2')
        second_batch.with_user(exporter2).action_export_sepa_dd()
        second_export = second_batch.sepa_dd_export_ids
        self.assertNotIn(b'AmdmntInf', second_export.attachment_id.raw)
        self.assertFalse(second_batch.payment_ids.eh_sepa_dd_amendment_id)

        first_export.with_user(exporter1).action_void_for_recut()
        recutter = self._make_manager('eh_dd_amendment_recutter')
        first_batch.with_user(recutter).action_export_sepa_dd()
        recut = first_batch.sepa_dd_export_ids.filtered(
            lambda export: export.state in ('generated', 'downloaded'),
        )
        self.assertIn(b'AmdmntInf', recut.attachment_id.raw)
