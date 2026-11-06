# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Banco Central del Uruguay (BCU) daily closing quotations.

Native base is UYU (Uruguayan peso). The BCU exposes a SOAP service that
returns the official buy/sell closing quotations for a date range. We POST
a single document/literal envelope spanning a 7-day window ending on the
requested date so a weekend or holiday still resolves to a published
business day without a second service round trip.

The response is document/literal SOAP and is parsed namespace-agnostically
by the local part of each tag name. Inside Salida a respuestastatus block
carries a status (1 ok, 0 no data) and a codigoerror (0 ok, 100 = no
quotation for the date). When the service reports no data we return an
empty table and let the cross derivation surface the gap rather than
raising on an expected weekend.

Each rate row has the literal local tag name 'datoscotizaciones.dato' and
carries Fecha (YYYY-MM-DD), CodigoISO (3-letter ISO), TCC (buy) and TCV
(sell). We use TCV, which states "1 unit of CODE = TCV UYU" (UYU per one
foreign unit). Since the native base is UYU we invert to key the table as
"1 UYU = 1 / TCV CODE" and let the base class pivot it onto the company
currency. When the range spans several days we keep only the rows of the
latest Fecha so the window still yields a single coherent day.

Source: Banco Central del Uruguay. The published quotations are Uruguay
open government data, declared machine-processable and not subject to any
licence restriction. This module is original work implementing the
documented SOAP contract; it does not derive from any third-party rate
library.
"""

import datetime
from decimal import Decimal

from ..rate_providers import BaseHttpProvider, register, to_decimal

_BCU_URL = (
    "https://cotizaciones.bcu.gub.uy/wscotizaciones/servlet/"
    "awsbcucotizaciones"
)
_BCU_HEADERS = {
    'Content-Type': 'text/xml; charset=UTF-8',
    'SOAPAction': 'Cotizaaction/AWSBCUCOTIZACIONES.Execute',
}
# Look back a week so a weekend or holiday still resolves to the latest
# published business day inside one service call.
_RANGE_DAYS = 7

# Service envelope. The two dates are substituted at request time.
_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<soapenv:Envelope'
    ' xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:cot="Cotiza">\n'
    '  <soapenv:Header/>\n'
    '  <soapenv:Body>\n'
    '    <cot:wsbcucotizaciones.Execute>\n'
    '      <cot:Entrada>\n'
    '        <cot:Moneda><cot:item>0</cot:item></cot:Moneda>\n'
    '        <cot:FechaDesde>{fecha_desde}</cot:FechaDesde>\n'
    '        <cot:FechaHasta>{fecha_hasta}</cot:FechaHasta>\n'
    '        <cot:Grupo>0</cot:Grupo>\n'
    '      </cot:Entrada>\n'
    '    </cot:wsbcucotizaciones.Execute>\n'
    '  </soapenv:Body>\n'
    '</soapenv:Envelope>'
)


def _local(tag):
    """Return the local part of an ElementTree tag, stripping any
    namespace. Document/literal SOAP wraps everything in namespaces we do
    not want to hard-code, so we match by local name throughout.
    """
    return tag.rsplit('}', 1)[-1]


def _find_local(element, name):
    """Return the first descendant whose local tag name is ``name``."""
    for node in element.iter():
        if _local(node.tag) == name:
            return node
    return None


class BcuRateProvider(BaseHttpProvider):
    """Banco Central del Uruguay closing quotations, native base UYU.

    Subclasses of BaseHttpProvider return the native table from
    ``_fetch_native``; the base class re-expresses it against the company
    currency through ``cross_derive``. The native table is keyed as
    "1 UYU expressed in units of CODE".
    """

    name = 'bcu'
    native_base = 'UYU'
    needs_key = False

    def _build_envelope(self, on_date):
        fecha_hasta = on_date.isoformat()
        fecha_desde = (
            on_date - datetime.timedelta(days=_RANGE_DAYS)
        ).isoformat()
        return _ENVELOPE.format(
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
        )

    def _fetch_native(self, base, quotes, on_date):
        """Return {CODE -> Decimal} as "1 UYU expressed in units of CODE".

        The service quotes "1 CODE = TCV UYU", so each row is inverted to
        1 / TCV. When the service reports no data (status 0, codigoerror
        100, or no rows) we return an empty dict so the cross derivation
        surfaces the gap instead of raising on an ordinary non-trading
        day. Only the rows of the latest Fecha in the range are used.
        """
        envelope = self._build_envelope(on_date)
        root = self._download_post_xml(
            _BCU_URL, envelope, headers=_BCU_HEADERS,
        )

        salida = _find_local(root, 'Salida')
        if salida is None:
            # No payload element at all: treat as no data for the window.
            return {}

        status_block = _find_local(salida, 'respuestastatus')
        if status_block is not None:
            status_el = _find_local(status_block, 'status')
            codigo_el = _find_local(status_block, 'codigoerror')
            status = (status_el.text or '').strip() if status_el is not None \
                else ''
            codigo = (codigo_el.text or '').strip() if codigo_el is not None \
                else ''
            if status == '0' or codigo == '100':
                return {}

        # Collect every quotation row, grouped by its Fecha, then keep
        # only the rows of the latest day so a multi-day window still
        # yields one coherent table.
        rows_by_date = {}
        for node in salida.iter():
            if _local(node.tag) != 'datoscotizaciones.dato':
                continue
            fecha = None
            code = None
            tcv = None
            for child in node:
                local = _local(child.tag)
                text = (child.text or '').strip()
                if local == 'Fecha':
                    fecha = text or None
                elif local == 'CodigoISO':
                    code = text.upper() or None
                elif local == 'TCV':
                    tcv = text or None
            rows_by_date.setdefault(fecha, []).append((code, tcv))

        # Drop any rows that carried no Fecha; they cannot be ranked.
        rows_by_date.pop(None, None)
        if not rows_by_date:
            return {}

        latest = max(rows_by_date)
        self._set_effective_date(latest)
        native = {}
        for code, tcv_raw in rows_by_date[latest]:
            if not code:
                continue
            tcv = to_decimal(tcv_raw)
            if not tcv:
                # None (unparseable) or zero: no sane rate, drop the row.
                continue
            # Service: 1 CODE = TCV UYU  ->  1 UYU = 1 / TCV CODE.
            native[code] = Decimal(1) / tcv
        return native


register(
    'bcu', BcuRateProvider,
    label="[UY] Central Bank of Uruguay", needs_key=False,
)
