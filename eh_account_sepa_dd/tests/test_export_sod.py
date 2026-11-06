# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank-file segregation of duties for the SEPA Direct Debit export.

Generating the PAIN.008 is a money-moving act (it is the collection
instruction the bank executes against debtor accounts), so it must
respect the maker/checker control: the manager group is required to
generate the file AND the exporter must be a different user from the
people who assembled the batch (confirmer / poster). These tests prove
the assembler cannot also cut the bank file, while a distinct manager
can.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_dd', 'integration', 'post_install', '-at_install')
class TestSepaDdExportSoD(EhAccountIntegrationTestCase):

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

        # Creditor so the export can build the PAIN.008 payload.
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

        # Active mandate authorising collection from partner_a.
        cls.env['eh.sepa.mandate'].create({
            'mandate_id': 'MNDT-DDSOD-001',
            'creditor_id': cls.creditor.id,
            'partner_id': cls.partner_a.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })

        # SEPA DD is euro-denominated; the export refuses a non-euro
        # batch. The base fixture pins the company currency to USD, so
        # activate EUR and give it a rate for the payment currency.
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

    def _posted_batch(self, poster):
        """Confirm (as the class user) then post (as `poster`) a one-line
        inbound batch, returning it in the posted state ready to export."""
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
        })
        batch.action_confirm()

        PaymentModel = type(self.env['account.payment'])

        def _fake_post(payment):
            payment.state = 'in_process'

        with patch.object(PaymentModel, 'action_post', _fake_post):
            batch.with_user(poster).action_post()
        self.assertEqual(batch.state, 'posted')
        return batch

    def test_confirmer_cannot_export(self):
        """The manager who assembled/confirmed the batch cannot also
        generate the SEPA DD bank file: four-eyes on a money-moving
        export."""
        poster = self._make_manager('eh_dd_poster')
        batch = self._posted_batch(poster)
        self.assertEqual(batch.confirmed_by_id, self.env.user)
        with self.assertRaises(UserError):
            batch.action_export_sepa_dd()
        self.assertFalse(batch.sepa_dd_export_ids)

    def test_poster_cannot_export(self):
        """The manager who posted the batch is also part of assembling it
        and must not be the one who cuts the bank file."""
        poster = self._make_manager('eh_dd_poster2')
        batch = self._posted_batch(poster)
        self.assertEqual(batch.posted_by_id, poster)
        with self.assertRaises(UserError):
            batch.with_user(poster).action_export_sepa_dd()
        self.assertFalse(batch.sepa_dd_export_ids)

    def test_non_manager_cannot_export(self):
        """Even a distinct user without the manager group is refused: the
        export gate requires the manager group."""
        poster = self._make_manager('eh_dd_poster3')
        batch = self._posted_batch(poster)
        member = self.env['res.users'].create({
            'name': 'eh_dd_member',
            'login': 'eh_dd_member',
            'email': 'eh_dd_member@example.com',
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })
        with self.assertRaises(UserError):
            batch.with_user(member).action_export_sepa_dd()
        self.assertFalse(batch.sepa_dd_export_ids)

    def test_rpc_context_cannot_bypass_pre_notification(self):
        """A client-controlled context key is not an emergency capability."""
        self.creditor.write({'pre_notification_days': 1})
        poster = self._make_manager('eh_dd_notice_poster')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_notice_exporter')

        with self.assertRaises(UserError):
            batch.with_user(exporter).with_context(
                eh_sepa_dd_force_now=True,
            ).action_export_sepa_dd()

        self.assertFalse(batch.sepa_dd_export_ids)

    def test_third_manager_can_export(self):
        """A distinct EH Accounting Manager who neither confirmed nor
        posted the batch may generate the bank file."""
        poster = self._make_manager('eh_dd_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_dd_exporter')
        self.assertNotEqual(exporter, batch.confirmed_by_id)
        self.assertNotEqual(exporter, batch.posted_by_id)

        batch.with_user(exporter).action_export_sepa_dd()
        self.assertEqual(len(batch.sepa_dd_export_ids), 1)
        self.assertEqual(
            batch.sepa_dd_export_ids.generated_by_id, exporter,
        )
