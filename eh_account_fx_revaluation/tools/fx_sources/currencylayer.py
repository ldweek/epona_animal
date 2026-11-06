# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Currencylayer aggregator source.

Native base USD. The free tier always quotes against the US dollar
regardless of the requested base, so this provider returns a USD-native
table and lets the base class pivot it onto the company currency through
``cross_derive``. An operator API key (the access_key parameter) is
required, so the provider registers with needs_key=True and the base
class guard rejects a fetch with no key.

The live endpoint serves JSON of the shape::

    {"success": true,
     "source": "USD",
     "quotes": {"USDEUR": 0.9176, "USDGBP": 0.7980, ...}}

Each quotes key is the literal source code 'USD' concatenated with the
quote code, and each value is already "1 USD in units of the quote
currency". That matches the native-base direction this module expects, so
the values pass through with no inversion; only the source prefix is
stripped to recover the bare ISO code.

A request that the API rejects comes back as ``{"success": false,
"error": {"code": ..., "info": ...}}``; we raise RateProviderError with
the upstream info string rather than return an empty table, so the cron
surfaces the outage.
"""

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    build_url,
    register,
    to_decimal,
)

# Live-rates endpoint. The access_key carries the operator key and the
# currencies parameter is a comma-joined list of the codes we want quoted.
# The free tier ignores any source override and always quotes USD-base.
_LIVE_URL = "https://api.currencylayer.com/live"


class CurrencylayerRateProvider(BaseHttpProvider):
    """Currencylayer live-rates provider.

    Base currency is USD. The base class drives the network seam, the
    API-key guard, and the cross-derivation; this class only resolves the
    USD-source quotes for the requested codes and exposes them as the
    native-base table "1 USD in units of code".
    """

    name = 'currencylayer'
    native_base = 'USD'
    needs_key = True

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD in units of code".

        The company base is folded into the requested currency list so the
        base class can pivot off it during cross-derivation; without the
        base row a non-USD company would have nothing to divide through.
        The on_date is accepted for interface symmetry but not honoured:
        the live endpoint serves latest rates only.
        """
        source = self.native_base
        wanted = {q.upper() for q in quotes}
        wanted.add(base.upper())
        # Comma-joined, sorted, upper-cased so the request is deterministic.
        csv = ','.join(sorted(c.upper() for c in wanted))
        payload = self._download_json(build_url(_LIVE_URL, {
            'access_key': self.api_key,
            'currencies': csv,
        }))

        if not isinstance(payload, dict) or not payload.get('success'):
            error = payload.get('error') if isinstance(payload, dict) else None
            info = (error or {}).get('info') if isinstance(error, dict) else None
            raise RateProviderError(
                "Currencylayer request failed: %s"
                % (info or "no detail returned by the source.")
            )

        # The API echoes the source it quoted against; honour it rather than
        # assuming USD so a future tier change does not silently mis-strip.
        source = (payload.get('source') or source).upper()
        quotes_map = payload.get('quotes') or {}
        if payload.get('timestamp'):
            self._set_effective_date(payload.get('timestamp'))

        native = {}
        for key, value in quotes_map.items():
            code = key.upper()
            # Each key is the source code concatenated with the quote code,
            # e.g. "USDEUR". Strip the source prefix to recover the ISO code.
            if code.startswith(source):
                code = code[len(source):]
            if not code:
                continue
            dec = to_decimal(value)
            if dec is None:
                continue
            # Value is already "1 USD = dec code", the native-base direction.
            native[code] = dec

        return native


register(
    'currencylayer',
    CurrencylayerRateProvider,
    label="Currencylayer",
    needs_key=True,
)
