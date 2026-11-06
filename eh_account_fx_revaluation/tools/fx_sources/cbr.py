# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank of Russia (Tsentralny Bank Rossiyskoy Federatsii) daily rates.

Native base is RUB. The CBR publishes a single XML document for the
latest business day at the endpoint below. The feed is declared as
windows-1251 and uses a decimal comma, both of which are handled
downstream: ElementTree honours the XML encoding declaration when given
bytes, and ``to_decimal`` normalises the comma.

Each ``<Valute>`` carries a ``<Nominal>`` (a quantity of the foreign
currency) and a ``<Value>`` (that quantity expressed in RUB). So the
feed states "Nominal units of CODE = Value RUB", which means
1 CODE = Value / Nominal RUB. Cross derivation wants the table keyed as
"1 RUB = X CODE", which is the inverse: X = Nominal / Value. We build the
native table directly in that direction and let the base class pivot it
onto the company currency.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


class CbrRateProvider(BaseHttpProvider):
    """Bank of Russia daily reference rates, native base RUB.

    The single daily document covers the latest published business day;
    the CBR does not expose a per-date series at this endpoint, so the
    ``on_date`` argument is accepted for interface symmetry but does not
    alter the request. Subclasses of BaseHttpProvider return the native
    table from ``_fetch_native``; the base class re-expresses it against
    the company currency through ``cross_derive``.
    """

    name = 'cbr'
    native_base = 'RUB'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 RUB expressed in units of CODE".

        The feed publishes the inverse direction (Nominal units of CODE
        cost Value RUB), so each entry is inverted to Nominal / Value. A
        row whose Value or Nominal cannot be parsed, or whose Value is
        zero, is skipped so one bad line never sinks the whole table.
        """
        root = self._download_xml(_CBR_DAILY_URL)
        self._set_effective_date(root.get('Date'))
        native = {}
        for valute in root.findall('Valute'):
            code_el = valute.find('CharCode')
            value_el = valute.find('Value')
            nominal_el = valute.find('Nominal')
            if code_el is None or code_el.text is None:
                continue
            code = code_el.text.strip().upper()
            value = to_decimal(value_el.text) if value_el is not None else None
            nominal = (
                to_decimal(nominal_el.text)
                if nominal_el is not None else None
            )
            if not value or not nominal:
                # None (unparseable) or zero on either side: a sane rate
                # cannot be derived, so drop the row.
                continue
            # Feed: Nominal CODE = Value RUB  ->  1 RUB = Nominal / Value CODE.
            native[code] = nominal / value
        return native


register('cbr', CbrRateProvider, label="Bank of Russia", needs_key=False)
