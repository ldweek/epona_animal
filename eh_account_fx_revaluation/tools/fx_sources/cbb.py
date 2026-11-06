# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Bank of Bahrain (CBB) Exchange Rates OpenAPI source.

CBB Exchange Rates OpenAPI lists ~34 currencies as units-per-USD; no API
key; date is DD-MON-YYYY and values are space-padded. BHD is USD-pegged
(0.376) so it falls out of the feed directly; a peg fallback exists in the
gulf-peg provider if the endpoint is down.

The feed is keyed against the US dollar: each row's ``UsCurr`` field is
"1 USD = UsCurr CODE" (units of CODE per one US dollar), which is exactly
the direction the native USD table expects, so values are taken as-is with
no inversion. The base class then cross-derives against the company
currency.

Endpoint:
* https://www.cbb.gov.bh/openapi/ExchangeRate

The endpoint mislabels its Content-Type header as text/html, but the body
is JSON, so ``_download_json`` parses the bytes regardless of header.
``UsCurr`` values are whitespace-padded strings; ``to_decimal`` trims them.
The ``BdCurr`` field (rate against the dinar) is ignored. A row whose
CurrCd is missing, or whose UsCurr parses to None or zero (the BHD self-row
may carry BdCurr 0), is skipped so one bad row never poisons the table.
"""

from ..rate_providers import (
    BaseHttpProvider,
    RateProviderError,
    register,
    to_decimal,
)

_CBB_URL = "https://www.cbb.gov.bh/openapi/ExchangeRate"


class CbbRateProvider(BaseHttpProvider):
    """Central Bank of Bahrain Exchange Rates OpenAPI, native base USD.

    The ``items`` list carries one row per currency. Each ``UsCurr`` value
    is "1 USD expressed in units of CurrCd", which is already the native
    USD direction, so the value is stored directly. Rows with a missing
    code or a missing, unparseable, or zero ``UsCurr`` are skipped.
    """

    name = 'cbb'
    native_base = 'USD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 USD expressed in units of code".

        Uses ``_download_json`` because the endpoint mislabels its
        Content-Type as text/html while the body is JSON. ``UsCurr`` is
        whitespace-padded, which ``to_decimal`` trims. Rows missing a
        currency code or carrying a None or zero ``UsCurr`` are skipped.
        """
        payload = self._download_json(_CBB_URL)
        items = (payload or {}).get('items')
        if not items:
            raise RateProviderError(
                "Central Bank of Bahrain feed carried no items."
            )

        native = {}
        for row in items:
            if not isinstance(row, dict):
                continue
            code = row.get('CurrCd')
            if not code:
                continue
            usd_per_unit = to_decimal(row.get('UsCurr'))
            if usd_per_unit is None or usd_per_unit == 0:
                continue
            if row.get('RateDt'):
                self._set_effective_date(row.get('RateDt'))
            native[code.upper()] = usd_per_unit
        return native


register('cbb', CbbRateProvider, label="Central Bank of Bahrain",
         needs_key=False)
