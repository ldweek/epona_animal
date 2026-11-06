# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Bank of Kuwait (CBK) daily exchange rates.

CBK publishes ~10 currencies (AED, BHD, CHF, EUR, GBP, JPY, OMR, QAR,
SAR, USD) as fils per unit against the Kuwaiti Dinar. KWD floats against
an undisclosed basket, so this daily feed is the correct source (a static
peg would be wrong). Undocumented but stable fixed-path XML.

Native base is KWD. The feed quotes each foreign currency as a rate in
fils per one unit of that currency, and 1 KWD = 1000 fils. So one unit of
a listed CODE costs rate/1000 KWD, and conversely 1 KWD buys 1000/rate
units of CODE. We build the native table directly in the cross-derivation
direction, "1 KWD expressed in units of CODE", and let the base class
pivot it onto the company currency.

The document declares a UTF-8 encoding and may be served with or without
a namespace, so we match on local tag names rather than a fixed path.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_CBK_DAILY_URL = (
    "https://www.cbk.gov.kw/rates/daily_exchange_rates_present.xml"
)


def _local(tag):
    """Strip any XML namespace from an ElementTree tag, returning the
    bare local name so the parser is namespace-agnostic. ElementTree
    renders namespaced tags as '{uri}local'; an un-namespaced tag is
    returned unchanged.
    """
    if tag is None:
        return ''
    return tag.rsplit('}', 1)[-1]


class CbkRateProvider(BaseHttpProvider):
    """Central Bank of Kuwait daily exchange rates, native base KWD.

    The single daily document covers the latest published business day;
    CBK does not expose a per-date series at this endpoint, so the
    ``on_date`` argument is accepted for interface symmetry but does not
    alter the request. Subclasses of BaseHttpProvider return the native
    table from ``_fetch_native``; the base class re-expresses it against
    the company currency through ``cross_derive``.
    """

    name = 'cbk'
    native_base = 'KWD'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 KWD expressed in units of CODE".

        Upstream publishes the inverse, "rate fils per 1 CODE", and
        1 KWD = 1000 fils, so 1 CODE = rate/1000 KWD and therefore
        1 KWD = 1000/rate CODE. We invert per row using Decimal
        arithmetic throughout. A currency whose rate parses to None or
        zero is skipped so one bad line never sinks the whole table.

        The feed may arrive with or without an XML namespace, so we walk
        every element and match on the bare local tag names 'currency',
        'code' and 'rate'.
        """
        root = self._download_xml(_CBK_DAILY_URL)
        for element in root.iter():
            if _local(element.tag) == 'date' and element.text:
                self._set_effective_date(element.text.strip())
                break
        native = {}
        for element in root.iter():
            if _local(element.tag) != 'currency':
                continue
            code = None
            rate = None
            for child in element:
                local = _local(child.tag)
                if local == 'code' and child.text is not None:
                    code = child.text.strip().upper()
                elif local == 'rate':
                    rate = to_decimal(child.text)
            if not code or not rate:
                # Missing code, or a rate that is None (unparseable) or
                # zero: no sane KWD rate can be derived, so drop the row.
                continue
            # Feed: rate fils per 1 CODE, 1 KWD = 1000 fils.
            # 1 CODE = rate/1000 KWD  ->  1 KWD = 1000/rate CODE.
            native[code] = to_decimal('1000') / rate
        return native


register(
    'cbk',
    CbkRateProvider,
    label="Central Bank of Kuwait",
    needs_key=False,
)
