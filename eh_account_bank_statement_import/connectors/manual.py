# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Manual connector: deliberate no-op.

Sites that route bank statements via file drop (CSV / OFX / CAMT.053 /
MT940 upload through the import wizard, or SFTP into an attachment
directory consumed by a separate cron) install this connector as a
sentinel. The fetch cron walks every active profile; profiles using
the manual key skip the API call and rely on the existing parser
pipeline instead.

Why ship a no-op rather than omit the entry?

* The dashboard's "configured connector" picker stays consistent: a
  site that has not deployed Plaid / Basiq / a bank-specific
  connector still sees a meaningful default rather than an empty
  dropdown.
* The fetch cron exits cleanly per profile when the connector is
  manual, avoiding a "no connector registered" error that would
  spam the chatter on every cron pass.
"""

import datetime
from typing import Iterable

from .base import LiveBankConnector, BankTransaction
from .registry import register_connector


@register_connector
class ManualConnector(LiveBankConnector):
    CONNECTOR_KEY = 'manual'
    CONNECTOR_LABEL = "Manual (file upload)"
    CREDENTIALS_HELP = (
        "No credentials required. Statements arrive via the import "
        "wizard (CSV / OFX / CAMT.053 / MT940) or via a separate "
        "ingestion cron that drops files into an attachment "
        "directory. The fetch cron skips manual profiles."
    )

    def authenticate(self, profile) -> dict:
        return {}

    def fetch_transactions(
        self, profile,
        since_date: datetime.date,
        until_date: datetime.date,
        session: dict,
    ) -> Iterable[BankTransaction]:
        # Deliberate no-op. Returning an empty iterable signals the
        # framework that there is nothing to import this run.
        return iter(())
