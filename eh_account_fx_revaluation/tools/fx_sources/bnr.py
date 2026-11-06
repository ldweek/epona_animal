# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
National Bank of Romania (Banca Nationala a Romaniei) daily rates.

Native base is RON. The bank publishes a single XML document for the
latest business day at the endpoint below. The reference rates sit under
DataSet/Body/Cube, one <Rate> per currency, declared in the
http://www.bnr.ro/xsd namespace.

Each <Rate> states "multiplier units of CODE = Rate RON", where the
multiplier attribute is optional and defaults to 1 (it is present only
for low-value currencies such as JPY, quoted per 100). So the feed reads
1 CODE = Rate / multiplier RON. The cross derivation wants the table
keyed as "1 RON = X CODE", which is the inverse: X = multiplier / Rate.
We build the native table directly in that direction and let the base
class pivot it onto the company currency.

The document mixes the BNR xsd namespace into every element, so we parse
it namespace-agnostically: each element is matched on its local tag name
(the part after the closing brace), so a change of namespace URI or
prefix on the BNR side does not silently break the parser.

Source feed:
* https://curs.bnr.ro/nbrfxrates.xml

Data reuse: the reference rates are sourced from the National Bank of
Romania and must be attributed as such. This module is original work;
the parser implements the published XML shape and does not derive from
any third-party rate library.

Source: National Bank of Romania.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_BNR_DAILY_URL = "https://curs.bnr.ro/nbrfxrates.xml"


def _local(tag):
    """Return the local part of an XML tag, dropping any namespace.

    ElementTree renders a namespaced tag as '{uri}localname'. Splitting
    on the closing brace yields the bare local name and lets the parser
    ignore which namespace URI or prefix the feed happens to use.
    """
    return tag.split('}')[-1] if tag else tag


def _iter_rate_elements(root):
    """Yield every element whose local tag name is 'Rate'.

    The BNR feed nests each rate under DataSet/Body/Cube, but we walk the
    whole tree and match on the local name so the parser does not depend
    on the exact ancestry or namespace the feed ships today.
    """
    for elem in root.iter():
        if _local(elem.tag) == 'Rate':
            yield elem


class BnrRateProvider(BaseHttpProvider):
    """National Bank of Romania daily reference rates, native base RON.

    The single daily document covers the latest published business day;
    the BNR does not expose a per-date series at this endpoint, so the
    ``on_date`` argument is accepted for interface symmetry but does not
    alter the request. The feed publishes the inverse direction
    (multiplier units of CODE cost Rate RON), so ``_fetch_native``
    inverts each entry to "1 RON = multiplier / Rate CODE" and the base
    class re-expresses that table against the company currency.
    """

    name = 'bnr'
    native_base = 'RON'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 RON expressed in units of CODE".

        Each <Rate> carries a ``currency`` attribute and an optional
        ``multiplier`` attribute (defaulting to 1). The text is RON per
        ``multiplier`` units of the currency, so 1 RON = multiplier / Rate
        units of CODE. A row whose currency is missing, or whose Rate is
        unparseable or zero, is skipped so one bad line never sinks the
        whole table.
        """
        root = self._download_xml(_BNR_DAILY_URL)
        for elem in root.iter():
            if _local(elem.tag) == 'Cube' and elem.get('date'):
                self._set_effective_date(elem.get('date'))
                break
        native = {}
        for rate_el in _iter_rate_elements(root):
            code = (rate_el.get('currency') or '').strip().upper()
            if not code:
                continue
            rate = to_decimal(rate_el.text)
            if not rate:
                # None (unparseable) or zero: no sane rate can be derived.
                continue
            multiplier = to_decimal(rate_el.get('multiplier') or '1')
            if not multiplier:
                multiplier = to_decimal('1')
            # Feed: multiplier CODE = Rate RON  ->  1 RON = multiplier / Rate CODE.
            native[code] = multiplier / rate
        return native


register('bnr', BnrRateProvider, label="[RO] National Bank of Romania",
         needs_key=False)
