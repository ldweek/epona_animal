# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Open Exchange Rates rate provider.

The provider is a keyed single-base feed: it quotes against USD and lets
the base class re-express the table against the company currency. The
tests stub _download with canned JSON bytes and assert:

* the native USD table parses to the right Decimal in the correct
  direction ("1 USD in units of code", direct, no inversion);
* cross-derivation for a company whose currency is not USD returns the
  expected Decimal (EUR base asking for USD and GBP);
* a quote the feed does not carry is omitted from the result;
* an error response (the OXR error flag plus description) raises
  RateProviderError;
* a missing api_key raises RateProviderError before any network call;
* malformed payload bytes raise RateProviderError.

No network, no database, no records: pure parser-level checks.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import oxr as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Success payload with USD as base. Values are "1 USD = X code", direct.
_OK_JSON = (
    b'{"base": "USD", "timestamp": 1787529600, '
    b'"rates": {"EUR": 0.9176, "GBP": 0.7980, "AUD": 1.5200}}'
)

# Error payload as the live API returns for a bad app_id. The description
# is the human reason we relay through RateProviderError.
_ERROR_JSON = (
    b'{"error": true, "status": 401, "message": "invalid_app_id", '
    b'"description": "Invalid App ID provided."}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestOxrProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.OxrRateProvider(timeout=1, api_key='TESTKEY')
        # Match the immutable fixing timestamp carried by the canned payload.
        self.on_date = datetime.date(2026, 8, 24)

    def _stub_download(self, payload):
        """Replace the network seam with a constant byte payload."""
        def fake(url, headers=None):
            return payload
        self.provider._download = fake

    def test_native_rates_parse_direct_direction(self):
        # Base USD: the native table passes through untouched, so each
        # value is the direct "1 USD in units of code" the feed publishes.
        self._stub_download(_OK_JSON)
        native = self.provider._fetch_native(
            'USD', ['EUR', 'GBP'], self.on_date,
        )
        self.assertEqual(native['EUR'], Decimal('0.9176'))
        self.assertEqual(native['GBP'], Decimal('0.7980'))

    def test_usd_base_passes_through_and_omits_unknown(self):
        # A USD company asks for EUR and a currency the feed lacks. The
        # carried quote parses to its direct value; the unknown quote is
        # dropped rather than guessed.
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('USD', ['EUR', 'XYZ'], self.on_date)
        self.assertEqual(rates.effective_date, self.on_date)
        self.assertEqual(rates['EUR'], Decimal('0.9176'))
        self.assertNotIn('XYZ', rates)

    def test_cross_derivation_for_non_usd_base(self):
        # Company in EUR asks for USD and GBP. The base class pivots the
        # USD-native table through EUR:
        #   EUR->USD = 1 / native[EUR]            = 1 / 0.9176
        #   EUR->GBP = native[GBP] / native[EUR]  = 0.7980 / 0.9176
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('EUR', ['USD', 'GBP'], self.on_date)
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

    def test_cross_omits_quote_feed_lacks(self):
        # An EUR company asks for a quote the USD table does not carry.
        # The cross derivation cannot reach it, so it is left out.
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('EUR', ['USD', 'ZZZ'], self.on_date)
        self.assertIn('USD', rates)
        self.assertNotIn('ZZZ', rates)

    def test_error_response_raises(self):
        # The OXR error flag is a hard failure, not an empty table, so the
        # cron surfaces the upstream reason.
        self._stub_download(_ERROR_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)

    def test_missing_key_raises_before_network(self):
        # No app_id: the base class key guard raises before any download,
        # so the stub here is never reached.
        keyless = mod.OxrRateProvider(timeout=1, api_key=None)

        def boom(url, headers=None):
            raise AssertionError("network must not be hit without a key")
        keyless._download = boom

        with self.assertRaises(rp.RateProviderError):
            keyless.fetch('USD', ['EUR'], self.on_date)

    def test_malformed_payload_raises(self):
        # Garbage bytes must be wrapped into RateProviderError by the JSON
        # download helper rather than escaping as a raw ValueError.
        self._stub_download(b'{ this is not json')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)
