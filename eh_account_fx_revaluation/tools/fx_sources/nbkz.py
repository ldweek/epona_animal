# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
National Bank of Kazakhstan (Qazaqstan Ulttyq Banki) daily rates.

Native base is KZT. The bank publishes its official rates as an RSS 2.0
document at the endpoint below. Each ``<item>`` carries a ``<title>``
(the ISO currency code), a ``<description>`` (a tenge figure), and a
``<quant>`` (the quantity of foreign units that figure prices). So the
feed states "quant units of CODE = description KZT", which means
1 CODE = description / quant KZT. Cross derivation wants the table keyed
as "1 KZT = X CODE", which is the inverse: X = quant / description. We
build the native table directly in that direction and let the base class
pivot it onto the company currency.

The ``<quant>`` element is not always 1: minor or low-value currencies
are quoted per 10 or per 100 units, so the quantity must be honoured or
the derived rate is off by an order of magnitude.

Source feed:
* https://nationalbank.kz/rss/rates_all.xml

We parse the feed namespace-agnostically: every element is matched on its
local tag name (the part after the closing brace) so a namespaced
``quant`` element, or a change of namespace URI or prefix on the bank's
side, does not silently break the parser.

Source: National Bank of Kazakhstan. The feed is published for reuse; the
data carries the source's attribution requirement, so any redistribution
must cite the National Bank of Kazakhstan as the originator.

This module is original work; the parser implements the published RSS
shape and does not derive from any third-party rate library.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_NBKZ_FEED_URL = "https://nationalbank.kz/rss/rates_all.xml"


def _local(tag):
    """Return the local part of an XML tag, dropping any namespace.

    ElementTree renders a namespaced tag as '{uri}localname'. Splitting
    on the closing brace yields the bare local name and lets the parser
    ignore which namespace URI or prefix the feed happens to use.
    """
    return tag.split('}')[-1] if tag else tag


def _iter_items(root):
    """Yield every element whose local tag name is 'item'.

    The feed nests each rate under channel/item, but we walk the whole
    tree and match on the local name so the parser does not depend on the
    exact ancestry the feed ships today.
    """
    for elem in root.iter():
        if _local(elem.tag) == 'item':
            yield elem


def _read_item(item):
    """Extract (code, description, quant, fixing_date) from one item.

    Reads the title (ISO code), the description (a tenge figure), and the
    quant (the quantity of foreign units that figure prices). Any of the
    three may be absent; a missing field comes back as None and the caller
    skips the incomplete row. The quant element may itself be namespaced,
    so it is matched on its local name like every other field.
    """
    code = None
    description = None
    quant = None
    fixing_date = None
    for child in item.iter():
        local = _local(child.tag)
        if local == 'title' and child.text:
            code = child.text.strip()
        elif local == 'description' and child.text:
            description = child.text.strip()
        elif local == 'quant' and child.text:
            quant = child.text.strip()
        elif local == 'pubDate' and child.text:
            fixing_date = child.text.strip()
    return code, description, quant, fixing_date


class NbkzRateProvider(BaseHttpProvider):
    """National Bank of Kazakhstan official rates, native base KZT.

    The single feed covers the latest published business day; the bank
    does not expose a per-date series at this endpoint, so the ``on_date``
    argument is accepted for interface symmetry but does not alter the
    request. ``_fetch_native`` returns "1 KZT = X CODE" by inverting the
    feed's "quant CODE = description KZT" direction, and the base class
    re-expresses it against the company currency through ``cross_derive``.
    """

    name = 'nbkz'
    native_base = 'KZT'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 KZT expressed in units of CODE".

        The feed publishes the inverse direction (quant units of CODE cost
        description KZT), so each entry is inverted to quant / description.
        A row whose description or quant cannot be parsed, or whose
        description is zero, is skipped so one bad line never sinks the
        whole table. A missing quant defaults to 1, the common quotation.
        """
        root = self._download_xml(_NBKZ_FEED_URL)
        native = {}
        for item in _iter_items(root):
            code, description, quant, fixing_date = _read_item(item)
            if not code or description is None:
                continue
            value = to_decimal(description)
            quantity = to_decimal(quant if quant is not None else '1')
            if not value or not quantity:
                # None (unparseable) or zero on either side: a sane rate
                # cannot be derived, so drop the row.
                continue
            if fixing_date:
                self._set_effective_date(fixing_date)
            # Feed: quant CODE = description KZT -> 1 KZT = quant / description CODE.
            native[code.upper()] = quantity / value
        return native


register('nbkz', NbkzRateProvider,
         label="[KZ] National Bank of Kazakhstan", needs_key=False)
