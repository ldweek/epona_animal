# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Central Bank of Brazil PTAX provider.

The provider is exercised purely at the parser level: ``_download`` is
replaced with a stub that maps each Olinda URL to a canned JSON payload,
so no network call is made and no database record is touched.

Coverage:

* The published selling quote (cotacaoVenda) inverts to the correct
  native-base Decimal, i.e. "1 BRL in units of code".
* Cross-derivation for a company whose currency is the dollar (not the
  native real) returns the expected Decimal for both the real and the
  euro.
* A base the feed cannot reach raises RateProviderError via cross_derive.
* Malformed payload bytes raise RateProviderError.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bcb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# PTAX OData rows. cotacaoVenda is "1 unit of the foreign currency in
# reais": one USD costs 5.0200 BRL, one EUR costs 6.1000 BRL.
_USD_JSON = (
    b'{"value":[{"cotacaoCompra":5.0100,"cotacaoVenda":5.0200,'
    b'"dataHoraCotacao":"2026-05-01 13:08:02.123"}]}'
)
_EUR_JSON = (
    b'{"value":[{"cotacaoCompra":6.0900,"cotacaoVenda":6.1000,'
    b'"dataHoraCotacao":"2026-05-01 13:08:02.123"}]}'
)
# A non-business day: the OData array comes back empty.
_EMPTY_JSON = b'{"value":[]}'


@tagged('eh_account_fx_revaluation', 'unit')
class TestBcbProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.bcb = mod.BcbRateProvider(timeout=1)
        self.on_date = datetime.date(2026, 5, 1)

    def _stub_download(self, dollar_bytes, currency_bytes):
        """Serve dollar vs generic-currency payloads by URL substring.

        The dollar resource is CotacaoDolarDia; the euro (and every other
        non-dollar quote) goes through CotacaoMoedaDia.
        """
        def fake(url, headers=None):
            if 'CotacaoDolarDia' in url:
                return dollar_bytes
            if 'CotacaoMoedaDia' in url:
                return currency_bytes
            raise AssertionError("unexpected URL: %s" % url)
        return fake

    def test_native_rates_parse_and_direction(self):
        # The native table is "1 BRL in units of code", the inverse of the
        # published selling quote. 1 / 5.0200 USD and 1 / 6.1000 EUR.
        self.bcb._download = self._stub_download(_USD_JSON, _EUR_JSON)
        native = self.bcb._fetch_native('BRL', ['USD', 'EUR'], self.on_date)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('5.0200'))
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('6.1000'))

    def test_native_base_brl_company(self):
        # A company that already keeps books in BRL gets the quotes back as
        # the plain native table (1 BRL in units of code).
        self.bcb._download = self._stub_download(_USD_JSON, _EUR_JSON)
        rates = self.bcb.fetch('BRL', ['USD', 'EUR'], self.on_date)
        self.assertEqual(rates['USD'], Decimal(1) / Decimal('5.0200'))
        self.assertEqual(rates['EUR'], Decimal(1) / Decimal('6.1000'))

    def test_cross_derivation_for_usd_company(self):
        # Company in USD asks for BRL and EUR. Native table:
        #   USD = 1 / 5.0200, EUR = 1 / 6.1000 (1 BRL in units of code).
        # cross_derive pivots through BRL with base_rate = native['USD']:
        #   USD->BRL = 1 / base_rate            = 5.0200
        #   USD->EUR = native['EUR'] / base_rate = (1/6.10) / (1/5.02)
        self.bcb._download = self._stub_download(_USD_JSON, _EUR_JSON)
        rates = self.bcb.fetch('USD', ['BRL', 'EUR'], self.on_date)

        self.assertAlmostEqual(float(rates['BRL']), 5.0200, places=8)
        expected_eur = (Decimal(1) / Decimal('6.1000')) / (
            Decimal(1) / Decimal('5.0200')
        )
        self.assertAlmostEqual(
            float(rates['EUR']), float(expected_eur), places=8,
        )

    def test_quote_not_carried_is_omitted(self):
        # The feed only serves USD and EUR. A GBP quote is not present in
        # the native table, so it never appears in the BRL-base output.
        self.bcb._download = self._stub_download(_USD_JSON, _EUR_JSON)
        rates = self.bcb.fetch('BRL', ['USD', 'EUR', 'GBP'], self.on_date)
        self.assertIn('USD', rates)
        self.assertIn('EUR', rates)
        self.assertNotIn('GBP', rates)

    def test_non_business_day_skips_currency(self):
        # An empty OData array (weekend or holiday) drops just that quote;
        # the other currency still resolves.
        self.bcb._download = self._stub_download(_EMPTY_JSON, _EUR_JSON)
        native = self.bcb._fetch_native('BRL', ['USD', 'EUR'], self.on_date)
        self.assertNotIn('USD', native)
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('6.1000'))

    def test_both_empty_yields_empty_native(self):
        # When neither quote resolves the native table is empty; for a BRL
        # base that simply yields an empty result.
        self.bcb._download = self._stub_download(_EMPTY_JSON, _EMPTY_JSON)
        rates = self.bcb.fetch('BRL', ['USD', 'EUR'], self.on_date)
        self.assertEqual(rates, {})

    def test_unreachable_base_raises(self):
        # A company in AUD: the native table carries no AUD entry, so
        # cross_derive cannot pivot and raises.
        self.bcb._download = self._stub_download(_USD_JSON, _EUR_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.bcb.fetch('AUD', ['USD', 'EUR'], self.on_date)

    def test_malformed_payload_raises(self):
        # Garbage bytes from either endpoint fail the shared JSON decoder
        # and surface as RateProviderError.
        def fake(url, headers=None):
            return b'{not-valid-json'
        self.bcb._download = fake
        with self.assertRaises(rp.RateProviderError):
            self.bcb.fetch('BRL', ['USD', 'EUR'], self.on_date)
