# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Open ExchangeRate API rate provider.

The provider is a multi-base aggregator: it asks the endpoint for the
company base directly and passes the returned "1 base in units of code"
values straight through, so there is no cross-derivation step. The tests
stub ``_download`` with canned JSON bytes and assert:

* a quoted rate parses to the right Decimal in the correct direction;
* a base currency that differs from USD still resolves directly because
  the API pivots server-side (here we feed an AUD-base payload);
* a quote the feed does not carry is omitted from the result;
* the base currency itself is never echoed back as a quote;
* a non-success result raises RateProviderError;
* malformed payload bytes raise RateProviderError.

No network, no database, no records: pure parser-level checks.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import erapi as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Success payload with USD as base. Values are "1 USD = X code", direct.
_USD_JSON = (
    b'{"result": "success", "base_code": "USD", '
    b'"time_last_update_unix": 1787529600, '
    b'"rates": {"USD": 1, "EUR": 0.9176, "GBP": 0.7980, "AUD": 1.5200}}'
)

# Same shape but pivoted to AUD base, as the live API would return when
# the base path segment is AUD. Used to prove the multi-base path needs
# no cross-derivation.
_AUD_JSON = (
    b'{"result": "success", "base_code": "AUD", '
    b'"time_last_update_unix": 1787529600, '
    b'"rates": {"AUD": 1, "USD": 0.6579, "EUR": 0.6037}}'
)

_ERROR_JSON = (
    b'{"result": "error", "error-type": "unsupported-code"}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestErApiProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.ErApiRateProvider(timeout=1)
        # Match the immutable fixing timestamp carried by the canned payload.
        self.on_date = datetime.date(2026, 8, 24)

    def _stub_download_json(self, payload):
        """Replace the network seam with a constant byte payload."""
        def fake(url, headers=None):
            return payload
        self.provider._download = fake

    def test_direct_rates_parse_and_omit_unknown(self):
        # Base USD, ask for EUR/AUD plus a currency the feed lacks. The
        # carried quotes parse to their direct Decimal values; the unknown
        # quote is dropped rather than guessed.
        self._stub_download_json(_USD_JSON)
        rates = self.provider.fetch('USD', ['EUR', 'AUD', 'XYZ'], self.on_date)
        self.assertEqual(rates.effective_date, self.on_date)
        # Direct values: "1 USD = 0.9176 EUR", "1 USD = 1.5200 AUD".
        self.assertEqual(rates['EUR'], Decimal('0.9176'))
        self.assertEqual(rates['AUD'], Decimal('1.5200'))
        self.assertNotIn('XYZ', rates)

    def test_base_currency_not_echoed_as_quote(self):
        # USD is in the payload as 1.0, but the company base must never
        # appear in its own quote table.
        self._stub_download_json(_USD_JSON)
        rates = self.provider.fetch('USD', ['USD', 'EUR'], self.on_date)
        self.assertNotIn('USD', rates)
        self.assertEqual(rates['EUR'], Decimal('0.9176'))

    def test_non_usd_base_resolves_directly(self):
        # A company in AUD asks for USD and EUR. The aggregator pivots
        # server-side, so the AUD-base payload's values pass straight
        # through with no cross-derivation. 1 AUD = 0.6579 USD directly.
        self._stub_download_json(_AUD_JSON)
        rates = self.provider.fetch('AUD', ['USD', 'EUR'], self.on_date)
        self.assertEqual(rates['USD'], Decimal('0.6579'))
        self.assertEqual(rates['EUR'], Decimal('0.6037'))
        # Sanity-check the direction: 1 AUD buys less than 1 USD, so the
        # AUD->USD figure is below 1.
        self.assertAlmostEqual(float(rates['USD']), 0.6579, places=8)

    def test_error_result_raises(self):
        # A result other than "success" is a hard failure, not an empty
        # table, so the cron surfaces the outage.
        self._stub_download_json(_ERROR_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)

    def test_malformed_payload_raises(self):
        # Garbage bytes must be wrapped into RateProviderError by the JSON
        # download helper rather than escaping as a raw ValueError.
        self._stub_download_json(b'{ this is not json')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['EUR'], self.on_date)
