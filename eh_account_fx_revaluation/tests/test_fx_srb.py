# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Sveriges Riksbank FX rate source.

The provider reads the SWEA "latest by group 130" observations, where
each series 'SEK<CODE>PMI' carries the kronor per one unit of CODE, and
inverts each row into an SEK-native table that the base class then
cross-derives onto the company currency. These tests stub the network
seam with a canned group payload so the parser, the pattern filter, the
inversion direction, and the cross-derivation are exercised with no live
call.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import srb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Canonical group 130 shape: a flat list of latest observations. value =
# "1 CODE = value SEK". The third entry omits the date to confirm the
# parser does not depend on it. A KIX series (length 9, code 'KIX') is
# included to show the pattern filter admits any nine-char 'SEK...PMI'
# id, and a deliberately misshapen id is included to show it is skipped.
_GROUP_JSON = b"""
[
  {"seriesId": "SEKUSDPMI", "date": "2026-06-22", "value": 9.60021},
  {"seriesId": "SEKEURPMI", "date": "2026-06-22", "value": 10.998},
  {"seriesId": "SEKGBPPMI", "value": 12.85},
  {"seriesId": "SEKKIXPMI", "date": "2026-06-22", "value": 122.5},
  {"seriesId": "SEKUSD", "date": "2026-06-22", "value": 1.23}
]
"""

_ON_DATE = datetime.date(2026, 6, 22)


@tagged('eh_account_fx_revaluation', 'unit')
class TestSrbProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.srb = mod.SrbRateProvider(timeout=1)
        self.srb._download = self._stub({mod._GROUP_130_URL: _GROUP_JSON})

    @staticmethod
    def _stub(payload_map):
        """Return a download stub serving the given URL->bytes map."""
        def fake(url, headers=None):
            return payload_map[url]
        return fake

    def test_native_table_inverts_value_direction(self):
        # Native base is SEK; the feed publishes "1 USD = 9.60021 SEK",
        # so the native table must read "1 SEK = 1/9.60021 USD".
        native = self.srb._fetch_native('SEK', ['USD', 'EUR'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('9.60021'))
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('10.998'))

    def test_direction_usd_to_native_sek(self):
        # Company in USD asks for SEK: out[SEK] = 1 / native[USD]
        # = value(USD) = 9.60021. This is the direct "1 USD in SEK"
        # direction the contract promises.
        rates = self.srb.fetch('USD', ['SEK', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['SEK']), float(Decimal('9.60021')), places=8,
        )

    def test_cross_derivation_for_usd_base(self):
        # USD company asking for EUR exercises a non-native-base cross:
        #   out[EUR] = native[EUR] / native[USD]
        #            = (1/10.998) / (1/9.60021)
        #            = 9.60021 / 10.998.
        rates = self.srb.fetch('USD', ['SEK', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('9.60021') / Decimal('10.998')),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # JPY is absent from the fixture; a USD company asking for it
        # gets no JPY key rather than a guessed rate.
        rates = self.srb.fetch('USD', ['SEK', 'JPY'], _ON_DATE)
        self.assertIn('SEK', rates)
        self.assertNotIn('JPY', rates)

    def test_misshapen_series_id_is_skipped(self):
        # 'SEKUSD' (length 6) does not match the nine-char pattern and is
        # dropped, so it cannot clobber the real 'SEKUSDPMI' observation.
        native = self.srb._fetch_native('SEK', ['USD'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('9.60021'))

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.srb._download = self._stub({mod._GROUP_130_URL: b'<not json'})
        with self.assertRaises(rp.RateProviderError):
            self.srb.fetch('USD', ['SEK'], _ON_DATE)

    def test_unexpected_shape_rejected(self):
        # Well-formed JSON but the wrong shape (an object, not a list) is
        # also surfaced as RateProviderError.
        self.srb._download = self._stub({mod._GROUP_130_URL: b'{"x": 1}'})
        with self.assertRaises(rp.RateProviderError):
            self.srb.fetch('USD', ['SEK'], _ON_DATE)
