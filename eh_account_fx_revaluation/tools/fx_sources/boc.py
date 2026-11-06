# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank of Canada daily FX rate provider.

The Bank of Canada publishes a daily series of foreign-exchange rates
through its Valet observations API. The FX_RATES_DAILY group carries
around 26 currencies quoted on Canadian business days; the bank does not
publish on weekends or Canadian holidays. We pull the latest observation
and use it for the daily revaluation cron.

Each series key follows the pattern FX<XXX>CAD where XXX is the foreign
currency, and the value is "1 unit of XXX expressed in CAD" (CAD per one
unit of the foreign currency). Our native table is published against the
native base CAD as "1 CAD in units of code", so we invert each published
value: native[XXX] = 1 / (CAD per XXX). The base class then cross-derives
against the company currency.
"""

from decimal import Decimal

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

_BOC_URL = (
    "https://www.bankofcanada.ca/valet/observations/group/"
    "FX_RATES_DAILY/json?recent=1"
)


class BocRateProvider(BaseHttpProvider):
    """Bank of Canada Valet daily foreign-exchange rates.

    Native base is CAD. The Valet group feed returns a list of dated
    observations; with recent=1 the list holds the single latest
    business day, but we defensively take the last element so an
    upstream change that returns several days still resolves to the
    newest one.
    """

    name = 'boc'
    native_base = 'CAD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as '1 CAD in units of code'.

        The Valet feed quotes CAD per one unit of the foreign currency,
        so each published value is inverted to express the native CAD
        base. Series whose value is missing, unparseable, or zero are
        skipped so one bad row never poisons the whole table.
        """
        payload = self._download_json(_BOC_URL)
        observations = (payload or {}).get('observations')
        if not observations:
            raise RateProviderError(
                "Bank of Canada feed carried no observations."
            )
        latest = observations[-1]
        self._set_effective_date(latest.get('d'))

        native = {}
        for key, cell in latest.items():
            # Series keys look like 'FXUSDCAD'; the date field 'd' and any
            # other metadata are skipped. We want a five-letter middle that
            # starts with 'FX', ends with 'CAD', and yields a 3-letter code.
            if not key.startswith('FX') or not key.endswith('CAD'):
                continue
            if len(key) != 8:
                continue
            code = key[2:5]
            if code == 'CAD':
                continue
            cad_per_unit = to_decimal((cell or {}).get('v'))
            if cad_per_unit is None or cad_per_unit == 0:
                continue
            native[code] = Decimal(1) / cad_per_unit
        return native


register('boc', BocRateProvider, label="Bank of Canada", needs_key=False)
