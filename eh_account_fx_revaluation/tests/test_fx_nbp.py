# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the National Bank of Poland FX rate source.

The provider reads table A (zloty per foreign unit) and inverts each
quote into a PLN-native table, which the base class cross-derives onto
the company currency. These tests stub the network seam with a canned
table A payload so the parser, the inversion direction, and the
cross-derivation are exercised with no live call.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import nbp as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Canonical table A shape: a single-element list whose one table object
# carries the dated mid rates. mid = "1 code = mid PLN".
_TABLE_A_JSON = b"""
[
  {
    "table": "A",
    "no": "084/A/NBP/2026",
    "effectiveDate": "2026-05-01",
    "rates": [
      {"currency": "dolar amerykanski", "code": "USD", "mid": 4.0512},
      {"currency": "euro", "code": "EUR", "mid": 4.3210}
    ]
  }
]
"""

_ON_DATE = datetime.date(2026, 5, 1)


@tagged('eh_account_fx_revaluation', 'unit')
class TestNbpProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.nbp = mod.NbpRateProvider(timeout=1)
        self.nbp._download = self._stub({mod._TABLE_A_URL: _TABLE_A_JSON})

    @staticmethod
    def _stub(payload_map):
        """Return a download stub serving the given URL->bytes map."""
        def fake(url, headers=None):
            return payload_map[url]
        return fake

    def test_native_table_inverts_mid_direction(self):
        # Native base is PLN; the feed publishes "1 USD = 4.0512 PLN",
        # so the native table must read "1 PLN = 1/4.0512 USD".
        native = self.nbp._fetch_native('PLN', ['USD', 'EUR'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('4.0512'))
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('4.3210'))

    def test_cross_derivation_for_usd_base(self):
        # Company in USD asks for PLN and EUR.
        #   out[PLN] = 1 / native[USD] = mid(USD) = 4.0512.
        #   out[EUR] = native[EUR] / native[USD]
        #            = (1/4.3210) / (1/4.0512) = 4.0512 / 4.3210.
        rates = self.nbp.fetch('USD', ['PLN', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['PLN']), float(Decimal('4.0512')), places=8,
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('4.0512') / Decimal('4.3210')),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # GBP is absent from the fixture; a USD company asking for it
        # gets no GBP key rather than a guessed rate.
        rates = self.nbp.fetch('USD', ['PLN', 'GBP'], _ON_DATE)
        self.assertIn('PLN', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.nbp._download = self._stub({mod._TABLE_A_URL: b'<not json'})
        with self.assertRaises(rp.RateProviderError):
            self.nbp.fetch('USD', ['PLN'], _ON_DATE)

    def test_unexpected_shape_rejected(self):
        # Well-formed JSON but the wrong shape (no rates array) is also
        # surfaced as RateProviderError.
        self.nbp._download = self._stub({mod._TABLE_A_URL: b'[{"table": "A"}]'})
        with self.assertRaises(rp.RateProviderError):
            self.nbp.fetch('USD', ['PLN'], _ON_DATE)
