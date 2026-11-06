# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Central Reserve Bank of Peru (BCRP) provider.

The provider is exercised purely at the parser level: ``_download`` is
replaced with a stub that serves canned BCRP statistical-series JSON
bytes, so no network call is made and no database record is touched.

Coverage:

* The series is walked newest-first; a non-business-day "n.d." sentinel
  at the newest period is skipped and the next real value ("1 USD in
  soles") parses to the right Decimal for a USD-base company, with no
  inversion (native base USD).
* Cross-derivation for a company whose currency is the sol (not the
  native dollar) returns the inverse, "1 PEN in units of USD".
* A quote the single-pair feed does not carry is omitted from the output.
* A base the single-pair feed cannot reach raises RateProviderError via
  cross_derive.
* An all-"n.d." series yields an empty table for the native USD base.
* Malformed payload bytes raise RateProviderError.

Network is stubbed by replacing ``_download`` so the parser runs with no
live call. No database records are created.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bcrp as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# BCRP series PD04640PD (PEN per 1 USD, sell). The ``periods`` array runs
# oldest first; the newest period (last) carries the "n.d." non-business-day
# sentinel, so the provider must walk back to the next usable value 3.388.
_SERIES_JSON = (
    b'{"config":{"title":"Tipo de cambio interbancario venta"},'
    b'"periods":['
    b'{"name":"17.Jun.26","values":["3.390"]},'
    b'{"name":"18.Jun.26","values":["3.388"]},'
    b'{"name":"23.Jun.26","values":["n.d."]}'
    b']}'
)

# Every published period carries the "n.d." sentinel: no usable rate.
_ALL_ND_JSON = (
    b'{"config":{"title":"Tipo de cambio interbancario venta"},'
    b'"periods":['
    b'{"name":"22.Jun.26","values":["n.d."]},'
    b'{"name":"23.Jun.26","values":["n.d."]}'
    b']}'
)


@tagged('eh_account_fx_revaluation', 'unit')
class TestBcrpProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.bcrp = mod.BcrpRateProvider(timeout=1)
        self.on_date = datetime.date(2026, 6, 23)

    def _stub(self, payload):
        """Return a download stub that serves the given bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_rate_direction_skips_nd(self):
        # native base USD: the newest period is "n.d." and must be skipped;
        # the next period's value 3.388 is "1 USD = 3.388 PEN", already the
        # native direction, so the parsed Decimal equals it with no inversion.
        self.bcrp._download = self._stub(_SERIES_JSON)
        native = self.bcrp._fetch_native('USD', ['PEN'], self.on_date)
        self.assertEqual(native['PEN'], Decimal('3.388'))

    def test_usd_base_passthrough(self):
        # Company base USD, quote PEN: cross_derive returns the native table
        # entry directly, "1 USD = 3.388 PEN".
        self.bcrp._download = self._stub(_SERIES_JSON)
        rates = self.bcrp.fetch('USD', ['PEN'], self.on_date)
        self.assertEqual(rates['PEN'], Decimal('3.388'))

    def test_cross_derivation_for_pen_company(self):
        # Company base PEN asks for USD. The native table reads
        # "1 USD = 3.388 PEN"; cross_derive pivots through USD:
        #   PEN->USD = 1 / native['PEN'] = 1 / 3.388.
        self.bcrp._download = self._stub(_SERIES_JSON)
        rates = self.bcrp.fetch('PEN', ['USD'], self.on_date)
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal(1) / Decimal('3.388')),
            places=12,
        )

    def test_quote_not_carried_is_omitted(self):
        # The feed only carries the sol. A EUR quote is not in the native
        # table, so for the USD base it never appears in the output.
        self.bcrp._download = self._stub(_SERIES_JSON)
        rates = self.bcrp.fetch('USD', ['PEN', 'EUR'], self.on_date)
        self.assertIn('PEN', rates)
        self.assertNotIn('EUR', rates)

    def test_unreachable_base_raises(self):
        # A company in EUR asking for PEN: the single-pair native table
        # carries no EUR entry, so cross_derive cannot pivot and raises.
        self.bcrp._download = self._stub(_SERIES_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.bcrp.fetch('EUR', ['PEN'], self.on_date)

    def test_all_nd_yields_empty_for_usd_base(self):
        # No period carries a usable value: the native table is empty, and
        # for the native USD base that simply yields an empty result.
        self.bcrp._download = self._stub(_ALL_ND_JSON)
        rates = self.bcrp.fetch('USD', ['PEN'], self.on_date)
        self.assertEqual(rates, {})

    def test_malformed_payload_raises(self):
        # Garbage bytes fail the shared JSON decoder and surface as
        # RateProviderError so the cron does not blow up.
        self.bcrp._download = self._stub(b'{not valid json')
        with self.assertRaises(rp.RateProviderError):
            self.bcrp.fetch('USD', ['PEN'], self.on_date)
