# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IBAN validator tests.

Sample IBANs are sourced from the public IBAN registry's example
section. These are well-known test IBANs published by SWIFT and
adopted by every payment library; they are factual data, not creative
work, so reusing them carries no copyright weight.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, is_iban, normalise_iban, IbanValidationError,
)


@tagged('eh_account_sepa_ct', 'unit')
class TestIbanValidator(TransactionCase):

    def test_normalise_strips_spaces_and_uppercases(self):
        self.assertEqual(
            normalise_iban('de89 3704 0044 0532 0130 00'),
            'DE89370400440532013000',
        )

    def test_valid_de_iban(self):
        cleaned = validate_iban('DE89 3704 0044 0532 0130 00')
        self.assertEqual(cleaned, 'DE89370400440532013000')

    def test_valid_fr_iban(self):
        # Example from the public IBAN registry FR section.
        cleaned = validate_iban('FR1420041010050500013M02606')
        self.assertEqual(cleaned, 'FR1420041010050500013M02606')

    def test_valid_gb_iban(self):
        cleaned = validate_iban('GB29 NWBK 6016 1331 9268 19')
        self.assertEqual(cleaned, 'GB29NWBK60161331926819')

    def test_invalid_country(self):
        with self.assertRaises(IbanValidationError) as cm:
            validate_iban('XX12345678')
        self.assertIn('country', str(cm.exception).lower())

    def test_invalid_length(self):
        # Truncated DE IBAN.
        with self.assertRaises(IbanValidationError) as cm:
            validate_iban('DE893704004405320130')
        self.assertIn('characters', str(cm.exception).lower())

    def test_invalid_check_digits_mod97(self):
        # Tamper with check digits of a known-good IBAN.
        with self.assertRaises(IbanValidationError) as cm:
            validate_iban('DE99370400440532013000')
        self.assertIn('mod-97', str(cm.exception).lower())

    def test_invalid_characters(self):
        with self.assertRaises(IbanValidationError):
            validate_iban('DE89-3704-0044-0532-0130-00')

    def test_empty_input(self):
        with self.assertRaises(IbanValidationError):
            validate_iban('')

    def test_is_iban_helper(self):
        self.assertTrue(is_iban('DE89370400440532013000'))
        self.assertFalse(is_iban('NOT_AN_IBAN'))
