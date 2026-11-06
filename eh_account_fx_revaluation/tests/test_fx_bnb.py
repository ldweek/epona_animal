# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the Bulgarian National Bank (bnb) FX rate source.

The parser is pure Python with no Odoo or network dependency in its happy
path. The network seam ``_download`` is replaced with a stub that serves
canned bytes, so the tests stay hermetic: no live call, no DB write.

The fixture mirrors the published ROWSET shape: a leading TITLE ROW with
no CODE child, followed by data ROWs carrying CODE and RATE children
expressed as "1 EUR = RATE CODE".

Coverage:
* Direction: native base EUR copies RATE straight across (no inversion).
* Cross-derivation against a non-native base (company in USD).
* A quote absent from the feed is omitted from the result.
* The injected BGN euro peg lets a BGN quote be derived.
* Malformed bytes raise RateProviderError.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bnb as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Two data ROWs plus a leading TITLE ROW that has no CODE child. RATE is
# "foreign currency units per one euro": 1 EUR = 1.1456 USD, 1 EUR = 0.8600
# GBP. The provider must skip the header ROW and read CODE/RATE off the
# data ROWs without a namespace.
_BNB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ROWSET>
    <ROW>
        <TITLE>Foreign currency rates against the Bulgarian lev / euro</TITLE>
    </ROW>
    <ROW>
        <NAME_>US Dollar</NAME_>
        <CODE>USD</CODE>
        <RATE>1.1456</RATE>
        <REVERSERATE>0.8729</REVERSERATE>
        <CURR_DATE>22.06.2026</CURR_DATE>
    </ROW>
    <ROW>
        <NAME_>Pound Sterling</NAME_>
        <CODE>GBP</CODE>
        <RATE>0.8600</RATE>
        <REVERSERATE>1.1628</REVERSERATE>
        <CURR_DATE>22.06.2026</CURR_DATE>
    </ROW>
</ROWSET>
"""


@tagged('eh_account_fx_revaluation', 'unit')
class TestBnbProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.BnbRateProvider(timeout=1)

    def _stub(self, payload):
        """Replace the network seam with a stub serving canned bytes."""
        def fake(url, headers=None):
            return payload
        self.provider._download = fake

    def test_registered(self):
        self.assertIn('bnb', rp.known_providers())
        self.assertFalse(rp.provider_needs_key('bnb'))
        self.assertEqual(
            rp.provider_label('bnb'), "[BG] Bulgarian National Bank",
        )

    def test_native_eur_base_direction(self):
        # Company reporting in EUR: the native EUR table is returned as-is,
        # each value reading "1 EUR = RATE CODE" with no inversion.
        self._stub(_BNB_XML)
        rates = self.provider.fetch(
            'EUR', ['USD', 'GBP', 'BGN'], datetime.date.today(),
        )
        self.assertEqual(rates['USD'], Decimal('1.1456'))
        self.assertEqual(rates['GBP'], Decimal('0.8600'))
        self.assertEqual(rates['BGN'], Decimal('1.95583'))

    def test_cross_derivation_for_non_eur_base(self):
        # Company in USD asks for EUR, GBP, BGN. The native table is
        # native[USD]=1.1456, native[GBP]=0.8600, native[BGN]=1.95583
        # (all "1 EUR = value CODE"). Pivoting onto USD:
        #   USD->EUR = 1 / native[USD]
        #   USD->GBP = native[GBP] / native[USD]
        #   USD->BGN = native[BGN] / native[USD]
        self._stub(_BNB_XML)
        rates = self.provider.fetch(
            'USD', ['EUR', 'GBP', 'BGN'], datetime.date.today(),
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('1') / Decimal('1.1456')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['GBP']),
            float(Decimal('0.8600') / Decimal('1.1456')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['BGN']),
            float(Decimal('1.95583') / Decimal('1.1456')),
            places=8,
        )

    def test_absent_quote_is_omitted(self):
        # JPY is not in the feed, so it is left out of the result rather
        # than guessed, letting the caller surface the gap.
        self._stub(_BNB_XML)
        rates = self.provider.fetch(
            'USD', ['EUR', 'JPY'], datetime.date.today(),
        )
        self.assertIn('EUR', rates)
        self.assertNotIn('JPY', rates)

    def test_malformed_bytes_raise(self):
        # Bad bytes are wrapped into RateProviderError so the cron does
        # not blow up on a corrupt response.
        self._stub(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch(
                'EUR', ['USD'], datetime.date.today(),
            )
