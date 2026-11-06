# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Czech National Bank (Ceska narodni banka) rate source.

The bank publishes a daily list of central (mid) rates for the
convertible currencies it tracks, expressed as koruna per a stated
number of units of the foreign currency. The JSON endpoint serves the
latest publication; we read its rate list and re-express each quote as
"1 CZK in units of code", which the base class then cross-derives
against the company currency.

Each row carries an ``amount`` field stating how many foreign units the
``rate`` is quoted for (commonly 1, but 100 for low-value currencies
such as the Japanese yen). A row is therefore "amount units of code =
rate CZK", so "1 code = rate/amount CZK" and, native-base CZK,
"1 CZK = amount/rate code". Honouring ``amount`` is essential; treating
every rate as a per-unit figure would mis-scale those currencies.

Endpoint:
* https://api.cnb.cz/cnbapi/exrates/daily?lang=EN

The feed publishes business-day data only; weekend and Czech holiday
requests resolve to the most recent published table, which matches the
provider contract of "the rate on or before this date".

Source: the Czech National Bank. Their terms permit reuse of the
published exchange-rate data provided the source is attributed; this
module surfaces that attribution requirement so deployments quote the
Czech National Bank as the origin of the rates.
"""

from decimal import Decimal

from ..rate_providers import BaseHttpProvider, to_decimal, register

_DAILY_URL = "https://api.cnb.cz/cnbapi/exrates/daily?lang=EN"


class CnbRateProvider(BaseHttpProvider):
    """Czech National Bank daily central rates, native base CZK.

    Each upstream row reads "``amount`` units of ``currencyCode`` =
    ``rate`` CZK". Native base is CZK, so each entry becomes
    "1 CZK = amount/rate code". Rows whose rate or amount is missing,
    unparseable, or zero are skipped rather than allowed to raise, so a
    single bad row never poisons the whole table.
    """

    name = 'cnb'
    native_base = 'CZK'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 CZK expressed in units of code".

        The feed quotes ``rate`` CZK per ``amount`` units of the foreign
        currency, so the CZK-native value is ``amount / rate``. Mind that
        ``amount`` is not always 1 (it is 100 for the yen and similar);
        scaling by it is what keeps those currencies correct.
        """
        payload = self._download_json(_DAILY_URL)
        try:
            rows = payload['rates']
        except (KeyError, TypeError) as exc:
            raise self._malformed(exc)
        if not isinstance(rows, list):
            raise self._malformed("rates is not a list")
        table_fixing = (
            payload.get('validFor') or payload.get('date')
            or payload.get('day')
        )
        if table_fixing:
            self._set_effective_date(table_fixing)

        native = {}
        for row in rows:
            try:
                code = row['currencyCode']
                amount = to_decimal(row['amount'])
                rate = to_decimal(row['rate'])
            except (KeyError, TypeError):
                continue
            # The live CNB API carries ``validFor`` on every row rather
            # than at the response root.  An undated row is not safe to
            # merge unless a table-level fixing date covers it.
            row_fixing = row.get('validFor')
            if row_fixing:
                self._set_effective_date(row_fixing)
            elif not self.effective_date:
                continue
            if not code or amount is None or amount == 0:
                continue
            if rate is None or rate == 0:
                continue
            native[code.upper()] = amount / rate
        return native

    def _malformed(self, detail):
        from ..rate_providers import RateProviderError
        return RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register('cnb', CnbRateProvider, label="[CZ] Czech National Bank",
         needs_key=False)
