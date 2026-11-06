# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank-file segregation of duties for the SEPA Credit Transfer export.

Generating the PAIN.001 is a money-moving act (it is the instruction the
bank executes), so it must respect the maker/checker control: the manager
group is required to generate the file AND the exporter must be a different
user from the people who assembled the batch (confirmer / poster). These
tests prove the assembler cannot also cut the bank file, while a distinct
manager can.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaExportSoD(EhAccountIntegrationTestCase):

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

        # SEPA originator so the export can build the PAIN.001 payload.
        cls.env['eh.sepa.originator'].create({
            'journal_id': cls.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'DE89370400440532013000',
            'bic': 'DEUTDEFFXXX',
            'segregation_policy': 'three_person',
        })

        # Creditor partner needs a valid IBAN bank account on file.
        cls.creditor_bank = cls._create_partner_bank_as_validator({
            'acc_number': 'FR1420041010050500013M02606',
            'partner_id': cls.partner_a.id,
            'allow_out_payment': True,
        })

        # SEPA CT is euro-denominated; the export refuses a non-euro
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
        outbound batch, returning it in the posted state ready to export."""
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
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'INV-001',
            'currency_id': self.eur.id,
            'partner_bank_id': self.creditor_bank.id,
        })
        batch.action_confirm()

        batch.with_user(poster).action_post()
        self.assertEqual(batch.state, 'posted')
        return batch

    def test_confirmer_cannot_export(self):
        """The manager who assembled/confirmed the batch cannot also
        generate the SEPA bank file: four-eyes on a money-moving export."""
        poster = self._make_manager('eh_sepa_poster')
        batch = self._posted_batch(poster)
        self.assertEqual(batch.confirmed_by_id, self.env.user)
        # The confirmer (class user) attempts the export: blocked.
        with self.assertRaises(UserError):
            batch.action_export_sepa_ct()
        self.assertFalse(batch.sepa_export_ids)

    def test_poster_cannot_export(self):
        """The manager who posted the batch is also part of assembling it
        and must not be the one who cuts the bank file."""
        poster = self._make_manager('eh_sepa_poster2')
        batch = self._posted_batch(poster)
        self.assertEqual(batch.posted_by_id, poster)
        with self.assertRaises(UserError):
            batch.with_user(poster).action_export_sepa_ct()
        self.assertFalse(batch.sepa_export_ids)

    def test_non_manager_cannot_export(self):
        """Even a distinct user without the manager group is refused: the
        export gate requires the manager group."""
        poster = self._make_manager('eh_sepa_poster3')
        batch = self._posted_batch(poster)
        member = self.env['res.users'].create({
            'name': 'eh_sepa_member',
            'login': 'eh_sepa_member',
            'email': 'eh_sepa_member@example.com',
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_user',
            ).id)],
        })
        with self.assertRaises(UserError):
            batch.with_user(member).action_export_sepa_ct()
        self.assertFalse(batch.sepa_export_ids)

    def test_third_manager_can_export(self):
        """A distinct EH Accounting Manager who neither confirmed nor
        posted the batch may generate the bank file."""
        poster = self._make_manager('eh_sepa_poster4')
        batch = self._posted_batch(poster)
        exporter = self._make_manager('eh_sepa_exporter')
        self.assertNotEqual(exporter, batch.confirmed_by_id)
        self.assertNotEqual(exporter, batch.posted_by_id)

        action = batch.with_user(exporter).action_export_sepa_ct()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(len(batch.sepa_export_ids), 1)
        self.assertEqual(
            batch.sepa_export_ids.generated_by_id, exporter,
        )

    def test_two_person_policy_allows_poster_to_export(self):
        originator = self.env['eh.sepa.originator'].search([
            ('journal_id', '=', self.bank_journal.id),
        ], limit=1)
        originator.segregation_policy = 'two_person'
        poster = self._make_manager('eh_sepa_two_person_poster')
        batch = self._posted_batch(poster)

        action = batch.with_user(poster).action_export_sepa_ct()

        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(batch.sepa_export_ids.state, 'generated')
