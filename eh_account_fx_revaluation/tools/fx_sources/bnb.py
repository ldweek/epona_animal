# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bulgarian National Bank (Balgarska Narodna Banka) rate source.

The bank publishes a daily list of exchange rates for the convertible
currencies it tracks, expressed as foreign currency units per one euro.
Each row already reads "1 EUR = RATE CODE", normalised per single euro,
so for the native EUR base we take the published RATE directly with no
inversion and no quantity scaling.

Endpoint:
* https://www.bnb.bg/Statistics/StExternalSector/StExchangeRates/StERForeignCurrencies/?download=xml&lang=EN

The XML root is a flat ROWSET of ROW elements. The first ROW carries a
TITLE header and has no CODE child; it is skipped. Every data ROW has a
CODE and a RATE child, and the ROWSET has no XML namespace, so plain
find('CODE') / find('RATE') resolve.

The feed does not list the Bulgarian lev itself. The lev is pegged to the
euro by law at 1 EUR = 1.95583 BGN, so we inject that fixed entry after
parsing. This lets a company reporting in BGN be cross-derived against any
currency the euro table reaches.

Source: Bulgarian National Bank. Reuse of the published rates is permitted
with attribution to the Bulgarian National Bank as the source of the data.
This module is original work; the parser implements the published XML
shape and does not derive from any third-party rate library.
"""

from decimal import Decimal

from ..rate_providers import BaseHttpProvider, to_decimal, register

_FX_URL = (
    "https://www.bnb.bg/Statistics/StExternalSector/StExchangeRates/"
    "StERForeignCurrencies/?download=xml&lang=EN"
)

# The Bulgarian lev euro peg fixed by the currency board arrangement.
# 1 EUR = 1.95583 BGN. The feed never carries a BGN row, so we add it.
_BGN_EURO_PEG = '1.95583'


class BnbRateProvider(BaseHttpProvider):
    """Bulgarian National Bank daily rates, native base EUR.

    Each published RATE is already "1 EUR expressed in units of CODE", the
    exact direction the registry expects, so the native table copies RATE
    straight across. The base class pivots that table onto the company
    currency. The lev (BGN) euro peg is injected because the feed omits it.
    """

    name = 'bnb'
    native_base = 'EUR'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 EUR expressed in units of code".

        Walks the flat ROWSET. The header ROW has no CODE child and is
        skipped. Rows whose RATE is missing, unparseable, or zero are
        skipped rather than allowed to raise, so a single bad row never
        poisons the whole table. The fixed BGN euro peg is added last.
        """
        root = self._download_xml(_FX_URL)

        native = {}
        for row in root.iter('ROW'):
            code_el = row.find('CODE')
            if code_el is None or not (code_el.text or '').strip():
                # Header / title ROW carries no CODE; skip it.
                continue
            rate_el = row.find('RATE')
            rate = to_decimal(rate_el.text if rate_el is not None else None)
            if rate is None or rate == 0:
                continue
            date_el = row.find('CURR_DATE')
            if date_el is not None and date_el.text:
                self._set_effective_date(date_el.text)
            native[code_el.text.strip().upper()] = rate

        # The lev is not in the feed; inject its statutory euro peg so a
        # BGN-reporting company can be cross-derived.
        native['BGN'] = to_decimal(_BGN_EURO_PEG)
        return native


register('bnb', BnbRateProvider, label="[BG] Bulgarian National Bank",
         needs_key=False)
