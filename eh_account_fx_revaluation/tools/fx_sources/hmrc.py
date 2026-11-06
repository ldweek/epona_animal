# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
HM Revenue and Customs (HMRC) monthly exchange-rate provider.

HMRC publishes ONE rate per currency per calendar month for UK customs
and VAT purposes through the Trade Tariff service. The rate is constant
within the month, so the daily revaluation cron always writes the
current month's value; HMRC does not vary the figure day to day.

The native base is always GBP. Each <exchangeRate> row carries a
<rateNew> field that reads "1 GBP = rateNew CODE" (foreign units per one
pound), which is already the direction the cross-derivation expects, so
``_fetch_native`` stores ``native[CODE] = to_decimal(rateNew)`` with no
inversion. The base class then re-expresses that table against the
company currency, so this module never calls cross_derive itself.

Endpoint (one XML file per month, keyed by the year-month of on_date):
* https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/files/
  monthly_xml_{YYYY-MM}.xml

The feed is parsed namespace-agnostically: every element is matched on
its local tag name (the part after the closing brace) so a change of
namespace URI or prefix on the HMRC side does not silently break the
parser.

This module is original work; the parser implements the published
monthly XML shape and does not derive from any third-party rate library.
"""

from ..rate_providers import BaseHttpProvider, register, to_decimal

_HMRC_MONTHLY_URL = (
    "https://www.trade-tariff.service.gov.uk/api/v2/exchange_rates/files/"
    "monthly_xml_%s.xml"
)


def _local(tag):
    """Return the local part of an XML tag, dropping any namespace.

    ElementTree renders a namespaced tag as '{uri}localname'. Splitting
    on the closing brace yields the bare local name and lets the parser
    ignore which namespace URI or prefix the feed happens to use.
    """
    return tag.split('}')[-1] if tag else tag


def _monthly_url(on_date):
    """Build the monthly file URL for the calendar month of on_date."""
    return _HMRC_MONTHLY_URL % on_date.strftime('%Y-%m')


def _read_block(exchange_rate):
    """Extract (currency_code, rate_text) from one exchangeRate block.

    Returns (None, None) when either the currencyCode or the rateNew
    descendant is absent so the caller can skip an incomplete row.
    """
    code = None
    rate = None
    for child in exchange_rate.iter():
        local = _local(child.tag)
        if local == 'currencyCode' and child.text:
            code = child.text.strip()
        elif local == 'rateNew' and child.text:
            rate = child.text.strip()
    return code, rate


class HmrcRateProvider(BaseHttpProvider):
    """HMRC monthly customs and VAT exchange rates, native base GBP.

    The monthly file holds one <exchangeRate> per quoted currency with a
    <rateNew> reading "1 GBP = rateNew CODE". That is already the native
    direction, so the table stores each value as-is. Rows with a missing
    currencyCode or an unparseable rateNew are skipped so a single bad row
    never poisons the whole feed.
    """

    name = 'hmrc'
    native_base = 'GBP'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as '1 GBP expressed in units of code'.

        rateNew is the pound-direct quote already, so no inversion is
        needed. We iterate every exchangeRate element by local tag name so
        the parser tolerates a namespaced feed.
        """
        root = self._download_xml(_monthly_url(on_date))
        native = {}
        for elem in root.iter():
            if _local(elem.tag) != 'exchangeRate':
                continue
            code, rate = _read_block(elem)
            if not code or rate is None:
                continue
            dec = to_decimal(rate)
            if dec is None:
                continue
            native[code.upper()] = dec
        if native:
            # Monthly customs rate applies throughout requested month.
            self._set_effective_date(on_date)
        return native


register('hmrc', HmrcRateProvider, label="HMRC (UK monthly)",
         needs_key=False)
