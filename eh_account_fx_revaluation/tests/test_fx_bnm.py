# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Bank Negara Malaysia FX rate provider.

The BNM OpenAPI quotes a middle rate as "1 ``unit`` units of CODE = rate
MYR", with the yen quoted per 100 units. The provider inverts each row
into a ringgit-native table ("1 MYR = unit / rate CODE"), which the base
class cross-derives onto the company currency. These tests stub the
network seam (``_download``) with canned JSON bytes and assert:

* the native table inverts the right direction and honours the ``unit``
  multiplier (the yen's unit of 100);
* cross-derivation against a non-native base (USD) yields the expected
  Decimals, both the native-base pivot (USD->MYR) and a true cross
  (USD->EUR);
* a currency the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError;
* the mandatory Accept header advertising the API version is sent on the
  request.

No network call, no database write.
"""

import datetime
import json
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bnm as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


_ON_DATE = datetime.date(2026, 6, 23)

# USD quoted per 1 unit, EUR per 1 unit, JPY per 100 units. middle_rate is
# MYR per unit-block of the foreign currency.
_BNM_JSON = json.dumps({
    "data": [
        {"currency_code": "USD", "unit": 1,
         "rate": {"date": "2026-06-23", "buying_rate": 4.136,
                  "selling_rate": 4.142, "middle_rate": 4.139}},
        {"currency_code": "JPY", "unit": 100,
         "rate": {"date": "2026-06-23", "middle_rate": 2.66}},
        {"currency_code": "EUR", "unit": 1,
         "rate": {"date": "2026-06-23", "buying_rate": 4.84,
                  "selling_rate": 4.86, "middle_rate": 4.85}},
    ],
    "meta": {"total": 3},
}).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestBnmProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.bnm = mod.BnmRateProvider(timeout=1)
        self._seen_headers = []
        self.bnm._download = self._stub(_BNM_JSON)

    def _stub(self, payload):
        """Return a download stub serving canned bytes and recording the
        headers each call carried so the Accept header can be asserted.
        """
        def fake(url, headers=None):
            self._seen_headers.append(headers)
            return payload
        return fake

    def test_native_table_inverts_and_honours_unit(self):
        # Native base is MYR. The feed says "1 USD = 4.139 MYR", so the
        # native entry is "1 MYR = 1/4.139 USD". The yen is quoted per 100
        # units: "100 JPY = 2.66 MYR", so "1 MYR = 100/2.66 JPY".
        native = self.bnm._fetch_native('MYR', ['USD', 'JPY', 'EUR'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal(1) / Decimal('4.139'))
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('4.85'))
        self.assertEqual(native['JPY'], Decimal('100') / Decimal('2.66'))

    def test_cross_derivation_for_usd_base(self):
        # Company in USD asks for MYR and EUR.
        #   USD->MYR = 1 / native[USD] = 4.139 (the native-base pivot).
        #   USD->EUR = native[EUR] / native[USD]
        #            = (1/4.85) / (1/4.139).
        rates = self.bnm.fetch('USD', ['MYR', 'EUR'], _ON_DATE)
        self.assertAlmostEqual(
            float(rates['MYR']), float(Decimal('4.139')), places=8,
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float((Decimal(1) / Decimal('4.85'))
                  / (Decimal(1) / Decimal('4.139'))),
            places=8,
        )

    def test_quote_not_in_feed_is_omitted(self):
        # GBP is absent from the fixture; a USD company asking for it gets
        # no GBP key rather than a guessed rate.
        rates = self.bnm.fetch('USD', ['MYR', 'GBP'], _ON_DATE)
        self.assertIn('MYR', rates)
        self.assertNotIn('GBP', rates)

    def test_accept_header_is_sent(self):
        # The OpenAPI rejects requests that do not advertise the API
        # version, so every call must carry the v1 media type.
        self.bnm._fetch_native('MYR', ['USD'], _ON_DATE)
        self.assertTrue(self._seen_headers)
        self.assertEqual(
            self._seen_headers[-1],
            {'Accept': 'application/vnd.BNM.API.v1+json'},
        )

    def test_selling_rate_fallback_when_middle_missing(self):
        # A row with no middle_rate falls back to selling_rate.
        payload = json.dumps({
            "data": [
                {"currency_code": "SGD", "unit": 1,
                 "rate": {"selling_rate": 3.07}},
            ],
        }).encode('utf-8')
        self.bnm._download = self._stub(payload)
        native = self.bnm._fetch_native('MYR', ['SGD'], _ON_DATE)
        self.assertEqual(native['SGD'], Decimal(1) / Decimal('3.07'))

    def test_zero_or_missing_rate_skipped(self):
        # A row whose chosen rate is zero, and a row with no usable rate,
        # are both skipped rather than allowed into the table.
        payload = json.dumps({
            "data": [
                {"currency_code": "XAU", "unit": 1,
                 "rate": {"middle_rate": 0}},
                {"currency_code": "XAG", "unit": 1,
                 "rate": {"middle_rate": None, "selling_rate": None}},
                {"currency_code": "EUR", "unit": 1,
                 "rate": {"middle_rate": 4.85}},
            ],
        }).encode('utf-8')
        self.bnm._download = self._stub(payload)
        native = self.bnm._fetch_native('MYR', ['EUR', 'XAU', 'XAG'], _ON_DATE)
        self.assertNotIn('XAU', native)
        self.assertNotIn('XAG', native)
        self.assertIn('EUR', native)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage instead of crashing.
        self.bnm._download = self._stub(b'<not valid json')
        with self.assertRaises(rp.RateProviderError):
            self.bnm.fetch('USD', ['MYR'], _ON_DATE)

    def test_missing_data_list_rejected(self):
        # Well-formed JSON but the wrong shape (no data list) is surfaced
        # as RateProviderError.
        self.bnm._download = self._stub(b'{"meta": {"total": 0}}')
        with self.assertRaises(rp.RateProviderError):
            self.bnm.fetch('USD', ['MYR'], _ON_DATE)
