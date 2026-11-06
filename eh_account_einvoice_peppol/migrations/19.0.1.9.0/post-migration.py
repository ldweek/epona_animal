# -*- encoding: utf-8 -*-
"""Backfill dated supplier claims and bounded inbound-source markers."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cr.execute(
        """
        UPDATE ir_attachment attachment
           SET eh_peppol_inbound_source = TRUE
         WHERE attachment.eh_peppol_inbound_source IS NOT TRUE
           AND EXISTS (
               SELECT 1
                 FROM eh_peppol_inbound inbound
                WHERE inbound.xml_attachment_id = attachment.id
           )
        """
    )
    marked = cr.rowcount

    Move = env['account.move'].sudo().with_context(
        eh_peppol_supplier_claim_internal=True,
        tracking_disable=True,
        mail_notrack=True,
    )
    posted = Move.search([
        ('state', '=', 'posted'),
        ('move_type', 'in', ['in_invoice', 'in_refund']),
        ('ref', '!=', False),
        ('invoice_date', '!=', False),
    ], order='id')
    claims = {}
    for move in posted:
        key = move._eh_peppol_supplier_reference_key(
            move.company_id,
            move.partner_id,
            move.move_type,
            move.ref,
            move.invoice_date,
        )
        if key:
            claims.setdefault(key, Move.browse())
            claims[key] |= move

    posted.filtered('eh_peppol_supplier_reference_claim').write({
        'eh_peppol_supplier_reference_claim': False,
    })
    claimed = conflicts = 0
    for key, moves in claims.items():
        if len(moves) != 1:
            conflicts += len(moves)
            _logger.error(
                "Peppol dated supplier claim conflict %s on moves %s; "
                "leaving claims empty for manual audit.", key, moves.ids,
            )
            continue
        moves.write({'eh_peppol_supplier_reference_claim': key})
        claimed += 1

    _logger.warning(
        "Peppol 1.9.0 migration marked %d inbound source attachments, "
        "backfilled %d dated supplier claims, and left %d conflicting "
        "legacy moves unclaimed.", marked, claimed, conflicts,
    )
