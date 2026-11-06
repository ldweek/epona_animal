# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
XRechnung (DE) PEPPOL profile tests.
"""


from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.de_profile import (
    DeProfileError, validate_de_payload,
    validate_de_vat, normalise_de_vat, is_valid_de_vat,
    validate_leitweg_id, normalise_leitweg, is_valid_leitweg_id,
    _de_vat_check_digit,
)


def _make_de_vat(body8):
    """Build a valid DE VAT from an 8-digit body."""
    return 'DE' + body8 + str(_de_vat_check_digit(body8))


VALID_VAT_A = _make_de_vat('11111111')
VALID_VAT_B = _make_de_vat('12345678')


@tagged('post_install', '-at_install')
class DeVatValidatorTest(TransactionCase):

    def test_normalise_strips_separators_and_uppercases(self):
        self.assertEqual(normalise_de_vat(' de-111-111-117 '), 'DE111111117')
        self.assertEqual(normalise_de_vat(''), '')
        self.assertEqual(normalise_de_vat(None), '')

    def test_validate_returns_canonical(self):
        self.assertEqual(validate_de_vat(VALID_VAT_A), VALID_VAT_A)
        self.assertEqual(
            validate_de_vat('de %s' % VALID_VAT_A[2:]),
            VALID_VAT_A,
        )

    def test_too_short_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_vat('DE12345678')

    def test_too_long_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_vat('DE1234567890')

    def test_wrong_country_prefix_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_vat('AT123456789')

    def test_non_numeric_body_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_vat('DEABCDEFGHI')

    def test_bad_checksum_rejected(self):
        bad = VALID_VAT_A[:-1] + str((int(VALID_VAT_A[-1]) + 1) % 10)
        with self.assertRaises(DeProfileError) as cm:
            validate_de_vat(bad)
        self.assertIn('checksum', str(cm.exception))

    def test_empty_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_vat('')

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_de_vat(VALID_VAT_A))
        self.assertFalse(is_valid_de_vat('DE000000000'))
        self.assertFalse(is_valid_de_vat(None))


@tagged('post_install', '-at_install')
class LeitwegValidatorTest(TransactionCase):

    def test_normalise_strips_whitespace_and_uppercases(self):
        self.assertEqual(
            normalise_leitweg(' 04011000-1234512345-06 '),
            '04011000-1234512345-06',
        )
        self.assertEqual(normalise_leitweg('04-99'), '04-99')

    def test_coarse_only_validates(self):
        self.assertTrue(is_valid_leitweg_id('04-99'))

    def test_coarse_plus_fine_validates(self):
        self.assertTrue(is_valid_leitweg_id('04011000-1234512345-06'))
        self.assertTrue(is_valid_leitweg_id('991-04011000-44'))

    def test_alphanumeric_coarse_validates(self):
        self.assertTrue(is_valid_leitweg_id('0123456789AB-99'))

    def test_coarse_too_long_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_leitweg_id('ABCDEFGHIJKLM-99')  # 13 chars

    def test_no_check_digits_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_leitweg_id('99-12345')

    def test_check_must_be_2_digits(self):
        with self.assertRaises(DeProfileError):
            validate_leitweg_id('04-9')
        with self.assertRaises(DeProfileError):
            validate_leitweg_id('04-999')

    def test_empty_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_leitweg_id('')

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_leitweg_id('04-99'))
        self.assertFalse(is_valid_leitweg_id(''))
        self.assertFalse(is_valid_leitweg_id(None))


def _de_b2b_payload(**overrides):
    payload = {
        'supplier': {
            'name': 'DE GmbH', 'country_code': 'DE',
            'endpoint_scheme': '9930', 'endpoint_id': VALID_VAT_A,
        },
        'customer': {
            'name': 'DE Buyer', 'country_code': 'DE',
            'endpoint_scheme': '9930', 'endpoint_id': VALID_VAT_B,
        },
        'tax_categories': [{'category_code': 'S', 'rate_pct': 19.0}],
        'lines': [
            {'tax_rate_pct': 19.0},
            {'tax_rate_pct': 7.0},
            {'tax_rate_pct': 0.0},
        ],
    }
    payload.update(overrides)
    return payload


@tagged('post_install', '-at_install')
class DeB2BPayloadTest(TransactionCase):

    def test_happy_b2b(self):
        validate_de_payload(_de_b2b_payload())

    def test_foreign_supplier_rejected(self):
        payload = _de_b2b_payload()
        payload['supplier']['country_code'] = 'AT'
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_supplier_non_vat_scheme_rejected(self):
        payload = _de_b2b_payload()
        payload['supplier']['endpoint_scheme'] = '0204'
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_supplier_bad_vat_rejected(self):
        payload = _de_b2b_payload()
        payload['supplier']['endpoint_id'] = 'DE000000000'
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_b2b_customer_with_no_endpoint_rejected(self):
        payload = _de_b2b_payload()
        payload['customer']['endpoint_id'] = ''
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)


@tagged('post_install', '-at_install')
class DeB2GPayloadTest(TransactionCase):

    def test_happy_b2g_with_leitweg(self):
        payload = _de_b2b_payload(customer={
            'name': 'Federal Authority', 'country_code': 'DE',
            'endpoint_scheme': '0204',
            'endpoint_id': '04011000-1234512345-06',
        })
        validate_de_payload(payload)

    def test_b2g_invalid_leitweg_rejected(self):
        payload = _de_b2b_payload(customer={
            'name': 'Bad Authority', 'country_code': 'DE',
            'endpoint_scheme': '0204', 'endpoint_id': 'not-a-leitweg',
        })
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)


@tagged('post_install', '-at_install')
class DeCrossBorderPayloadTest(TransactionCase):

    def test_de_to_at_validates(self):
        payload = _de_b2b_payload(
            customer={
                'name': 'AT Buyer', 'country_code': 'AT',
                'endpoint_scheme': '9930', 'endpoint_id': 'ATU12345678',
            },
            tax_categories=[{'category_code': 'K'}],
            lines=[{'tax_rate_pct': 0.0}],
        )
        validate_de_payload(payload)

    def test_country_prefix_mismatch_rejected(self):
        payload = _de_b2b_payload(customer={
            'name': 'AT Buyer', 'country_code': 'AT',
            'endpoint_scheme': '9930', 'endpoint_id': 'FRBADVAT123',
        })
        with self.assertRaises(DeProfileError) as cm:
            validate_de_payload(payload)
        self.assertIn('country prefix', str(cm.exception))


@tagged('post_install', '-at_install')
class DeRatesAndCategoriesTest(TransactionCase):

    def test_uk_rate_rejected(self):
        payload = _de_b2b_payload(lines=[{'tax_rate_pct': 20.0}])
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_au_rate_rejected(self):
        payload = _de_b2b_payload(lines=[{'tax_rate_pct': 10.0}])
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_anz_categories_rejected(self):
        # The PEPPOL standard category 'O' (Out of scope) is allowed
        # by some profiles but not by the German XRechnung set.
        payload = _de_b2b_payload(tax_categories=[{'category_code': 'O'}])
        with self.assertRaises(DeProfileError):
            validate_de_payload(payload)

    def test_my_categories_rejected(self):
        for bad in ('SR', 'ZR', 'ES', 'DS', 'OS'):
            payload = _de_b2b_payload(
                tax_categories=[{'category_code': bad}],
            )
            with self.assertRaises(DeProfileError):
                validate_de_payload(payload)

    def test_reverse_charge_AE_validates(self):
        payload = _de_b2b_payload(
            tax_categories=[{'category_code': 'AE'}],
            lines=[{'tax_rate_pct': 0.0}],
        )
        validate_de_payload(payload)

    def test_zero_seven_nineteen_all_validate(self):
        payload = _de_b2b_payload(lines=[
            {'tax_rate_pct': 0.0},
            {'tax_rate_pct': 7.0},
            {'tax_rate_pct': 19.0},
        ])
        validate_de_payload(payload)


@tagged('post_install', '-at_install')
class DePayloadEdgesTest(TransactionCase):

    def test_non_dict_payload_rejected(self):
        with self.assertRaises(DeProfileError):
            validate_de_payload('not a dict')
        with self.assertRaises(DeProfileError):
            validate_de_payload([])

    def test_empty_lines_does_not_break(self):
        payload = _de_b2b_payload()
        payload['lines'] = []
        validate_de_payload(payload)
