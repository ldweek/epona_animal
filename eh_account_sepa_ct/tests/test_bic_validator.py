# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
BIC validator tests built around the ISO 9362 structural rules.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, is_bic, BicValidationError,
)


@tagged('eh_account_sepa_ct', 'unit')
class TestBicValidator(TransactionCase):

    def test_8_char_bic_canonicalises_to_11(self):
        # 8-char BIC must extend with the default branch 'XXX'.
        self.assertEqual(validate_bic('DEUTDEFF'), 'DEUTDEFFXXX')

    def test_11_char_bic_passes_through(self):
        self.assertEqual(validate_bic('DEUTDEFF500'), 'DEUTDEFF500')

    def test_lowercase_uppercased(self):
        self.assertEqual(validate_bic('deutdeff'), 'DEUTDEFFXXX')

    def test_spaces_stripped(self):
        self.assertEqual(validate_bic(' DEUT DEFF '), 'DEUTDEFFXXX')

    def test_invalid_length(self):
        with self.assertRaises(BicValidationError):
            validate_bic('DEUT')

    def test_invalid_country_position(self):
        # Country segment must be 2 letters; digits in slot 5-6 fails.
        with self.assertRaises(BicValidationError):
            validate_bic('DEUT12FF')

    def test_invalid_chars_in_branch(self):
        with self.assertRaises(BicValidationError):
            validate_bic('DEUTDEFF50!')

    def test_empty_raises(self):
        with self.assertRaises(BicValidationError):
            validate_bic('')

    def test_is_bic_helper(self):
        self.assertTrue(is_bic('DEUTDEFFXXX'))
        self.assertFalse(is_bic('NOPE'))

    def test_2014_bic_accepts_alphanumeric_business_party_prefix(self):
        self.assertEqual(validate_bic('1EUTDEFF'), '1EUTDEFFXXX')

    def test_legacy_pain_03_uses_its_stricter_xsd_pattern(self):
        self.assertEqual(
            validate_bic('DEUTDE2F', version='03'),
            'DEUTDE2FXXX',
        )
        with self.assertRaises(BicValidationError):
            validate_bic('1EUTDEFF', version='03')
        with self.assertRaises(BicValidationError):
            validate_bic('DEUTDE0F', version='03')
