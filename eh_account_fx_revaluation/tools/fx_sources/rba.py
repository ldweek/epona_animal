# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Reserve Bank of Australia (RBA) exchange-rate provider.

The RBA publishes its daily exchange rates as an RDF/RSS feed. The
native base is always AUD: every quoted figure is "1 AUD expressed in
units of the target currency", so the table is already in the direction
the cross-derivation expects and no inversion is needed.

The feed mixes the RDF syntax namespace with the central-bank 'cb'
vocabulary (http://www.cbwiki.net/wiki/index.php/Specification_1.1). We
parse it namespace-agnostically: every element is matched on its local
tag name (the part after the closing brace) so a change of namespace URI
or prefix on the RBA side does not silently break the parser.

Source feed:
* https://www.rba.gov.au/rss/rss-cb-exchange-rates.xml

This module is original work; the parser implements the published RDF/RSS
shape and does not derive from any third-party rate library.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_RBA_FEED_URL = "https://www.rba.gov.au/rss/rss-cb-exchange-rates.xml"


def _local(tag):
    """Return the local part of an XML tag, dropping any namespace.

    ElementTree renders a namespaced tag as '{uri}localname'. Splitting
    on the closing brace yields the bare local name and lets the parser
    ignore which namespace URI or prefix the feed happens to use.
    """
    return tag.split('}')[-1] if tag else tag


def _iter_exchange_rates(root):
    """Yield every element whose local tag name is 'exchangeRate'.

    The RBA feed nests each rate under item/statistics/exchangeRate, but
    we walk the whole tree and match on the local name so the parser does
    not depend on the exact ancestry the feed ships today.
    """
    for elem in root.iter():
        if _local(elem.tag) == 'exchangeRate':
            yield elem


def _read_block(exchange_rate):
    """Extract (target_code, value_text) from one exchangeRate block.

    Returns (None, None) when either the targetCurrency or the value
    descendant is absent so the caller can skip an incomplete row.
    """
    target = None
    value = None
    for child in exchange_rate.iter():
        local = _local(child.tag)
        if local == 'targetCurrency' and child.text:
            target = child.text.strip()
        elif local == 'value' and child.text:
            value = child.text.strip()
    return target, value


class RbaRateProvider(BaseHttpProvider):
    """Reserve Bank of Australia daily exchange rates.

    Native base is AUD. The feed publishes "1 AUD = value TARGET" for
    each item, which is already the direction the base table needs, so
    ``_fetch_native`` stores ``native[TARGET] = to_decimal(value)`` with
    no inversion. The base class then cross-derives against the company
    currency when it is not AUD.
    """

    name = 'rba'
    native_base = 'AUD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        root = self._download_xml(_RBA_FEED_URL)
        for elem in root.iter():
            if _local(elem.tag).lower() in ('date', 'valuedate') and elem.text:
                self._set_effective_date(elem.text.strip())
                break
        native = {}
        for exchange_rate in _iter_exchange_rates(root):
            target, value = _read_block(exchange_rate)
            if not target or value is None:
                continue
            dec = to_decimal(value)
            if dec is None:
                continue
            native[target.upper()] = dec
        return native


register('rba', RbaRateProvider, label="Reserve Bank of Australia",
         needs_key=False)
