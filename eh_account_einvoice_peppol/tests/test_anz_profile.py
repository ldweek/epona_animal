# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
A-NZ PEPPOL profile tests.
"""


from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.anz_profile import (
    AnzProfileError, validate_anz_payload,
    validate_nzbn, normalise_nzbn, is_valid_nzbn,
)


def _make_nzbn(body12):
    """Build a valid NZBN from a 12-digit body using GS1 mod-10."""
    digits = [int(d) for d in body12]
    weighted = 0
    for pos, d in enumerate(reversed(digits)):
        weight = 3 if pos % 2 == 0 else 1
        weighted += d * weight
    check = (10 - (weighted % 10)) % 10
    return body12 + str(check)


VALID_NZBN = _make_nzbn('942900012345')
VALID_ABN_A = '83914571673'
VALID_ABN_B = '53004085616'


@tagged('post_install', '-at_install')
class NzbnValidatorTest(TransactionCase):

    def test_normalise_strips_non_digits(self):
        self.assertEqual(normalise_nzbn(' 9429-0001 23457 '), '9429000123457')
        self.assertEqual(normalise_nzbn(''), '')
        self.assertEqual(normalise_nzbn(None), '')

    def test_validate_returns_canonical(self):
        self.assertEqual(validate_nzbn(VALID_NZBN), VALID_NZBN)
        # With separators
        spaced = ' '.join([VALID_NZBN[:4], VALID_NZBN[4:9], VALID_NZBN[9:]])
        self.assertEqual(validate_nzbn(spaced), VALID_NZBN)

    def test_validate_rejects_wrong_length(self):
        with self.assertRaises(AnzProfileError):
            validate_nzbn('123')
        with self.assertRaises(AnzProfileError):
            validate_nzbn('1' * 14)

    def test_validate_rejects_bad_checksum(self):
        bad = VALID_NZBN[:-1] + str((int(VALID_NZBN[-1]) + 1) % 10)
        with self.assertRaises(AnzProfileError) as cm:
            validate_nzbn(bad)
        self.assertIn('checksum', str(cm.exception))

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_nzbn(VALID_NZBN))
        self.assertFalse(is_valid_nzbn('9999999999999'))
        self.assertFalse(is_valid_nzbn(''))
        self.assertFalse(is_valid_nzbn(None))


def _au_payload(**overrides):
    payload = {
        'supplier': {
            'name': 'AU Co', 'country_code': 'AU',
            'endpoint_scheme': '0151', 'endpoint_id': VALID_ABN_A,
        },
        'customer': {
            'name': 'AU Buyer', 'country_code': 'AU',
            'endpoint_scheme': '0151', 'endpoint_id': VALID_ABN_B,
        },
        'tax_categories': [{'category_code': 'S', 'rate_pct': 10.0}],
        'lines': [{'tax_rate_pct': 10.0}, {'tax_rate_pct': 0.0}],
    }
    payload.update(overrides)
    return payload


@tagged('post_install', '-at_install')
class AnzPayloadValidationTest(TransactionCase):

    def test_happy_au_payload(self):
        validate_anz_payload(_au_payload())

    def test_cross_tasman_au_supplier_nz_buyer(self):
        payload = _au_payload(customer={
            'name': 'NZ Buyer', 'country_code': 'NZ',
            'endpoint_scheme': '0088', 'endpoint_id': VALID_NZBN,
        })
        validate_anz_payload(payload)

    def test_nz_supplier_uses_15_pct(self):
        payload = {
            'supplier': {
                'name': 'NZ Co', 'country_code': 'NZ',
                'endpoint_scheme': '0088', 'endpoint_id': VALID_NZBN,
            },
            'customer': {
                'name': 'NZ Buyer', 'country_code': 'NZ',
                'endpoint_scheme': '0088', 'endpoint_id': VALID_NZBN,
            },
            'tax_categories': [{'category_code': 'S'}],
            'lines': [{'tax_rate_pct': 15.0}],
        }
        validate_anz_payload(payload)

    def test_au_supplier_with_15_pct_rejected(self):
        payload = _au_payload()
        payload['lines'] = [{'tax_rate_pct': 15.0}]
        with self.assertRaises(AnzProfileError) as cm:
            validate_anz_payload(payload)
        self.assertIn('AU GST', str(cm.exception))

    def test_nz_supplier_with_au_scheme_rejected(self):
        payload = {
            'supplier': {
                'name': 'NZ Co', 'country_code': 'NZ',
                'endpoint_scheme': '0151',  # ABN, not GLN
                'endpoint_id': VALID_ABN_A,
            },
            'customer': {
                'name': 'NZ Buyer', 'country_code': 'NZ',
                'endpoint_scheme': '0088', 'endpoint_id': VALID_NZBN,
            },
            'tax_categories': [],
            'lines': [{'tax_rate_pct': 15.0}],
        }
        with self.assertRaises(AnzProfileError) as cm:
            validate_anz_payload(payload)
        self.assertIn('0088', str(cm.exception))

    def test_au_supplier_with_nz_scheme_rejected(self):
        payload = _au_payload()
        payload['supplier']['endpoint_scheme'] = '0088'
        with self.assertRaises(AnzProfileError) as cm:
            validate_anz_payload(payload)
        self.assertIn('0151', str(cm.exception))

    def test_non_au_nz_country_rejected(self):
        payload = _au_payload()
        payload['supplier']['country_code'] = 'US'
        with self.assertRaises(AnzProfileError) as cm:
            validate_anz_payload(payload)
        self.assertIn('AU or NZ', str(cm.exception))

    def test_invalid_tax_category_rejected(self):
        payload = _au_payload()
        payload['tax_categories'] = [{'category_code': 'AE'}]
        with self.assertRaises(AnzProfileError) as cm:
            validate_anz_payload(payload)
        self.assertIn('AE', str(cm.exception))

    def test_export_and_outside_scope_categories_are_permitted(self):
        for category in ('G', 'O'):
            payload = _au_payload()
            payload['tax_categories'] = [{'category_code': category}]
            payload['lines'] = [{'tax_rate_pct': 0.0}]
            self.assertTrue(validate_anz_payload(payload))

    def test_zero_rate_permitted_for_export_lines(self):
        payload = _au_payload()
        payload['lines'] = [{'tax_rate_pct': 0.0}]  # GST-free supply
        validate_anz_payload(payload)

    def test_missing_endpoint_id_rejected(self):
        payload = _au_payload()
        payload['supplier']['endpoint_id'] = ''
        with self.assertRaises(AnzProfileError):
            validate_anz_payload(payload)

    def test_non_dict_payload_rejected(self):
        with self.assertRaises(AnzProfileError):
            validate_anz_payload([])
        with self.assertRaises(AnzProfileError):
            validate_anz_payload('not a dict')

    def test_payload_with_no_lines_skips_rate_check(self):
        # Edge case: a payload with no lines (eg pre-line stage in a
        # future API) should still validate party info.
        payload = _au_payload()
        payload['lines'] = []
        validate_anz_payload(payload)
