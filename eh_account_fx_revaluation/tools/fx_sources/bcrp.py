# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Reserve Bank of Peru (Banco Central de Reserva del Peru) rate
source.

Native base is USD. The bank publishes its statistical series through a
JSON API, one series per endpoint. Series PD04640PD is the official sol
per US dollar interbank sell rate, so this source carries a single pair,
the sol against the dollar, and lets cross_derive surface a gap for any
other currency.

Endpoint:
* https://estadisticas.bcrp.gob.pe/estadisticas/series/api/PD04640PD/json

The payload holds a ``periods`` array, newest period last, where each
period exposes a ``name`` (the publication day) and a ``values`` array
whose first element is the published rate. Non-business days publish the
sentinel string "n.d." instead of a number, so we walk the periods from
the last (newest) to the first (oldest) and take the first whose value
parses, matching the provider contract of "the rate on or before this
date". Each usable value is "1 USD = value PEN", which is already the
native direction this source needs; we key it as "1 USD in units of PEN".
When no period carries a usable value we return an empty table, which the
base class derivation then surfaces as a gap, or raises when the company
base itself cannot be served.

Data reuse: the series are published as open statistical data. Any
redistribution must retain the attribution "Source: Banco Central de
Reserva del Peru"; reproduction is permitted citing the source.

This module is original work; the parser below implements the published
JSON shape and does not derive from any third-party rate library.
"""

from .. import rate_providers as rp
from ..rate_providers import BaseHttpProvider, to_decimal


# BCRP statistical series API. Series PD04640PD is the interbank sol per
# US dollar sell rate; the JSON format carries the full available history
# in the ``periods`` array, oldest first.
_BCRP_URL = (
    "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
    "/PD04640PD/json"
)


class BcrpRateProvider(BaseHttpProvider):
    """Central Reserve Bank of Peru official PEN/USD sell rate, base USD.

    The feed publishes the sol against the dollar as "1 USD = value PEN",
    which is already the native direction this source needs (one US dollar
    expressed in the quoted currency). We walk the published periods from
    newest to oldest, take the first that carries a real number rather than
    the "n.d." non-business-day sentinel, and hand the one-entry table to
    the base class, which pivots it onto the company currency.
    """

    name = 'bcrp'
    native_base = 'USD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD expressed in units of code".

        The series carries only the sol against the dollar, so the native
        table holds at most a single PEN entry. Periods are walked from the
        last (newest) to the first (oldest); the first whose ``values[0]``
        parses via to_decimal wins. Non-business days publish "n.d.", which
        to_decimal rejects, so those rows are skipped. When no period yields
        a usable value the table is empty rather than an error, so the base
        class derivation decides whether the gap is fatal for the requested
        base.
        """
        payload = self._download_json(_BCRP_URL)
        if not isinstance(payload, dict):
            raise self._malformed("top-level JSON is not an object")
        periods = payload.get('periods')
        if not isinstance(periods, list):
            raise self._malformed("periods is not an array")

        native = {}
        for period in reversed(periods):
            if not isinstance(period, dict):
                continue
            values = period.get('values')
            if not isinstance(values, list) or not values:
                continue
            # values[0] is the published sell rate; "n.d." on a non-business
            # day fails to_decimal and we keep walking back.
            value = to_decimal(values[0])
            if value is None or value == 0:
                continue
            self._set_effective_date(period.get('name'))
            # Published as "1 USD = value PEN"; already the native direction
            # (one unit of native_base in units of the quote).
            native['PEN'] = value
            break
        return native

    def _malformed(self, detail):
        return rp.RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register = rp.register
register('bcrp', BcrpRateProvider,
         label="[PE] Central Reserve Bank of Peru", needs_key=False)
