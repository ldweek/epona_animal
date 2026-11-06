# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Currencylayer FX rate provider.

The provider is native base USD and needs an operator API key. The feed
publishes "1 USD = value CODE" under keys prefixed with the source code,
e.g. "USDEUR", so the parser strips the source prefix and keeps the value
in its native-base direction. These tests stub ``_download`` with canned
JSON bytes (no network, no database) and assert:

* the native table parses to the right Decimal in the correct direction,
  "1 USD = X CODE", with the source prefix stripped;
* cross derivation for a company whose currency is not USD yields the
  expected Decimal, computed by hand below;
* a quote the feed does not carry is omitted from the output;
* a non-success response raises RateProviderError carrying the upstream
  info string;
* a fetch with no API key raises RateProviderError from the key guard;
* malformed payload bytes raise RateProviderError.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import currencylayer as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Success payload, USD source. Values are "1 USD = X code", direct.
# USDEUR 0.9176, USDGBP 0.7980. No AUD row so it can prove omission.
_LIVE_JSON = (
    b'{"success": true, "timestamp": 1777593600, "source": "USD", '
    b'"quotes": {"USDEUR": 0.9176, "USDGBP": 0.7980}}'
)

# A rejected request: free tier code 105 / invalid access key, etc. The
# provider surfaces the info string rather than an empty table.
_ERROR_JSON = (
    b'{"success": false, "error": '
    b'{"code": 101, "info": "You have not supplied a valid API Access Key."}}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestCurrencylayerProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.CurrencylayerRateProvider(
            timeout=1, api_key='TESTKEY',
        )
        self.provider._download = self._stub_download(_LIVE_JSON)
        self.on_date = datetime.date(2026, 5, 1)

    @staticmethod
    def _stub_download(payload):
        """Return a download stub serving the same bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_usd_per_code(self):
        # _fetch_native must publish "1 USD = X CODE", the value as served,
        # with the "USD" source prefix stripped off each quote key.
        native = self.provider._fetch_native(
            'USD', ['EUR', 'GBP'], self.on_date,
        )
        self.assertEqual(native['EUR'], Decimal('0.9176'))
        self.assertEqual(native['GBP'], Decimal('0.7980'))

    def test_cross_derivation_for_non_usd_base(self):
        # Company in EUR asks for USD and GBP. The base class pivots
        # through USD:
        #   EUR -> USD = 1 / native[EUR] = 1 / 0.9176
        #   EUR -> GBP = native[GBP] / native[EUR] = 0.7980 / 0.9176
        rates = self.provider.fetch('EUR', ['USD', 'GBP'], self.on_date)
        self.assertEqual(rates.effective_date, self.on_date)
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal('1') / Decimal('0.9176')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal('0.7980') / Decimal('0.9176')),
            places=8,
        )

    def test_usd_base_direct(self):
        # Company in USD: out[CODE] is the native value as-is, "1 USD = X".
        rates = self.provider.fetch('USD', ['EUR', 'GBP'], self.on_date)
        self.assertEqual(rates['EUR'], Decimal('0.9176'))
        self.assertEqual(rates['GBP'], Decimal('0.7980'))

    def test_missing_quote_omitted(self):
        # AUD is not in the fixture; it must be absent from the output
        # rather than defaulted, so the caller surfaces the gap.
        rates = self.provider.fetch('USD', ['EUR', 'AUD'], self.on_date)
        self.assertIn('EUR', rates)
        self.assertNotIn('AUD', rates)

    def test_unsuccessful_response_raises(self):
        # success=false is a hard failure; the provider raises with the
        # upstream info string rather than returning an empty table.
        self.provider._download = self._stub_download(_ERROR_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)

    def test_missing_key_raises(self):
        # Constructed with no key, the base-class guard rejects the fetch
        # before any network call so the operator sees a clear message.
        keyless = mod.CurrencylayerRateProvider(timeout=1, api_key=None)
        keyless._download = self._stub_download(_LIVE_JSON)
        with self.assertRaises(rp.RateProviderError):
            keyless.fetch('USD', ['EUR'], self.on_date)

    def test_malformed_payload_rejected(self):
        # Garbage bytes are wrapped into RateProviderError by the JSON
        # download helper rather than escaping as a raw ValueError.
        self.provider._download = self._stub_download(b'{ this is not json')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)
