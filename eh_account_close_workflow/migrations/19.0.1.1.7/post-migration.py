# -*- encoding: utf-8 -*-
"""Attach durable user-visible provenance to legacy reopened runs."""

import logging

from odoo import SUPERUSER_ID, _, api


_logger = logging.getLogger(__name__)
_PROVENANCE_MARKER = 'EH_CLOSE_MIGRATION_1_1_7'


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    env.invalidate_all()
    runs = env['eh.close.run'].sudo().search([
        ('reopened_by_id', '!=', False),
        ('state', '=', 'reopened'),
    ])
    Message = env['mail.message'].sudo()
    noted = 0
    for run in runs:
        already_noted = Message.search_count([
            ('model', '=', 'eh.close.run'),
            ('res_id', '=', run.id),
            ('body', 'ilike', _PROVENANCE_MARKER),
        ])
        if already_noted:
            continue
        run.with_context(
            mail_create_nosubscribe=True,
            mail_notify_force_send=False,
        ).message_post(
            body=_(
                "Upgrade provenance (%(marker)s): this legacy reopened run "
                "is held in Reopened because its in-flight current-cycle "
                "preparer/reviewer evidence could not be reconstructed. "
                "Use Start to capture a fresh preparer before review.",
                marker=_PROVENANCE_MARKER,
            ),
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        noted += 1

    _logger.warning(
        "Close Workflow 19.0.1.1.7 attached user-visible upgrade "
        "provenance to %d legacy reopened run(s).",
        noted,
    )
