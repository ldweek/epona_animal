# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Fixer.io foreign-exchange rate source.

Fixer is a keyed aggregator. The free tier publishes against a fixed
EUR base; the operator supplies an API key on the FX rate configuration.
Each ``rates`` value is "1 EUR expressed in units of the quoted
currency", which is exactly the direction the registry expects, so the
values pass straight through with no inversion. The native base is EUR
and the base class pivots the table onto the company currency.

Endpoint:
* https://data.fixer.io/api/latest?access_key={KEY}&symbols={CSV}

The symbol list includes the company base alongside the wanted quotes so
the cross derivation has the EUR/base leg available when the company is
not on EUR. The response is JSON::

    {"success": true, "timestamp": ..., "base": "EUR",
     "date": "2026-05-01", "rates": {"USD": 1.0800, "GBP": 0.8500}}

On a feed-level problem Fixer returns ``"success": false`` with an
``error`` object; we raise the published ``info`` string so the caller
surfaces the real cause (bad key, exhausted quota) rather than a blank
table. The base class fetch() already calls _require_key() before
_fetch_native, so a missing key raises without a duplicate guard here.
"""

from decimal import Decimal  # noqa: F401  documents the return element type

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    build_url,
    register,
    to_decimal,
)

_LATEST_URL = "https://data.fixer.io/api/latest"


class FixerRateProvider(BaseHttpProvider):
    """Fixer.io latest rates, native base EUR, operator API key required.

    Each upstream ``rates`` value is already "1 EUR = value code", the
    direction the registry expects, so the values pass through without
    inversion. The base class cross-derives the EUR table onto the
    company currency.
    """

    name = 'fixer'
    native_base = 'EUR'
    needs_key = True

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 EUR expressed in units of code".

        The symbol list is the company base plus the wanted quotes,
        upper-cased and de-duplicated, so the cross derivation can pivot
        through EUR when the company currency is not EUR itself. Rows
        whose value is missing or unparseable are skipped so a single bad
        entry never poisons the whole table.
        """
        base = (base or '').upper()
        wanted = {q.upper() for q in quotes}
        symbols = sorted(wanted | {base})
        url = build_url(_LATEST_URL, {
            'access_key': self.api_key,
            'symbols': ','.join(symbols),
        })

        payload = self._download_json(url)
        if not isinstance(payload, dict) or not payload.get('success'):
            # Fixer reports failures in an error object; surface its
            # human-readable info string so the cause is visible.
            error = payload.get('error') if isinstance(payload, dict) else None
            info = (error or {}).get('info') if isinstance(error, dict) else None
            raise RateProviderError(
                "Fixer.io rejected the request: %s" % (info or "unknown error")
            )

        rates = payload.get('rates')
        if not isinstance(rates, dict):
            raise RateProviderError(
                "Fixer.io returned no rates for base EUR."
            )
        self._set_effective_date(payload.get('date'))

        native = {}
        for code, value in rates.items():
            dec = to_decimal(value)
            if not code or dec is None:
                continue
            native[code.upper()] = dec
        return native


register('fixer', FixerRateProvider, label="Fixer.io", needs_key=True)
