# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Peppol participant identifier validator.

The validator is pure Python (no ORM dependency) so the tests run fast
without a database fixture. Cases cover:

* Scheme code allow-list (positive + rejected unknowns).
* ABN (0151) checksum: positive case, mutated digit fails, separator
  normalisation accepts the human-readable form.
* GLN (0088) GS1 mod-10: positive, mutated digit fails.
* Numeric fixed-length schemes (0007, 0192, 0184, 0196).
* German VAT shape (9930): country prefix required.
* Generic alphanumeric for unhandled schemes.
* normalise_id strips whitespace and separators consistently.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.peppol_id_validator import (
    PeppolIdentifierError,
    normalise_id,
    validate_participant,
    validate_scheme,
)


@tagged('eh_account_einvoice_peppol', 'unit')
class TestPeppolIdValidator(TransactionCase):

    # ---- normalise_id ----

    def test_normalise_strips_separators(self):
        self.assertEqual(normalise_id('51 824 753 556'), '51824753556')
        self.assertEqual(normalise_id('51-824.753_556'), '51824753556')
        self.assertEqual(normalise_id('0151:51824753556'), '015151824753556')
        self.assertEqual(normalise_id(' 51824753556 '), '51824753556')

    def test_normalise_handles_none_and_empty(self):
        self.assertEqual(normalise_id(None), '')
        self.assertEqual(normalise_id(''), '')

    # ---- validate_scheme ----

    def test_validate_scheme_accepts_known(self):
        for code in ('0151', '0088', '0192', '0221', '0230', '9930'):
            self.assertEqual(validate_scheme(code), code)

    def test_validate_scheme_rejects_unknown(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_scheme('9999')

    def test_validate_scheme_rejects_malformed(self):
        for bad in ('151', '01510', 'abcd', ''):
            with self.assertRaises(PeppolIdentifierError):
                validate_scheme(bad)

    # ---- ABN (0151) ----

    def test_abn_valid_passes(self):
        # 51824753556 is a published ATO test ABN (passes the
        # weighted checksum). No real entity is identified.
        scheme, ident = validate_participant('0151', '51824753556')
        self.assertEqual((scheme, ident), ('0151', '51824753556'))

    def test_abn_with_separators_normalises(self):
        scheme, ident = validate_participant('0151', '51 824 753 556')
        self.assertEqual(ident, '51824753556')

    def test_abn_wrong_length_rejected(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0151', '5182475355')  # 10 digits

    def test_abn_bad_checksum_rejected(self):
        # Mutate one digit so the weighted sum mod 89 != 0.
        with self.assertRaises(PeppolIdentifierError) as ctx:
            validate_participant('0151', '51824753557')
        self.assertIn('checksum', str(ctx.exception))

    # ---- GLN (0088) ----

    def test_gln_valid_passes(self):
        # 5012345678900 is a synthetic GLN; verified against the
        # GS1 mod-10 (Luhn-like) algorithm.
        scheme, ident = validate_participant('0088', '5012345678900')
        self.assertEqual((scheme, ident), ('0088', '5012345678900'))

    def test_gln_wrong_length_rejected(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0088', '501234567890')  # 12 digits

    def test_gln_bad_check_rejected(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0088', '5012345678901')

    # ---- Numeric fixed-length schemes ----

    def test_numeric_fixed_schemes(self):
        # Norway 0192: valid 9-digit organisation number with mod-11.
        scheme, ident = validate_participant('0192', '991825827')
        self.assertEqual(ident, '991825827')
        # Sweden 0007: 10 digits.
        scheme, ident = validate_participant('0007', '5567030000')
        self.assertEqual(ident, '5567030000')
        # Denmark 0184: 8 digits.
        scheme, ident = validate_participant('0184', '12345678')
        self.assertEqual(ident, '12345678')
        # Iceland 0196: 10 digits.
        scheme, ident = validate_participant('0196', '1234567890')
        self.assertEqual(ident, '1234567890')

    def test_numeric_fixed_wrong_length_rejected(self):
        for scheme, value in (
            ('0192', '1234567890'),   # 10 digits
            ('0007', '1234567'),      # 7 digits
            ('0184', '1234567'),      # 7 digits
        ):
            with self.assertRaises(PeppolIdentifierError):
                validate_participant(scheme, value)

    def test_norwegian_org_number_bad_checksum_rejected(self):
        with self.assertRaisesRegex(PeppolIdentifierError, 'mod-11'):
            validate_participant('0192', '991825828')

    # ---- German VAT (9930) ----

    def test_german_vat_shape(self):
        scheme, ident = validate_participant('9930', 'DE123456789')
        self.assertEqual((scheme, ident), ('9930', 'DE123456789'))

    def test_german_vat_lowercase_uppercased(self):
        scheme, ident = validate_participant('9930', 'de123456789')
        self.assertEqual(ident, 'DE123456789')

    def test_german_vat_missing_prefix_rejected(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('9930', '123456789')

    # ---- Generic schemes ----

    def test_generic_scheme_accepts_alphanumeric(self):
        scheme, ident = validate_participant('0002', 'ABC123')
        self.assertEqual(ident, 'ABC123')

    def test_new_japan_and_malaysia_schemes_accept_generic_ids(self):
        self.assertEqual(
            validate_participant('0221', 'JP1234567890123'),
            ('0221', 'JP1234567890123'),
        )
        self.assertEqual(
            validate_participant('0230', 'C1234567890'),
            ('0230', 'C1234567890'),
        )

    def test_generic_scheme_rejects_special_chars(self):
        # Underscore is a separator stripped by normalise_id, so it
        # passes; a slash is not stripped and fails the alphanumeric
        # regex.
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0002', 'AB/CD')

    # ---- Empty / missing ----

    def test_empty_identifier_rejected(self):
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0151', '')
        with self.assertRaises(PeppolIdentifierError):
            validate_participant('0151', None)
