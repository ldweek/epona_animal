# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Central Bank of Kuwait (CBK) FX provider.

The CBK feed is native base KWD and publishes "rate fils per 1 CODE"
with 1 KWD = 1000 fils. These tests feed the parser canned bytes (no
network, no database) and assert:

* the native table is inverted to the correct direction, "1 KWD = X CODE",
  and the company-base output carries the right DIRECTION;
* cross derivation for a non-KWD company currency yields the expected
  Decimal, computed by hand below;
* a quote the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.

The synthetic payload mirrors the published schema (USD 307.65, EUR
352.4131, GBP 406.2672 fils per unit) and is declared UTF-8 as the live
feed serves it.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import cbk as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Verified live shape: fils per one unit of the foreign currency.
_CBK_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<daily_exchange_rate>'
    '<date>20260622000000</date>'
    '<currency><code>USD</code><rate>307.65</rate></currency>'
    '<currency><code>EUR</code><rate>352.4131</rate></currency>'
    '<currency><code>GBP</code><rate>406.2672</rate></currency>'
    '</daily_exchange_rate>'
).encode('utf-8')


@tagged('eh_account_fx_revaluation', 'unit')
class TestCbkProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.cbk = mod.CbkRateProvider(timeout=1)
        self.cbk._download = self._stub_download(_CBK_XML)

    @staticmethod
    def _stub_download(payload):
        """Return a download stub serving the same bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_kwd_per_code(self):
        # _fetch_native must publish "1 KWD = X CODE", the inverse of the
        # feed. USD: 1 KWD = 1000 / 307.65 USD.
        native = self.cbk._fetch_native(
            'KWD', ['USD', 'EUR'], datetime.date(2026, 6, 22),
        )
        self.assertEqual(native['USD'], Decimal('1000') / Decimal('307.65'))
        self.assertEqual(native['EUR'], Decimal('1000') / Decimal('352.4131'))

    def test_usd_to_kwd_direction(self):
        # Company in USD asks for KWD. The quote is the native base, so
        # out[KWD] = 1 / native[USD] = rate/1000 = 307.65/1000 = 0.30765
        # exactly. This pins the DIRECTION: 1 USD buys 0.30765 KWD.
        rates = self.cbk.fetch(
            'USD', ['KWD', 'EUR'], datetime.date(2026, 6, 22),
        )
        self.assertAlmostEqual(
            float(rates['KWD']), float(Decimal('0.30765')), places=8,
        )

    def test_cross_derivation_for_non_kwd_base(self):
        # Company in USD asks for EUR (a NON-native, NON-base quote).
        # USD -> EUR = native[EUR] / native[USD]
        #            = (1000/352.4131) / (1000/307.65)
        #            = 307.65 / 352.4131.
        rates = self.cbk.fetch(
            'USD', ['KWD', 'EUR'], datetime.date(2026, 6, 22),
        )
        expected_eur = Decimal('307.65') / Decimal('352.4131')
        self.assertAlmostEqual(
            float(rates['EUR']), float(expected_eur), places=8,
        )

    def test_missing_quote_omitted(self):
        # JPY is not in the fixture; it must be absent from the output
        # rather than defaulted, so the caller surfaces the gap.
        rates = self.cbk.fetch(
            'USD', ['KWD', 'JPY'], datetime.date(2026, 6, 22),
        )
        self.assertIn('KWD', rates)
        self.assertNotIn('JPY', rates)

    def test_zero_rate_row_skipped(self):
        # A currency whose rate parses to zero yields no derivable KWD
        # rate, so it is dropped rather than producing a division error.
        zero_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<daily_exchange_rate>'
            '<date>20260622000000</date>'
            '<currency><code>USD</code><rate>307.65</rate></currency>'
            '<currency><code>XXX</code><rate>0</rate></currency>'
            '</daily_exchange_rate>'
        ).encode('utf-8')
        self.cbk._download = self._stub_download(zero_xml)
        native = self.cbk._fetch_native(
            'KWD', ['USD', 'XXX'], datetime.date(2026, 6, 22),
        )
        self.assertIn('USD', native)
        self.assertNotIn('XXX', native)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron handler
        # treats it as a recoverable outage rather than a crash.
        self.cbk._download = self._stub_download(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.cbk.fetch(
                'USD', ['KWD', 'EUR'], datetime.date(2026, 6, 22),
            )
