# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Open Exchange Rates aggregator source.

Native base USD on the free tier: the latest.json endpoint always quotes
against the US dollar regardless of the company currency, so this is a
single-base feed that the base class re-expresses against whatever base
the caller wants. Because of that this provider implements _fetch_native
and never calls cross_derive itself.

An operator API key (the app_id) is required. The base class key guard
raises before any network call when the key is missing.

Each rate is published as "1 USD in units of the listed code", which is
already the native-base direction the cross derivation expects, so the
values pass straight through with no inversion. We send an explicit
symbols list (the base plus the wanted quotes, upper-cased and sorted) so
the response stays small and the base currency is always present for the
pivot.

An error response carries an "error" flag and a human "description"; we
surface that text through RateProviderError so the cron chatter records
the upstream reason (a bad key, an exhausted plan) rather than a generic
parse failure.
"""

from decimal import Decimal

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    build_url,
    register,
    to_decimal,
)

# Latest-rates endpoint. The app_id carries the operator key; symbols is a
# comma-joined CSV that restricts the response to the currencies we need.
_LATEST_URL = "https://openexchangerates.org/api/latest.json"


class OxrRateProvider(BaseHttpProvider):
    """Open Exchange Rates latest-rates provider, native base USD.

    The response is JSON of the shape::

        {"base": "USD",
         "rates": {"EUR": 0.9176, "GBP": 0.7980, "AUD": 1.5200}}

    Each rates value is already "1 USD in units of the quoted code", which
    is the native-base direction the base class re-expresses against the
    company currency, so the values pass through with no inversion. An
    error response replaces that body with an error flag and a description
    we relay verbatim.
    """

    name = 'oxr'
    native_base = 'USD'
    needs_key = True

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD in units of code".

        The symbols list is the base plus the wanted quotes, upper-cased
        and de-duplicated so the dollar base of any pivot is always carried
        and the request stays minimal. on_date is accepted for interface
        symmetry but not honoured: the free tier serves latest rates only.
        A rate that does not parse is skipped so a single bad row does not
        fail the whole feed.
        """
        wanted = {(base or '').upper(), *(q.upper() for q in quotes)}
        symbols = ','.join(sorted(wanted))
        payload = self._download_json(build_url(_LATEST_URL, {
            'app_id': self.api_key,
            'symbols': symbols,
        }))

        if not isinstance(payload, dict):
            raise RateProviderError(
                "Open Exchange Rates returned an unexpected payload shape."
            )
        if payload.get('error'):
            # The description is the human-readable reason (bad app_id,
            # plan limit). Fall back to the terse message when absent.
            reason = payload.get('description') or payload.get('message') \
                or "unspecified error"
            raise RateProviderError(
                "Open Exchange Rates error: %s" % reason
            )

        rates = payload.get('rates') or {}
        if payload.get('timestamp'):
            self._set_effective_date(payload.get('timestamp'))
        native = {}
        for code, value in rates.items():
            dec = to_decimal(value)
            if dec is None:
                continue
            native[code.upper()] = dec
        return native


register(
    'oxr',
    OxrRateProvider,
    label="Open Exchange Rates",
    needs_key=True,
)
