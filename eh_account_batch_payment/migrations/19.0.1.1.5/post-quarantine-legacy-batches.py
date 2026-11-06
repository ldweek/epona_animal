# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Do not fabricate approval digests for pre-snapshot payment batches."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    result = env['eh.batch.payment']._eh_quarantine_legacy_integrity()
    reset_ids = result['reset_confirmed_ids']
    posted_ids = result['quarantined_posted_ids']
    if reset_ids or posted_ids:
        _logger.warning(
            "Batch approval snapshot upgrade: reset %d legacy confirmed "
            "batch(es) for re-review and quarantined %d legacy posted "
            "batch(es) in place (no digest was fabricated). Confirmed=%s; "
            "posted=%s",
            len(reset_ids), len(posted_ids), reset_ids, posted_ids,
        )
