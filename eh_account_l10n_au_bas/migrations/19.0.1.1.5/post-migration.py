# -*- encoding: utf-8 -*-
"""Snapshot legacy BAS label definitions without rewriting amounts."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    lines = env['eh.bas.run.line'].sudo().search([], order='id')
    migrated = 0
    for line in lines:
        label = line.label_id
        line.with_context(
            eh_bas_engine=True,
            eh_bas_migration=True,
            tracking_disable=True,
        ).write({
            'label_code': label.code,
            'label_name': label.name,
            'label_sequence': label.sequence,
            'label_aggregation': label.aggregation,
        })
        migrated += 1
    _logger.info(
        "eh_account_l10n_au_bas 19.0.1.1.5: snapshotted %d legacy BAS "
        "worksheet label definitions.",
        migrated,
    )
