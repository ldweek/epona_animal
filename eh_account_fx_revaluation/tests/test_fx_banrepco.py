# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Colombia TRM (Bank of the Republic) provider.

The provider is exercised purely at the parser level: ``_download`` is
replaced with a stub that serves canned Socrata JSON bytes, so no network
call is made and no database record is touched.

Coverage:

* The published ``valor`` ("1 USD in pesos") parses to the right Decimal
  for a USD-base company, with no inversion (native base USD).
* Cross-derivation for a company whose currency is the peso (not the
  native dollar) returns the inverse, "1 COP in units of USD".
* A base the single-pair feed cannot reach raises RateProviderError via
  cross_derive.
* An empty Socrata array yields an empty table for the native USD base.
* Malformed payload bytes raise RateProviderError.

Network is stubbed by replacing ``_download`` so the parser runs with no
live call. No database records are created.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import banrepco as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# One Socrata record, newest-first and limited to a single row. valor is
# "1 USD = 3406.14 COP", the validity window spans a single day.
_TRM_JSON = (
    b'[{"valor":"3406.14","unidad":"COP",'
    b'"vigenciadesde":"2026-06-23T00:00:00.000",'
    b'"vigenciahasta":"2026-06-23T00:00:00.000"}]'
)

# No published record: the Socrata array comes back empty.
_EMPTY_JSON = b'[]'


@tagged('eh_account_fx_revaluation', 'unit')
class TestBanrepcoProvider(TransactionCase):

    def setUp(self):
        super().setUp()
        self.banrepco = mod.BanrepcoRateProvider(timeout=1)
        self.on_date = datetime.date(2026, 6, 23)

    def _stub(self, payload):
        """Return a download stub that serves the given bytes for any URL."""
        def fake(url, headers=None):
            return payload
        return fake

    def test_native_rate_direction(self):
        # native base USD: valor is "1 USD = 3406.14 COP", already the
        # native direction, so the parsed Decimal must equal valor with no
        # inversion applied.
        self.banrepco._download = self._stub(_TRM_JSON)
        native = self.banrepco._fetch_native(
            'USD', ['COP'], self.on_date,
        )
        self.assertEqual(native['COP'], Decimal('3406.14'))

    def test_usd_base_passthrough(self):
        # Company base USD, quote COP: cross_derive returns the native
        # table entry directly, "1 USD = 3406.14 COP".
        self.banrepco._download = self._stub(_TRM_JSON)
        rates = self.banrepco.fetch('USD', ['COP'], self.on_date)
        self.assertEqual(rates['COP'], Decimal('3406.14'))

    def test_cross_derivation_for_cop_company(self):
        # Company base COP asks for USD. The native table reads
        # "1 USD = 3406.14 COP"; cross_derive pivots through USD:
        #   COP->USD = 1 / native['COP'] = 1 / 3406.14.
        self.banrepco._download = self._stub(_TRM_JSON)
        rates = self.banrepco.fetch('COP', ['USD'], self.on_date)
        self.assertAlmostEqual(
            float(rates['USD']),
            float(Decimal(1) / Decimal('3406.14')),
            places=12,
        )

    def test_quote_not_carried_is_omitted(self):
        # The feed only carries the peso. A EUR quote is not in the native
        # table, so for the USD base it never appears in the output.
        self.banrepco._download = self._stub(_TRM_JSON)
        rates = self.banrepco.fetch('USD', ['COP', 'EUR'], self.on_date)
        self.assertIn('COP', rates)
        self.assertNotIn('EUR', rates)

    def test_unreachable_base_raises(self):
        # A company in EUR asking for COP: the single-pair native table
        # carries no EUR entry, so cross_derive cannot pivot and raises.
        self.banrepco._download = self._stub(_TRM_JSON)
        with self.assertRaises(rp.RateProviderError):
            self.banrepco.fetch('EUR', ['COP'], self.on_date)

    def test_empty_array_yields_empty_for_usd_base(self):
        # No published record: the native table is empty, and for the
        # native USD base that simply yields an empty result.
        self.banrepco._download = self._stub(_EMPTY_JSON)
        rates = self.banrepco.fetch('USD', ['COP'], self.on_date)
        self.assertEqual(rates, {})

    def test_malformed_payload_raises(self):
        # Garbage bytes fail the shared JSON decoder and surface as
        # RateProviderError so the cron does not blow up.
        self.banrepco._download = self._stub(b'[not valid json')
        with self.assertRaises(rp.RateProviderError):
            self.banrepco.fetch('USD', ['COP'], self.on_date)
