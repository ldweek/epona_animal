# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank of the Republic (Colombia) official TRM rate source.

The Tasa Representativa del Mercado (TRM) is Colombia's official market
representative exchange rate, set by the Superintendencia Financiera de
Colombia and published through the national open-data portal as a Socrata
JSON dataset. The feed carries a single pair, the peso against the US
dollar, so this source publishes native base USD and lets cross_derive
surface a gap for any other currency.

Endpoint:
* https://www.datos.gov.co/resource/32sa-8pi3.json?$limit=1&$order=vigenciadesde%20DESC

Each record exposes ``valor``, the number of Colombian pesos per one US
dollar, plus the validity window (``vigenciadesde`` / ``vigenciahasta``).
Ordering by ``vigenciadesde`` descending and limiting to one row returns
the latest published TRM; we read that first record and key it as
"1 USD in units of COP". When the array comes back empty (no published
record) we return an empty table, which the base class derivation then
surfaces as a gap, or raises when the company base itself cannot be served.

Data reuse: the datos.gov.co dataset is published under the Creative
Commons Attribution-ShareAlike 4.0 International licence (CC BY-SA 4.0).
Any redistribution must retain the attribution: "Source: Superintendencia
Financiera de Colombia / Banco de la Republica (datos.gov.co), CC BY-SA
4.0".

This module is original work; the parser below implements the published
Socrata JSON shape and does not derive from any third-party rate library.
"""

from decimal import Decimal

from .. import rate_providers as rp
from ..rate_providers import BaseHttpProvider, to_decimal


# Socrata resource for the TRM series. Ordered newest-first and limited to
# a single row so we read only the latest published rate.
_TRM_URL = (
    "https://www.datos.gov.co/resource/32sa-8pi3.json"
    "?$limit=1&$order=vigenciadesde%20DESC"
)


class BanrepcoRateProvider(BaseHttpProvider):
    """Colombia TRM official rate, native base USD.

    The feed publishes ``valor`` as "1 USD = valor COP", which is already
    the native direction this source needs (one US dollar expressed in the
    quoted currency). We key the single COP quote off the first (latest)
    record and hand the one-entry table to the base class, which pivots it
    onto the company currency.
    """

    name = 'banrepco'
    native_base = 'USD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD expressed in units of code".

        The TRM dataset carries only the peso against the dollar, so the
        native table holds at most a single COP entry. An empty array (no
        published record) yields an empty table rather than an error, so
        the base class derivation decides whether the gap is fatal for the
        requested base. A record whose ``valor`` is missing, unparseable,
        or zero is skipped for the same reason.
        """
        payload = self._download_json(_TRM_URL)
        if not isinstance(payload, list):
            raise self._malformed("payload is not a JSON array")
        if not payload:
            return {}

        first = payload[0]
        if not isinstance(first, dict):
            raise self._malformed("first record is not an object")

        native = {}
        valor = to_decimal(first.get('valor'))
        if valor is not None and valor != 0:
            self._set_effective_date(first.get('vigenciadesde'))
            # Published as "1 USD = valor COP"; this is already the native
            # direction (one unit of native_base in units of the quote).
            native['COP'] = valor
        return native

    def _malformed(self, detail):
        return rp.RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register = rp.register
register('banrepco', BanrepcoRateProvider,
         label="[CO] Bank of the Republic (Colombia TRM)", needs_key=False)
