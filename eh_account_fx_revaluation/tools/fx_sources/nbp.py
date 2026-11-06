# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
National Bank of Poland (Narodowy Bank Polski) rate source.

The bank publishes "table A", a daily list of the average (mid) rates
for roughly 33 convertible currencies, expressed as zloty per one unit
of the foreign currency. The JSON endpoint serves the latest
publication; we read its newest table and invert each quote so the
native table reads "1 PLN in units of code", which the base class then
cross-derives against the company currency.

Endpoint:
* https://api.nbp.pl/api/exchangerates/tables/A?format=json

The feed publishes business-day data only; weekend and Polish holiday
requests resolve to the most recent published table, which matches the
provider contract of "the rate on or before this date".
"""

from decimal import Decimal

from ..rate_providers import BaseHttpProvider, to_decimal, register

_TABLE_A_URL = "https://api.nbp.pl/api/exchangerates/tables/A?format=json"


class NbpRateProvider(BaseHttpProvider):
    """National Bank of Poland table A mid rates, native base PLN.

    The upstream ``mid`` field is "1 unit of code = mid PLN", the inverse
    of the direction the registry expects. We invert each value so the
    native table is keyed as "1 PLN = 1/mid code"; the base class pivots
    that table onto the company currency.
    """

    name = 'nbp'
    native_base = 'PLN'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 PLN expressed in units of code".

        Table A carries the convertible currencies as ``mid`` values in
        zloty per foreign unit, so each entry is inverted. Rows whose mid
        is missing, unparseable, or zero are skipped rather than allowed
        to raise, so a single bad row never poisons the whole table.
        """
        payload = self._download_json(_TABLE_A_URL)
        try:
            table = payload[0]
            rates = table['rates']
        except (KeyError, IndexError, TypeError) as exc:
            raise self._malformed(exc)
        if not isinstance(rates, list):
            raise self._malformed("rates is not a list")
        self._set_effective_date(table.get('effectiveDate'))

        native = {}
        for row in rates:
            try:
                code = row['code']
                mid = to_decimal(row['mid'])
            except (KeyError, TypeError):
                continue
            if not code or mid is None or mid == 0:
                continue
            native[code.upper()] = Decimal(1) / mid
        return native

    def _malformed(self, detail):
        from ..rate_providers import RateProviderError
        return RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register('nbp', NbpRateProvider, label="National Bank of Poland",
         needs_key=False)
