# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the National Bank of Romania (BNR) FX provider.

The BNR feed is native base RON and publishes "multiplier CODE = Rate RON"
inside a namespaced XML document, with the multiplier attribute optional
(defaulting to 1). These tests feed the parser canned bytes (no network,
no DB writes) and assert:

* the native table is inverted to the correct direction, "1 RON = X CODE",
  for both the plain case and the multiplier=100 case;
* cross derivation for a non-RON company currency yields the expected
  Decimal, including the path where a quote is the native base itself;
* a quote the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.

The synthetic payload mirrors the published schema, including the JPY
multiplier=100 case that exercises the quantity normalisation, and carries
the BNR xsd namespace to confirm the namespace-agnostic parsing.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bnr as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Namespaced exactly as the live feed serves it. EUR has no multiplier
# attribute (defaults to 1); JPY is quoted per 100 units.
_BNR_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<DataSet xmlns="http://www.bnr.ro/xsd">'
    '<Header><Publisher>National Bank of Romania</Publisher></Header>'
    '<Body>'
    '<OrigCurrency>RON</OrigCurrency>'
    '<Cube date="2026-06-22">'
    '<Rate currency="EUR">5.2386</Rate>'
    '<Rate currency="USD">4.5706</Rate>'
    '<Rate currency="JPY" multiplier="100">2.8261</Rate>'
    '</Cube>'
    '</Body>'
    '</DataSet>'
).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestBnrProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.bnr = mod.BnrRateProvider(timeout=1)
        self.bnr._download = self._stub_download(_BNR_XML)

    @staticmethod
    def _stub_download(payload):
        """Return a download stub serving the same bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_ron_per_code(self):
        # _fetch_native must publish "1 RON = X CODE", the inverse of the
        # feed. EUR: multiplier 1 / Rate 5.2386 -> 1 RON = 1/5.2386 EUR.
        native = self.bnr._fetch_native(
            'RON', ['EUR', 'JPY'], datetime.date(2026, 6, 22),
        )
        self.assertEqual(native['EUR'], Decimal('1') / Decimal('5.2386'))
        # JPY: multiplier 100 / Rate 2.8261 -> 1 RON = 100/2.8261 JPY.
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('2.8261'))

    def test_cross_derivation_for_non_ron_base(self):
        # Company in USD asks for RON, EUR and JPY.
        rates = self.bnr.fetch(
            'USD', ['RON', 'EUR', 'JPY'], datetime.date(2026, 6, 22),
        )
        # USD -> RON: quote is the native base, so out[RON] = 1 / native[USD]
        # = 1 / (1/4.5706) = 4.5706. The double Decimal division leaves a
        # rounding artifact in the final digits, so compare on floats.
        self.assertAlmostEqual(
            float(rates['RON']), float(Decimal('4.5706')), places=8,
        )
        # USD -> EUR: native[EUR] / native[USD]
        #           = (1 / 5.2386) / (1 / 4.5706).
        expected_eur = (
            (Decimal('1') / Decimal('5.2386'))
            / (Decimal('1') / Decimal('4.5706'))
        )
        self.assertAlmostEqual(
            float(rates['EUR']), float(expected_eur), places=8,
        )
        # USD -> JPY: native[JPY] / native[USD]
        #           = (100 / 2.8261) / (1 / 4.5706).
        expected_jpy = (
            (Decimal('100') / Decimal('2.8261'))
            / (Decimal('1') / Decimal('4.5706'))
        )
        self.assertAlmostEqual(
            float(rates['JPY']), float(expected_jpy), places=8,
        )

    def test_missing_quote_omitted(self):
        # GBP is not in the fixture; it must be absent from the output
        # rather than defaulted, so the caller surfaces the gap.
        rates = self.bnr.fetch(
            'USD', ['RON', 'GBP'], datetime.date(2026, 6, 22),
        )
        self.assertIn('RON', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron handler
        # treats it as a recoverable outage rather than a crash.
        self.bnr._download = self._stub_download(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.bnr.fetch(
                'USD', ['RON', 'EUR'], datetime.date(2026, 6, 22),
            )
