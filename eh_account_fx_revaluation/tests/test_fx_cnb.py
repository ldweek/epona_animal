# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Czech National Bank FX rate source.

The provider reads the daily central rates (koruna per a stated number
of foreign units) and re-expresses each quote into a CZK-native table,
which the base class cross-derives onto the company currency. These
tests stub the network seam with a canned payload so the parser, the
amount scaling, and the cross-derivation are exercised with no live
call. Company base is USD because EUR also appears in the feed and would
make a EUR-base assertion awkward.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import cnb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Canonical daily shape: a "rates" list of rows, each "amount units of
# currencyCode = rate CZK". AUD/USD are quoted per single unit; JPY is
# quoted per 100 units, which the parser must scale by amount.
_DAILY_JSON = b"""
{
  "rates": [
    {"validFor": "2026-05-01", "currencyCode": "AUD", "amount": 1,
     "rate": 14.802},
    {"validFor": "2026-05-01", "currencyCode": "USD", "amount": 1,
     "rate": 22.5},
    {"validFor": "2026-05-01", "currencyCode": "JPY", "amount": 100,
     "rate": 15.9}
  ]
}
"""

_ON_DATE = datetime.date(2026, 5, 1)


@tagged('eh_account_fx_revaluation', 'unit')
class TestCnbProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cnb = mod.CnbRateProvider(timeout=1)
        self.cnb._download = self._stub({mod._DAILY_URL: _DAILY_JSON})

    @staticmethod
    def _stub(payload_map):
        """Return a download stub serving the given URL->bytes map."""
        def fake(url, headers=None):
            return payload_map[url]
        return fake

    def test_native_table_applies_amount_scaling(self):
        # Native base is CZK. AUD is "1 AUD = 14.802 CZK", so the native
        # table reads "1 CZK = 1/14.802 AUD". JPY is "100 JPY = 15.9 CZK",
        # so "1 CZK = 100/15.9 JPY" - the amount=100 must be honoured.
        native = self.cnb._fetch_native('CZK', ['AUD', 'JPY'], _ON_DATE)
        self.assertEqual(native['AUD'], Decimal('1') / Decimal('14.802'))
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('15.9'))

    def test_cross_derivation_for_usd_base(self):
        # Company in USD asks for CZK and JPY.
        #   out[CZK] = 1 / native[USD] = rate/amount = 22.5 (USD amount=1).
        #   out[JPY] = native[JPY] / native[USD]
        #            = (100/15.9) / (1/22.5).
        rates = self.cnb.fetch('USD', ['CZK', 'JPY'], _ON_DATE)
        self.assertEqual(rates.effective_date, _ON_DATE)
        self.assertAlmostEqual(
            float(rates['CZK']), float(Decimal('22.5')), places=8,
        )
        self.assertAlmostEqual(
            float(rates['JPY']),
            float((Decimal('100') / Decimal('15.9'))
                  / (Decimal('1') / Decimal('22.5'))),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # GBP is absent from the fixture; a USD company asking for it
        # gets no GBP key rather than a guessed rate.
        rates = self.cnb.fetch('USD', ['CZK', 'GBP'], _ON_DATE)
        self.assertIn('CZK', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.cnb._download = self._stub({mod._DAILY_URL: b'<not json'})
        with self.assertRaises(rp.RateProviderError):
            self.cnb.fetch('USD', ['CZK'], _ON_DATE)
