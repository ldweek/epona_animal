# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the National Bank of Kazakhstan (NBKZ) provider.

The NBKZ feed is native base KZT and publishes an RSS 2.0 document whose
items read "quant CODE = description KZT". These tests feed the parser
canned bytes (no network) and assert:

* the native table is inverted to the correct direction, "1 KZT = X CODE",
  honouring a quant of 10 so a per-ten quotation is not off by an order of
  magnitude;
* cross derivation for a non-KZT company currency yields the expected
  Decimal, computed by hand below;
* a quote the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.

The synthetic payload mirrors the published RSS shape, including a
namespaced ``quant`` element to exercise the namespace-agnostic walk.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import nbkz as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# AUD: description 340.91, quant 1  -> 1 KZT = 1/340.91 AUD.
# AMD: description 13.32,  quant 10 -> 1 KZT = 10/13.32 AMD (per-ten case).
# USD: description 526.0,  quant 1  -> 1 KZT = 1/526.0 USD.
# The AMD quant is declared in a foreign namespace to prove the parser
# matches on the local tag name rather than the fully qualified name.
_NBKZ_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0" xmlns:nb="http://nationalbank.kz/ns">'
    '<channel>'
    '<title>Official rates</title>'
    '<item><title>AUD</title><description>340.91</description>'
    '<quant>1</quant><pubDate>23.06.2026</pubDate></item>'
    '<item><title>AMD</title><description>13.32</description>'
    '<nb:quant>10</nb:quant></item>'
    '<item><title>USD</title><description>526.0</description>'
    '<quant>1</quant></item>'
    '</channel></rss>'
).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestNbkzProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.nbkz = mod.NbkzRateProvider(timeout=1)
        self.nbkz._download = self._stub_download(_NBKZ_XML)

    @staticmethod
    def _stub_download(payload):
        """Return a download stub serving the same bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_kzt_per_code(self):
        # _fetch_native must publish "1 KZT = X CODE", the inverse of the
        # feed. AUD: quant 1 / description 340.91 -> 1 KZT = 1/340.91 AUD.
        native = self.nbkz._fetch_native(
            'KZT', ['AUD', 'AMD'], datetime.date(2026, 6, 23),
        )
        self.assertEqual(native['AUD'], Decimal('1') / Decimal('340.91'))
        # AMD: quant 10 / description 13.32 -> 1 KZT = 10/13.32 AMD. The
        # quant is namespaced, so a successful match also proves the
        # namespace-agnostic walk.
        self.assertEqual(native['AMD'], Decimal('10') / Decimal('13.32'))

    def test_kzt_base_direct(self):
        # Company in KZT: out[CODE] is the native value as-is, "1 KZT = X".
        rates = self.nbkz.fetch(
            'KZT', ['USD', 'AUD'], datetime.date(2026, 6, 23),
        )
        self.assertEqual(rates['USD'], Decimal('1') / Decimal('526.0'))
        self.assertEqual(rates['AUD'], Decimal('1') / Decimal('340.91'))

    def test_cross_derivation_for_non_kzt_base(self):
        # Company in USD asks for KZT and AUD.
        rates = self.nbkz.fetch(
            'USD', ['KZT', 'AUD'], datetime.date(2026, 6, 23),
        )
        # USD -> KZT: quote is the native base, so out[KZT] = 1 / native[USD]
        # = 1 / (1/526.0) = 526.0. The double Decimal division leaves a
        # rounding artifact in the final digits, so compare on floats.
        self.assertAlmostEqual(
            float(rates['KZT']), float(Decimal('526.0')), places=8,
        )
        # USD -> AUD: native[AUD] / native[USD]
        #           = (1/340.91) / (1/526.0) = 526.0 / 340.91.
        expected_aud = (
            (Decimal('1') / Decimal('340.91'))
            / (Decimal('1') / Decimal('526.0'))
        )
        self.assertAlmostEqual(
            float(rates['AUD']), float(expected_aud), places=8,
        )
        self.assertAlmostEqual(
            float(rates['AUD']),
            float(Decimal('526.0') / Decimal('340.91')),
            places=8,
        )

    def test_missing_quote_omitted(self):
        # GBP is not in the fixture; it must be absent from the output
        # rather than defaulted, so the caller surfaces the gap.
        rates = self.nbkz.fetch(
            'USD', ['KZT', 'GBP'], datetime.date(2026, 6, 23),
        )
        self.assertIn('KZT', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron handler
        # treats it as a recoverable outage rather than a crash.
        self.nbkz._download = self._stub_download(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.nbkz.fetch(
                'USD', ['KZT', 'AUD'], datetime.date(2026, 6, 23),
            )
