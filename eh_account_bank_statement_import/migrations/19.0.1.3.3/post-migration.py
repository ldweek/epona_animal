# -*- coding: utf-8 -*-
"""Quarantine legacy live profiles with unsafe journal/credential bindings."""

import json
import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        SELECT profile.id, profile.credentials_json,
               profile.company_id, journal.company_id, journal.type
          FROM eh_bank_live_connector_profile AS profile
          LEFT JOIN account_journal AS journal ON journal.id = profile.journal_id
         WHERE profile.active IS TRUE
        """,
    )
    unsafe_ids = []
    for profile_id, payload, profile_company, journal_company, journal_type in cr.fetchall():
        malformed = False
        if payload:
            try:
                malformed = not isinstance(json.loads(payload), dict)
            except (TypeError, ValueError):
                malformed = True
        if (profile_company != journal_company
                or journal_type not in ('bank', 'cash') or malformed):
            unsafe_ids.append(profile_id)
    if not unsafe_ids:
        return
    cr.execute(
        """
        UPDATE eh_bank_live_connector_profile
           SET active = FALSE,
               last_run_state = 'error',
               last_run_at = NOW(),
               last_run_message = %s
         WHERE id = ANY(%s)
        """,
        [
            "Disabled during upgrade: legacy profile has an invalid company, "
            "journal, or credential binding. Review before reactivation.",
            unsafe_ids,
        ],
    )
    _logger.warning(
        "Quarantined %d unsafe legacy live connector profile(s): %s",
        len(unsafe_ids), unsafe_ids,
    )
