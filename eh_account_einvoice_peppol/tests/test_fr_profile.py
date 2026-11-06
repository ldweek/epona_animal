# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Factur-X (FR) PEPPOL profile tests.
"""


from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools.fr_profile import (
    FrProfileError, validate_fr_payload,
    validate_fr_vat, normalise_fr_vat, is_valid_fr_vat,
    validate_fr_siret, normalise_fr_siret, is_valid_fr_siret,
)


def _make_fr_vat(siren9):
    """Build a valid FR VAT from a SIREN body using DGFiP mod-97."""
    siren_int = int(siren9)
    key = (12 + 3 * (siren_int % 97)) % 97
    return 'FR%02d%s' % (key, siren9)


def _make_luhn14(prefix13):
    """Build a 14-digit Luhn-valid number by brute-forcing the last digit."""
    for last in range(10):
        cand = prefix13 + str(last)
        total = 0
        for pos, ch in enumerate(reversed(cand)):
            d = int(ch)
            if pos % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        if total % 10 == 0:
            return cand
    raise RuntimeError("could not construct Luhn-valid number")


# Two valid FR VATs: L'Oreal SIREN (well-known) and a synthetic one.
VALID_VAT_A = _make_fr_vat('732829320')
VALID_VAT_B = _make_fr_vat('123456789')
VALID_SIRET = _make_luhn14('7328293200001')


@tagged('post_install', '-at_install')
class FrVatValidatorTest(TransactionCase):

    def test_normalise_strips_separators_and_uppercases(self):
        self.assertEqual(
            normalise_fr_vat(' fr 44 732 829 320 '),
            VALID_VAT_A,
        )
        self.assertEqual(normalise_fr_vat(''), '')

    def test_validate_returns_canonical(self):
        self.assertEqual(validate_fr_vat(VALID_VAT_A), VALID_VAT_A)

    def test_known_siren_l_oreal(self):
        # The L'Oreal SIREN (732 829 320) computes to key 44.
        self.assertTrue(is_valid_fr_vat('FR44732829320'))

    def test_too_short_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_vat('FR12345')

    def test_too_long_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_vat('FR4473282932099')

    def test_wrong_country_prefix_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_vat('DE44732829320')

    def test_bad_numeric_key_rejected(self):
        bad = 'FR99' + VALID_VAT_A[4:]
        with self.assertRaises(FrProfileError) as cm:
            validate_fr_vat(bad)
        self.assertIn('checksum', str(cm.exception))

    def test_alphanumeric_key_accepted_structurally(self):
        # Alphanumeric keys are reserved for overseas territories /
        # certain entities. Format passes; checksum verification is
        # delegated to the DGFiP registry.
        self.assertTrue(is_valid_fr_vat('FRAA123456789'))
        self.assertTrue(is_valid_fr_vat('FRZ9123456789'))

    def test_lower_case_input_normalised(self):
        self.assertEqual(
            validate_fr_vat('fr44732829320'),
            'FR44732829320',
        )

    def test_empty_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_vat('')

    def test_is_valid_returns_bool(self):
        self.assertTrue(is_valid_fr_vat(VALID_VAT_A))
        self.assertFalse(is_valid_fr_vat(None))
        self.assertFalse(is_valid_fr_vat('DE12345'))


@tagged('post_install', '-at_install')
class FrSiretValidatorTest(TransactionCase):

    def test_normalise_strips_separators(self):
        self.assertEqual(normalise_fr_siret(' 732 829 320 00017 '),
                         '73282932000017')

    def test_valid_siret(self):
        self.assertTrue(is_valid_fr_siret(VALID_SIRET))

    def test_wrong_length_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_siret('1234')

    def test_bad_luhn_rejected(self):
        # Random 14-digit number unlikely to be Luhn-valid.
        with self.assertRaises(FrProfileError):
            validate_fr_siret('12345678901234')

    def test_empty_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_siret('')


def _fr_b2b_payload(**overrides):
    payload = {
        'supplier': {
            'name': 'FR SARL', 'country_code': 'FR',
            'endpoint_scheme': '9930', 'endpoint_id': VALID_VAT_A,
        },
        'customer': {
            'name': 'FR Buyer SAS', 'country_code': 'FR',
            'endpoint_scheme': '9930', 'endpoint_id': VALID_VAT_B,
        },
        'tax_categories': [{'category_code': 'S', 'rate_pct': 20.0}],
        'lines': [
            {'tax_rate_pct': 20.0},
            {'tax_rate_pct': 10.0},
            {'tax_rate_pct': 5.5},
            {'tax_rate_pct': 2.1},
            {'tax_rate_pct': 0.0},
        ],
    }
    payload.update(overrides)
    return payload


@tagged('post_install', '-at_install')
class FrB2BPayloadTest(TransactionCase):

    def test_happy_b2b(self):
        validate_fr_payload(_fr_b2b_payload())

    def test_foreign_supplier_rejected(self):
        payload = _fr_b2b_payload()
        payload['supplier']['country_code'] = 'DE'
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_supplier_non_vat_scheme_rejected(self):
        payload = _fr_b2b_payload()
        payload['supplier']['endpoint_scheme'] = '0009'
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_supplier_bad_vat_rejected(self):
        payload = _fr_b2b_payload()
        payload['supplier']['endpoint_id'] = 'FR00000000000'
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)


@tagged('post_install', '-at_install')
class FrB2CPayloadTest(TransactionCase):

    def test_b2c_no_endpoint_validates(self):
        payload = _fr_b2b_payload(customer={
            'name': 'Mme Dupont', 'country_code': 'FR',
        })
        validate_fr_payload(payload)

    def test_b2c_with_siret_validates(self):
        payload = _fr_b2b_payload(customer={
            'name': 'Sole Trader', 'country_code': 'FR',
            'endpoint_scheme': '0009', 'endpoint_id': VALID_SIRET,
        })
        validate_fr_payload(payload)

    def test_b2c_bad_siret_rejected(self):
        payload = _fr_b2b_payload(customer={
            'name': 'Sole Trader', 'country_code': 'FR',
            'endpoint_scheme': '0009', 'endpoint_id': '12345678901234',
        })
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)


@tagged('post_install', '-at_install')
class FrCrossBorderPayloadTest(TransactionCase):

    def test_fr_to_be_validates(self):
        payload = _fr_b2b_payload(
            customer={
                'name': 'BE Buyer', 'country_code': 'BE',
                'endpoint_scheme': '9930', 'endpoint_id': 'BE0123456789',
            },
            tax_categories=[{'category_code': 'K'}],
            lines=[{'tax_rate_pct': 0.0}],
        )
        validate_fr_payload(payload)

    def test_country_prefix_mismatch_rejected(self):
        payload = _fr_b2b_payload(customer={
            'name': 'BE Buyer', 'country_code': 'BE',
            'endpoint_scheme': '9930', 'endpoint_id': 'ITBADVAT123',
        })
        with self.assertRaises(FrProfileError) as cm:
            validate_fr_payload(payload)
        self.assertIn('country prefix', str(cm.exception))


@tagged('post_install', '-at_install')
class FrRatesAndCategoriesTest(TransactionCase):

    def test_de_rate_rejected(self):
        payload = _fr_b2b_payload(lines=[{'tax_rate_pct': 19.0}])
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_nz_rate_rejected(self):
        payload = _fr_b2b_payload(lines=[{'tax_rate_pct': 15.0}])
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_french_intermediate_ten_percent_rate_is_accepted(self):
        payload = _fr_b2b_payload(lines=[{'tax_rate_pct': 10.0}])
        # 10.0 happens to BE a valid FR rate (intermediate tax),
        # so this case actually validates. Adjust test to a rate
        # not in the FR set.
        validate_fr_payload(payload)

    def test_my_rate_rejected(self):
        payload = _fr_b2b_payload(lines=[{'tax_rate_pct': 6.0}])
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_my_categories_rejected(self):
        for bad in ('SR', 'ZR', 'ES', 'DS', 'OS'):
            payload = _fr_b2b_payload(
                tax_categories=[{'category_code': bad}],
            )
            with self.assertRaises(FrProfileError):
                validate_fr_payload(payload)

    def test_o_category_rejected(self):
        payload = _fr_b2b_payload(tax_categories=[{'category_code': 'O'}])
        with self.assertRaises(FrProfileError):
            validate_fr_payload(payload)

    def test_reverse_charge_AE_validates(self):
        payload = _fr_b2b_payload(
            tax_categories=[{'category_code': 'AE'}],
            lines=[{'tax_rate_pct': 0.0}],
        )
        validate_fr_payload(payload)

    def test_all_five_fr_rates_validate(self):
        payload = _fr_b2b_payload(lines=[
            {'tax_rate_pct': 0.0},
            {'tax_rate_pct': 2.1},
            {'tax_rate_pct': 5.5},
            {'tax_rate_pct': 10.0},
            {'tax_rate_pct': 20.0},
        ])
        validate_fr_payload(payload)


@tagged('post_install', '-at_install')
class FrPayloadEdgesTest(TransactionCase):

    def test_non_dict_payload_rejected(self):
        with self.assertRaises(FrProfileError):
            validate_fr_payload('not a dict')
        with self.assertRaises(FrProfileError):
            validate_fr_payload([])

    def test_empty_lines_does_not_break(self):
        payload = _fr_b2b_payload()
        payload['lines'] = []
        validate_fr_payload(payload)
