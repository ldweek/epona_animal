# -*- encoding: utf-8 -*-
"""Backfill exact UBL identity; quarantine unproved legacy approvals."""

import hashlib
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.eh_account_einvoice_peppol.models.peppol_inbound import (
    _normalize_reference,
)
from odoo.addons.eh_account_einvoice_peppol.tools import ubl_parser


_logger = logging.getLogger(__name__)


_LEGACY_HEADER_FIELDS = (
    'document_type', 'invoice_number', 'issue_date', 'due_date',
    'currency_code', 'note', 'buyer_reference', 'order_reference',
    'supplier_name', 'supplier_endpoint_id', 'supplier_endpoint_scheme',
    'supplier_vat_id',
)


def _comparable(value):
    return '' if value is False or value is None else value


def _source_values(record):
    attachment = record.xml_attachment_id
    if not attachment:
        raise ValueError("source XML attachment is missing")
    raw = attachment.sudo().raw or b''
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    if record.file_hash and digest != record.file_hash:
        raise ValueError("source XML hash differs from reviewed hash")
    payload = ubl_parser.parse_invoice_xml(raw)
    values = record._eh_payload_header_values(payload)
    mismatches = [
        field_name for field_name in _LEGACY_HEADER_FIELDS
        if _comparable(record[field_name])
        != _comparable(values[field_name])
    ]
    if mismatches:
        raise ValueError(
            "source XML differs from legacy reviewed fields: %s"
            % ', '.join(sorted(mismatches))
        )
    record._eh_validate_buyer_identity(payload)
    values['file_hash'] = digest
    return values


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Inbound = env['eh.peppol.inbound'].sudo().with_context(
        tracking_disable=True,
        mail_notrack=True,
    )
    records = Inbound.search([
        ('state', 'in', [
            'parsed', 'matched', 'exception', 'posted', 'rejected',
        ]),
    ], order='id')
    backfilled = quarantined = unresolved_posted = 0
    for record in records:
        try:
            values = _source_values(record)
        except Exception as exc:  # noqa: BLE001 - migration must continue
            reason = (
                "Pre-hardening Peppol evidence could not be proved against "
                "the exact source UBL: %s" % exc
            )[:8000]
            if record.state not in ('posted', 'rejected'):
                record.write({
                    'state': 'exception',
                    'rejection_reason': reason,
                    'invoice_number_key': _normalize_reference(
                        record.invoice_number,
                    ) or False,
                })
                quarantined += 1
            else:
                unresolved_posted += 1
                _logger.error(
                    "Peppol inbound %s (%s) requires manual audit: %s",
                    record.id, record.state, reason,
                )
            continue

        values['invoice_number_key'] = _normalize_reference(
            values.get('invoice_number'),
        ) or False
        # Pre-hardening `matched` proves neither immutable source bytes nor
        # review of strict totals/buyer identity. Exact fields are backfilled,
        # but human re-match is required before posting.
        if record.state == 'matched':
            values.update({
                'state': 'exception',
                'rejection_reason': (
                    "Legacy match quarantined by Peppol hardening. Source "
                    "UBL passed strict parsing and identity checks; an "
                    "Accounting Manager must review and match it again."
                ),
            })
            quarantined += 1
        record.write(values)
        backfilled += 1

    # Received records have not been reviewed yet, but normalize any legacy
    # number defensively so duplicate searches cannot miss whitespace/case.
    for record in Inbound.search([
        ('invoice_number', '!=', False),
        ('invoice_number_key', '=', False),
    ], order='id'):
        record.write({
            'invoice_number_key': _normalize_reference(
                record.invoice_number,
            ) or False,
        })

    Move = env['account.move'].sudo().with_context(
        eh_peppol_supplier_claim_internal=True,
        tracking_disable=True,
        mail_notrack=True,
    )
    posted_moves = Move.search([
        ('state', '=', 'posted'),
        ('move_type', 'in', ['in_invoice', 'in_refund']),
        ('ref', '!=', False),
    ], order='id')
    claims = {}
    for move in posted_moves:
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
    claimed_moves = conflicting_moves = 0
    for key, moves in claims.items():
        if len(moves) != 1:
            conflicting_moves += len(moves)
            _logger.error(
                "Legacy posted vendor bills %s share supplier reference "
                "claim %s. Existing postings remain immutable; duplicate "
                "searches stay fail-closed and no claim is fabricated.",
                moves.ids, key,
            )
            continue
        moves.write({'eh_peppol_supplier_reference_claim': key})
        claimed_moves += 1

    _logger.warning(
        "Peppol hardening migration: %d source records backfilled, %d "
        "legacy records quarantined, %d posted/rejected records require "
        "manual audit; %d unique posted supplier claims backfilled, %d "
        "conflicting moves left unclaimed for manual audit.",
        backfilled, quarantined, unresolved_posted,
        claimed_moves, conflicting_moves,
    )
