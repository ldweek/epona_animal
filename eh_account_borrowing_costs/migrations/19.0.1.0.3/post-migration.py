# -*- encoding: utf-8 -*-
"""Proof-first backfill of IAS 23 move provenance; quarantine ambiguity."""

import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.eh_account_borrowing_costs.models.account_move import (
    BORROWING_MOVE_CAPABILITY,
    BORROWING_MOVE_CONTEXT,
)


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    # Upgrade hooks can share a cursor with prior ORM work.  Discard cached
    # pre-migration relations before grouping legacy provenance.
    env.invalidate_all()
    records = env['eh.borrowing.cost'].search([('move_id', '!=', False)])
    by_move = {}
    for record in records:
        by_move.setdefault(record.move_id.id, []).append(record)

    linked = 0
    quarantined = 0
    for move_id, candidates in by_move.items():
        move = env['account.move'].browse(move_id).exists()
        if len(candidates) != 1:
            valid = False
        else:
            record = candidates[0]
            try:
                valid = bool(
                    move
                    and record.state == 'capitalised'
                    and (
                        not move.eh_borrowing_cost_id
                        or move.eh_borrowing_cost_id == record
                    )
                    and record._eh_proves_original_move(
                        move, require_link=False,
                    )
                )
            except Exception:
                _logger.exception(
                    "Could not prove legacy borrowing-cost move %s.", move_id,
                )
                valid = False
        if valid:
            if not move.eh_borrowing_cost_id:
                move.with_context(**{
                    BORROWING_MOVE_CONTEXT: BORROWING_MOVE_CAPABILITY,
                })._eh_write_sealed_metadata(
                    {'eh_borrowing_cost_id': record.id},
                    {'eh_borrowing_cost_id'},
                )
            linked += 1
            continue
        for candidate in candidates:
            candidate._eh_engine_write({
                'eh_integrity_review_required': True,
                'eh_integrity_review_reason': (
                    'Legacy capitalisation move provenance is ambiguous or '
                    'fails posted/sealed/company/journal/date/signature proof.'
                ),
            })
            quarantined += 1

    _logger.info(
        "eh_account_borrowing_costs 19.0.1.0.3: linked %d proved legacy "
        "moves; quarantined %d source rows without proof.",
        linked, quarantined,
    )
