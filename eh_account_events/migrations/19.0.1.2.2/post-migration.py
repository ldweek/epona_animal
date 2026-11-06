# -*- encoding: utf-8 -*-
"""Intentionally do not backfill provenance from legacy graph shape.

This historical migration once linked a unique-looking sealed move to its
source. Later review established that every field used by that proof was
writable before the durable engine stamp existed. Multi-hop upgrades must not
briefly bless that graph; the strict-greater fail-closed migration quarantines
the source and clears unstamped links instead.
"""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if version:
        _logger.info(
            "eh_account_events 19.0.1.2.2 skipped unsafe legacy provenance "
            "shape backfill; the fail-closed stamped-evidence audit follows."
        )
