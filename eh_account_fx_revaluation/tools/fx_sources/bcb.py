# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Central Bank of Brazil (Banco Central do Brasil) PTAX rates.

Native base BRL. The PTAX series is published through the Olinda OData
API and, for the per-day endpoints used here, only carries the US dollar
and the euro against the real. That is by design: we return whatever the
feed serves and let ``cross_derive`` surface the gap for any other quote.

The Olinda feed publishes each rate as "1 unit of the foreign currency in
reais" (the cotacaoVenda / selling quote). Our table is expressed the
other way round, as "1 BRL in units of the foreign currency", so we invert
each published quote before handing the table to the base class.

Endpoints take the quote date as MM-DD-YYYY. The dollar has its own
resource (CotacaoDolarDia); every other currency, including the euro, goes
through the generic CotacaoMoedaDia resource keyed by the ISO code. On a
non-business day the OData "value" array comes back empty; we skip that
currency rather than treat the gap as an error.
"""

import json
from decimal import Decimal

from .. import rate_providers as rp
from ..rate_providers import BaseHttpProvider, to_decimal


# Per-day PTAX resources. The dollar resource is keyed only by date; the
# generic currency resource is additionally keyed by the ISO code.
_DOLLAR_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarDia(dataCotacao=@dataCotacao)"
    "?@dataCotacao='%s'&$top=1&$format=json"
)
_CURRENCY_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoMoedaDia(moeda=@moeda,dataCotacao=@dataCotacao)"
    "?@moeda='%s'&@dataCotacao='%s'&$top=1&$format=json"
)


class BcbRateProvider(BaseHttpProvider):
    """Banco Central do Brasil PTAX daily reference rates.

    Base currency is BRL. The base class drives the network seam and the
    cross-derivation; this class only resolves the dollar and euro quotes
    for the requested date and inverts them into the native-base table.
    """

    name = 'bcb'
    native_base = 'BRL'
    needs_key = False

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as "1 BRL in units of code".

        The dollar and the euro are fetched independently so a single
        endpoint outage does not suppress the other quote. A non-business
        day resolves to an empty OData array and the currency is skipped.
        When neither quote resolves the table is empty and the base class
        derivation surfaces the gap.
        """
        on_date_str = on_date.strftime('%m-%d-%Y')
        native = {}

        # The euro shares the generic resource with every non-dollar
        # currency, so it is keyed by its ISO code as well as the date.
        for code, url in (
            ('USD', _DOLLAR_URL % on_date_str),
            ('EUR', _CURRENCY_URL % ('EUR', on_date_str)),
        ):
            # A transport failure on one endpoint must not suppress the
            # other quote, so the raw download is guarded per-currency.
            # Decoding stays outside the guard: a malformed payload is a
            # data fault, not a transient outage, and aborts the run.
            try:
                raw = self._download(url)
            except rp.RateProviderError:
                continue
            venda = self._venda_from_payload(raw)
            if venda is None:
                continue
            # Date-keyed endpoint returns no row on a non-business day.
            self._set_effective_date(on_date)
            # Published as "1 code = venda BRL"; invert to native base.
            native[code] = Decimal(1) / venda

        return native

    def _venda_from_payload(self, raw):
        """Parse one PTAX response and return its selling quote as Decimal.

        Returns None when the feed serves no row for the date (an empty
        OData "value" array, i.e. a weekend or holiday) or when the quote
        cannot be parsed. Malformed JSON raises RateProviderError through
        the shared decoder.
        """
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise rp.RateProviderError(
                "%s returned malformed JSON: %s" % (self.name, exc),
            ) from exc
        rows = payload.get('value') if isinstance(payload, dict) else None
        if not rows:
            return None
        venda = to_decimal(rows[0].get('cotacaoVenda'))
        if venda is None or venda == 0:
            return None
        return venda


register = rp.register
register('bcb', BcbRateProvider, label="Central Bank of Brazil", needs_key=False)
