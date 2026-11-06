# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Bank of Mexico (Banxico) rate provider.

The provider is a keyed single-pair feed: it carries the MXN/USD FIX,
quotes against USD, and lets the base class re-express the table against
the company currency. The token travels in the ``Bmx-Token`` HTTP header.
The tests stub _download with canned JSON bytes and assert:

* the native USD->MXN value parses to the right Decimal in the correct
  direction ("1 USD in units of MXN", direct, no inversion);
* the ``Bmx-Token`` header carrying the api_key is sent on the request;
* cross-derivation for a company whose currency is not USD returns the
  expected Decimal (MXN base asking for USD gives 1 / FIX);
* a quote the feed does not carry is omitted from the result;
* an error envelope (the Banxico ``error``/``mensaje`` shape) raises
  RateProviderError;
* a missing api_key raises RateProviderError before any network call;
* malformed payload bytes raise RateProviderError.

No network, no database, no records: pure parser-level checks.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import banxico as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Success payload: the most recent FIX observation, MXN per 1 USD.
_OK_JSON = (
    b'{"bmx": {"series": [{"idSerie": "SF43718", '
    b'"datos": [{"fecha": "23/06/2026", "dato": "17.0925"}]}]}}'
)

# Error envelope as the live API returns for an invalid token. The
# ``mensaje`` is the human reason we relay through RateProviderError.
_ERROR_JSON = (
    b'{"error": {"mensaje": "Token invalido", "detail": "...", '
    b'"errorcode": "..."}}'
)

# Empty-series payload: no observations to read, so an empty table.
_EMPTY_JSON = (
    b'{"bmx": {"series": [{"idSerie": "SF43718", "datos": []}]}}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestBanxicoProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.BanxicoRateProvider(timeout=1, api_key='TESTKEY')
        self.on_date = datetime.date.today()
        self._seen_headers = []

    def _stub_download(self, payload):
        """Replace the network seam with a constant byte payload.

        Records the headers passed on each call so a test can assert the
        ``Bmx-Token`` auth header was sent.
        """
        def fake(url, headers=None):
            self._seen_headers.append(headers)
            return payload
        self.provider._download = fake

    def test_native_rate_parses_direct_direction(self):
        # Base USD: the native table passes through untouched, so the value
        # is the direct "1 USD in units of MXN" the FIX publishes.
        self._stub_download(_OK_JSON)
        native = self.provider._fetch_native(
            'USD', ['MXN'], self.on_date,
        )
        self.assertEqual(native['MXN'], Decimal('17.0925'))

    def test_token_header_is_sent(self):
        # The free token must travel in the Bmx-Token request header, not
        # the URL, so the API authorises the call.
        self._stub_download(_OK_JSON)
        self.provider.fetch('USD', ['MXN'], self.on_date)
        self.assertTrue(self._seen_headers)
        self.assertEqual(self._seen_headers[0].get('Bmx-Token'), 'TESTKEY')

    def test_usd_base_passes_through_and_omits_unknown(self):
        # A USD company asks for MXN and a currency the feed lacks. The
        # carried quote parses to its direct value; the unknown quote is
        # dropped rather than guessed.
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('USD', ['MXN', 'XYZ'], self.on_date)
        self.assertEqual(rates['MXN'], Decimal('17.0925'))
        self.assertNotIn('XYZ', rates)

    def test_cross_derivation_for_non_usd_base(self):
        # Company in MXN asks for USD. The base class pivots the USD-native
        # table through MXN:
        #   MXN->USD = 1 / native[MXN] = 1 / 17.0925
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('MXN', ['USD'], self.on_date)
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal('1') / Decimal('17.0925')),
            places=10,
        )

    def test_quote_omitted_when_absent(self):
        # An MXN company asks for a quote the single-pair table cannot
        # reach. The cross derivation drops it rather than guessing.
        self._stub_download(_OK_JSON)
        rates = self.provider.fetch('MXN', ['USD', 'EUR'], self.on_date)
        self.assertIn('USD', rates)
        self.assertNotIn('EUR', rates)

    def test_empty_series_yields_empty_table(self):
        # No observations: an empty table, not an error, so the caller
        # surfaces the gap.
        self._stub_download(_EMPTY_JSON)
        rates = self.provider.fetch('USD', ['MXN'], self.on_date)
        self.assertEqual(rates, {})

    def test_error_envelope_raises(self):
        # The Banxico error envelope is a hard failure, not an empty table,
        # so the cron surfaces the upstream reason.
        self._stub_download(_ERROR_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['MXN'], self.on_date)

    def test_missing_key_raises_before_network(self):
        # No token: the base class key guard raises before any download,
        # so the stub here is never reached.
        keyless = mod.BanxicoRateProvider(timeout=1, api_key=None)

        def boom(url, headers=None):
            raise AssertionError("network must not be hit without a key")
        keyless._download = boom

        with self.assertRaises(rp.RateProviderError):
            keyless.fetch('USD', ['MXN'], self.on_date)

    def test_malformed_payload_raises(self):
        # Garbage bytes must be wrapped into RateProviderError by the JSON
        # download helper rather than escaping as a raw ValueError.
        self._stub_download(b'{ this is not json')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('USD', ['MXN'], self.on_date)
