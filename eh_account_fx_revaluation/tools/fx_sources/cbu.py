# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Bank of the Republic of Uzbekistan daily rates.

Native base is UZS. The bank publishes a single JSON array for the
latest archive day at the endpoint below, one object per currency. The
feed is open government data; reuse carries the attribution requirement
"Source: Central Bank of the Republic of Uzbekistan".

Each object carries a ``Nominal`` (a quantity of the foreign currency)
and a ``Rate`` (that quantity expressed in soms). So the feed states
"Nominal units of CODE = Rate UZS", which means 1 CODE = Rate / Nominal
UZS. Cross derivation wants the table keyed as "1 UZS = X CODE", which is
the inverse: X = Nominal / Rate. We build the native table directly in
that direction and let the base class pivot it onto the company
currency. The ``Nominal`` is not always 1 (for example JPY is quoted per
100 units), so it must be read per row rather than assumed.

Endpoint:
* https://cbu.uz/en/arkhiv-kursov-valyut/json/

The feed publishes business-day data only; weekend and Uzbek holiday
requests resolve to the most recent published archive, which matches the
provider contract of "the rate on or before this date".
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_CBU_DAILY_URL = "https://cbu.uz/en/arkhiv-kursov-valyut/json/"


class CbuRateProvider(BaseHttpProvider):
    """Central Bank of Uzbekistan daily reference rates, native base UZS.

    The single archive document covers the latest published business day;
    the ``on_date`` argument is accepted for interface symmetry but does
    not alter the request. Subclasses of BaseHttpProvider return the
    native table from ``_fetch_native``; the base class re-expresses it
    against the company currency through ``cross_derive``.
    """

    name = 'cbu'
    native_base = 'UZS'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 UZS expressed in units of CODE".

        The feed publishes the inverse direction (Nominal units of CODE
        cost Rate UZS), so each entry is inverted to Nominal / Rate. A row
        whose Rate or Nominal cannot be parsed, or whose Rate is zero, is
        skipped so one bad line never sinks the whole table. Nominal is
        read per row because it is not always 1.
        """
        payload = self._download_json(_CBU_DAILY_URL)
        if not isinstance(payload, list):
            raise self._malformed("top-level JSON is not an array")

        native = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            code = row.get('Ccy')
            rate = to_decimal(row.get('Rate'))
            nominal = to_decimal(row.get('Nominal'))
            if not code or not rate or not nominal:
                # None (unparseable) or zero on either side: a sane rate
                # cannot be derived, so drop the row.
                continue
            if row.get('Date'):
                self._set_effective_date(row.get('Date'))
            # Feed: Nominal CODE = Rate UZS  ->  1 UZS = Nominal / Rate CODE.
            native[code.strip().upper()] = nominal / rate
        return native

    def _malformed(self, detail):
        from ..rate_providers import RateProviderError
        return RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register('cbu', CbuRateProvider,
         label="[UZ] Central Bank of Uzbekistan", needs_key=False)
