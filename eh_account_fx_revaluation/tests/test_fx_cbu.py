# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Central Bank of Uzbekistan FX rate source.

The provider reads the daily archive (Rate UZS per Nominal foreign
units) and inverts each row into a UZS-native table, which the base
class cross-derives onto the company currency. These tests stub the
network seam with a canned archive payload so the parser, the inversion
direction, the per-row Nominal handling, and the cross-derivation are
exercised with no live call and no database writes.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import cbu as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Canonical archive shape: a flat array, one object per currency. Rate is
# "Nominal units of CODE = Rate UZS". JPY carries Nominal 100 to confirm
# the parser reads Nominal per row rather than assuming 1. EUR is present
# so a USD-base company can cross-derive USD->EUR through the UZS pivot.
_ARCHIVE_JSON = b"""
[
  {"Ccy": "USD", "Nominal": "1", "Rate": "11990.26", "Date": "23.06.2026"},
  {"Ccy": "JPY", "Nominal": "100", "Rate": "82.5", "Date": "23.06.2026"},
  {"Ccy": "EUR", "Nominal": "1", "Rate": "13000", "Date": "23.06.2026"}
]
"""

_ON_DATE = datetime.date(2026, 6, 23)


@tagged('eh_account_fx_revaluation', 'unit')
class TestCbuProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cbu = mod.CbuRateProvider(timeout=1)
        self.cbu._download = self._stub({mod._CBU_DAILY_URL: _ARCHIVE_JSON})

    @staticmethod
    def _stub(payload_map):
        """Return a download stub serving the given URL->bytes map."""
        def fake(url, headers=None):
            return payload_map[url]
        return fake

    def test_native_table_inverts_rate_direction(self):
        # Native base is UZS; the feed publishes "1 USD = 11990.26 UZS",
        # so the native table must read "1 UZS = 1/11990.26 USD". JPY is
        # quoted per 100 units (Nominal 100), so 100 JPY = 82.5 UZS and
        # the native entry is 100 / 82.5.
        native = self.cbu._fetch_native('UZS', ['USD', 'JPY'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal('1') / Decimal('11990.26'))
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('82.5'))

    def test_direction_usd_base_to_native(self):
        # USD->UZS = 1 / native[USD] = Rate(USD) = 11990.26.
        rates = self.cbu.fetch('USD', ['UZS'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['UZS']), float(Decimal('11990.26')), places=8,
        )

    def test_cross_derivation_usd_base_to_eur(self):
        # Company in USD asks for a non-native quote (EUR). The table is
        # pivoted through the UZS native base:
        #   out[EUR] = native[EUR] / native[USD]
        #            = (1/13000) / (1/11990.26).
        rates = self.cbu.fetch('USD', ['UZS', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['EUR']),
            float((Decimal('1') / Decimal('13000'))
                  / (Decimal('1') / Decimal('11990.26'))),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # GBP is absent from the fixture; a USD company asking for it
        # gets no GBP key rather than a guessed rate.
        rates = self.cbu.fetch('USD', ['UZS', 'GBP'], _ON_DATE)
        self.assertIn('UZS', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.cbu._download = self._stub({mod._CBU_DAILY_URL: b'<not json'})
        with self.assertRaises(rp.RateProviderError):
            self.cbu.fetch('USD', ['UZS'], _ON_DATE)

    def test_unexpected_shape_rejected(self):
        # Well-formed JSON but the wrong shape (object, not an array) is
        # also surfaced as RateProviderError.
        self.cbu._download = self._stub({mod._CBU_DAILY_URL: b'{"Ccy": "USD"}'})
        with self.assertRaises(rp.RateProviderError):
            self.cbu.fetch('USD', ['UZS'], _ON_DATE)
