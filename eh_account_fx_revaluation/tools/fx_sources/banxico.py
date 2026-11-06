# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank of Mexico (Banco de Mexico) FIX rate source.

The bank publishes the official MXN/USD FIX, the daily reference rate it
determines for settling US dollar obligations payable in Mexico. The
Banxico SIE API serves the most recent observation of series SF43718, the
FIX expressed as Mexican pesos per one US dollar. We read that single
observation and key the native table as "1 USD in units of MXN", which
the base class then cross-derives against the company currency.

Endpoint:
* https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718/datos/oportuno

Auth:
* An operator API token is required. Request a free token from the Banxico
  SIE API portal and send it on every call through the HTTP request header
  ``Bmx-Token: <token>``. The base class key guard raises before any
  network call when the token is missing.

The feed publishes business-day data only; weekend and Mexican holiday
requests resolve to the most recent published observation, which matches
the provider contract of "the rate on or before this date".

Source: Banco de Mexico. Reuse of the published series is permitted with
attribution to Banco de Mexico as the originating source.
"""

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

# Most-recent observation of the FIX series (SF43718), JSON format.
_FIX_URL = (
    "https://www.banxico.org.mx/SieAPIRest/service/v1/series/"
    "SF43718/datos/oportuno"
)


class BanxicoRateProvider(BaseHttpProvider):
    """Banco de Mexico FIX rate, native base USD, keyed.

    The response is JSON of the shape::

        {"bmx": {"series": [{"idSerie": "SF43718",
                             "datos": [{"fecha": "23/06/2026",
                                        "dato": "17.0925"}]}]}}

    The ``dato`` value is the FIX, "1 USD in units of MXN", which is the
    native-base direction the base class re-expresses against the company
    currency, so the value passes through with no inversion. An error
    response replaces that body with an ``error`` envelope carrying a
    ``mensaje`` we relay verbatim through RateProviderError so the cron
    chatter records the upstream reason (a bad token, a service outage).
    """

    name = 'banxico'
    native_base = 'USD'
    needs_key = True

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD in units of code".

        The series carries a single observation, the MXN/USD FIX, so the
        native table holds at most the one MXN entry. The token travels in
        the ``Bmx-Token`` request header. on_date is accepted for interface
        symmetry but not honoured: the endpoint serves the most recent
        observation only. A value that does not parse is skipped so a bad
        observation yields an empty table rather than a raw error.
        """
        headers = {'Bmx-Token': self.api_key}
        payload = self._download_json(_FIX_URL, headers=headers)

        if not isinstance(payload, dict):
            raise RateProviderError(
                "Banco de Mexico returned an unexpected payload shape."
            )

        # The error envelope is a hard failure: relay the human message so
        # the cron surfaces the upstream reason rather than a parse gap.
        error = payload.get('error')
        if isinstance(error, dict):
            reason = error.get('mensaje') or error.get('detail') \
                or "unspecified error"
            raise RateProviderError(
                "Banco de Mexico error: %s" % reason
            )

        try:
            series = payload['bmx']['series']
            datos = series[0]['datos']
        except (KeyError, IndexError, TypeError):
            # No series or no observations: an empty table, per the
            # contract, so the caller surfaces the gap.
            return {}

        if not datos:
            return {}

        # The most recent observation is the only one returned; take it.
        try:
            observation = datos[0]
            dato = observation['dato']
        except (KeyError, IndexError, TypeError):
            return {}

        fix = to_decimal(dato)
        if fix is None or fix == 0:
            return {}

        self._set_effective_date(observation.get('fecha'))

        return {'MXN': fix}


register(
    'banxico',
    BanxicoRateProvider,
    label="[MX] Bank of Mexico",
    needs_key=True,
)
