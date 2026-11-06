# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Fail closed for legacy scheduled-report ownership on upgrade.

Older releases allowed ``user_id`` delegation and module data was commonly
created by user 1.  Neither is proof that the immutable creator is a live user
who can still read the selected report.  Do not guess or transfer authority:
deactivate every active row that fails the same runtime authorization check.
"""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    quarantined_ids = (
        env['eh.report.schedule']._eh_quarantine_unsafe_schedules()
    )
    if quarantined_ids:
        _logger.warning(
            "Disabled %d legacy scheduled reports without a valid explicit "
            "execution owner: %s",
            len(quarantined_ids), quarantined_ids,
        )
