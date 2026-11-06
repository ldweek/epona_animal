# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Reserve Bank of Australia FX provider.

The RBA feed publishes "1 AUD = value TARGET" against a native AUD base.
These tests feed the parser a synthetic RDF/RSS payload (no network) and
assert:

* the native table parses to the right Decimal in the right direction
  (AUD base, value stored as-is, no inversion);
* a company whose currency is not AUD gets correctly cross-derived rates;
* a quote the feed does not carry is omitted from the output;
* malformed payload bytes raise RateProviderError.

The provider's single network seam (``_download``) is replaced with a
stub so the parser runs without a live call.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import rba as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Synthetic RBA feed: native AUD base. value = "1 AUD = value TARGET".
# USD at 0.6500, EUR at 0.6000. The namespace prefixes deliberately mirror
# the live feed so the namespace-agnostic parser is exercised for real.
_RBA_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:cb="http://www.cbwiki.net/wiki/index.php/Specification_1.1"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:date>2026-05-01T16:45:00+10:00</dc:date>
  <item>
    <cb:statistics>
      <cb:exchangeRate>
        <cb:value>0.6500</cb:value>
        <cb:baseCurrency>AUD</cb:baseCurrency>
        <cb:targetCurrency>USD</cb:targetCurrency>
      </cb:exchangeRate>
    </cb:statistics>
  </item>
  <item>
    <cb:statistics>
      <cb:exchangeRate>
        <cb:value>0.6000</cb:value>
        <cb:baseCurrency>AUD</cb:baseCurrency>
        <cb:targetCurrency>EUR</cb:targetCurrency>
      </cb:exchangeRate>
    </cb:statistics>
  </item>
</rdf:RDF>
"""


@tagged('eh_account_fx_revaluation', 'unit')
class TestRbaProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.rba = mod.RbaRateProvider(timeout=1)

    def _stub(self, payload):
        """Replace the network seam with a function serving fixed bytes."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_table_direction(self):
        # AUD is the native base, so fetching with base AUD returns the
        # table verbatim: native[TARGET] = value, no inversion applied.
        self.rba._download = self._stub(_RBA_XML)
        rates = self.rba.fetch('AUD', ['USD', 'EUR'], datetime.date.today())
        self.assertEqual(rates['USD'], Decimal('0.6500'))
        self.assertEqual(rates['EUR'], Decimal('0.6000'))
        self.assertEqual(rates.effective_date, datetime.date(2026, 5, 1))

    def test_cross_derivation_for_non_aud_base(self):
        # Company in USD asks for AUD and EUR. The feed gives
        # 1 AUD = 0.6500 USD and 1 AUD = 0.6000 EUR.
        # USD->AUD = 1 / native[USD]      = 1 / 0.6500
        # USD->EUR = native[EUR] / native[USD] = 0.6000 / 0.6500
        self.rba._download = self._stub(_RBA_XML)
        rates = self.rba.fetch('USD', ['AUD', 'EUR'], datetime.date.today())
        self.assertAlmostEqual(
            float(rates['AUD']),
            float(Decimal('1') / Decimal('0.6500')),
            places=8,
        )
        self.assertAlmostEqual(
            float(rates['EUR']),
            float(Decimal('0.6000') / Decimal('0.6500')),
            places=8,
        )

    def test_unknown_quote_omitted(self):
        # GBP is not carried by the fixture, so it must not appear in the
        # output. USD is still returned for the AUD base.
        self.rba._download = self._stub(_RBA_XML)
        rates = self.rba.fetch('AUD', ['USD', 'GBP'], datetime.date.today())
        self.assertIn('USD', rates)
        self.assertNotIn('GBP', rates)

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError by _download_xml so
        # the cron handler treats it as a recoverable outage.
        self.rba._download = self._stub(b'<not valid xml')
        with self.assertRaises(rp.RateProviderError):
            self.rba.fetch('AUD', ['USD'], datetime.date.today())
