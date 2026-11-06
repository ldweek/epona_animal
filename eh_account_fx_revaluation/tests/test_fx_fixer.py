# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Fixer.io FX rate source.

Fixer publishes against a fixed EUR base, with each rate meaning
"1 EUR in units of the quoted currency", the direction the registry
expects. The tests feed the provider a canned success payload and check
that:

* the native EUR table parses to the right Decimal in the correct
  direction (no inversion);
* a company on a non-EUR base gets the correct cross-derived rates;
* a quote the feed does not carry is omitted from the output;
* a ``success: false`` payload raises RateProviderError carrying the
  upstream info string;
* the missing-key guard raises when the provider is built without a key;
* malformed payload bytes raise RateProviderError.

Network is stubbed by replacing ``_download`` so the parser runs with no
live call. No database records are created.
"""

import datetime
import urllib.parse
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import fixer as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Success payload: 1 EUR = 1.08 USD, 1 EUR = 0.85 GBP. Direct direction.
_SUCCESS_JSON = (
    b'{"success": true, "timestamp": 1746057600, "base": "EUR",'
    b' "date": "2026-05-01",'
    b' "rates": {"USD": 1.0800, "GBP": 0.8500}}'
)

# Feed-level failure: invalid access key (Fixer error code 101).
_FAILURE_JSON = (
    b'{"success": false,'
    b' "error": {"code": 101, "info": "You have not supplied a valid'
    b' API Access Key."}}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestFixerProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.fixer = mod.FixerRateProvider(timeout=1, api_key='TESTKEY')

    def _stub(self, payload):
        """Return a download stub that serves the given bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_rate_direction(self):
        # The EUR-base native table reads "1 EUR = value code" directly,
        # so the parsed Decimal must equal the upstream value with no
        # inversion applied.
        self.fixer._download = self._stub(_SUCCESS_JSON)
        native = self.fixer._fetch_native(
            'EUR', ['USD', 'GBP'], datetime.date.today(),
        )
        self.assertEqual(native['USD'], Decimal('1.0800'))
        self.assertEqual(native['GBP'], Decimal('0.8500'))

    def test_key_and_symbols_are_urlencoded_as_query_values(self):
        captured = []
        provider = mod.FixerRateProvider(
            timeout=1, api_key='key&symbols=INJECTED',
        )

        def download(url, headers=None):
            captured.append(url)
            return _SUCCESS_JSON

        provider._download = download
        provider.fetch('EUR', ['GBP', 'USD'], datetime.date(2026, 5, 1))
        self.assertEqual(len(captured), 1)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(captured[0]).query,
        )
        self.assertEqual(query['access_key'], ['key&symbols=INJECTED'])
        self.assertEqual(query['symbols'], ['EUR,GBP,USD'])
        self.assertIn('key%26symbols%3DINJECTED', captured[0])
        self.assertIn('EUR%2CGBP%2CUSD', captured[0])

    def test_eur_base_passthrough(self):
        # A company on EUR gets the rates straight off the table.
        self.fixer._download = self._stub(_SUCCESS_JSON)
        rates = self.fixer.fetch(
            'EUR', ['USD', 'GBP'], datetime.date.today(),
        )
        self.assertEqual(rates['USD'], Decimal('1.0800'))
        self.assertEqual(rates['GBP'], Decimal('0.8500'))

    def test_cross_derivation_for_non_eur_base(self):
        # Company in USD asks for EUR and GBP. Fixer serves
        # EUR/USD = 1.08 and EUR/GBP = 0.85.
        #   USD->EUR = 1 / (EUR/USD)           = 1 / 1.08
        #   USD->GBP = (EUR/GBP) / (EUR/USD)   = 0.85 / 1.08
        self.fixer._download = self._stub(_SUCCESS_JSON)
        rates = self.fixer.fetch(
            'USD', ['EUR', 'GBP'], datetime.date.today(),
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('1') / Decimal('1.0800')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal('0.8500') / Decimal('1.0800')),
            places=8,
        )

    def test_quote_not_in_feed_omitted(self):
        # AUD is not carried by the success payload, so it must be absent
        # from the cross-derived output rather than guessed.
        self.fixer._download = self._stub(_SUCCESS_JSON)
        rates = self.fixer.fetch(
            'USD', ['EUR', 'GBP', 'AUD'], datetime.date.today(),
        )
        self.assertIn('EUR', rates)
        self.assertIn('GBP', rates)
        self.assertNotIn('AUD', rates)

    def test_success_false_raises_with_info(self):
        # A success:false payload raises and carries the upstream info
        # string so the operator sees the real cause.
        self.fixer._download = self._stub(_FAILURE_JSON)
        with self.assertRaises(rp.RateProviderError) as ctx:
            self.fixer.fetch(
                'EUR', ['USD'], datetime.date.today(),
            )
        self.assertIn('valid', str(ctx.exception))

    def test_missing_key_raises(self):
        # Built without a key, fetch() must trip the base-class guard
        # before any download is attempted.
        keyless = mod.FixerRateProvider(timeout=1, api_key=None)
        with self.assertRaises(rp.RateProviderError):
            keyless.fetch('EUR', ['USD'], datetime.date.today())

    def test_malformed_bytes_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # does not blow up on a truncated or HTML error response.
        self.fixer._download = self._stub(b'<not valid json')
        with self.assertRaises(rp.RateProviderError):
            self.fixer.fetch(
                'EUR', ['USD'], datetime.date.today(),
            )
