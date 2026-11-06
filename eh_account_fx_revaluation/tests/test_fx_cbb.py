# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Central Bank of Bahrain FX rate provider.

The CBB OpenAPI feed quotes units-per-USD: each row's ``UsCurr`` field is
"1 USD = UsCurr CurrCd". The provider publishes its native table against
USD directly (no inversion). These tests stub the network seam
(``_download``) with canned JSON bytes and assert:

* the native table reads units-per-USD with the right direction, and a
  whitespace-padded ``UsCurr`` parses correctly;
* cross-derivation against a non-USD company base (BHD) yields the
  expected Decimal;
* a currency the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.
"""

import datetime
import json
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import cbb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Units-per-USD, with whitespace-padded values as the live feed serves
# them. 1 USD = 3.673000 AED, 1 USD = 1.165000 EUR, 1 USD = 0.376081 BHD.
_CBB_JSON = json.dumps({
    "items": [
        {"RateDt": "22-JUN-2026", "CurrCd": "AED",
         "UsCurr": "   3.673000", "BdCurr": "   0.102391", "Region": "G"},
        {"RateDt": "22-JUN-2026", "CurrCd": "EUR",
         "UsCurr": "   1.165000", "BdCurr": "   0.438000", "Region": "E"},
        {"RateDt": "22-JUN-2026", "CurrCd": "BHD",
         "UsCurr": "   0.376081", "BdCurr": "0", "Region": "G"},
    ],
    "count": 3,
    "hasMore": False,
    "limit": 10000,
    "offset": 0,
}).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestCbbProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cbb = mod.CbbRateProvider(timeout=1)
        self.cbb._download = lambda url, headers=None: _CBB_JSON

    def test_native_table_is_units_per_usd(self):
        # Native table is "1 USD in units of code", which is exactly the
        # feed's direction, so values are taken as-is. The whitespace
        # padding on UsCurr must be trimmed by to_decimal.
        native = self.cbb._fetch_native(
            'USD', ['AED', 'EUR'], datetime.date.today(),
        )
        self.assertEqual(native['AED'], Decimal('3.673000'))
        self.assertEqual(native['EUR'], Decimal('1.165000'))
        self.assertEqual(native['BHD'], Decimal('0.376081'))

    def test_whitespace_padded_value_parsed(self):
        # The live feed pads UsCurr with leading spaces; the parser must
        # still resolve the exact Decimal.
        native = self.cbb._fetch_native(
            'USD', ['AED'], datetime.date.today(),
        )
        self.assertEqual(native['AED'], Decimal('3.673000'))

    def test_cross_derivation_for_non_usd_base(self):
        # Company on BHD asks for USD and AED.
        #   BHD->USD: native[BHD] = 0.376081, so out[USD] = 1/native[BHD]
        #             = 1 / 0.376081.
        #   BHD->AED: out[AED] = native[AED] / native[BHD]
        #             = 3.673000 / 0.376081.
        rates = self.cbb.fetch(
            'BHD', ['USD', 'AED'], datetime.date.today(),
        )
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal(1) / Decimal('0.376081')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['AED']),
            float(Decimal('3.673000') / Decimal('0.376081')),
            places=8,
        )

    def test_missing_quote_omitted(self):
        # JPY is not carried by the fixture, so it is absent from the
        # output rather than guessed.
        rates = self.cbb.fetch(
            'BHD', ['USD', 'JPY'], datetime.date.today(),
        )
        self.assertIn('USD', rates)
        self.assertNotIn('JPY', rates)

    def test_missing_code_or_zero_uscurr_skipped(self):
        # A row with no CurrCd, and a row whose UsCurr is zero, are both
        # skipped rather than allowed to enter the table.
        payload = json.dumps({
            "items": [
                {"RateDt": "22-JUN-2026", "CurrCd": "",
                 "UsCurr": "   1.000000", "BdCurr": "0"},
                {"RateDt": "22-JUN-2026", "CurrCd": "XAU",
                 "UsCurr": "0", "BdCurr": "0"},
                {"RateDt": "22-JUN-2026", "CurrCd": "EUR",
                 "UsCurr": "   1.165000", "BdCurr": "   0.438000"},
            ],
        }).encode('utf-8')
        self.cbb._download = lambda url, headers=None: payload
        native = self.cbb._fetch_native(
            'USD', ['EUR', 'XAU'], datetime.date.today(),
        )
        self.assertNotIn('XAU', native)
        self.assertIn('EUR', native)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage.
        self.cbb._download = lambda url, headers=None: b'<not valid json'
        with self.assertRaises(rp.RateProviderError):
            self.cbb.fetch(
                'BHD', ['USD'], datetime.date.today(),
            )

    def test_empty_items_rejected(self):
        # A feed with no items is an error, not a silent empty.
        payload = json.dumps({"items": []}).encode('utf-8')
        self.cbb._download = lambda url, headers=None: payload
        with self.assertRaises(rp.RateProviderError):
            self.cbb.fetch(
                'BHD', ['USD'], datetime.date.today(),
            )
