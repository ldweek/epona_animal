# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Gulf Cooperation Council pegged rates (offline, no network).

Several Gulf currencies hold a hard peg to the US dollar that is fixed
by national decree rather than by a daily market fixing. Because the
peg is a legal constant, there is nothing to download: the rate today
is the rate the decree set and will be the rate until a decree changes
it. This provider therefore serves a static table and never touches the
network, which makes it ideal for offline or air-gapped books and for
shops that simply want the decree-fixed conversion without depending on
a third-party feed being reachable.

Native base is USD. Each entry in ``PEGS`` reads "1 USD = value CODE",
the direction the registry expects, so no inversion is needed. The base
class cross-derives the table onto the company currency, which works
for a company based in USD or in any of the pegged currencies below.

The Kuwaiti Dinar (KWD) is deliberately excluded. Kuwait pegs the dinar
to an undisclosed weighted basket of currencies, not to the dollar
alone, so a fixed USD figure would drift from the official rate and be
wrong on most days. For KWD use the 'cbk' provider, which reads the
Central Bank of Kuwait publication.
"""

from decimal import Decimal

from ..rate_providers import BaseHttpProvider, register, RateProviderError


class GccPegRateProvider(BaseHttpProvider):
    """Decree-fixed Gulf pegs to the US dollar, served offline.

    ``PEGS`` is keyed "1 USD = value CODE". The values are legal
    constants, not market quotes, so ``_fetch_native`` returns the table
    directly and never calls ``self._download``. The base class pivots
    the USD table onto whatever currency the company is based in.

    KWD is intentionally absent; the dinar floats against a basket and a
    USD peg would be incorrect. Direct KWD users to the 'cbk' provider.
    """

    name = 'gcc_peg'
    native_base = 'USD'
    needs_key = False

    # Units of currency per 1 USD, each fixed by decree:
    PEGS = {
        'SAR': Decimal('3.75'),       # Saudi Riyal, pegged to USD since 1986
        'AED': Decimal('3.6725'),     # UAE Dirham, decree peg to USD
        'QAR': Decimal('3.64'),       # Qatari Riyal, Royal Decree 34/2001
        'BHD': Decimal('0.376'),      # Bahraini Dinar, Decree 48/2001
        # Omani Rial: the peg is USD 2.6008 per 1 OMR, so 1 USD = 1/2.6008 OMR.
        'OMR': Decimal('1') / Decimal('2.6008'),
    }

    def _fetch_native(self, base, quotes, on_date):
        """Return the static USD peg table, "1 USD expressed in CODE".

        There is no network call: the pegs are decree constants. The
        ``base``, ``quotes`` and ``on_date`` arguments are accepted to
        satisfy the contract but are not used here; the base class
        cross-derivation filters the table down to the requested quotes
        and re-expresses it against the company currency.
        """
        if on_date:
            self._set_effective_date(on_date)
        return dict(self.PEGS)


register('gcc_peg', GccPegRateProvider,
         label="Gulf pegged rates (offline)", needs_key=False)
