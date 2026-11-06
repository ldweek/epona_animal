# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank Negara Malaysia (BNM) Exchange Rate OpenAPI source.

BNM publishes a daily list of foreign-exchange reference rates against the
ringgit. The BNM OpenAPI is free for everyone to use and needs no API key,
but it requires the request to advertise the API version through an Accept
header: every call must send

    Accept: application/vnd.BNM.API.v1+json

The endpoint rejects requests that omit it, so the header is passed through
``_download_json`` rather than relying on the default. Reuse of the data
should carry the standard attribution to Bank Negara Malaysia as the source.

Endpoint:
* https://api.bnm.gov.my/public/exchange-rate

Each row in ``data`` carries a ``currency_code``, a ``unit`` (the number of
foreign units the quote is expressed per, e.g. 100 for the yen), and a
``rate`` object with ``buying_rate``, ``selling_rate``, and ``middle_rate``.
The middle rate is "1 unit-block of CODE = middle_rate MYR", so one unit of
CODE is ``middle_rate / unit`` MYR. The native base is the ringgit, so the
table is keyed as "1 MYR = unit / middle_rate CODE"; the base class then
cross-derives that table onto the company currency.

``middle_rate`` is preferred; a row that omits it (or carries null) falls
back to ``selling_rate``. Rows whose chosen rate or unit is missing,
unparseable, or zero are skipped so one bad row never poisons the table.

This module is original work; the parser implements the published JSON
shape and does not derive from any third-party rate library.
"""

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

_BNM_URL = "https://api.bnm.gov.my/public/exchange-rate"
# BNM rejects requests that do not advertise the API version it serves.
_BNM_ACCEPT = "application/vnd.BNM.API.v1+json"


class BnmRateProvider(BaseHttpProvider):
    """Bank Negara Malaysia exchange-rate OpenAPI, native base MYR.

    The feed quotes the middle rate as "1 ``unit`` units of CODE = rate
    MYR". One unit of CODE is therefore ``rate / unit`` MYR, so the
    native MYR table reads "1 MYR = unit / rate CODE". The ``unit``
    multiplier matters: the yen, for example, is quoted per 100 units.
    Rows with a missing code, or a missing, unparseable, or zero rate or
    unit, are skipped.
    """

    name = 'bnm'
    native_base = 'MYR'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 MYR expressed in units of code".

        The Accept header advertising the API version is mandatory, so it
        is passed through ``_download_json``. The chosen rate is the
        ``middle_rate``, falling back to ``selling_rate`` when the middle
        rate is missing or null. With ``rate`` MYR per ``unit`` units of
        CODE, one unit of CODE is ``rate / unit`` MYR, so the native MYR
        entry is the inverse, ``unit / rate``.
        """
        payload = self._download_json(
            _BNM_URL, headers={'Accept': _BNM_ACCEPT},
        )
        rows = (payload or {}).get('data')
        if not isinstance(rows, list):
            raise RateProviderError(
                "Bank Negara Malaysia feed carried no data list."
            )

        native = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = row.get('currency_code')
            if not code:
                continue
            rate_block = row.get('rate')
            if not isinstance(rate_block, dict):
                continue
            quote = rate_block.get('middle_rate')
            if quote is None:
                quote = rate_block.get('selling_rate')
            myr_per_block = to_decimal(quote)
            unit = to_decimal(row.get('unit'))
            if (myr_per_block is None or myr_per_block == 0
                    or unit is None or unit == 0):
                continue
            if rate_block.get('date'):
                self._set_effective_date(rate_block.get('date'))
            # 1 unit of CODE = myr_per_block / unit MYR, so in the MYR
            # native direction: 1 MYR = unit / myr_per_block CODE.
            native[code.upper()] = unit / myr_per_block
        return native


register('bnm', BnmRateProvider, label="[MY] Bank Negara Malaysia",
         needs_key=False)
