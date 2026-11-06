# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.peppol.inbound: incoming Peppol invoice / credit note.

Lifecycle:

    received -> parsed -> matched -> posted
                                  \\-> exception -> matched (after fix)
                                                  \\-> rejected
                       -> rejected

Per the brutal-review feedback, this closes the inbound half of the
PEPPOL story: the existing module only emitted XML. The flow:

  1. An incoming UBL XML arrives, either via the access-point poll
     cron, or by a manual upload, or by a per-vendor email alias.
  2. The parser converts the XML to a normalised payload dict.
  3. Partner resolution: lookup by ABN / VAT first, then by Peppol
     endpoint id, then by name + country. Strict: more than one
     candidate leaves partner_id empty for manual review.
  4. Optional PO reference resolves to a purchase.order so the
     downstream three-way match in eh_account_ap_automation can pick
     up the bill.
  5. Posting creates an account.move (move_type=in_invoice or
     in_refund) and links it back so the audit trail joins both
     directions.

Idempotency: detection keys primarily on the business identity
(company, supplier, invoice number, document type), which is stable
when the same logical invoice is redelivered with a re-serialised UBL
(a raw SHA-256 of the bytes would miss that). The raw-XML hash is kept
as a fallback for identical redelivery before the supplier is resolved.
A cross-channel check also refuses to post when a vendor bill for the
same supplier and reference is already posted (AP intake, manual entry,
or an earlier inbound post). Any match is flagged via message_post and
blocks posting. Duplicate evidence is server-owned and cannot be cleared by
editing the record; a duplicate delivery must be rejected.
"""

import hashlib
import hmac
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.eh_account_einvoice_peppol.tools import (
    ubl_parser, access_point_registry,
)


_logger = logging.getLogger(__name__)

_EH_INBOUND_ATTACHMENT_INTERNAL = 'eh_peppol_inbound_attachment_internal'
_EH_INBOUND_ATTACHMENT_CAPABILITY = object()


def _normalize_identifier(value):
    """Canonical comparison form for VAT/participant/legal identifiers."""
    return ''.join(
        char for char in str(value or '').upper() if char.isalnum()
    )


def _normalize_reference(value):
    """Canonical supplier-reference form shared with posting locks."""
    return ' '.join(str(value or '').split()).upper()


class EhPeppolInbound(models.Model):
    _name = 'eh.peppol.inbound'
    _description = "Inbound Peppol invoice"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'received_at desc, id desc'

    # These fields are evidence produced by the transport/parser/workflow,
    # not user input. ``readonly=True`` is only a client hint, so every field
    # that decides what gets posted is guarded at ORM level as well. Source
    # transport/company fields may be supplied on create, but cannot be
    # repointed afterwards. Parser, duplicate and audit fields may only be
    # stamped by the server-side actions, which operate under env.su.
    _EH_TRANSPORT_FIELDS = (
        'company_id', 'access_point_key', 'transmission_id',
        'as4_message_id', 'smp_discovery_log', 'mlr_status',
    )
    _EH_PARSED_FIELDS = (
        'file_hash', 'document_type', 'invoice_number',
        'invoice_number_key', 'issue_date',
        'due_date', 'currency_code', 'note', 'buyer_reference',
        'order_reference', 'supplier_name', 'supplier_endpoint_id',
        'supplier_endpoint_scheme', 'supplier_vat_id',
        'supplier_legal_id', 'buyer_name', 'buyer_endpoint_id',
        'buyer_endpoint_scheme', 'buyer_vat_id', 'buyer_legal_id',
    )
    _EH_AUDIT_FIELDS = (
        'received_at', 'parsed_at', 'parsed_by_id', 'matched_at',
        'matched_by_id', 'posted_at', 'posted_by_id', 'move_id',
        'duplicate_inbound_id', 'duplicate_move_id',
    )
    _eh_guarded_fields = (
        ('state',) + _EH_TRANSPORT_FIELDS + _EH_PARSED_FIELDS
        + _EH_AUDIT_FIELDS
    )
    # Transport/company values are legitimate create-time inputs. Everything
    # derived from the XML or workflow is stripped from a non-su create so an
    # RPC caller cannot manufacture an already-reviewed inbound record.
    _eh_create_guarded_fields = (
        ('state', 'partner_id', 'purchase_order_id')
        + _EH_PARSED_FIELDS + _EH_AUDIT_FIELDS
    )

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
        help="Sequence-assigned reference for the inbound record.",
    )
    state = fields.Selection([
        ('received', "Received"),
        ('parsed', "Parsed"),
        ('matched', "Matched"),
        ('posted', "Posted"),
        ('exception', "Exception"),
        ('rejected', "Rejected"),
    ], default='received', required=True, tracking=True, index=True)

    # ---- transport ----
    access_point_key = fields.Char(
        help=(
            "Adapter key under which the inbound XML arrived (e.g. "
            "'storecove', 'pagero', 'manual'). Identifies which "
            "transport channel the message took; surfaces in the "
            "audit trail. Adapter must be registered via the "
            "access_point_registry before the value will roundtrip "
            "into a real submit/poll call."
        ),
    )
    transmission_id = fields.Char(
        help=(
            "Access-point transmission id, when one is supplied. "
            "Used for cross-reference against the access-point's "
            "delivery receipts and the MLR (message level response) "
            "queue."
        ),
    )
    as4_message_id = fields.Char(
        help="ebMS3 / AS4 MessageId reported by the access point.",
    )
    smp_discovery_log = fields.Text(
        help="SMP / NAPTR participant-lookup trace recorded at poll time.",
    )
    mlr_status = fields.Char(
        help="Message Level Response status from the access point.",
    )

    # ---- raw payload ----
    xml_attachment_id = fields.Many2one(
        'ir.attachment', string="Source XML",
        copy=False, index=True,
        help="Original UBL XML the access point delivered.",
    )
    file_hash = fields.Char(
        help=(
            "SHA-256 hex digest of the source XML. Used for the "
            "duplicate-detection check; a re-submission with the same "
            "hash from the same supplier in the same company refuses "
            "to post twice."
        ),
    )

    # ---- parsed header ----
    document_type = fields.Selection([
        ('invoice', "Invoice"),
        ('credit_note', "Credit note"),
    ], default='invoice', required=True)
    invoice_number = fields.Char(tracking=True)
    invoice_number_key = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        help="Server-normalized supplier reference used for atomic duplicate "
             "detection.",
    )
    issue_date = fields.Date(tracking=True)
    due_date = fields.Date()
    currency_code = fields.Char(
        help="ISO 4217 currency code from the source UBL.",
    )
    note = fields.Text()
    buyer_reference = fields.Char()
    order_reference = fields.Char(
        help=(
            "Purchase-order reference carried in the source UBL, when "
            "present. Used to look up purchase.order for the three-"
            "way match in the AP automation module."
        ),
    )

    # ---- party resolution ----
    supplier_name = fields.Char()
    supplier_endpoint_id = fields.Char()
    supplier_endpoint_scheme = fields.Char()
    supplier_vat_id = fields.Char()
    supplier_legal_id = fields.Char()

    # ---- buyer identity (binds the UBL to the receiving company) ----
    buyer_name = fields.Char()
    buyer_endpoint_id = fields.Char()
    buyer_endpoint_scheme = fields.Char()
    buyer_vat_id = fields.Char()
    buyer_legal_id = fields.Char()
    partner_id = fields.Many2one(
        'res.partner', string="Resolved supplier",
        ondelete='restrict', tracking=True,
        help=(
            "Vendor record matched from the inbound supplier block. "
            "Resolution order: VAT/ABN, Peppol endpoint id, name + "
            "country. Strict: ambiguous matches leave the field empty "
            "for manual selection."
        ),
    )

    # ---- linked records ----
    purchase_order_id = fields.Many2one(
        'purchase.order', string="Resolved PO",
        ondelete='restrict',
        domain="[('state', 'in', ['purchase', 'done'])]",
    )
    move_id = fields.Many2one(
        'account.move', string="Posted bill",
        readonly=True, copy=False, ondelete='set null',
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', compute='_compute_currency_id', store=False,
    )

    # ---- audit ----
    received_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now, tracking=True,
    )
    parsed_at = fields.Datetime(readonly=True, tracking=True)
    parsed_by_id = fields.Many2one('res.users', readonly=True)
    matched_at = fields.Datetime(readonly=True, tracking=True)
    matched_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    rejection_reason = fields.Text(tracking=True)

    # ---- duplicate detection ----
    duplicate_inbound_id = fields.Many2one(
        'eh.peppol.inbound', readonly=True,
        help=(
            "Set when another inbound record exists for the same "
            "supplier and invoice number in the same company (or, "
            "before the supplier is resolved, the same raw-XML hash). "
            "The business-identity key catches a redelivery that "
            "re-serialised the UBL, which a raw-bytes hash would miss."
        ),
    )
    duplicate_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help=(
            "Set when a vendor bill for the same supplier and "
            "reference is already posted in this company (via AP "
            "intake, manual entry, or an earlier inbound post). Blocks "
            "posting a second payable. Duplicate evidence is server-owned; "
            "the duplicate delivery must be rejected or the original posting "
            "must be legitimately reversed before review is repeated."
        ),
    )

    # ---- compute ----

    @api.depends('currency_code')
    def _compute_currency_id(self):
        Curr = self.env['res.currency']
        codes = {
            (rec.currency_code or '').upper()
            for rec in self if rec.currency_code
        }
        by_code = {
            currency.name: currency
            for currency in Curr.search([
                ('name', 'in', sorted(codes)),
                ('active', '=', True),
            ])
        }
        for rec in self:
            if not rec.currency_code:
                rec.currency_id = False
                continue
            rec.currency_id = by_code.get(rec.currency_code.upper()) or False

    # ---- create ----

    def _eh_lock_rows(self):
        """Lock and refresh inbound rows before evaluating workflow state."""
        if not self.ids:
            return
        self.env.cr.execute(
            "SELECT id FROM eh_peppol_inbound "
            "WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(sorted(self.ids))],
        )
        self.invalidate_recordset([
            'state', 'xml_attachment_id', 'partner_id',
            'purchase_order_id', 'move_id', 'duplicate_inbound_id',
            'duplicate_move_id',
        ])

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not self.env.su and vals.get('xml_attachment_id'):
                self.env['ir.attachment'].browse(
                    vals['xml_attachment_id'],
                )._eh_check_access('read')
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.peppol.inbound',
                ) or '/'
                vals['name'] = seq
        records = super().create(vals_list)
        records._eh_mark_source_attachments()
        return records

    def write(self, vals):
        """Freeze source and reviewer choices once they become evidence.

        A manual upload may set its attachment once while still received.
        Partner/PO resolution may be corrected before matching. Once matched,
        the reviewed source and resolution are immutable to ordinary ORM/RPC
        callers. Server actions retain their normal sudo path.
        """
        if not self.env.su:
            if 'rejection_reason' in vals and self.filtered(
                    lambda rec: rec.state == 'rejected'):
                raise AccessError(_(
                    "Recorded Peppol rejection reasons are immutable."
                ))
            guarded_choice_fields = {
                'xml_attachment_id', 'partner_id', 'purchase_order_id',
            }
            if guarded_choice_fields.intersection(vals):
                # Public write() normally checks this in super(), but the
                # state-dependent guard must authorize before its raw row lock
                # and must inspect state only after that lock is held.
                self._eh_check_access('write')
                self._eh_lock_rows()
            if 'xml_attachment_id' in vals:
                attachment_id = vals.get('xml_attachment_id')
                if attachment_id:
                    self.env['ir.attachment'].browse(
                        attachment_id,
                    )._eh_check_access('read')
                locked = self.filtered(
                    lambda rec: rec.state != 'received'
                    or bool(rec.xml_attachment_id)
                )
                if locked:
                    raise AccessError(_(
                        "Source XML can only be attached once while an "
                        "inbound record is in Received state. Create a new "
                        "inbound record for a replacement document."
                    ))
            resolution_fields = {'partner_id', 'purchase_order_id'}
            if resolution_fields.intersection(vals):
                locked = self.filtered(
                    lambda rec: rec.state in ('matched', 'posted', 'rejected')
                )
                if locked:
                    raise AccessError(_(
                        "Supplier and purchase-order resolution are frozen "
                        "after matching. Return through the controlled review "
                        "workflow instead of rewriting reviewed evidence."
                    ))
        result = super().write(vals)
        if vals.get('xml_attachment_id'):
            self._eh_mark_source_attachments()
        return result

    def _eh_mark_source_attachments(self):
        """Stamp source attachments for bounded immutable-source checks."""
        attachments = self.mapped('xml_attachment_id').filtered(
            lambda attachment: not attachment.eh_peppol_inbound_source
        )
        if attachments:
            attachments.sudo().with_context(**{
                _EH_INBOUND_ATTACHMENT_INTERNAL:
                    _EH_INBOUND_ATTACHMENT_CAPABILITY,
            }).write({'eh_peppol_inbound_source': True})
        return True

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "Inbound Peppol audit records cannot be deleted. Reject an "
                "invalid delivery so its transport and review trail remain "
                "available."
            ))
        return super().unlink()

    # ---- transitions ----

    def _eh_authorize_workflow_action(self, manager=False):
        """Prove ACL/rule/group authority before any sudo transition."""
        self._eh_check_access('write')
        if manager and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage Accounting Managers can perform this "
                "inbound Peppol action."
            ))

    def action_parse(self):
        """Parse the source UBL XML into the header / supplier fields."""
        self._eh_authorize_workflow_action()
        actor_id = self.env.user.id
        self = self._eh_workflow_action()
        self._eh_lock_rows()
        for rec in self:
            # Successful parse is deliberately one-shot. Re-parsing a reviewed
            # row would bless replacement bytes and defeat the immutable
            # parse-time hash/header evidence.
            if rec.state != 'received':
                raise UserError(_(
                    "Parse is only available once, while the inbound record "
                    "is in Received state.",
                ))
            rec._parse()
            rec.write({
                'state': 'parsed',
                'parsed_at': fields.Datetime.now(),
                'parsed_by_id': actor_id,
            })

    def action_match(self):
        """Resolve supplier and PO references."""
        self._eh_authorize_workflow_action()
        actor_id = self.env.user.id
        actor_is_manager = self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        )
        self = self._eh_workflow_action()
        self._eh_lock_rows()
        for rec in self:
            if rec.state not in ('parsed', 'exception'):
                raise UserError(_(
                    "Match is only available in parsed or exception "
                    "state.",
                ))
            manual_resolution = bool(
                rec.partner_id or rec.purchase_order_id,
            )
            if manual_resolution and not actor_is_manager:
                raise AccessError(_(
                    "Only an ERP Heritage Accounting Manager can confirm a "
                    "manually selected supplier or purchase order."
                ))
            if not rec.partner_id:
                rec._resolve_partner()
            if rec.partner_id:
                rec._eh_validate_resolved_partner()
            if rec.partner_id:
                if not rec.purchase_order_id:
                    rec._resolve_purchase_order()
                if rec.purchase_order_id:
                    rec._eh_validate_purchase_order()
            rec._eh_check_duplicate()
            new_state = (
                'matched'
                if rec.partner_id
                and not rec.duplicate_inbound_id
                and not rec.duplicate_move_id
                else 'exception'
            )
            rec.write({
                'state': new_state,
                'matched_at': fields.Datetime.now(),
                'matched_by_id': actor_id,
            })

    def action_post(self):
        """Create and post the corresponding account.move."""
        self.ensure_one()
        self._eh_authorize_workflow_action(manager=True)
        actor_id = self.env.user.id
        self = self._eh_workflow_action()
        self._eh_lock_rows()
        if self.state != 'matched':
            raise UserError(_(
                "Only matched inbound records can be posted (state is %s).",
            ) % self.state)
        # Lock mutable master-data rows that financial validation consumes.
        # Attachment and inbound rows are already locked separately.
        self._eh_lock_reviewed_sources()
        # Lock the attachment, re-read the exact bytes, and compare immutable
        # parse-time headers. Revalidate buyer, supplier and PO identities, then
        # take the same business-key lock used by manual/AP vendor-bill posts.
        payload = self._eh_validate_reviewed_payload()
        self._eh_validate_resolved_partner(payload=payload)
        if self.purchase_order_id:
            self._eh_validate_purchase_order()
        move_type = (
            'in_invoice'
            if payload['document_type'] == 'invoice'
            else 'in_refund'
        )
        reference = payload['invoice_number'] or self.name
        Move = self.env['account.move']
        Move._eh_peppol_lock_supplier_reference(
            self.company_id,
            self.partner_id,
            move_type,
            reference,
            self.issue_date,
        )
        self._eh_check_duplicate()
        duplicate_message = False
        if self.duplicate_inbound_id:
            duplicate_message = _(
                "Inbound %(name)s duplicates %(other)s for the same supplier "
                "invoice. Reject this delivery; duplicate evidence cannot "
                "be cleared by editing the record.",
                name=self.name,
                other=self.duplicate_inbound_id.name,
            )
        elif self.duplicate_move_id:
            duplicate_message = _(
                "Inbound %(name)s matches already-posted vendor bill %(move)s. "
                "Posting was blocked to avoid a second payable.",
                name=self.name,
                move=self.duplicate_move_id.display_name,
            )
        if duplicate_message:
            # Do not raise after stamping evidence: an RPC exception rolls the
            # whole transaction back. Persist the exception state and return a
            # warning action so the audit facts survive the request.
            self.write({'state': 'exception'})
            self.message_post(body=duplicate_message)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Duplicate Peppol invoice blocked"),
                    'message': duplicate_message,
                    'type': 'warning',
                    'sticky': True,
                },
            }
        # Explicit savepoint matters when an RPC wrapper or integration caller
        # catches posting errors and keeps its transaction alive: no orphan
        # draft bill may survive a failed inbound post attempt.
        with self.env.cr.savepoint():
            move = self._build_move(payload=payload)
            self._eh_verify_move_amounts(move, payload)
            move.action_post()
            self._eh_verify_move_amounts(move, payload)
            self.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': actor_id,
                'move_id': move.id,
            })
        return True

    def action_reject(self):
        self._eh_authorize_workflow_action(manager=True)
        self = self._eh_workflow_action()
        self._eh_lock_rows()
        for rec in self:
            if rec.state == 'posted':
                raise UserError(_(
                    "Posted inbound records cannot be rejected.",
                ))
            reason = ' '.join((rec.rejection_reason or '').split())
            if not reason:
                raise UserError(_(
                    "A rejection reason is required before rejecting inbound "
                    "%s.", rec.name,
                ))
            rec.write({'state': 'rejected', 'rejection_reason': reason})

    # ---- internals ----

    def _eh_lock_reviewed_sources(self):
        """Hold bounded source rows stable through validation and posting."""
        self.ensure_one()
        # Company owns both receiving identity and document currency. Lock it
        # before following partner links so neither pointer can move between
        # validation and bill creation.
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            [self.company_id.id],
        )
        self.company_id.invalidate_recordset(['partner_id', 'currency_id'])
        selected_partner_ids = sorted(set(
            (self.partner_id | self.company_id.partner_id).ids
        ))
        partner_ids = set(selected_partner_ids)
        if selected_partner_ids:
            # Lock selected rows first, then stored commercial parents. This
            # closes a parent_id/commercial_partner_id substitution race.
            self.env.cr.execute(
                "SELECT id, commercial_partner_id FROM res_partner "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                [tuple(selected_partner_ids)],
            )
            for partner_id, commercial_partner_id in self.env.cr.fetchall():
                partner_ids.add(partner_id)
                if commercial_partner_id:
                    partner_ids.add(commercial_partner_id)
            parent_ids = sorted(partner_ids - set(selected_partner_ids))
            if parent_ids:
                self.env.cr.execute(
                    "SELECT id FROM res_partner WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    [tuple(parent_ids)],
                )
            self.env['res.partner'].browse(
                sorted(partner_ids),
            ).invalidate_recordset([
                'active', 'company_id', 'parent_id',
                'commercial_partner_id', 'name', 'vat',
                'eh_peppol_endpoint_id', 'eh_peppol_endpoint_scheme',
                'eh_peppol_legal_id',
            ])
        if self.purchase_order_id:
            self.env.cr.execute(
                "SELECT id FROM purchase_order WHERE id = %s FOR UPDATE",
                [self.purchase_order_id.id],
            )
            self.purchase_order_id.invalidate_recordset([
                'state', 'company_id', 'partner_id', 'name', 'currency_id',
                'order_line', 'fiscal_position_id',
            ])
            self.env.cr.execute(
                "SELECT id FROM purchase_order_line WHERE order_id = %s "
                "ORDER BY id FOR UPDATE",
                [self.purchase_order_id.id],
            )
            self.env['purchase.order.line'].browse(
                [row[0] for row in self.env.cr.fetchall()]
            ).invalidate_recordset()

    def _parse(self):
        self.ensure_one()
        _raw, file_hash, payload = self._eh_parse_source(lock=True)
        self._eh_validate_buyer_identity(payload)
        self.write({
            'file_hash': file_hash,
            **self._eh_payload_header_values(payload),
        })
        self._eh_check_duplicate()

    def _eh_parse_source(self, lock=False, expected_hash=None):
        """Return ``(raw, sha256, parsed_payload)`` for the source XML.

        Posting takes a row lock before reading. A concurrent attachment write
        must then wait until the posting transaction finishes, and the bill is
        built from the exact parsed payload validated under that lock.
        """
        self.ensure_one()
        attachment = self.xml_attachment_id
        if not attachment:
            raise UserError(_(
                "Inbound record %s has no source XML attachment.",
            ) % self.name)
        if (
            attachment.file_size
            and attachment.file_size > ubl_parser.MAX_UBL_BYTES
        ):
            raise UserError(_(
                "Inbound %(name)s source XML exceeds the maximum allowed "
                "size of %(limit)d bytes.",
                name=self.name,
                limit=ubl_parser.MAX_UBL_BYTES,
            ))
        if lock:
            self.env.cr.execute(
                "SELECT id FROM ir_attachment WHERE id = %s FOR UPDATE",
                [attachment.id],
            )
            attachment.invalidate_recordset([
                'raw', 'datas', 'store_fname', 'checksum',
            ])
        raw = attachment.raw or b''
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        file_hash = hashlib.sha256(raw).hexdigest()
        # Hash before parsing. Replacement bytes must be reported as source
        # tampering even when their manipulated financial content is itself
        # invalid under the strict parser.
        if expected_hash and not hmac.compare_digest(
                file_hash, expected_hash):
            raise UserError(_(
                "Inbound %(name)s source XML changed after it was parsed. "
                "Posting is blocked; reject this record and create a new "
                "inbound record for the replacement document.",
                name=self.name,
            ))
        try:
            payload = ubl_parser.parse_invoice_xml(raw)
        except ubl_parser.PeppolParserError as exc:
            raise UserError(_(
                "Could not parse inbound UBL on %(name)s: %(err)s",
                name=self.name, err=str(exc),
            ))
        return raw, file_hash, payload

    @staticmethod
    def _eh_payload_header_values(payload):
        supplier = payload.get('supplier') or {}
        buyer = payload.get('customer') or {}
        return {
            'document_type': payload['document_type'],
            'invoice_number': payload['invoice_number'],
            'invoice_number_key': _normalize_reference(
                payload['invoice_number'],
            ),
            'issue_date': payload['issue_date'],
            'due_date': payload['due_date'],
            'currency_code': (payload['currency_code'] or '').upper(),
            'note': payload['note'] or '',
            'buyer_reference': payload['buyer_reference'] or '',
            'order_reference': payload['order_reference'] or '',
            'supplier_name': supplier.get('name') or '',
            'supplier_endpoint_id': supplier.get('endpoint_id') or '',
            'supplier_endpoint_scheme':
                supplier.get('endpoint_scheme') or '',
            'supplier_vat_id': supplier.get('vat_id') or '',
            'supplier_legal_id': supplier.get('legal_id') or '',
            'buyer_name': buyer.get('name') or '',
            'buyer_endpoint_id': buyer.get('endpoint_id') or '',
            'buyer_endpoint_scheme': buyer.get('endpoint_scheme') or '',
            'buyer_vat_id': buyer.get('vat_id') or '',
            'buyer_legal_id': buyer.get('legal_id') or '',
        }

    @staticmethod
    def _eh_comparable_value(value):
        return '' if value is False or value is None else value

    def _eh_validate_reviewed_payload(self):
        """Revalidate immutable bytes and parsed identity before posting."""
        self.ensure_one()
        _raw, current_hash, payload = self._eh_parse_source(
            lock=True, expected_hash=self.file_hash,
        )
        if not self.file_hash or not hmac.compare_digest(
                current_hash, self.file_hash):
            raise UserError(_(
                "Inbound %(name)s source XML changed after it was parsed. "
                "Posting is blocked; reject this record and create a new "
                "inbound record for the replacement document.",
                name=self.name,
            ))
        expected = self._eh_payload_header_values(payload)
        changed = [
            field_name
            for field_name, expected_value in expected.items()
            if self._eh_comparable_value(self[field_name])
            != self._eh_comparable_value(expected_value)
        ]
        if changed:
            raise UserError(_(
                "Inbound %(name)s reviewed metadata no longer matches its "
                "source XML (%(fields)s). Posting is blocked.",
                name=self.name,
                fields=', '.join(sorted(changed)),
            ))

        currency_code = (payload.get('currency_code') or '').upper()
        currency = self.env['res.currency'].search([
            ('name', '=', currency_code),
            ('active', '=', True),
        ], limit=1)
        if not currency:
            raise UserError(_(
                "Inbound %(name)s declares unknown or inactive currency "
                "%(currency)s. Activate/configure that currency before "
                "posting; it cannot silently fall back to company currency.",
                name=self.name,
                currency=currency_code or '?',
            ))
        self._eh_validate_buyer_identity(payload)
        return payload

    def _eh_validate_buyer_identity(self, payload):
        """Require UBL customer identity to match the receiving company."""
        self.ensure_one()
        buyer = payload.get('customer') or {}
        company_partner = self.company_id.partner_id.commercial_partner_id
        source_ids = {
            _normalize_identifier(value)
            for value in (
                buyer.get('endpoint_id'),
                buyer.get('vat_id'),
                buyer.get('legal_id'),
            )
            if _normalize_identifier(value)
        }
        company_ids = {
            _normalize_identifier(value)
            for value in (
                company_partner.eh_peppol_endpoint_id,
                company_partner.vat,
                company_partner.eh_peppol_legal_id,
            )
            if _normalize_identifier(value)
        }
        if not company_ids:
            raise UserError(_(
                "Company %(company)s has no Peppol endpoint, VAT, or legal "
                "identifier configured. Buyer identity cannot be verified.",
                company=self.company_id.display_name,
            ))
        source_endpoint = _normalize_identifier(buyer.get('endpoint_id'))
        company_endpoint = _normalize_identifier(
            company_partner.eh_peppol_endpoint_id,
        )
        identity_mismatch = (
            source_endpoint != company_endpoint
            if company_endpoint
            else not source_ids.intersection(company_ids)
        )
        if identity_mismatch:
            raise UserError(_(
                "Inbound %(name)s is addressed to buyer %(buyer)s, whose "
                "endpoint/VAT/legal identity does not match receiving company "
                "%(company)s. Posting in this company is blocked.",
                name=self.name,
                buyer=buyer.get('name') or '?',
                company=self.company_id.display_name,
            ))
        source_scheme = str(buyer.get('endpoint_scheme') or '').upper()
        company_scheme = str(
            company_partner.eh_peppol_endpoint_scheme or '',
        ).upper()
        if (source_endpoint and company_endpoint
                and source_endpoint == company_endpoint
                and source_scheme and company_scheme
                and source_scheme != company_scheme):
            raise UserError(_(
                "Inbound %(name)s buyer endpoint scheme %(source)s does not "
                "match company scheme %(company)s.",
                name=self.name,
                source=source_scheme,
                company=company_scheme,
            ))
        return True

    def _eh_validate_resolved_partner(self, payload=None):
        """Bind reviewed supplier choice to immutable UBL supplier identity."""
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Inbound %s has no resolved supplier.", self.name,
            ))
        partner = self.partner_id.commercial_partner_id
        if not partner.active:
            raise UserError(_(
                "Resolved supplier %s is archived.", partner.display_name,
            ))
        if partner.company_id and partner.company_id != self.company_id:
            raise UserError(_(
                "Resolved supplier %(partner)s belongs to another company.",
                partner=partner.display_name,
            ))
        supplier = (payload or {}).get('supplier') or {
            'name': self.supplier_name,
            'endpoint_id': self.supplier_endpoint_id,
            'endpoint_scheme': self.supplier_endpoint_scheme,
            'vat_id': self.supplier_vat_id,
            'legal_id': self.supplier_legal_id,
        }
        source_ids = {
            _normalize_identifier(value)
            for value in (
                supplier.get('endpoint_id'),
                supplier.get('vat_id'),
                supplier.get('legal_id'),
            )
            if _normalize_identifier(value)
        }
        partner_ids = {
            _normalize_identifier(value)
            for value in (
                partner.eh_peppol_endpoint_id,
                partner.vat,
                partner.eh_peppol_legal_id,
            )
            if _normalize_identifier(value)
        }
        source_endpoint = _normalize_identifier(
            supplier.get('endpoint_id'),
        )
        partner_endpoint = _normalize_identifier(
            partner.eh_peppol_endpoint_id,
        )
        if source_ids:
            identity_mismatch = (
                source_endpoint != partner_endpoint
                if partner_endpoint and source_endpoint
                else not source_ids.intersection(partner_ids)
            )
            if identity_mismatch:
                raise UserError(_(
                    "Resolved supplier %(partner)s does not carry the "
                    "endpoint/VAT/legal identity declared by inbound "
                    "%(inbound)s.",
                    partner=partner.display_name,
                    inbound=self.name,
                ))
        elif ' '.join((partner.name or '').split()).casefold() != \
                ' '.join((supplier.get('name') or '').split()).casefold():
            raise UserError(_(
                "Resolved supplier %(partner)s does not match supplier name "
                "%(source)s declared in inbound %(inbound)s.",
                partner=partner.display_name,
                source=supplier.get('name') or '?',
                inbound=self.name,
            ))
        source_scheme = str(supplier.get('endpoint_scheme') or '').upper()
        partner_scheme = str(partner.eh_peppol_endpoint_scheme or '').upper()
        if (source_endpoint and partner_endpoint
                and source_endpoint == partner_endpoint
                and source_scheme and partner_scheme
                and source_scheme != partner_scheme):
            raise UserError(_(
                "Resolved supplier %(partner)s uses endpoint scheme "
                "%(partner_scheme)s but inbound %(inbound)s declares "
                "%(source_scheme)s.",
                partner=partner.display_name,
                partner_scheme=partner_scheme,
                inbound=self.name,
                source_scheme=source_scheme,
            ))
        return True

    def _eh_validate_purchase_order(self):
        """Ensure reviewed PO still belongs to this company and supplier."""
        self.ensure_one()
        po = self.purchase_order_id
        if not po:
            return True
        if po.company_id != self.company_id:
            raise UserError(_(
                "Purchase order %s belongs to another company.", po.name,
            ))
        if po.state not in ('purchase', 'done'):
            raise UserError(_(
                "Purchase order %s is no longer confirmed.", po.name,
            ))
        if (self.partner_id and po.partner_id.commercial_partner_id
                != self.partner_id.commercial_partner_id):
            raise UserError(_(
                "Purchase order %(po)s belongs to %(po_partner)s, not resolved "
                "supplier %(supplier)s.",
                po=po.name,
                po_partner=po.partner_id.display_name,
                supplier=self.partner_id.display_name,
            ))
        if self.order_reference and po.name != self.order_reference:
            raise UserError(_(
                "Purchase order %(po)s does not match UBL order reference "
                "%(reference)s.",
                po=po.name,
                reference=self.order_reference,
            ))
        if self.currency_code and po.currency_id.name != self.currency_code:
            raise UserError(_(
                "Purchase order %(po)s uses %(po_currency)s, not UBL document "
                "currency %(ubl_currency)s.",
                po=po.name,
                po_currency=po.currency_id.name,
                ubl_currency=self.currency_code,
            ))
        return True

    def _eh_check_duplicate(self):
        """Flag this record when it duplicates another inbound message or an
        already-posted vendor bill.

        Keying only on the raw-XML SHA-256 misses the common case where the
        SAME logical invoice is redelivered with any byte difference (a
        different SBDH/envelope, a re-serialised UBL, a fresh message UUID).
        So detection is driven primarily by the business identity
        (company, supplier, invoice number, document type), which is stable
        across such a redelivery, and falls back to the file hash before the
        supplier is resolved. A second, cross-channel check looks for a
        posted account.move with the same supplier and reference so a bill
        already booked by AP intake, manual entry, or an earlier inbound post
        is not paid twice.
        """
        self.ensure_one()
        Inbound = self.env['eh.peppol.inbound'].sudo()
        base_domain = [
            ('id', '!=', self.id),
            ('company_id', '=', self.company_id.id),
            ('state', 'not in', ('rejected',)),
        ]
        match = Inbound.browse()
        # 1. Business identity: same supplier + invoice number + document
        #    type is the same logical invoice even when the bytes differ.
        if self.partner_id and self.invoice_number:
            reference_key = _normalize_reference(self.invoice_number)
            candidates = Inbound.search(base_domain + [
                ('partner_id', '=', self.partner_id.id),
                ('document_type', '=', self.document_type),
                '|',
                ('invoice_number_key', '=', reference_key),
                ('invoice_number', '=ilike', self.invoice_number.strip()),
            ], limit=10)
            match = candidates.filtered(
                lambda inbound: _normalize_reference(
                    inbound.invoice_number,
                ) == reference_key,
            )[:1]
        # 2. Raw-bytes hash fallback: catches an identical redelivery before
        #    the supplier is resolved (the parse-time first pass).
        if not match and self.file_hash:
            hash_domain = base_domain + [('file_hash', '=', self.file_hash)]
            if self.partner_id:
                hash_domain.append(('partner_id', '=', self.partner_id.id))
            match = Inbound.search(hash_domain, limit=1)
        self.duplicate_inbound_id = match.id if match else False
        # 3. Cross-channel: an already-posted vendor bill for this supplier
        #    and reference would be double-paid if we post again.
        dup_move = self._eh_find_duplicate_move()
        self.duplicate_move_id = dup_move.id if dup_move else False
        if match:
            self.message_post(body=_(
                "Duplicate flag raised: inbound %(other)s already "
                "carries this supplier invoice (matched on supplier + "
                "invoice number, or the raw UBL hash).",
                other=match.name,
            ))
        if dup_move:
            self.message_post(body=_(
                "Duplicate flag raised: vendor bill %(move)s is already "
                "posted for this supplier and reference.",
                move=dup_move.display_name,
            ))

    def _eh_find_duplicate_move(self):
        """Return an already-posted vendor bill this inbound would duplicate.

        Matches company, supplier (commercial partner), bill direction and
        reference, so a payable booked through another channel is caught.
        Empty recordset when the supplier is not yet resolved or no match.
        """
        self.ensure_one()
        if not self.partner_id:
            return self.env['account.move']
        ref = self.invoice_number or self.name
        move_type = (
            'in_invoice' if self.document_type == 'invoice'
            else 'in_refund'
        )
        return self.env[
            'account.move'
        ]._eh_peppol_find_supplier_reference_duplicate(
            self.company_id,
            self.partner_id,
            move_type,
            ref,
            self.issue_date,
            exclude_move_ids=self.move_id.ids,
        )

    def _resolve_partner(self):
        self.ensure_one()
        Partner = self.env['res.partner'].sudo()
        candidates = Partner.browse()
        source_ids = sorted({
            _normalize_identifier(value)
            for value in (
                self.supplier_endpoint_id,
                self.supplier_vat_id,
                self.supplier_legal_id,
            )
            if _normalize_identifier(value)
        })
        if source_ids:
            candidates = Partner.search([
                ('parent_id', '=', False),
                '|',
                ('company_id', '=', self.company_id.id),
                ('company_id', '=', False),
                '|', '|',
                ('eh_peppol_endpoint_id_normalized', 'in', source_ids),
                ('eh_peppol_vat_normalized', 'in', source_ids),
                ('eh_peppol_legal_id_normalized', 'in', source_ids),
            ], limit=3)
        # Name fallback is safe only when the source carries no stronger
        # participant/VAT/legal identity. Otherwise require the vendor master
        # to be configured and leave the record in Exception for review.
        source_has_identifier = bool(source_ids)
        if not candidates and self.supplier_name and not source_has_identifier:
            candidates = Partner.search([
                ('name', '=ilike', self.supplier_name),
                ('parent_id', '=', False),
                '|',
                ('company_id', '=', self.company_id.id),
                ('company_id', '=', False),
            ], limit=2)
        if len(candidates) == 1:
            self.partner_id = candidates.id
        else:
            self.partner_id = False

    def _resolve_purchase_order(self):
        self.ensure_one()
        if not self.order_reference:
            return
        PO = self.env['purchase.order'].sudo()
        po = PO.search([
            ('name', '=', self.order_reference),
            ('company_id', '=', self.company_id.id),
            ('state', 'in', ('purchase', 'done')),
        ], limit=1)
        if po:
            self.purchase_order_id = po.id

    def _build_move(self, payload=None):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "Cannot create vendor bill without a resolved supplier.",
            ))
        Journal = self.env['account.journal'].sudo()
        journal = Journal.search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No purchase journal configured for company %s.",
                self.company_id.display_name,
            ))
        self.env.cr.execute(
            "SELECT id FROM account_journal WHERE id = %s FOR UPDATE",
            [journal.id],
        )
        journal.invalidate_recordset(['active', 'type', 'company_id'])
        if not journal.active or journal.type != 'purchase' \
                or journal.company_id != self.company_id:
            raise UserError(_(
                "Purchase journal changed while inbound %s was posting.",
                self.name,
            ))
        if payload is None:
            _raw, _file_hash, payload = self._eh_parse_source(lock=True)
        move_type = (
            'in_invoice' if payload['document_type'] == 'invoice'
            else 'in_refund'
        )
        po_lines = self.env['purchase.order.line']
        if self.purchase_order_id:
            self._eh_validate_purchase_order()
            if payload['document_type'] != 'invoice':
                raise UserError(_(
                    "Inbound credit notes cannot be auto-linked to purchase "
                    "order %s; review the source bill correction manually.",
                    self.purchase_order_id.name,
                ))
            po_lines = self.purchase_order_id.order_line.filtered(
                lambda po_line: not po_line.display_type
            ).sorted(lambda po_line: (po_line.sequence, po_line.id))
            if len(po_lines) != len(payload['lines']):
                raise UserError(_(
                    "Purchase order %(po)s has %(po_count)s economic lines, "
                    "but inbound %(inbound)s has %(ubl_count)s; exact line "
                    "linkage is not provable.",
                    po=self.purchase_order_id.name,
                    po_count=len(po_lines),
                    inbound=self.name,
                    ubl_count=len(payload['lines']),
                ))
        invoice_line_ids = []
        for index, line in enumerate(payload['lines']):
            quantity = float(line['quantity'])
            line_total = float(line['line_total'])
            if quantity <= 0:
                raise UserError(_(
                    "Inbound %(name)s carries a non-positive quantity on "
                    "line %(line)s.",
                    name=self.name,
                    line=line.get('id') or '?',
                ))
            line_vals = {
                'name': line['description'] or '/',
                'quantity': quantity,
                # UBL LineExtensionAmount is legal net amount. Deriving unit
                # price from that amount safely absorbs permitted unit-price
                # precision and validated line allowances/charges instead of
                # silently booking quantity * advertised gross unit price.
                'price_unit': line_total / quantity,
            }
            if po_lines:
                po_line = po_lines[index]
                discount = (
                    po_line.discount
                    if 'discount' in po_line._fields else 0.0
                )
                expected_unit = po_line.price_unit * (
                    1.0 - discount / 100.0
                )
                if self.purchase_order_id.currency_id.compare_amounts(
                        line_total / quantity, expected_unit):
                    raise UserError(_(
                        "Inbound %(inbound)s line %(line)s net unit price "
                        "does not match purchase order %(po)s line %(po_line)s.",
                        inbound=self.name,
                        line=line.get('id') or index + 1,
                        po=self.purchase_order_id.name,
                        po_line=po_line.display_name,
                    ))
                po_unit_code = po_line.product_uom_id._get_unece_code()
                source_unit_code = (line.get('unit_code') or '').upper()
                if not po_unit_code or source_unit_code != po_unit_code:
                    raise UserError(_(
                        "Inbound %(inbound)s line %(line)s unit %(ubl_unit)s "
                        "does not match purchase order %(po)s line unit "
                        "%(po_unit)s.",
                        inbound=self.name,
                        line=line.get('id') or index + 1,
                        ubl_unit=source_unit_code or '?',
                        po=self.purchase_order_id.name,
                        po_unit=po_unit_code or '?',
                    ))
                if quantity > po_line.product_qty + 1e-9:
                    raise UserError(_(
                        "Inbound %(inbound)s line %(line)s quantity exceeds "
                        "purchase order %(po)s line quantity.",
                        inbound=self.name,
                        line=line.get('id') or index + 1,
                        po=self.purchase_order_id.name,
                    ))
                product_accounts = po_line.product_id.product_tmpl_id \
                    .get_product_accounts(
                        fiscal_pos=self.purchase_order_id.fiscal_position_id,
                    )
                expense_account = product_accounts.get('expense')
                if not expense_account:
                    raise UserError(_(
                        "Purchase order %(po)s line %(line)s has no expense "
                        "account for inbound bill linkage.",
                        po=self.purchase_order_id.name,
                        line=po_line.display_name,
                    ))
                line_vals.update({
                    'purchase_line_id': po_line.id,
                    'product_id': po_line.product_id.id,
                    'product_uom_id': po_line.product_uom_id.id,
                    'account_id': expense_account.id,
                })
            # Map the declared line tax rate to a purchase tax on the buyer
            # company so the bill books input VAT/GST. Dropping the tax makes
            # the bill under-report the payable and leaves no reclaimable tax.
            tax = self._eh_resolve_purchase_tax(
                line.get('tax_rate_pct'), line.get('tax_category_code'),
            )
            if tax:
                line_vals['tax_ids'] = [(6, 0, tax.ids)]
            invoice_line_ids.append((0, 0, line_vals))
        if not invoice_line_ids:
            raise UserError(_(
                "Inbound %s has no lines after parse; refusing to "
                "post an empty bill.",
            ) % self.name)
        move_vals = {
            'move_type': move_type,
            'partner_id': self.partner_id.id,
            'invoice_date': payload['issue_date'],
            'invoice_date_due': payload['due_date'],
            'ref': payload['invoice_number'] or self.name,
            'journal_id': journal.id,
            'invoice_line_ids': invoice_line_ids,
        }
        if self.purchase_order_id:
            move_vals['invoice_origin'] = self.purchase_order_id.name
        # Book the bill in the DOCUMENT currency the UBL declared, not the
        # company currency. Without this a foreign-currency vendor bill posts
        # every line amount as if it were company currency, materially
        # misstating the payable.
        document_currency = self.env['res.currency'].search([
            ('name', '=', (payload.get('currency_code') or '').upper()),
            ('active', '=', True),
        ], limit=1)
        if not document_currency:
            raise UserError(_(
                "Cannot post inbound %s: document currency is unknown or "
                "inactive.", self.name,
            ))
        self.env.cr.execute(
            "SELECT id FROM res_currency WHERE id = %s FOR UPDATE",
            [document_currency.id],
        )
        document_currency.invalidate_recordset(['active', 'name', 'rounding'])
        if (not document_currency.active
                or document_currency.name
                != (payload.get('currency_code') or '').upper()):
            raise UserError(_(
                "Document currency changed while inbound %s was posting.",
                self.name,
            ))
        if document_currency != self.company_id.currency_id:
            move_vals['currency_id'] = document_currency.id
        move = self.env['account.move']._eh_create_sealed(move_vals)
        if self.purchase_order_id:
            self.purchase_order_id.invalidate_recordset(['invoice_ids'])
        if self.purchase_order_id and move not in self.purchase_order_id.invoice_ids:
            raise UserError(_(
                "Created bill is not linked to purchase order %s; posting "
                "stopped because three-way-match provenance is incomplete.",
                self.purchase_order_id.name,
            ))
        return move

    def _eh_verify_move_amounts(self, move, payload):
        """Prove created Odoo bill equals reviewed UBL financial totals."""
        self.ensure_one()
        totals = payload.get('monetary_totals') or {}
        required = {
            'tax_exclusive_amount', 'tax_inclusive_amount', 'payable_amount',
        }
        if not required.issubset(totals):
            raise UserError(_(
                "Inbound %s payload has no validated monetary totals.",
                self.name,
            ))
        currency = move.currency_id

        def _assert_amount(label, actual, expected):
            if currency.compare_amounts(float(actual), float(expected)):
                raise UserError(_(
                    "Inbound %(name)s %(label)s mismatch: UBL %(expected)s, "
                    "created bill %(actual)s %(currency)s.",
                    name=self.name,
                    label=label,
                    expected=expected,
                    actual=actual,
                    currency=currency.name,
                ))

        lines = move.invoice_line_ids.sorted(lambda line: (line.sequence, line.id))
        source_lines = payload.get('lines') or []
        if len(lines) != len(source_lines):
            raise UserError(_(
                "Inbound %(name)s line-count mismatch: UBL %(ubl)s, bill "
                "%(bill)s.",
                name=self.name,
                ubl=len(source_lines),
                bill=len(lines),
            ))
        for index, (move_line, source_line) in enumerate(
                zip(lines, source_lines), start=1):
            _assert_amount(
                _("line %s net amount") % index,
                move_line.price_subtotal,
                source_line['line_total'],
            )
        _assert_amount(
            _("untaxed amount"), move.amount_untaxed,
            totals['tax_exclusive_amount'],
        )
        _assert_amount(
            _("tax amount"), move.amount_tax, payload.get('tax_amount', 0.0),
        )
        _assert_amount(
            _("tax-inclusive amount"), move.amount_total,
            totals['tax_inclusive_amount'],
        )
        _assert_amount(
            _("payable amount"), move.amount_total,
            totals['payable_amount'],
        )
        return True

    def _eh_resolve_purchase_tax(self, rate_pct, category_code=None):
        """Map a parsed UBL line tax rate to a purchase account.tax on the
        buyer company so the bill books input VAT/GST.

        A zero payable impact does not mean "no tax semantics": Z/E/G/O need
        an explicit zero-rate tax so localization reporting tags survive, and
        AE needs an explicit balanced group tax so the buyer books both sides
        of reverse-charge self-assessment. Guessing among duplicate taxes is
        unsafe, so every mapping is unique and locked before move creation.
        """
        self.ensure_one()
        Tax = self.env['account.tax']
        company = self.company_id or self.env.company
        try:
            rate = float(rate_pct)
        except (TypeError, ValueError):
            raise UserError(_(
                "Inbound %(name)s carries a non-numeric tax rate %(rate)r.",
                name=self.name,
                rate=rate_pct,
            ))
        category = (category_code or '').strip().upper()

        def _leaf_taxes(tax):
            if tax.amount_type != 'group':
                return tax
            leaves = Tax
            pending = tax.children_tax_ids
            seen = Tax
            while pending:
                current = pending[:1]
                pending -= current
                if current in seen:
                    continue
                seen |= current
                if current.amount_type == 'group':
                    pending |= current.children_tax_ids
                else:
                    leaves |= current
            return leaves

        def _matches(tax):
            if (not tax.active or tax.company_id != company
                    or tax.type_tax_use != 'purchase' or tax.price_include):
                return False
            if abs(rate) >= 0.0001:
                return (
                    tax.amount_type == 'percent'
                    and abs(tax.amount - rate) < 0.0001
                    and tax.eh_edi_resolve_category() == category
                )
            # At zero rate the category must be explicit. A derived fallback
            # cannot distinguish zero-rated, exempt, export, out-of-scope and
            # reverse-charge reporting semantics.
            if tax.eh_edi_tax_category != category:
                return False
            if category != 'AE':
                return (
                    category in {'Z', 'E', 'G', 'O'}
                    and tax.amount_type == 'percent'
                    and abs(tax.amount) < 0.0001
                )
            # AE must actually self-assess, not merely carry an AE label on a
            # zero-percent tax. Require a non-empty group whose percentage
            # leaves include offsetting positive and negative rates.
            if tax.amount_type != 'group':
                return False
            leaves = _leaf_taxes(tax)
            if not leaves or any(
                    leaf.amount_type != 'percent' or leaf.price_include
                    for leaf in leaves):
                return False
            amounts = leaves.mapped('amount')
            return (
                any(amount > 0 for amount in amounts)
                and any(amount < 0 for amount in amounts)
                and abs(sum(amounts)) < 0.0001
            )

        candidates = Tax.search([
            ('type_tax_use', '=', 'purchase'),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ]).filtered(_matches)
        if len(candidates) != 1:
            raise UserError(_(
                "Inbound %(name)s requires exactly one active, tax-excluded "
                "purchase tax mapping in category %(category)s at "
                "%(rate)s%% for company "
                "%(company)s; found %(count)s.",
                name=self.name,
                category=category or '?',
                rate=rate_pct,
                company=company.display_name,
                count=len(candidates),
            ))
        self.env.cr.execute(
            "SELECT id FROM account_tax WHERE id = %s FOR UPDATE",
            [candidates.id],
        )
        candidates.invalidate_recordset([
            'active', 'type_tax_use', 'amount_type', 'amount', 'company_id',
            'price_include', 'eh_edi_tax_category', 'children_tax_ids',
        ])
        candidates.children_tax_ids.invalidate_recordset([
            'active', 'amount_type', 'amount', 'price_include',
            'children_tax_ids',
        ])
        if not _matches(candidates):
            raise UserError(_(
                "Purchase tax configuration changed while inbound %s was "
                "posting.", self.name,
            ))
        return candidates

    # ---- transport ----

    @api.model
    def _cron_poll_access_points(self, batch_size=200):
        """Cron entry point: poll every configured access point for
        new inbound messages and create one inbound record per
        message.

        The actual transport is delegated to the registered adapter;
        this cron only orchestrates the per-company config lookup
        and persistence. Sites that route through a manual file drop
        can leave the adapter as 'manual' (no-op poll) and write
        inbound records via mail alias / scripted upload instead.
        """
        Company = self.env['res.company']
        for company in Company.search([]):
            # One poll per company at a time. Concurrent cron workers skip a
            # company already being drained instead of fetching same page.
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id = %s "
                "FOR UPDATE SKIP LOCKED",
                [company.id],
            )
            if not self.env.cr.fetchone():
                continue
            company.invalidate_recordset(['eh_peppol_poll_cursor'])
            key = company.eh_peppol_access_point_key or 'manual'
            if not access_point_registry.has_adapter(key):
                continue
            try:
                adapter = access_point_registry.get_adapter(
                    key, self._eh_company_config(company),
                )
            except access_point_registry.AccessPointError:
                continue
            poll_started_at = fields.Datetime.now()
            cursor = company.sudo().eh_peppol_poll_cursor
            # First activation must fetch existing inbox backlog, not ask for
            # messages received after "now". Subsequent calls overlap five
            # seconds; stable message ids make boundary redelivery harmless.
            since = (
                cursor - timedelta(seconds=5)
                if cursor
                else poll_started_at - timedelta(days=30)
            )
            since_iso = fields.Datetime.to_string(since)
            try:
                messages = adapter.poll(since_iso)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Peppol poll failed for company %s adapter %s: %s",
                    company.display_name, key, exc,
                )
                continue
            messages = list(messages or [])
            malformed = any(
                not isinstance(msg, dict) or not msg.get('message_id')
                for msg in messages
            )
            valid_messages = [
                msg for msg in messages
                if isinstance(msg, dict) and msg.get('message_id')
            ]
            message_ids = list(dict.fromkeys(
                str(msg['message_id']) for msg in valid_messages
            ))
            existing_ids = set(self.sudo().search([
                ('company_id', '=', company.id),
                ('access_point_key', '=', key),
                ('transmission_id', 'in', message_ids),
            ]).mapped('transmission_id')) if message_ids else set()
            pending = []
            seen = set(existing_ids)
            for msg in valid_messages:
                message_id = str(msg['message_id'])
                if message_id in seen:
                    continue
                seen.add(message_id)
                pending.append(msg)

            all_persisted = not malformed and len(pending) <= batch_size
            service = self.sudo().with_company(company)
            for msg in pending[:batch_size]:
                try:
                    with self.env.cr.savepoint():
                        if not service._eh_persist_incoming_message(
                            company, key, msg,
                        ):
                            raise UserError(_(
                                "Access point returned an empty Peppol "
                                "message payload."
                            ))
                except Exception as exc:  # noqa: BLE001
                    all_persisted = False
                    _logger.warning(
                        "Peppol message %s failed persistence for company "
                        "%s: %s",
                        msg.get('message_id'), company.display_name, exc,
                    )
            if all_persisted:
                company.sudo().write({
                    'eh_peppol_poll_cursor': poll_started_at,
                })

    @api.model
    def _eh_company_config(self, company):
        """Default config dict handed to the adapter factory.

        Sites can extend res.company with their own per-vendor
        credentials and override this method to enrich the dict.
        """
        import json
        credentials = {}
        # eh_peppol_ap_config is restricted to base.group_system so a plain
        # user cannot RPC-read the access-point credentials. Read it via sudo
        # here so the sanctioned transmission path (send button / poll cron)
        # still resolves the config for non-admin operators without exposing
        # the secret back to them.
        raw = company.sudo().eh_peppol_ap_config
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    credentials = parsed
            except (TypeError, ValueError):
                credentials = {}
        return {
            'company_id': company.id,
            'company_name': company.display_name,
            'access_point_key':
                company.eh_peppol_access_point_key or 'manual',
            'inbox_email': company.eh_peppol_inbox_email or '',
            'credentials': credentials,
        }

    @api.model
    def _eh_persist_incoming_message(self, company, key, msg):
        """Create an inbound record from an adapter poll() entry.

        Expected msg shape: dict with keys message_id, sender,
        xml_bytes, received_at. Missing keys default to safe values.
        """
        xml_bytes = msg.get('xml_bytes') or b''
        if not xml_bytes:
            return
        attachment = self.env['ir.attachment'].create({
            'name': 'peppol_inbound_%s.xml' % (
                msg.get('message_id') or fields.Datetime.now(),
            ),
            'type': 'binary',
            'raw': xml_bytes,
            'mimetype': 'application/xml',
            'company_id': company.id,
        })
        received_at = False
        if msg.get('received_at'):
            try:
                received_at = fields.Datetime.to_datetime(
                    msg['received_at'],
                )
            except (TypeError, ValueError):
                received_at = False
        record = self.create({
            'company_id': company.id,
            'access_point_key': key,
            'transmission_id': msg.get('message_id') or '',
            'as4_message_id': msg.get('as4_message_id') or '',
            'smp_discovery_log': msg.get('smp_discovery_log') or '',
            'mlr_status': msg.get('mlr_status') or '',
            'xml_attachment_id': attachment.id,
            'received_at': received_at or fields.Datetime.now(),
        })
        # Auto-parse to surface header info immediately; downstream
        # match + post still require user action so an unattended
        # cron does not auto-create vendor bills without review.
        try:
            record.action_parse()
        except UserError as exc:
            record.message_post(body=str(exc))
        return record


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    eh_peppol_inbound_source = fields.Boolean(
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Server-owned marker for XML used by an inbound Peppol review. "
            "It bounds immutable-source checks to relevant attachments."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_peppol_inbound_source') for vals in vals_list) \
                and self.env.context.get(
                    _EH_INBOUND_ATTACHMENT_INTERNAL,
                ) is not _EH_INBOUND_ATTACHMENT_CAPABILITY:
            raise AccessError(_(
                "Inbound Peppol source markers are server-owned."
            ))
        return super().create(vals_list)

    def _eh_marked_peppol_sources(self):
        """Return marked rows without an ORM search on ordinary attachments."""
        if not self.ids:
            return self.browse()
        self.env.cr.execute(
            "SELECT id FROM ir_attachment "
            "WHERE id IN %s AND eh_peppol_inbound_source IS TRUE",
            [tuple(sorted(self.ids))],
        )
        return self.browse([row[0] for row in self.env.cr.fetchall()])

    def _eh_lock_peppol_review_guard(self):
        """Serialize attachment mutation with inbound parse/post review."""
        if not self.ids:
            return
        self.env.cr.execute(
            "SELECT id FROM ir_attachment "
            "WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(sorted(self.ids))],
        )

    def _eh_reviewed_peppol_sources(self):
        """Return reviewed inbound records that use these attachments."""
        if not self.ids:
            return self.env['eh.peppol.inbound']
        return self.env['eh.peppol.inbound'].sudo().search([
            ('xml_attachment_id', 'in', self.ids),
            ('state', '!=', 'received'),
        ], limit=1)

    def write(self, vals):
        if 'eh_peppol_inbound_source' in vals:
            if self.env.context.get(
                    _EH_INBOUND_ATTACHMENT_INTERNAL,
            ) is not _EH_INBOUND_ATTACHMENT_CAPABILITY:
                raise AccessError(_(
                    "Inbound Peppol source markers are server-owned."
                ))
            return super().write(vals)
        if not self.env.su:
            self._eh_check_access('write')
            marked = self._eh_marked_peppol_sources()
            if not marked:
                return super().write(vals)
            # Re-query reviewed state after marked-row lock. Writer waiting
            # behind Parse cannot use stale Received state to replace bytes.
            marked._eh_lock_peppol_review_guard()
            if marked._eh_reviewed_peppol_sources():
                raise AccessError(_(
                    "Reviewed Peppol source XML is immutable. Reject the "
                    "inbound record and create a new one for replacement "
                    "bytes."
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            marked = self._eh_marked_peppol_sources()
            if not marked:
                return super().unlink()
            marked._eh_lock_peppol_review_guard()
            if marked._eh_reviewed_peppol_sources():
                raise AccessError(_(
                    "Reviewed Peppol source XML cannot be deleted because it "
                    "is part of the invoice audit trail."
                ))
        return super().unlink()
