# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the HMRC monthly FX rate source.

HMRC publishes one rate per currency per calendar month as
"1 GBP = rateNew CODE", which is already the GBP-native direction the
base table needs, so the provider stores each value without inverting.
These tests stub the network seam with a canned monthly XML payload so
the parser, the native direction, and the cross-derivation onto a
non-GBP company currency are exercised with no live call.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import hmrc as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Canonical monthly file shape: one <exchangeRate> per quoted currency,
# rateNew = "1 GBP = rateNew CODE".
_MONTHLY_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<exchangeRateMonthList period="2026-05-01 to 2026-05-31">
  <exchangeRate>
    <countryName>USA</countryName>
    <countryCode>US</countryCode>
    <currencyCode>USD</currencyCode>
    <currencyName>Dollar</currencyName>
    <rateNew>1.2700</rateNew>
  </exchangeRate>
  <exchangeRate>
    <countryName>Eurozone</countryName>
    <countryCode>EU</countryCode>
    <currencyCode>EUR</currencyCode>
    <currencyName>Euro</currencyName>
    <rateNew>1.1700</rateNew>
  </exchangeRate>
</exchangeRateMonthList>
"""

_ON_DATE = datetime.date(2026, 5, 15)
_URL = mod._monthly_url(_ON_DATE)


@tagged('eh_account_fx_revaluation', 'unit')
class TestHmrcProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.hmrc = mod.HmrcRateProvider(timeout=1)
        self.hmrc._download = self._stub({_URL: _MONTHLY_XML})

    @staticmethod
    def _stub(payload_map):
        """Return a download stub serving the given URL->bytes map."""
        def fake(url, headers=None):
            return payload_map[url]
        return fake

    def test_url_is_keyed_by_year_month(self):
        # The monthly file is selected by the year-month of on_date.
        self.assertTrue(_URL.endswith('monthly_xml_2026-05.xml'))

    def test_native_table_keeps_pound_direct_direction(self):
        # Native base is GBP and rateNew is already "1 GBP = rateNew CODE",
        # so the native table stores each value with no inversion.
        native = self.hmrc._fetch_native('GBP', ['USD', 'EUR'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal('1.2700'))
        self.assertEqual(native['EUR'], Decimal('1.1700'))

    def test_cross_derivation_for_usd_base(self):
        # Company in USD asks for GBP and EUR.
        #   out[GBP] = 1 / native[USD] = 1 / 1.2700.
        #   out[EUR] = native[EUR] / native[USD] = 1.1700 / 1.2700.
        rates = self.hmrc.fetch('USD', ['GBP', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal(1) / Decimal('1.2700')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('1.1700') / Decimal('1.2700')),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # AUD is absent from the fixture; a USD company asking for it gets
        # no AUD key rather than a guessed rate.
        rates = self.hmrc.fetch('USD', ['GBP', 'AUD'], _ON_DATE)
        self.assertIn('GBP', rates)
        self.assertNotIn('AUD', rates)

    def test_malformed_payload_rejected(self):
        # Non-XML bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.hmrc._download = self._stub({_URL: b'<not valid xml'})
        with self.assertRaises(rp.RateProviderError):
            self.hmrc.fetch('USD', ['GBP'], _ON_DATE)
