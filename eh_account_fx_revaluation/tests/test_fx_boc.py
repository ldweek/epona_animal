# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Bank of Canada FX rate provider.

The Valet group feed quotes CAD per one unit of each foreign currency
(key FX<XXX>CAD, value v = "1 XXX = v CAD"). The provider publishes its
native table against CAD, so each value is inverted to "1 CAD in units
of XXX". These tests stub the network seam (``_download``) with canned
JSON bytes and assert:

* the native inversion has the right magnitude and direction;
* cross-derivation against a non-CAD company base yields the expected
  Decimal;
* a currency the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.
"""

import datetime
import json
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import boc as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# One observation, CAD per one unit of each foreign currency.
#   1 USD = 1.3700 CAD, 1 EUR = 1.4800 CAD, 1 GBP = 1.7200 CAD.
_BOC_JSON = json.dumps({
    "observations": [
        {
            "d": "2026-05-01",
            "FXUSDCAD": {"v": "1.3700"},
            "FXEURCAD": {"v": "1.4800"},
            "FXGBPCAD": {"v": "1.7200"},
        },
    ],
}).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestBocProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.boc = mod.BocRateProvider(timeout=1)
        self.boc._download = lambda url, headers=None: _BOC_JSON

    def test_native_table_inverts_to_cad_base(self):
        # Native table is "1 CAD in units of code". The feed quotes
        # CAD per foreign unit, so native[USD] = 1 / 1.3700.
        native = self.boc._fetch_native(
            'CAD', ['USD', 'EUR'], datetime.date.today(),
        )
        self.assertEqual(native['USD'], Decimal(1) / Decimal('1.3700'))
        self.assertEqual(native['EUR'], Decimal(1) / Decimal('1.4800'))
        self.assertEqual(native['GBP'], Decimal(1) / Decimal('1.7200'))
        # CAD itself is implicit and not emitted.
        self.assertNotIn('CAD', native)

    def test_cad_base_returns_native_table(self):
        # A company on CAD (the native base) receives the table as-is:
        # "1 CAD in units of quote". The feed gives CAD per foreign unit,
        # so out[USD] = native[USD] = 1 / 1.3700, i.e. CAD is the weaker
        # of the pair here. We assert the inverse of the published rate,
        # not the published rate itself.
        rates = self.boc.fetch(
            'CAD', ['USD', 'GBP'], datetime.date.today(),
        )
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal(1) / Decimal('1.3700')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal(1) / Decimal('1.7200')),
            places=8,
        )

    def test_cross_derivation_for_non_cad_base(self):
        # Company on USD asks for CAD and EUR.
        #   USD->CAD: native[USD] = 1/1.3700, so out[CAD] = 1/native[USD]
        #             = 1.3700.
        #   USD->EUR: out[EUR] = native[EUR] / native[USD]
        #             = (1/1.4800) / (1/1.3700) = 1.3700 / 1.4800.
        rates = self.boc.fetch(
            'USD', ['CAD', 'EUR'], datetime.date.today(),
        )
        self.assertAlmostEqual(float(rates['CAD']), 1.3700, places=8)
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('1.3700') / Decimal('1.4800')),
            places=8,
        )

    def test_missing_quote_omitted(self):
        # JPY is not carried by the fixture, so it is absent from the
        # output rather than guessed.
        rates = self.boc.fetch(
            'USD', ['CAD', 'JPY'], datetime.date.today(),
        )
        self.assertIn('CAD', rates)
        self.assertNotIn('JPY', rates)

    def test_zero_value_series_skipped(self):
        # A zero v would invert to a division error; the parser must
        # skip the row instead.
        payload = json.dumps({
            "observations": [
                {
                    "d": "2026-05-01",
                    "FXUSDCAD": {"v": "0"},
                    "FXEURCAD": {"v": "1.4800"},
                },
            ],
        }).encode('utf-8')
        self.boc._download = lambda url, headers=None: payload
        native = self.boc._fetch_native(
            'CAD', ['USD', 'EUR'], datetime.date.today(),
        )
        self.assertNotIn('USD', native)
        self.assertIn('EUR', native)

    def test_malformed_payload_rejected(self):
        # Non-JSON bytes are wrapped into RateProviderError so the cron
        # treats it as a recoverable outage.
        self.boc._download = lambda url, headers=None: b'<not valid json'
        with self.assertRaises(rp.RateProviderError):
            self.boc.fetch(
                'CAD', ['USD'], datetime.date.today(),
            )

    def test_empty_observations_rejected(self):
        # A feed with no observations is an error, not a silent empty.
        payload = json.dumps({"observations": []}).encode('utf-8')
        self.boc._download = lambda url, headers=None: payload
        with self.assertRaises(rp.RateProviderError):
            self.boc.fetch(
                'CAD', ['USD'], datetime.date.today(),
            )
