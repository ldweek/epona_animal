# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Open ExchangeRate API broad-coverage source.

A free, multi-base aggregator that serves the requested base currency
directly, so no cross-derivation is needed: the feed already returns
"1 unit of base in units of code" for the base passed in the URL.

Characteristics:
* Broad coverage, roughly 160 currencies in a single response.
* Latest rates only; the free tier exposes no historical lookup, so
  ``on_date`` is accepted for interface symmetry but not honoured.
* Aggregated mid-market data, no operator API key required.

Because the API pivots to any requested base itself, this provider
overrides ``fetch`` rather than ``_fetch_native``, and never calls
``cross_derive``.
"""

from decimal import Decimal

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

# Latest-rates endpoint. The base currency is the final path segment;
# the response carries every supported currency under "rates".
_LATEST_URL = "https://open.er-api.com/v6/latest/%s"


class ErApiRateProvider(BaseHttpProvider):
    """Open ExchangeRate API latest-rates provider.

    The response is JSON of the shape::

        {"result": "success",
         "base_code": "USD",
         "rates": {"EUR": 0.9176, "GBP": 0.7980, ...}}

    Each ``rates`` value is already "1 unit of base_code in units of the
    quoted currency", which is exactly the contract this module expects,
    so the values pass straight through with no inversion or pivot.
    """

    name = 'erapi'
    native_base = None
    needs_key = False

    def fetch(self, base, quotes, on_date):
        self._start_fetch()
        # No key guard: the source is open. on_date is ignored because the
        # free tier serves latest rates only.
        base = (base or '').upper()
        wanted = {q.upper() for q in quotes}
        payload = self._download_json(_LATEST_URL % base)
        if not isinstance(payload, dict) or payload.get('result') != 'success':
            raise RateProviderError(
                "Open ExchangeRate API did not return a success result "
                "for base %s." % base
            )
        rates = payload.get('rates') or {}
        fixing = (
            payload.get('time_last_update_unix')
            or payload.get('time_last_update_utc')
        )
        if fixing:
            self._set_effective_date(fixing)
        out = {}
        for code in wanted:
            if code == base:
                continue
            dec = to_decimal(rates.get(code))
            if dec is not None:
                out[code] = dec
        return self._result(out)


register(
    'erapi',
    ErApiRateProvider,
    label="Open ExchangeRate API (broad coverage)",
    needs_key=False,
)
