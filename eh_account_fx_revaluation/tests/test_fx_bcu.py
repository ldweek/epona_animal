# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Hermetic unit tests for the Banco Central del Uruguay (BCU) FX provider.

The BCU service is native base UYU and returns document/literal SOAP rows
quoting "1 CODE = TCV UYU" for a date range. These tests stub the POST
network seam (_download_post) with canned SOAP bytes (no network, no DB
writes) and assert:

* only the rows of the MAX (latest) Fecha in the range are used, so an
  earlier day in the same window is ignored;
* the native table is inverted to "1 UYU = 1/TCV CODE" and cross
  derivation for a UYU and a non-UYU quote yields the hand-computed
  Decimal;
* a quote the service does not carry is omitted from the output;
* a status 0 / codigoerror 100 (no quotation) response yields {};
* malformed payload bytes raise RateProviderError.

The fixture carries two latest-day rows (USD TCV 39.908, EUR TCV 45.5949)
plus one earlier-day USD row (TCV 39.0) to prove the max-Fecha selection
ignores the older day.
"""

import datetime
from decimal import Decimal

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_fx_revaluation.tools.fx_sources import bcu as mod
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp


# Document/literal SOAP response. Salida wraps a respuestastatus (status 1,
# codigoerror 0) and the quotation rows whose local tag name is the literal
# 'datoscotizaciones.dato'. The latest Fecha is 2026-06-19; an earlier USD
# row dated 2026-06-12 must be ignored by the max-Fecha selection.
_BCU_OK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope'
    ' xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soapenv:Body>'
    '<wsbcucotizaciones.ExecuteResponse xmlns="Cotiza">'
    '<Salida>'
    '<respuestastatus>'
    '<status>1</status><codigoerror>0</codigoerror><mensaje>OK</mensaje>'
    '</respuestastatus>'
    '<datoscotizaciones>'
    '<datoscotizaciones.dato>'
    '<Fecha>2026-06-12</Fecha><Moneda>2225</Moneda>'
    '<Nombre>Dolar USA</Nombre><CodigoISO>USD</CodigoISO>'
    '<TCC>38.5</TCC><TCV>39.0</TCV>'
    '</datoscotizaciones.dato>'
    '<datoscotizaciones.dato>'
    '<Fecha>2026-06-19</Fecha><Moneda>2225</Moneda>'
    '<Nombre>Dolar USA</Nombre><CodigoISO>USD</CodigoISO>'
    '<TCC>39.408</TCC><TCV>39.908</TCV>'
    '</datoscotizaciones.dato>'
    '<datoscotizaciones.dato>'
    '<Fecha>2026-06-19</Fecha><Moneda>1111</Moneda>'
    '<Nombre>Euro</Nombre><CodigoISO>EUR</CodigoISO>'
    '<TCC>45.0949</TCC><TCV>45.5949</TCV>'
    '</datoscotizaciones.dato>'
    '</datoscotizaciones>'
    '</Salida>'
    '</wsbcucotizaciones.ExecuteResponse>'
    '</soapenv:Body>'
    '</soapenv:Envelope>'
).encode('utf-8')


# No-quotation response: status 0, codigoerror 100, no rows. The provider
# must surface this as an empty table rather than raising.
_BCU_NODATA = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope'
    ' xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soapenv:Body>'
    '<wsbcucotizaciones.ExecuteResponse xmlns="Cotiza">'
    '<Salida>'
    '<respuestastatus>'
    '<status>0</status><codigoerror>100</codigoerror>'
    '<mensaje>No hay datos para la fecha</mensaje>'
    '</respuestastatus>'
    '</Salida>'
    '</wsbcucotizaciones.ExecuteResponse>'
    '</soapenv:Body>'
    '</soapenv:Envelope>'
).encode('utf-8')


_ON_DATE = datetime.date(2026, 6, 19)


@tagged('eh_account_fx_revaluation', 'unit')
class TestBcuProviderParsing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.bcu = mod.BcuRateProvider(timeout=1)
        self.bcu._download_post = self._stub_post(_BCU_OK)

    @staticmethod
    def _stub_post(payload):
        """Return a POST stub serving the same bytes for any request."""
        def fake(url, body, headers=None):
            return payload
        return fake

    def test_native_direction_is_one_uyu_per_code(self):
        # _fetch_native publishes "1 UYU = 1/TCV CODE", the inverse of the
        # service. Only the latest Fecha (2026-06-19) rows count, so USD
        # uses TCV 39.908 not the earlier 39.0.
        native = self.bcu._fetch_native('USD', ['UYU', 'EUR'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal('1') / Decimal('39.908'))
        self.assertEqual(native['EUR'], Decimal('1') / Decimal('45.5949'))

    def test_max_fecha_selection_ignores_earlier_day(self):
        # The earlier-day USD row (TCV 39.0) must not influence the table;
        # the inverse of 39.0 would be the wrong value.
        native = self.bcu._fetch_native('USD', ['UYU'], _ON_DATE)
        self.assertEqual(native['USD'], Decimal('1') / Decimal('39.908'))
        self.assertNotEqual(native['USD'], Decimal('1') / Decimal('39.0'))

    def test_cross_derivation_for_usd_base(self):
        # Company in USD asks for UYU and EUR.
        rates = self.bcu.fetch('USD', ['UYU', 'EUR'], _ON_DATE)
        # USD -> UYU: quote is the native base, so out[UYU] = 1/native[USD]
        # = 1 / (1/39.908) = 39.908 (the TCV).
        self.assertAlmostEqual(
            float(rates['UYU']), float(Decimal('39.908')), places=8,
        )
        # USD -> EUR: native[EUR] / native[USD]
        #           = (1/45.5949) / (1/39.908).
        expected_eur = (
            (Decimal('1') / Decimal('45.5949'))
            / (Decimal('1') / Decimal('39.908'))
        )
        self.assertAlmostEqual(
            float(rates['EUR']), float(expected_eur), places=8,
        )

    def test_missing_quote_omitted(self):
        # JPY is not in the fixture; it must be absent rather than
        # defaulted, so the caller surfaces the gap.
        rates = self.bcu.fetch('USD', ['UYU', 'JPY'], _ON_DATE)
        self.assertIn('UYU', rates)
        self.assertNotIn('JPY', rates)

    def test_no_data_response_yields_empty(self):
        # status 0 / codigoerror 100 is an ordinary non-trading day; the
        # provider returns {} and lets cross derivation surface the gap.
        self.bcu._download_post = self._stub_post(_BCU_NODATA)
        native = self.bcu._fetch_native('USD', ['UYU', 'EUR'], _ON_DATE)
        self.assertEqual(native, {})

    def test_malformed_xml_rejected(self):
        # Bad bytes are wrapped into RateProviderError so the cron handler
        # treats it as a recoverable outage rather than a crash.
        self.bcu._download_post = self._stub_post(b'<not valid soap')
        with self.assertRaises(rp.RateProviderError):
            self.bcu.fetch('USD', ['UYU', 'EUR'], _ON_DATE)
