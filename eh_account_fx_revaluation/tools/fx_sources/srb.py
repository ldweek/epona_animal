# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Sveriges Riksbank (Swedish central bank) FX rate source.

The Riksbank publishes a daily "observations" group of cross rates for
its main convertible currencies through the SWEA API. Group 130 carries
the latest observation per series, each series keyed by a nine-character
seriesId of the form 'SEK' + three-letter CODE + 'PMI', whose value is
the number of Swedish kronor per one unit of CODE (1 CODE = value SEK).

Native base is SEK. Because the feed states "1 CODE = value SEK", we
invert each row into the cross-derivation direction "1 SEK = 1/value
CODE" and let the base class re-express that table against the company
currency. Only series whose seriesId is nine characters long and matches
the 'SEK...PMI' pattern are read; the CODE is taken from seriesId[3:6].
Rows with a missing, unparseable, or zero value are skipped so one bad
line never sinks the whole table.

Endpoint:
* https://api.riksbank.se/swea/v1/Observations/Latest/ByGroup/130

The Riksbank SWEA API is freely reusable without charge and requires no
key. Data reuse should credit Sveriges Riksbank as the source per the
bank's published terms.
"""

from decimal import Decimal

from ..rate_providers import BaseHttpProvider, register, to_decimal

_GROUP_130_URL = (
    "https://api.riksbank.se/swea/v1/Observations/Latest/ByGroup/130"
)

# Each Riksbank seriesId is 'SEK' + a three-letter currency CODE + 'PMI',
# so a well-formed id is exactly nine characters with that prefix and
# suffix. Anything else (a longer id, a differently shaped series) is not
# a per-currency observation and is skipped.
_SERIES_LEN = 9
_SERIES_PREFIX = 'SEK'
_SERIES_SUFFIX = 'PMI'


class SrbRateProvider(BaseHttpProvider):
    """Sveriges Riksbank daily cross rates, native base SEK.

    The SWEA "latest by group" endpoint returns the most recent published
    observation per series, which matches the provider contract of "the
    rate on or before this date"; CBK-style, ``on_date`` is accepted for
    interface symmetry but does not alter the request. The base class
    pivots the SEK-native table returned by ``_fetch_native`` onto the
    company currency through ``cross_derive``.
    """

    name = 'srb'
    native_base = 'SEK'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 SEK expressed in units of CODE".

        The feed is a flat list of observations. Each entry carries a
        nine-character seriesId 'SEK<CODE>PMI' and a ``value`` that is the
        kronor per one unit of CODE, so we read CODE from seriesId[3:6]
        and invert: native[CODE] = 1 / value. Entries whose seriesId does
        not match the pattern, or whose value is missing, unparseable, or
        zero, are skipped rather than allowed to raise.
        """
        payload = self._download_json(_GROUP_130_URL)
        if not isinstance(payload, list):
            raise self._malformed("payload is not a list")

        native = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            series_id = row.get('seriesId')
            if not isinstance(series_id, str):
                continue
            if len(series_id) != _SERIES_LEN:
                continue
            if not series_id.startswith(_SERIES_PREFIX):
                continue
            if not series_id.endswith(_SERIES_SUFFIX):
                continue
            code = series_id[3:6].upper()
            value = to_decimal(row.get('value'))
            if value is None or value == 0:
                continue
            if row.get('date'):
                self._set_effective_date(row.get('date'))
            # Feed: 1 CODE = value SEK  ->  1 SEK = 1/value CODE.
            native[code] = Decimal(1) / value
        return native

    def _malformed(self, detail):
        from ..rate_providers import RateProviderError
        return RateProviderError(
            "%s returned an unexpected payload shape: %s"
            % (self.name, detail)
        )


register(
    'srb',
    SrbRateProvider,
    label="[SE] Sveriges Riksbank",
    needs_key=False,
)
