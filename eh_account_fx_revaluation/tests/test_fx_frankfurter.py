# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Frankfurter FX rate source.

Frankfurter is a multi-base source: the API serves rates that already
mean "1 base = value quote", so the provider returns them directly with
no cross-derivation. These tests stub ``_download`` to serve a canned
JSON payload and exercise the parser hermetically with no live call.

Coverage:

* A requested quote present in the payload parses to the right Decimal,
  in the correct direction (the value is taken verbatim from 'rates').
* A company whose currency is not the API default (EUR, not USD) still
  gets correct numbers because the requested base travels in the URL and
  the published rates already mean "1 base = value quote"; we assert the
  returned Decimals equal the payload values verbatim.
* A quote the feed does not carry is omitted from the output.
* Malformed payload bytes raise RateProviderError.

These tests never touch the database: the provider has no ORM
dependency, so the suite only stubs the single network seam
(``_download``) to serve canned bytes.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import (
    frankfurter as mod,
)
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Payload for base USD: rates already mean "1 USD = value CODE".
_USD_JSON = (
    b'{"amount":1.0,"base":"USD","date":"2026-05-01",'
    b'"rates":{"EUR":0.9176,"GBP":0.7980}}'
)

# Payload for base EUR: a non-default base. Rates already mean
# "1 EUR = value CODE", carried straight from the API response.
_EUR_JSON = (
    b'{"amount":1.0,"base":"EUR","date":"2026-05-01",'
    b'"rates":{"USD":1.0898,"GBP":0.8697}}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestFrankfurterProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.FrankfurterRateProvider(timeout=1)

    def _stub(self, payload):
        """Serve fixed bytes for any URL the provider builds."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_quotes_parse_in_correct_direction(self):
        # Base USD: the payload's rates already express '1 USD = X CODE',
        # so each returned Decimal must equal the raw payload value.
        self.provider._download = self._stub(_USD_JSON)
        rates = self.provider.fetch(
            'USD', ['EUR', 'GBP'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(rates['EUR'], Decimal('0.9176'))
        self.assertEqual(rates['GBP'], Decimal('0.7980'))

    def test_non_default_base_returns_payload_values(self):
        # A company whose currency is EUR, not the API's default, asks for
        # USD and GBP. The base travels in the URL, so the published rates
        # already mean '1 EUR = X CODE' and come back verbatim. No
        # cross-derivation happens; assert exact Decimal equality.
        self.provider._download = self._stub(_EUR_JSON)
        rates = self.provider.fetch(
            'EUR', ['USD', 'GBP'], datetime.date(2026, 5, 1),
        )
        self.assertEqual(rates['USD'], Decimal('1.0898'))
        self.assertEqual(rates['GBP'], Decimal('0.8697'))

    def test_unknown_quote_omitted(self):
        # XYZ is not carried by the feed, so it must be absent from the
        # output rather than appearing with a None or zero value.
        self.provider._download = self._stub(_USD_JSON)
        rates = self.provider.fetch(
            'USD', ['EUR', 'GBP', 'XYZ'], datetime.date(2026, 5, 1),
        )
        self.assertIn('EUR', rates)
        self.assertIn('GBP', rates)
        self.assertNotIn('XYZ', rates)

    def test_base_excluded_from_quotes(self):
        # The base never quotes against itself; requesting it is a no-op.
        self.provider._download = self._stub(_USD_JSON)
        rates = self.provider.fetch(
            'USD', ['USD', 'EUR'], datetime.date(2026, 5, 1),
        )
        self.assertNotIn('USD', rates)
        self.assertEqual(rates['EUR'], Decimal('0.9176'))

    def test_missing_rates_key_raises(self):
        # An unsupported base yields an error object with no 'rates' key;
        # the provider must surface it as a feed failure.
        self.provider._download = self._stub(
            b'{"message":"not found: base ZZZ"}'
        )
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch(
                'ZZZ', ['EUR'], datetime.date(2026, 5, 1),
            )

    def test_malformed_json_rejected(self):
        # Garbage bytes are wrapped into RateProviderError so the cron
        # does not blow up on a transient bad response.
        self.provider._download = self._stub(b'<not valid json')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch(
                'USD', ['EUR'], datetime.date(2026, 5, 1),
            )
