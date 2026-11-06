# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Central Bank of Turkey (TCMB) rate provider.

The parser is pure Python with no Odoo or network dependency. The tests
replace the provider's `_download` seam with a stub that serves a canned
today.xml payload, then assert:

* the native table inverts the published "Unit code = selling lira"
  direction into "1 lira = Unit / selling code", in the right magnitude;
* a company whose currency is not the native base (TRY) gets correct
  cross rates, including the lira leg itself;
* a quote the feed does not carry is omitted from the output;
* a row carrying only a banknote selling value still resolves;
* malformed payload bytes raise RateProviderError.

All tests are hermetic: no database, no records, no live network.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import tcmb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Synthetic today.xml. USD has Unit 1, JPY Unit 100. EUR carries only a
# banknote selling column to exercise the fallback. CHF carries neither
# usable selling value and must be skipped.
_TODAY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Tarih_Date Tarih="01.05.2026" Date="05/01/2026">
    <Currency CrossOrder="0" Kod="USD" CurrencyCode="USD">
        <Unit>1</Unit>
        <Isim>ABD DOLARI</Isim>
        <ForexBuying>32,1000</ForexBuying>
        <ForexSelling>32,2000</ForexSelling>
        <BanknoteBuying>32,0000</BanknoteBuying>
        <BanknoteSelling>32,3000</BanknoteSelling>
    </Currency>
    <Currency CrossOrder="6" Kod="JPY" CurrencyCode="JPY">
        <Unit>100</Unit>
        <Isim>JAPON YENI</Isim>
        <ForexBuying>21,4000</ForexBuying>
        <ForexSelling>21,5000</ForexSelling>
    </Currency>
    <Currency CrossOrder="1" Kod="EUR" CurrencyCode="EUR">
        <Unit>1</Unit>
        <Isim>EURO</Isim>
        <ForexBuying></ForexBuying>
        <ForexSelling></ForexSelling>
        <BanknoteBuying>34,8000</BanknoteBuying>
        <BanknoteSelling>35,0000</BanknoteSelling>
    </Currency>
    <Currency CrossOrder="3" Kod="CHF" CurrencyCode="CHF">
        <Unit>1</Unit>
        <Isim>ISVICRE FRANGI</Isim>
        <ForexBuying>36,0000</ForexBuying>
        <ForexSelling></ForexSelling>
    </Currency>
</Tarih_Date>
"""


@tagged('eh_account_fx_revaluation', 'unit')
class TestTcmbProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tcmb = mod.TcmbRateProvider(timeout=1)

    def _stub_download(self, payload):
        """Return a download stub serving `payload` bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_and_magnitude(self):
        # native[CODE] is "1 TRY = X CODE". USD Unit 1, selling 32,2000
        # means 1 USD = 32.2 TRY, so 1 TRY = 1 / 32.2 USD.
        self.tcmb._download = self._stub_download(_TODAY_XML)
        native = self.tcmb._fetch_native('TRY', ['USD', 'JPY'], None)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('32.2000'))
        # JPY Unit 100, selling 21,5000: 100 JPY = 21.5 TRY, so
        # 1 TRY = 100 / 21.5 JPY.
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('21.5000'))

    def test_cross_for_usd_base(self):
        # Company in USD asks for TRY and JPY.
        self.tcmb._download = self._stub_download(_TODAY_XML)
        rates = self.tcmb.fetch('USD', ['TRY', 'JPY'], None)
        # USD -> TRY is the native base leg: 1 USD = 32.2 TRY exactly.
        self.assertAlmostEqual(float(rates['TRY']), 32.2000, places=8)
        # USD -> JPY = native[JPY] / native[USD]
        #            = (100 / 21.5) / (1 / 32.2).
        expected_jpy = (Decimal('100') / Decimal('21.5000')) / (
            Decimal(1) / Decimal('32.2000')
        )
        self.assertAlmostEqual(
            float(rates['JPY']), float(expected_jpy), places=8,
        )

    def test_unknown_quote_omitted(self):
        # The feed carries no ZAR row, so it must not appear in output.
        self.tcmb._download = self._stub_download(_TODAY_XML)
        rates = self.tcmb.fetch('USD', ['TRY', 'ZAR'], None)
        self.assertIn('TRY', rates)
        self.assertNotIn('ZAR', rates)

    def test_banknote_fallback_and_blank_skip(self):
        # EUR has no forex selling but does carry banknote selling, so
        # it resolves via the fallback. CHF has neither usable selling
        # value and is skipped entirely.
        self.tcmb._download = self._stub_download(_TODAY_XML)
        native = self.tcmb._fetch_native('TRY', ['EUR', 'CHF'], None)
        # EUR Unit 1, banknote selling 35,0000: 1 EUR = 35.0 TRY, so
        # 1 TRY = 1 / 35.0 EUR.
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('35.0000'))
        self.assertNotIn('CHF', native)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron does
        # not blow up on a corrupt feed.
        self.tcmb._download = self._stub_download(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.tcmb.fetch('USD', ['TRY'], None)

    def test_historical_weekend_walks_archive_and_reports_effective_date(self):
        requested = datetime.date(2026, 5, 3)  # Sunday
        requested_urls = []

        def download(url, headers=None):
            requested_urls.append(url)
            if url.endswith('/01052026.xml'):
                return _TODAY_XML
            raise rp.RateProviderError('TCMB bulletin not published')

        self.tcmb._download = download
        rates = self.tcmb.fetch('USD', ['TRY'], requested)

        self.assertEqual(rates.effective_date, datetime.date(2026, 5, 1))
        self.assertEqual(requested_urls, [
            'https://www.tcmb.gov.tr/kurlar/202605/03052026.xml',
            'https://www.tcmb.gov.tr/kurlar/202605/02052026.xml',
            'https://www.tcmb.gov.tr/kurlar/202605/01052026.xml',
        ])
