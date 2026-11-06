# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
ABN validator tests.

The validator is pure-Python so the unit tests do not need a DB.
The partner-integration test exercises the constrains hook via a
TransactionCase.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_l10n_au_bas.tools.abn_validator import (
    validate_abn, normalise_abn, is_valid_abn, AbnValidationError,
)


@tagged('eh_account_l10n_au_bas', 'unit')
class TestAbnValidator(TransactionCase):

    # Two ATO sample ABNs known to satisfy the mod-89 weighted check.
    VALID_ABN_A = "83914571673"
    VALID_ABN_B = "53004085616"

    def test_normalise_strips_whitespace_and_punctuation(self):
        self.assertEqual(normalise_abn(" 83 914 571 673 "), self.VALID_ABN_A)
        self.assertEqual(normalise_abn("53-004-085-616"), self.VALID_ABN_B)
        self.assertEqual(normalise_abn(""), "")
        self.assertEqual(normalise_abn(None), "")

    def test_validate_returns_canonical_form(self):
        self.assertEqual(validate_abn(" 83 914 571 673 "), self.VALID_ABN_A)
        self.assertEqual(validate_abn(self.VALID_ABN_B), self.VALID_ABN_B)

    def test_validate_rejects_wrong_length(self):
        with self.assertRaises(AbnValidationError):
            validate_abn("12345")
        with self.assertRaises(AbnValidationError):
            validate_abn("123456789012")

    def test_validate_rejects_bad_checksum(self):
        # Flip the last digit; checksum should fail.
        bad = self.VALID_ABN_A[:-1] + "0"
        with self.assertRaises(AbnValidationError):
            validate_abn(bad)

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_abn(self.VALID_ABN_A))
        self.assertFalse(is_valid_abn("12345678901"))
        self.assertFalse(is_valid_abn(""))
        self.assertFalse(is_valid_abn(None))


@tagged('eh_account_l10n_au_bas', 'integration', 'post_install', '-at_install')
class TestPartnerAbn(TransactionCase):

    VALID_ABN = "83914571673"

    def test_partner_accepts_valid_abn(self):
        partner = self.env['res.partner'].create({
            'name': 'AU Vendor',
            'eh_au_abn': self.VALID_ABN,
        })
        self.assertEqual(partner.eh_au_abn, self.VALID_ABN)
        self.assertEqual(partner.vat, self.VALID_ABN)
        self.assertTrue(partner.eh_au_abn_valid)

    def test_partner_normalises_whitespace_on_create(self):
        partner = self.env['res.partner'].create({
            'name': 'AU Vendor 2',
            'eh_au_abn': ' 83 914 571 673 ',
        })
        self.assertEqual(partner.eh_au_abn, self.VALID_ABN)

    def test_partner_rejects_bad_checksum(self):
        with self.assertRaises(ValidationError):
            self.env['res.partner'].create({
                'name': 'Bad ABN Vendor',
                'eh_au_abn': '12345678901',
            })

    def test_partner_rejects_wrong_length(self):
        with self.assertRaises(ValidationError):
            self.env['res.partner'].create({
                'name': 'Short ABN Vendor',
                'eh_au_abn': '123',
            })

    def test_partner_allows_blank(self):
        # Blank ABN is permitted; the field is optional.
        partner = self.env['res.partner'].create({
            'name': 'No ABN Vendor',
        })
        self.assertFalse(partner.eh_au_abn)
        self.assertFalse(partner.eh_au_abn_valid)

    def test_partner_normalises_on_write(self):
        partner = self.env['res.partner'].create({'name': 'Write Test'})
        partner.eh_au_abn = '83-914-571-673'
        self.assertEqual(partner.eh_au_abn, self.VALID_ABN)
        self.assertEqual(partner.vat, self.VALID_ABN)

    def test_standard_vat_abn_populates_alias(self):
        partner = self.env['res.partner'].create({
            'name': 'Standard VAT ABN',
            'vat': '83 914 571 673',
        })
        self.assertEqual(partner.vat, self.VALID_ABN)
        self.assertEqual(partner.eh_au_abn, self.VALID_ABN)

    def test_conflicting_vat_and_abn_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.env['res.partner'].create({
                'name': 'Conflicting ABN',
                'eh_au_abn': self.VALID_ABN,
                'vat': '53004085616',
            })
