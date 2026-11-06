# -*- encoding: utf-8 -*-
"""Quarantine unstamped legacy employee-benefit ledger evidence.

Pre-control source links, reversal links, and seals were writable. A matching
legacy graph therefore proves nothing by shape. Only histories whose every
move carries the new immutable engine version stamp may proceed to the exact
runtime topology/signature validators.
"""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
_PROVENANCE_VERSION = 1


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_benefit_provenance_version integer DEFAULT 0"
    )
    env = api.Environment(cr, SUPERUSER_ID, {})
    dc_invalid = _invalid_sources(env['eh.benefit.dc.accrual'].search([]))
    valuation_invalid = _invalid_sources(
        env['eh.benefit.valuation'].search([]),
    )
    _quarantine(
        cr, 'eh_benefit_dc_accrual', dc_invalid,
        "Fail-closed employee-benefit provenance audit: this DC accrual "
        "carries journal evidence without the immutable engine version "
        "stamp, or its stamped topology/signature is invalid. Evidence was "
        "preserved and accounting is blocked pending review. No legacy "
        "graph was trusted from shape.",
    )
    _quarantine(
        cr, 'eh_benefit_valuation', valuation_invalid,
        "Fail-closed employee-benefit provenance audit: this valuation "
        "carries journal evidence without the immutable engine version "
        "stamp, or its stamped topology/signature is invalid. Evidence was "
        "preserved and accounting is blocked pending review. No legacy "
        "graph was trusted from shape.",
    )
    if dc_invalid or valuation_invalid:
        _logger.warning(
            "Employee-benefit provenance audit quarantined %d DC "
            "accrual(s) and %d valuation(s). No unstamped legacy graph was "
            "trusted from shape.",
            len(dc_invalid), len(valuation_invalid),
        )


def _invalid_sources(sources):
    invalid = []
    for source in sources:
        primary = source.move_id
        linked = source.move_ids
        has_evidence = bool(
            source.state != 'draft' or primary or linked
        )
        if not has_evidence:
            continue
        evidence_moves = primary | linked
        if (
            not primary
            or not evidence_moves
            or any(
                move.eh_benefit_provenance_version
                != _PROVENANCE_VERSION
                for move in evidence_moves
            )
        ):
            invalid.append(source.id)
            continue
        try:
            _validate_stamped_history(source)
        except Exception as exc:
            _logger.warning(
                "Stamped employee-benefit source %s failed exact history "
                "validation: %s", source.id, exc,
            )
            invalid.append(source.id)
    return invalid


def _quarantine(cr, table, ids, note):
    if not ids:
        return
    cr.execute(
        "UPDATE %s SET provenance_quarantined = TRUE, "
        "provenance_quarantine_note = %%s WHERE id IN %%s" % table,
        (note, tuple(ids)),
    )


def _validate_stamped_history(source):
    """Validate topology only after durable engine provenance is proved."""
    moves = source.move_ids
    primary = source.move_id
    if source.state not in ('posted', 'reversed'):
        raise ValueError("journal evidence exists outside a posted state")
    source._eh_validate_posting_move()
    if source.state == 'posted':
        if moves != primary:
            raise ValueError("posted source carries non-primary move links")
        return
    reversal = moves - primary
    if len(reversal) != 1:
        raise ValueError("reversed source lacks one exact reversal")
    source._eh_validate_reversal(reversal)
