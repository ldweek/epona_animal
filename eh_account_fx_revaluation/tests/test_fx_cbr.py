# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Bank of Russia (CBR) FX rate provider.

The CBR feed is native base RUB and publishes "Nominal CODE = Value RUB"
with a decimal comma and a windows-1251 declaration. These tests feed the
parser canned bytes (no network) and assert:

* the native table is inverted to the correct direction, "1 RUB = X CODE";
* cross derivation for a non-RUB company currency yields the expected
  Decimal, computed by hand below;
* a quote the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.

The synthetic payload mirrors the published schema, including the JPY
Nominal=100 case that exercises the quantity normalisation, and is encoded
as windows-1251 to match the live declaration.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import cbr as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Declared windows-1251 with a decimal comma, as the live feed serves it.
# USD: Nominal 1, Value 90,1234. EUR: Nominal 1, Value 98,5000.
# JPY: Nominal 100, Value 59,0000 (the per-hundred quantity case).
_CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?>'
    '<ValCurs Date="01.05.2026" name="Foreign Currency Market">'
    '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode>'
    '<Nominal>1</Nominal><Name>US Dollar</Name>'
    '<Value>90,1234</Value><VunitRate>90,1234</VunitRate></Valute>'
    '<Valute ID="R01239"><CharCode>EUR</CharCode><Nominal>1</Nominal>'
    '<Value>98,5000</Value></Valute>'
    '<Valute ID="R01375"><CharCode>JPY</CharCode><Nominal>100</Nominal>'
    '<Value>59,0000</Value></Valute>'
    '</ValCurs>'
).encode('windows-1251')


@tagged('eh_account_fx_revaluation', 'unit')
class TestCbrProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cbr = mod.CbrRateProvider(timeout=1)
        self.cbr._download = self._stub_download(_CBR_XML)

    @staticmethod
    def _stub_download(payload):
        """Return a download stub serving the same bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_rub_per_code(self):
        # _fetch_native must publish "1 RUB = X CODE", the inverse of the
        # feed. USD: Nominal 1 / Value 90.1234 -> 1 RUB = 1/90.1234 USD.
        native = self.cbr._fetch_native(
            'RUB', ['USD', 'JPY'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(native['USD'], Decimal('1') / Decimal('90.1234'))
        # JPY: Nominal 100 / Value 59.0000 -> 1 RUB = 100/59 JPY.
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('59.0000'))

    def test_rub_base_direct(self):
        # Company in RUB: out[CODE] is the native value as-is, "1 RUB = X".
        rates = self.cbr.fetch(
            'RUB', ['USD', 'JPY'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(rates['USD'], Decimal('1') / Decimal('90.1234'))
        self.assertEqual(rates['JPY'], Decimal('100') / Decimal('59.0000'))

    def test_cross_derivation_for_non_rub_base(self):
        # Company in USD asks for RUB and JPY.
        rates = self.cbr.fetch(
            'USD', ['RUB', 'JPY'], datetime.date(2026, 5, 1),
        )
        # USD -> RUB: quote is the native base, so out[RUB] = 1 / native[USD]
        # = 1 / (1/90.1234) = 90.1234. The double Decimal division leaves a
        # rounding artifact in the final digits, so compare on floats.
        self.assertAlmostEqual(
            float(rates['RUB']), float(Decimal('90.1234')), places=8,
        )
        # USD -> JPY: native[JPY] / native[USD]
        #           = (100 / 59.0) / (1 / 90.1234).
        expected_jpy = (
            (Decimal('100') / Decimal('59.0000'))
            / (Decimal('1') / Decimal('90.1234'))
        )
        self.assertAlmostEqual(
            float(rates['JPY']), float(expected_jpy), places=8,
        )

    def test_missing_quote_omitted(self):
        # GBP is not in the fixture; it must be absent from the output
        # rather than defaulted, so the caller surfaces the gap.
        rates = self.cbr.fetch(
            'USD', ['RUB', 'GBP'], datetime.date(2026, 5, 1),
        )
        self.assertIn('RUB', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron handler
        # treats it as a recoverable outage rather than a crash.
        self.cbr._download = self._stub_download(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.cbr.fetch(
                'USD', ['RUB', 'JPY'], datetime.date(2026, 5, 1),
            )
