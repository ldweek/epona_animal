# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Frankfurter foreign-exchange rate source.

Frankfurter is a free, no-key API that serves the European Central Bank
reference rates against any supported base currency, with daily history
back to 1999. Coverage is the roughly 30 currencies the ECB publishes
(major Europe, North America, Asia-Pacific, plus a handful of others).

Unlike the central-bank feeds, this is a multi-base source: the API
accepts the requested base as a query parameter and returns rates that
already mean "1 base = value quote". There is no native base to pivot
through, so this provider overrides ``fetch`` and never calls
``cross_derive``. ``native_base`` is therefore None.

Endpoint shape:

    https://api.frankfurter.dev/v1/{date}?base={BASE}&symbols={CSV}

where ``{date}`` is the ISO date for a historical lookup or the literal
string ``latest`` for the most recent publication. The response is JSON:

    {"amount": 1.0, "base": "USD", "date": "2026-05-01",
     "rates": {"EUR": 0.9176, "GBP": 0.7980}}

An unsupported base produces an error object with no ``rates`` key; we
treat the absence of that key as a hard failure so the caller surfaces
the misconfiguration rather than silently returning nothing.
"""

import datetime
from decimal import Decimal  # noqa: F401  documents the return element type

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    build_url,
    register,
    to_decimal,
)

_BASE_URL = "https://api.frankfurter.dev/v1/%s"


class FrankfurterRateProvider(BaseHttpProvider):
    """ECB reference rates served against any supported base.

    Multi-base source: the API does the pivot for us, so ``fetch`` builds
    the URL for the requested base, downloads the JSON, and returns the
    requested quotes directly. No API key is required.
    """

    name = 'frankfurter'
    native_base = None
    needs_key = False

    def fetch(self, base, quotes, on_date):
        self._start_fetch()
        # No key required, but call the guard for symmetry with keyed
        # providers; it is a no-op while needs_key is False.
        self._require_key()
        base = (base or '').upper()
        wanted = {q.upper() for q in quotes}
        # The base never needs quoting against itself.
        symbols = sorted(wanted - {base})
        if not symbols:
            return self._result({})

        # 'latest' for today (or any future date the caller might pass),
        # an ISO date for a genuine historical lookup.
        today = datetime.date.today()
        when = on_date.isoformat() if on_date < today else 'latest'
        url = build_url(_BASE_URL % when, {
            'base': base,
            'symbols': ','.join(symbols),
        })

        payload = self._download_json(url)
        rates = payload.get('rates')
        if not isinstance(rates, dict):
            # Frankfurter returns an error object (no 'rates' key) for an
            # unsupported base or symbol set; treat it as a feed failure.
            raise RateProviderError(
                "Frankfurter returned no rates for base %s on %s." % (
                    base, when,
                )
            )
        self._set_effective_date(payload.get('date'))

        out = {}
        for code in wanted:
            if code == base:
                continue
            dec = to_decimal(rates.get(code))
            if dec is not None:
                out[code] = dec
        return self._result(out)


register(
    'frankfurter',
    FrankfurterRateProvider,
    label="Frankfurter (ECB data, any base)",
    needs_key=False,
)
