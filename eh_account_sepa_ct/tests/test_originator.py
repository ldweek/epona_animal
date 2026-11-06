# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
SEPA originator model tests: verifies IBAN/BIC validation runs on
write, that the canonicalised forms are persisted, and that name
length is enforced per the scheme.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaOriginator(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Originator = cls.env['eh.sepa.originator']
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
        cls.company_bank = cls.env['res.partner.bank'].create({
            'acc_number': 'DE89370400440532013000',
            'partner_id': cls.env.company.partner_id.id,
            'company_id': cls.env.company.id,
        })
        cls.bank_journal.bank_account_id = cls.company_bank

    def test_create_canonicalises_iban_and_bic(self):
        rec = self.Originator.create({
            'journal_id': self.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'de89 3704 0044 0532 0130 00',
            'bic': 'deutdeff',
        })
        self.assertEqual(rec.iban, 'DE89370400440532013000')
        self.assertEqual(rec.bic, 'DEUTDEFFXXX')
        self.assertEqual(rec.pain_001_version, '09')
        self.assertEqual(rec.segregation_policy, 'two_person')

    def test_invalid_iban_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co',
                'iban': 'DE99370400440532013000',  # tampered check digits
            })

    def test_invalid_bic_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co',
                'iban': 'DE89370400440532013000',
                'bic': 'TOO_SHORT',
            })

    def test_too_long_party_name_rejected(self):
        with self.assertRaises(ValidationError):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'X' * 71,
                'iban': 'DE89370400440532013000',
            })

    def test_unique_per_journal(self):
        self.Originator.create({
            'journal_id': self.bank_journal.id,
            'initiating_party_name': 'Demo Co',
            'iban': 'DE89370400440532013000',
        })
        with self.assertRaises(Exception):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co Duplicate',
                'iban': 'DE89370400440532013000',
            })

    def test_execution_offset_must_be_bounded_calendar_days(self):
        for invalid in (-1, 366):
            with self.subTest(offset=invalid), self.assertRaises(
                    ValidationError):
                with self.env.cr.savepoint():
                    self.Originator.create({
                        'journal_id': self.bank_journal.id,
                        'initiating_party_name': 'Demo Co',
                        'iban': 'DE89370400440532013000',
                        'requested_execution_offset_days': invalid,
                    })

    def test_originator_must_match_gl_journal_bank_account(self):
        with self.assertRaisesRegex(ValidationError, 'does not match'):
            self.Originator.create({
                'journal_id': self.bank_journal.id,
                'initiating_party_name': 'Demo Co',
                'iban': 'FR1420041010050500013M02606',
            })
