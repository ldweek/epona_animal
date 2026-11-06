# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
MyInvois (MY) PEPPOL profile tests.
"""


from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.my_profile import (
    MyProfileError, validate_my_payload,
    validate_my_tin, normalise_my_tin, is_valid_my_tin,
)


VALID_SUPPLIER_TIN = 'C1234567890'
VALID_CUSTOMER_TIN = 'C9876543210'


@tagged('post_install', '-at_install')
class MyTinValidatorTest(TransactionCase):

    def test_normalise_strips_separators_and_uppercases(self):
        self.assertEqual(normalise_my_tin(' c-1234.5678.90 '), 'C1234567890')
        self.assertEqual(normalise_my_tin(''), '')
        self.assertEqual(normalise_my_tin(None), '')

    def test_validate_returns_canonical(self):
        self.assertEqual(validate_my_tin(VALID_SUPPLIER_TIN), VALID_SUPPLIER_TIN)
        self.assertEqual(validate_my_tin('c1234567890'), 'C1234567890')

    def test_one_letter_prefix_accepted(self):
        self.assertTrue(is_valid_my_tin('C1234567890'))
        self.assertTrue(is_valid_my_tin('D1234567890'))

    def test_two_letter_prefix_accepted(self):
        self.assertTrue(is_valid_my_tin('CS12345678901'))
        self.assertTrue(is_valid_my_tin('SG1234567890'))
        self.assertTrue(is_valid_my_tin('PT123456789012'))

    def test_three_letter_prefix_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_tin('CCC1234567890')

    def test_no_letter_prefix_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_tin('12345678901')

    def test_too_short_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_tin('C123')

    def test_too_long_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_tin('C12345678901234567890')

    def test_empty_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_tin('')

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_my_tin(VALID_SUPPLIER_TIN))
        self.assertFalse(is_valid_my_tin('not-a-tin'))
        self.assertFalse(is_valid_my_tin(None))


def _my_domestic_payload(**overrides):
    payload = {
        'supplier': {
            'name': 'MY Co Sdn Bhd', 'country_code': 'MY',
            'endpoint_scheme': 'TIN', 'endpoint_id': VALID_SUPPLIER_TIN,
        },
        'customer': {
            'name': 'MY Buyer', 'country_code': 'MY',
            'endpoint_scheme': 'TIN', 'endpoint_id': VALID_CUSTOMER_TIN,
        },
        'tax_categories': [{'category_code': '02', 'rate_pct': 6.0}],
        'lines': [
            {'tax_category_code': '02', 'tax_rate_pct': 6.0},
            {'tax_category_code': '02', 'tax_rate_pct': 8.0},
            {'tax_category_code': '06', 'tax_rate_pct': 0.0},
        ],
    }
    payload.update(overrides)
    return payload


@tagged('post_install', '-at_install')
class MyDomesticPayloadTest(TransactionCase):

    def test_happy_domestic(self):
        validate_my_payload(_my_domestic_payload())

    def test_foreign_supplier_rejected(self):
        payload = _my_domestic_payload()
        payload['supplier']['country_code'] = 'SG'
        with self.assertRaises(MyProfileError) as cm:
            validate_my_payload(payload)
        self.assertIn('MY', str(cm.exception))

    def test_supplier_missing_tin_rejected(self):
        payload = _my_domestic_payload()
        payload['supplier']['endpoint_id'] = ''
        with self.assertRaises(MyProfileError):
            validate_my_payload(payload)

    def test_supplier_bad_tin_rejected(self):
        payload = _my_domestic_payload()
        payload['supplier']['endpoint_id'] = 'not-a-tin'
        with self.assertRaises(MyProfileError) as cm:
            validate_my_payload(payload)
        self.assertIn('TIN', str(cm.exception))

    def test_domestic_customer_without_tin_rejected(self):
        payload = _my_domestic_payload()
        payload['customer']['endpoint_id'] = ''
        with self.assertRaises(MyProfileError):
            validate_my_payload(payload)

    def test_invalid_tax_category_rejected(self):
        payload = _my_domestic_payload()
        payload['tax_categories'] = [{'category_code': 'S'}]  # EN 16931 code
        with self.assertRaises(MyProfileError) as cm:
            validate_my_payload(payload)
        self.assertIn('S', str(cm.exception))

    def test_legacy_and_en16931_categories_are_rejected(self):
        # MyInvois uses its published tax-type codelist, not GST-era
        # SR/ZR/ES/DS/OS labels or EN 16931 S/Z/AE/K categories.
        for bad in ('SR', 'ZR', 'ES', 'DS', 'OS', 'S', 'Z', 'AE', 'K'):
            payload = _my_domestic_payload()
            payload['tax_categories'] = [{'category_code': bad}]
            with self.assertRaises(MyProfileError):
                validate_my_payload(payload)

    def test_five_and_ten_percent_sales_tax_rates_are_valid(self):
        payload = _my_domestic_payload()
        payload['tax_categories'] = [
            {'category_code': '01', 'rate_pct': 5.0},
            {'category_code': '01', 'rate_pct': 10.0},
        ]
        payload['lines'] = [
            {'tax_category_code': '01', 'tax_rate_pct': 5.0},
            {'tax_category_code': '01', 'tax_rate_pct': 10.0},
        ]
        validate_my_payload(payload)

    def test_invalid_rate_rejected_for_domestic(self):
        payload = _my_domestic_payload()
        payload['lines'] = [
            {'tax_category_code': '02', 'tax_rate_pct': 7.0},
        ]
        with self.assertRaises(MyProfileError) as cm:
            validate_my_payload(payload)
        self.assertIn('Allowed', str(cm.exception))

    def test_percentage_rate_must_match_myinvois_tax_type(self):
        for tax_type, wrong_rate in (('01', 8.0), ('02', 10.0)):
            payload = _my_domestic_payload()
            payload['tax_categories'] = [
                {'category_code': tax_type, 'rate_pct': wrong_rate},
            ]
            payload['lines'] = [{
                'tax_category_code': tax_type,
                'tax_rate_pct': wrong_rate,
            }]
            with self.assertRaises(MyProfileError):
                validate_my_payload(payload)

    def test_zero_rate_permitted_for_domestic(self):
        payload = _my_domestic_payload()
        payload['lines'] = [
            {'tax_category_code': 'E', 'tax_rate_pct': 0.0},
        ]
        validate_my_payload(payload)


@tagged('post_install', '-at_install')
class MyExportPayloadTest(TransactionCase):

    def test_export_to_sg_validates(self):
        payload = _my_domestic_payload(customer={
            'name': 'SG Buyer', 'country_code': 'SG', 'endpoint_id': '',
        })
        payload['tax_categories'] = [{'category_code': '06'}]
        payload['lines'] = [
            {'tax_category_code': '06', 'tax_rate_pct': 0.0},
        ]
        validate_my_payload(payload)

    def test_export_with_foreign_vat_rate_rejected(self):
        # A MyInvois document carries Malaysian tax semantics even when the
        # buyer is foreign; a foreign VAT percentage is not an SST rate.
        payload = _my_domestic_payload(customer={
            'name': 'EU Buyer', 'country_code': 'DE', 'endpoint_id': '',
        })
        payload['lines'] = [
            {'tax_category_code': '02', 'tax_rate_pct': 19.0},
        ]
        with self.assertRaises(MyProfileError):
            validate_my_payload(payload)

    def test_all_published_myinvois_tax_type_codes_are_valid(self):
        payload = _my_domestic_payload()
        payload['tax_categories'] = [
            {'category_code': code}
            for code in ('01', '02', '03', '04', '05', '06', 'E')
        ]
        validate_my_payload(payload)

    def test_foreign_country_code_must_be_2_letters(self):
        payload = _my_domestic_payload(customer={
            'name': 'Foreign Buyer', 'country_code': 'XYZ',
            'endpoint_id': '',
        })
        with self.assertRaises(MyProfileError):
            validate_my_payload(payload)


@tagged('post_install', '-at_install')
class MyPayloadEdgesTest(TransactionCase):

    def test_non_dict_payload_rejected(self):
        with self.assertRaises(MyProfileError):
            validate_my_payload('not a dict')
        with self.assertRaises(MyProfileError):
            validate_my_payload([])

    def test_empty_lines_does_not_break(self):
        payload = _my_domestic_payload()
        payload['lines'] = []
        validate_my_payload(payload)
