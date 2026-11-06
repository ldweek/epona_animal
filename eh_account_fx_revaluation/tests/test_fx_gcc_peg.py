# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Gulf pegged-rate source (gcc_peg).

This provider is offline: the pegs are decree constants, so it never
touches the network. The tests therefore install no ``_download`` stub
at all; if the provider ever tried to fetch, the real network seam would
either reach out or fail, and these assertions would expose it.

Coverage:
* Direction: a USD-based company gets "1 USD = value CODE" exactly as
  decreed for each Gulf peg.
* Cross-derivation for a non-native base: a company based in AED (one of
  the pegged currencies) gets AED->USD = 1 / 3.6725.
* Omitted quote: KWD is never returned even when explicitly requested,
  because the dinar floats against a basket and a USD peg would be wrong.
* A base the table cannot pivot through (EUR is not pegged here) raises
  RateProviderError rather than guessing.

There is no malformed-bytes path because the provider parses no bytes;
the "bad input" analogue for an offline source is an un-pivotable base,
covered by the EUR case.
"""

from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import gcc_peg as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


@tagged('eh_account_fx_revaluation', 'unit')
class TestGccPegProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.provider = mod.GccPegRateProvider()
        # A fixed date is accepted by the contract but unused offline.
        self.on_date = None

    def test_registered(self):
        self.assertIn('gcc_peg', rp.known_providers())
        self.assertFalse(rp.provider_needs_key('gcc_peg'))
        self.assertEqual(
            rp.provider_label('gcc_peg'), "Gulf pegged rates (offline)")

    def test_usd_base_exact_decree_pegs(self):
        # 1 USD expressed in each Gulf currency, the registry direction.
        rates = self.provider.fetch(
            'USD', ['SAR', 'AED', 'QAR', 'BHD', 'OMR'], self.on_date)
        self.assertEqual(rates['SAR'], Decimal('3.75'))
        self.assertEqual(rates['AED'], Decimal('3.6725'))
        self.assertEqual(rates['QAR'], Decimal('3.64'))
        self.assertEqual(rates['BHD'], Decimal('0.376'))
        # OMR is stored as 1/2.6008; compare on the float to avoid
        # depending on Decimal division precision in the assertion.
        self.assertAlmostEqual(
            float(rates['OMR']),
            float(Decimal('1') / Decimal('2.6008')),
            places=10,
        )

    def test_cross_derivation_for_non_native_base(self):
        # Company based in AED (a pegged currency, not the native USD)
        # asks for USD. Pivot: AED->USD = 1 / (1 USD = 3.6725 AED).
        rates = self.provider.fetch('AED', ['USD'], self.on_date)
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal('1') / Decimal('3.6725')),
            places=10,
        )

    def test_kwd_never_returned(self):
        # KWD floats against a basket; the provider must not peg it,
        # even when the caller explicitly asks for it.
        rates = self.provider.fetch(
            'USD', ['SAR', 'KWD'], self.on_date)
        self.assertIn('SAR', rates)
        self.assertNotIn('KWD', rates)
        # And it is absent from the underlying decree table too.
        self.assertNotIn('KWD', mod.GccPegRateProvider.PEGS)

    def test_unpivotable_base_raises(self):
        # EUR is not one of the Gulf pegs, so cross_derive cannot pivot
        # through it. The provider raises rather than guess, documenting
        # that this source serves USD/Gulf-based companies only.
        with self.assertRaises(rp.RateProviderError):
            self.provider.fetch('EUR', ['SAR'], self.on_date)
