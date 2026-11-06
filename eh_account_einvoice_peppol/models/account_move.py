# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Adapter on `account.move` that wraps the Peppol UBL generator.

Two model methods:

* `action_eh_export_peppol_xml` -- render the move to UBL 2.1 bytes,
  attach as `ir.attachment`, return the download action. Available
  on out_invoice / out_refund moves only; raises on draft moves
  because Peppol invoices are by definition issued documents.
* `_eh_build_peppol_payload` -- pure data transform from the move to
  the generator's dict shape. Override this on a localization to
  enrich tax-category mapping or partner-id schemes per jurisdiction.
"""

import base64
import hashlib

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext

from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
    PeppolGeneratorError, make_invoice_payload, render_invoice_xml,
    validate_rendered,
)
from odoo.addons.eh_account_einvoice_peppol.tools.peppol_id_validator import (
    PeppolIdentifierError, validate_participant,
)
from odoo.addons.eh_account_einvoice_peppol.tools import access_point_registry
from odoo.addons.eh_edi_core.tools.en16931 import mapper as core_mapper


_MOVE_TYPE_TO_DOC = {
    'out_invoice': 'invoice',
    'out_refund': 'credit_note',
}

_VENDOR_MOVE_TYPES = frozenset({'in_invoice', 'in_refund'})
_EH_PEPPOL_TRANSMISSION_FIELDS = frozenset({
    'eh_peppol_transmission_status',
    'eh_peppol_transmission_id',
    'eh_peppol_ap_key',
    'eh_peppol_sent_at',
    'eh_peppol_transmission_message',
    'eh_peppol_payload_attachment_id',
    'eh_peppol_payload_hash',
    'eh_peppol_recipient_endpoint_snapshot',
    'eh_peppol_recipient_scheme_snapshot',
    'eh_peppol_legacy_unpinned',
})
_EH_PEPPOL_METADATA_FIELDS = (
    _EH_PEPPOL_TRANSMISSION_FIELDS
    | {'eh_peppol_supplier_reference_claim'}
)
_EH_PEPPOL_TRANSMISSION_INTERNAL = 'eh_peppol_transmission_internal'
_EH_PEPPOL_CLAIM_INTERNAL = 'eh_peppol_supplier_claim_internal'
_EH_PEPPOL_OUTBOUND_ATTACHMENT_INTERNAL = (
    'eh_peppol_outbound_attachment_internal'
)
_EH_PEPPOL_OUTBOUND_ATTACHMENT_CAPABILITY = object()
_EH_PEPPOL_CORRECTION_INTERNAL = 'eh_peppol_correction_internal'
_EH_PEPPOL_CORRECTION_CAPABILITY = object()
_EH_PEPPOL_FROZEN_STATUSES = frozenset({'queued', 'sent', 'delivered'})
_EH_PEPPOL_DOCUMENT_MOVE_FIELDS = frozenset({
    'company_id', 'partner_id', 'currency_id', 'move_type', 'journal_id',
    'date', 'invoice_date', 'invoice_date_due', 'invoice_payment_term_id',
    'name', 'ref', 'narration', 'invoice_origin', 'fiscal_position_id',
    'invoice_line_ids', 'line_ids', 'state',
})
_EH_PEPPOL_DOCUMENT_LINE_FIELDS = frozenset({
    'move_id', 'name', 'display_type', 'product_id', 'product_uom_id',
    'quantity', 'price_unit', 'discount', 'tax_ids', 'account_id',
    'currency_id', 'amount_currency', 'balance', 'debit', 'credit',
    'tax_line_id', 'tax_base_amount',
})


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_peppol_endpoint_scheme = fields.Char(
        related='partner_id.eh_peppol_endpoint_scheme', readonly=True,
    )
    eh_peppol_endpoint_id = fields.Char(
        related='partner_id.eh_peppol_endpoint_id', readonly=True,
    )
    eh_peppol_ready = fields.Boolean(
        compute='_compute_eh_peppol_ready',
        help=(
            "True when the partner has both a Peppol endpoint id "
            "and a scheme set. Surfaces on the move form so the "
            "operator knows whether they can press Export Peppol."
        ),
    )

    # ---- transmission audit ----
    eh_peppol_transmission_status = fields.Selection(
        [
            ('not_sent', "Not sent"),
            ('queued', "Queued"),
            ('sent', "Sent"),
            ('delivered', "Delivered"),
            ('error', "Error"),
        ],
        default='not_sent', readonly=True, copy=False, tracking=True,
    )
    eh_peppol_transmission_id = fields.Char(readonly=True, copy=False)
    eh_peppol_ap_key = fields.Char(
        string="Sent via access point", readonly=True, copy=False,
    )
    eh_peppol_sent_at = fields.Datetime(readonly=True, copy=False)
    eh_peppol_transmission_message = fields.Text(readonly=True, copy=False)
    eh_peppol_payload_attachment_id = fields.Many2one(
        'ir.attachment',
        string="Current Peppol payload",
        readonly=True,
        copy=False,
        ondelete='restrict',
        help=(
            "Exact immutable UBL bytes used for the current outbound attempt. "
            "A voided/re-cut attempt remains in the chatter audit trail."
        ),
    )
    eh_peppol_payload_hash = fields.Char(
        string="Peppol payload SHA-256",
        readonly=True,
        copy=False,
        help="SHA-256 of the exact UBL bytes submitted to the access point.",
    )
    eh_peppol_recipient_endpoint_snapshot = fields.Char(
        string="Sent recipient endpoint",
        readonly=True,
        copy=False,
    )
    eh_peppol_recipient_scheme_snapshot = fields.Char(
        string="Sent recipient scheme",
        readonly=True,
        copy=False,
    )
    eh_peppol_legacy_unpinned = fields.Boolean(
        string="Legacy unpinned Peppol transmission",
        readonly=True,
        copy=False,
        help=(
            "Upgrade quarantine: the move records an older transmission but "
            "the exact submitted attachment/hash cannot be proven. Correct it "
            "through a linked credit note; never reconstruct or re-send it."
        ),
    )
    eh_peppol_supplier_reference_claim = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Server-owned canonical company/supplier/type/reference claim. "
            "A database constraint makes concurrent duplicate posting "
            "impossible even when two intake channels race."
        ),
    )

    _eh_peppol_supplier_reference_claim_unique = models.Constraint(
        'unique(eh_peppol_supplier_reference_claim)',
        'A posted supplier reference can only be claimed once.',
    )

    @api.depends('partner_id.eh_peppol_endpoint_id',
                 'partner_id.eh_peppol_endpoint_scheme',
                 'partner_id.country_id')
    def _compute_eh_peppol_ready(self):
        for move in self:
            partner = move.partner_id.commercial_partner_id or move.partner_id
            country_code = partner.country_id.code if partner else ''
            scheme = (
                partner.eh_peppol_endpoint_scheme
                or move._EH_PEPPOL_SCHEME_BY_COUNTRY.get(country_code)
            ) if partner else False
            move.eh_peppol_ready = bool(
                partner
                and (partner.eh_peppol_endpoint_id or partner.vat
                     or partner.email)
                and scheme
            )

    def _eh_authorize_peppol_action(self, operation='write', manager=False):
        """Authorize actor and record rules before any elevated lifecycle."""
        self._eh_check_access(operation)
        if self.env.su:
            return
        required_group = (
            'eh_account_base.group_eh_manager'
            if manager else 'eh_account_base.group_eh_user'
        )
        if not self.env.user.has_group(required_group):
            raise AccessError(_(
                "You are not allowed to perform this Peppol action."
            ))

    def _eh_lock_transmitted_peppol_documents(self):
        """Lock/re-read moves and return those whose issued UBL is frozen.

        The lock closes the send-vs-edit race: an editor that started from a
        stale ``not_sent`` cache waits behind the sender, then re-reads the
        committed transmission status before it can mutate document inputs.
        """
        if not self:
            return self
        actor_moves = self.sudo(False)
        actor_moves._eh_check_access('write')
        actor_moves.flush_recordset(['eh_peppol_transmission_status'])
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(sorted(actor_moves.ids))],
        )
        actor_moves.invalidate_recordset(['eh_peppol_transmission_status'])
        return actor_moves.filtered(
            lambda move: move.eh_peppol_transmission_status
            in _EH_PEPPOL_FROZEN_STATUSES
        )

    def _eh_peppol_assert_posted_identity_unique(self, vals):
        """Keep supplier-reference uniqueness true after posted-move edits."""
        identity_fields = {
            'company_id', 'partner_id', 'move_type', 'ref', 'invoice_date',
            'state',
        }
        if not identity_fields.intersection(vals):
            return
        posted = self.filtered(
            lambda move: vals.get('state', move.state) == 'posted'
            and vals.get('move_type', move.move_type) in _VENDOR_MOVE_TYPES
        )
        if not posted:
            return
        self._eh_check_access('write')
        targets = []
        by_key = {}
        for move in posted:
            company = (
                self.env['res.company'].browse(int(vals['company_id']))
                if vals.get('company_id') else move.company_id
            )
            partner = (
                self.env['res.partner'].browse(int(vals['partner_id']))
                if vals.get('partner_id') else move.partner_id
            )
            move_type = vals.get('move_type', move.move_type)
            reference = vals.get('ref', move.ref)
            invoice_date = vals.get('invoice_date', move.invoice_date)
            key = self._eh_peppol_supplier_reference_key(
                company, partner, move_type, reference, invoice_date,
            )
            if not key:
                continue
            by_key.setdefault(key, self.browse())
            by_key[key] |= move
            targets.append((
                key, move, company, partner, move_type, reference,
                invoice_date,
            ))
        repeated = next(
            (moves for moves in by_key.values() if len(moves) > 1),
            self.browse(),
        )
        if repeated:
            raise UserError(_(
                "This edit would give posted vendor bills %(moves)s the "
                "same supplier reference.",
                moves=', '.join(repeated.mapped('display_name')),
            ))
        for key in sorted(by_key):
            (_key, move, company, partner, move_type, reference,
             invoice_date) = next(
                target for target in targets if target[0] == key
            )
            self._eh_peppol_lock_supplier_reference(
                company, partner, move_type, reference, invoice_date,
            )
            duplicate = self._eh_peppol_find_supplier_reference_duplicate(
                company,
                partner,
                move_type,
                reference,
                invoice_date,
                exclude_move_ids=move.ids,
            )
            if duplicate:
                raise UserError(_(
                    "This edit would make vendor bill %(move)s duplicate "
                    "posted bill %(duplicate)s.",
                    move=move.display_name,
                    duplicate=duplicate.display_name,
                ))

    def write(self, vals):
        if _EH_PEPPOL_DOCUMENT_MOVE_FIELDS.intersection(vals):
            frozen = self._eh_lock_transmitted_peppol_documents()
            if frozen:
                raise UserError(_(
                    "Peppol document %(moves)s has already been queued or sent. "
                    "Its accounting and invoice inputs are immutable; issue a "
                    "linked credit note, or have a manager void an unconfirmed "
                    "queued attempt before editing.",
                    moves=', '.join(frozen.mapped('display_name')),
                ))
        if 'eh_peppol_supplier_reference_claim' in vals and not (
                self.env.su
                and self.env.context.get(_EH_PEPPOL_CLAIM_INTERNAL)):
            raise AccessError(_(
                "Peppol supplier-reference claims are server-owned."
            ))
        if _EH_PEPPOL_TRANSMISSION_FIELDS.intersection(vals) and not (
                self.env.su
                and self.env.context.get(_EH_PEPPOL_TRANSMISSION_INTERNAL)):
            raise AccessError(_(
                "Peppol transmission audit fields are server-owned and "
                "cannot be edited directly."
            ))
        identity_changed = bool({
            'company_id', 'partner_id', 'move_type', 'ref', 'invoice_date',
            'state',
        }.intersection(vals))
        self._eh_peppol_assert_posted_identity_unique(vals)
        result = super().write(vals)
        if identity_changed:
            for move in self:
                claim = (
                    move._eh_peppol_supplier_reference_key(
                        move.company_id,
                        move.partner_id,
                        move.move_type,
                        move.ref,
                        move.invoice_date,
                    )
                    if move.state == 'posted'
                    and move.move_type in _VENDOR_MOVE_TYPES
                    else False
                )
                if move.eh_peppol_supplier_reference_claim != claim:
                    move.sudo().with_context(
                        **{_EH_PEPPOL_CLAIM_INTERNAL: True}
                    )._eh_write_sealed_metadata(
                        {'eh_peppol_supplier_reference_claim': claim},
                        _EH_PEPPOL_METADATA_FIELDS,
                    )
        return result

    @api.model_create_multi
    def create(self, vals_list):
        if any(
                vals.get('eh_peppol_supplier_reference_claim')
                for vals in vals_list) and not (
                    self.env.su
                    and self.env.context.get(_EH_PEPPOL_CLAIM_INTERNAL)):
            raise AccessError(_(
                "Peppol supplier-reference claims are server-owned."
            ))
        if any(
                any(
                    vals.get(field_name) not in (False, None, 'not_sent')
                    for field_name in _EH_PEPPOL_TRANSMISSION_FIELDS
                )
                for vals in vals_list) and not (
                    self.env.su
                    and self.env.context.get(
                        _EH_PEPPOL_TRANSMISSION_INTERNAL,
                    )):
            raise AccessError(_(
                "Peppol transmission audit fields are server-owned and "
                "cannot be supplied during move creation."
            ))
        direct_posted_vendor = any(
            vals.get('state') == 'posted'
            and vals.get('move_type') in _VENDOR_MOVE_TYPES
            for vals in vals_list
        )
        if direct_posted_vendor:
            raise UserError(_(
                "Create vendor bills in draft and use the posting action so "
                "the atomic supplier-reference duplicate claim runs."
            ))
        return super().create(vals_list)

    # ---- atomic supplier-reference claim ----

    @api.model
    def _eh_peppol_supplier_reference_key(
            self, company, partner, move_type, reference, invoice_date):
        """Return dated canonical key shared by every vendor-bill path."""
        commercial_partner = partner.commercial_partner_id or partner
        normalized_reference = ' '.join(str(reference or '').split()).upper()
        invoice_date = (
            fields.Date.to_date(invoice_date) if invoice_date else None
        )
        if not (company and commercial_partner and normalized_reference
                and invoice_date
                and move_type in _VENDOR_MOVE_TYPES):
            return False
        return '%s|%s|%s|%s|%s' % (
            company.id,
            commercial_partner.id,
            move_type,
            invoice_date.isoformat(),
            normalized_reference,
        )

    @api.model
    def _eh_peppol_lock_supplier_reference(
            self, company, partner, move_type, reference, invoice_date):
        """Serialize posts sharing a supplier invoice business identity."""
        key = self._eh_peppol_supplier_reference_key(
            company, partner, move_type, reference, invoice_date,
        )
        if key:
            # Transaction-scoped, deterministic, and shared with inbound
            # posting.  hashtextextended collisions only over-serialize; they
            # cannot permit a duplicate.
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key],
            )
        return key

    @api.model
    def _eh_peppol_find_supplier_reference_duplicate(
            self, company, partner, move_type, reference, invoice_date,
            exclude_move_ids=()):
        """Find a posted vendor bill with the same normalized identity."""
        key = self._eh_peppol_supplier_reference_key(
            company, partner, move_type, reference, invoice_date,
        )
        if not key:
            return self.browse()
        reference_key = ' '.join(str(reference or '').split()).upper()
        commercial_partner = partner.commercial_partner_id or partner
        params = [
            company.id,
            key,
            commercial_partner.id,
            move_type,
            fields.Date.to_date(invoice_date),
            '[[:space:]]+',
            reference_key,
        ]
        exclude_ids = tuple(int(move_id) for move_id in exclude_move_ids)
        exclude_sql = ''
        if exclude_ids:
            exclude_sql = 'AND id NOT IN %s'
            params.append(exclude_ids)
        self.env.cr.execute(
            """
                SELECT id
                  FROM account_move
                 WHERE company_id = %s
                   AND state = 'posted'
                   AND (
                       eh_peppol_supplier_reference_claim = %s
                       OR (
                           commercial_partner_id = %s
                           AND move_type = %s
                           AND invoice_date = %s
                           AND REGEXP_REPLACE(
                               UPPER(BTRIM(COALESCE(ref, ''))), %s, ' ', 'g'
                           ) = %s
                       )
                   )
                   %s
                 ORDER BY id
                 LIMIT 1
            """ % (
                '%s', '%s', '%s', '%s', '%s', '%s', '%s', exclude_sql,
            ),
            params,
        )
        row = self.env.cr.fetchone()
        return self.sudo().browse(row[0]).exists() if row else self.browse()

    def action_post(self):
        """Atomically reject duplicate posted supplier references.

        Peppol, manual entry, and AP automation all finish through this public
        posting method.  Taking the same advisory lock here closes the race in
        which each channel searched before the other transaction committed.
        """
        undated = self.filtered(
            lambda move: move.state == 'draft'
            and move.move_type in _VENDOR_MOVE_TYPES
            and bool(' '.join((move.ref or '').split()))
            and not move.invoice_date
        )
        if undated:
            raise UserError(_(
                "Vendor bills %(moves)s need supplier invoice dates before "
                "posting. Dated identity is required for duplicate control.",
                moves=', '.join(undated.mapped('display_name')),
            ))
        candidates = self.filtered(
            lambda move: move.state == 'draft'
            and move.move_type in _VENDOR_MOVE_TYPES
            and move.company_id
            and move.commercial_partner_id
            and bool(' '.join((move.ref or '').split())),
        )
        by_key = {}
        for move in candidates:
            key = self._eh_peppol_supplier_reference_key(
                move.company_id,
                move.commercial_partner_id,
                move.move_type,
                move.ref,
                move.invoice_date,
            )
            by_key.setdefault(key, self.browse())
            by_key[key] |= move
        repeated = next(
            (moves for moves in by_key.values() if len(moves) > 1),
            self.browse(),
        )
        if repeated:
            raise UserError(_(
                "Vendor bills %(moves)s carry the same supplier reference "
                "and cannot be posted together.",
                moves=', '.join(repeated.mapped('display_name')),
            ))
        for key in sorted(by_key):
            move = by_key[key]
            self._eh_peppol_lock_supplier_reference(
                move.company_id,
                move.commercial_partner_id,
                move.move_type,
                move.ref,
                move.invoice_date,
            )
        for move in candidates:
            duplicate = self._eh_peppol_find_supplier_reference_duplicate(
                move.company_id,
                move.commercial_partner_id,
                move.move_type,
                move.ref,
                move.invoice_date,
                exclude_move_ids=move.ids,
            )
            if duplicate:
                raise UserError(_(
                    "Vendor bill %(move)s duplicates posted bill %(duplicate)s "
                    "for the same company, supplier, document type, and "
                    "normalized supplier reference.",
                    move=move.display_name,
                    duplicate=duplicate.display_name,
                ))
        result = super().action_post()
        for move in candidates.filtered(
                lambda candidate: candidate.state == 'posted'):
            claim = move._eh_peppol_supplier_reference_key(
                move.company_id,
                move.partner_id,
                move.move_type,
                move.ref,
                move.invoice_date,
            )
            move.sudo().with_context(
                **{_EH_PEPPOL_CLAIM_INTERNAL: True}
            )._eh_write_sealed_metadata(
                {'eh_peppol_supplier_reference_claim': claim},
                _EH_PEPPOL_METADATA_FIELDS,
            )
        return result

    def action_eh_export_peppol_xml(self):
        """Render the move to UBL 2.1 XML and offer it as a download."""
        self.ensure_one()
        self._eh_authorize_peppol_action(operation='read')
        if self.eh_legacy_unverified_seal:
            raise UserError(_(
                "An unverified legacy journal entry cannot be exported as "
                "a trusted Peppol document. Create a new reviewed invoice."
            ))
        if self.state != 'posted':
            raise UserError(_(
                "Peppol export requires a posted invoice; the move is %s."
            ) % self.state)
        if self.move_type not in _MOVE_TYPE_TO_DOC:
            raise UserError(_(
                "Peppol export is only defined for customer invoices and "
                "credit notes (got move_type=%s)."
            ) % self.move_type)
        try:
            payload = self._eh_build_peppol_payload()
            xml_bytes = render_invoice_xml(payload)
            # Structural sanity-check: catches bugs in the generator
            # (missing mandatory tags, currency mismatch, sum drift)
            # before the file goes to the access point. Cheap (~ms on
            # a typical invoice); always run it.
            validate_rendered(xml_bytes)
        except PeppolGeneratorError as exc:
            raise UserError(_("Peppol generator rejected the input: %s") % exc)
        attachment = self.env['ir.attachment'].create({
            'name': "%s.peppol.xml" % (self.name or 'invoice'),
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
        })
        self.message_post(body=_(
            "Peppol UBL 2.1 XML generated; %s bytes."
        ) % len(xml_bytes), attachment_ids=[attachment.id])
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'download',
        }

    def _eh_peppol_recipient(self):
        """Resolve the recipient (endpoint_id, scheme) for transmission,
        applying the same country fallback as the party builder.
        """
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        country_code = (partner.country_id.code
                        or self.company_id.country_id.code or '')
        endpoint_id = (partner.eh_peppol_endpoint_id
                       or partner.vat or partner.email or partner.name or '')
        scheme = (partner.eh_peppol_endpoint_scheme
                  or self._EH_PEPPOL_SCHEME_BY_COUNTRY.get(country_code))
        return endpoint_id, scheme

    def _eh_lock_peppol_outbound_snapshot(self):
        """Lock immutable document rows used to build outbound UBL.

        A state/status check made before the move lock is a TOCTOU bypass:
        another transaction can reset or edit the invoice while this request
        is preparing its payload. Lock parent first (also blocks new child
        rows through the FK), then existing lines and their relation rows.
        Master-data rows are deliberately not locked across adapter network
        I/O: exact rendered bytes and endpoint snapshots are persisted as the
        transmission evidence, while holding company/partner/tax/product locks
        during a remote call would block unrelated accounting work.
        """
        self.ensure_one()
        self.flush_recordset()
        Line = self.env['account.move.line'].sudo()
        Line.flush_model()
        cr = self.env.cr
        cr.execute(
            'SELECT id FROM account_move WHERE id = %s FOR UPDATE',
            [self.id],
        )
        if not cr.fetchone():
            raise UserError(_("Invoice no longer exists."))
        self.invalidate_recordset()

        cr.execute(
            """
                SELECT id
                  FROM account_move_line
                 WHERE move_id = %s
                 ORDER BY id
                   FOR UPDATE
            """,
            [self.id],
        )
        line_ids = [row[0] for row in cr.fetchall()]
        lines = Line.browse(line_ids)
        lines.invalidate_recordset()

        if line_ids:
            # Lock relation rows too. Existing tax removals now serialize;
            # new links need a FK key-share lock on account_move_line, which
            # conflicts with line FOR UPDATE above.
            cr.execute(
                """
                    SELECT account_tax_id
                      FROM account_move_line_account_tax_rel
                     WHERE account_move_line_id IN %s
                     ORDER BY account_move_line_id, account_tax_id
                       FOR UPDATE
                """,
                [tuple(line_ids)],
            )
            cr.fetchall()
        lines.invalidate_recordset()
        self.invalidate_recordset()
        return self

    def _eh_create_peppol_outbound_evidence(self, xml_bytes):
        """Persist append-only bytes before recording an outbound outcome."""
        self.ensure_one()
        attachment = self.env['ir.attachment'].with_context(**{
            _EH_PEPPOL_OUTBOUND_ATTACHMENT_INTERNAL:
                _EH_PEPPOL_OUTBOUND_ATTACHMENT_CAPABILITY,
        }).create({
            'name': "%s.peppol.xml" % (self.name or 'invoice'),
            'type': 'binary',
            'raw': xml_bytes,
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
            'company_id': self.company_id.id,
            'eh_peppol_outbound_evidence': True,
        })
        return attachment, hashlib.sha256(xml_bytes).hexdigest()

    def action_eh_send_peppol(self):
        """Render the move and transmit it through the company's
        registered access-point adapter.

        Works with any registered adapter: the 'manual' default returns
        a 'queued' status (the deployment routes the attached XML by its
        own channel); a live adapter (commercial AP or self-host AS4)
        returns a real transmission id. The result is recorded on the
        move for the audit trail, and the sent XML is attached.
        """
        self.ensure_one()
        self._eh_authorize_peppol_action()
        if self.eh_legacy_unverified_seal:
            raise UserError(_(
                "An unverified legacy journal entry cannot enter the Peppol "
                "delivery workflow. Create a new reviewed invoice."
            ))
        self = self.sudo().with_context(
            **{_EH_PEPPOL_TRANSMISSION_INTERNAL: True}
        )
        self._eh_lock_peppol_outbound_snapshot()
        if self.state != 'posted':
            raise UserError(_(
                "Peppol transmission requires a posted invoice; the "
                "move is %s.") % self.state)
        if self.move_type not in _MOVE_TYPE_TO_DOC:
            raise UserError(_(
                "Peppol transmission is only defined for customer "
                "invoices and credit notes (got move_type=%s).")
                % self.move_type)
        # Idempotency: refuse to re-transmit a document that already left for
        # the access point. A live adapter genuinely re-submits on every call,
        # so a second click / browser retry / another user opening the invoice
        # would book the same legal e-invoice twice on the buyer side. Take a
        # row lock first so two concurrent sends serialise: the second blocks,
        # re-reads the now-'sent' status, and is refused here. Only 'error' /
        # 'not_sent' may (re-)send; a correction needs an explicit void/recut.
        if self.eh_peppol_transmission_status in (
                'queued', 'sent', 'delivered'):
            raise UserError(_(
                "Invoice %(name)s was already transmitted via Peppol "
                "(status %(status)s, id %(tid)s). Re-sending would emit a "
                "second legal e-invoice to the buyer. Void the "
                "transmission and re-cut the document if a correction is "
                "needed.",
                name=self.name or 'invoice',
                status=self.eh_peppol_transmission_status,
                tid=self.eh_peppol_transmission_id or 'n/a',
            ))
        company = self.company_id
        key = company.eh_peppol_access_point_key or 'manual'
        if not access_point_registry.has_adapter(key):
            raise UserError(_(
                "No Peppol access-point adapter is registered for key "
                "%r. Install the corresponding ERP Heritage adapter "
                "module, or set the company access point to 'manual'.")
                % key)
        try:
            payload = self._eh_build_peppol_payload()
            xml_bytes = render_invoice_xml(payload)
            validate_rendered(xml_bytes)
        except PeppolGeneratorError as exc:
            raise UserError(
                _("Peppol generator rejected the input: %s") % exc)
        # Submit to the exact normalized recipient serialized in the UBL. A
        # second partner read here could otherwise route bytes to a different
        # endpoint if master data changed between payload build and submit.
        endpoint_id = payload['customer']['endpoint_id']
        scheme = payload['customer']['endpoint_scheme']
        config = self.env['eh.peppol.inbound']._eh_company_config(company)
        try:
            adapter = access_point_registry.get_adapter(key, config)
            result = adapter.submit(xml_bytes, endpoint_id, scheme) or {}
        except access_point_registry.AccessPointError as exc:
            # A timeout/connection error does not prove the access point
            # rejected the request; it may have accepted the document before
            # the response was lost. Persist the exact attempted bytes and
            # quarantine as queued. A manager must check the AP before voiding;
            # an ordinary retry could emit a second legal invoice.
            attachment, payload_hash = (
                self._eh_create_peppol_outbound_evidence(xml_bytes)
            )
            self._eh_write_sealed_metadata({
                    'eh_peppol_transmission_status': 'queued',
                    'eh_peppol_ap_key': key,
                    'eh_peppol_sent_at': fields.Datetime.now(),
                    'eh_peppol_transmission_message': str(exc)[:8000],
                    'eh_peppol_payload_attachment_id': attachment.id,
                    'eh_peppol_payload_hash': payload_hash,
                    'eh_peppol_recipient_endpoint_snapshot': endpoint_id,
                    'eh_peppol_recipient_scheme_snapshot': scheme,
                    'eh_peppol_legacy_unpinned': False,
                }, _EH_PEPPOL_METADATA_FIELDS)
            self._eh_message_post_sealed_metadata(
                body=_(
                    "Peppol transport outcome is unconfirmed and has been "
                    "quarantined: %(error)s. Check the access point before a "
                    "manager voids this attempt.",
                    error=exc,
                ),
                attachment_ids=[attachment.id],
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Peppol outcome unconfirmed"),
                    'message': _(
                        "%(error)s Check the access point before voiding; "
                        "automatic re-send is blocked.",
                        error=exc,
                    ),
                    'type': 'danger',
                    'sticky': True,
                },
            }
        # Absence of a provider state is not evidence of acceptance. Keep the
        # exact attempt quarantined until its outcome is confirmed or a manager
        # deliberately voids it after checking the access point.
        status = result.get('status') or 'queued'
        if status not in ('queued', 'sent', 'delivered', 'error'):
            # An unknown provider state is neither proof of delivery nor proof
            # of failure. Quarantine it as queued: automatic re-send remains
            # blocked, while a manager can void it after external confirmation.
            status = 'queued'
        raw = result.get('raw_response')
        attachment, payload_hash = self._eh_create_peppol_outbound_evidence(
            xml_bytes,
        )
        self._eh_write_sealed_metadata({
                'eh_peppol_transmission_status': status,
                'eh_peppol_transmission_id': (
                    result.get('transmission_id') or ''
                ),
                'eh_peppol_ap_key': key,
                'eh_peppol_sent_at': fields.Datetime.now(),
                'eh_peppol_transmission_message': (
                    raw[:8000] if isinstance(raw, str) else ''),
                'eh_peppol_payload_attachment_id': attachment.id,
                'eh_peppol_payload_hash': payload_hash,
                'eh_peppol_recipient_endpoint_snapshot': endpoint_id,
                'eh_peppol_recipient_scheme_snapshot': scheme,
                'eh_peppol_legacy_unpinned': False,
            }, _EH_PEPPOL_METADATA_FIELDS)
        self._eh_message_post_sealed_metadata(
            body=_(
                "Peppol invoice transmitted via %(ap)s (status "
                "%(status)s, id %(tid)s).",
                ap=key, status=status,
                tid=result.get('transmission_id') or 'n/a',
            ),
            attachment_ids=[attachment.id],
        )
        return True

    def action_eh_void_peppol_transmission(self):
        """Retire an UNCONFIRMED Peppol transmission so the move can be re-cut.

        action_eh_send_peppol refuses to re-transmit a document whose status is
        'queued'/'sent'/'delivered', so a double click, a browser retry, or a
        second operator cannot emit the legal e-invoice twice. That guard has no
        escape hatch on its own, and the out-of-box 'manual' adapter always
        parks the move at 'queued' (the deployment routes the attached XML by
        its own channel). That out-of-band routing can legitimately fail (wrong
        endpoint, lost file), leaving a stranded 'queued' move that no UI path
        could re-send.

        This manager-only action resets such an unconfirmed transmission --
        only 'queued' or 'error' -- back to 'not_sent' so a corrected file can
        be sent again, and records who voided it plus the prior transmission id
        in the chatter. It NEVER touches a confirmed 'sent'/'delivered'
        transmission: a genuinely delivered legal e-invoice can still never be
        re-emitted, so the double-send protection stays intact. The re-send
        guard itself is unchanged; the only way past it remains this deliberate,
        gated, audited reset.
        """
        self.ensure_one()
        self._eh_authorize_peppol_action(manager=True)
        self = self.sudo().with_context(
            **{_EH_PEPPOL_TRANSMISSION_INTERNAL: True}
        )
        self.flush_recordset([
            'eh_peppol_transmission_status', 'eh_peppol_legacy_unpinned',
        ])
        self.env.cr.execute(
            'SELECT id FROM account_move WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset([
            'eh_peppol_transmission_status',
            'eh_peppol_transmission_id',
            'eh_peppol_legacy_unpinned',
        ])
        if self.eh_peppol_legacy_unpinned:
            raise UserError(_(
                "Invoice %s carries a legacy transmission whose exact submitted "
                "bytes cannot be pinned. It cannot be voided or re-sent; issue "
                "a linked credit note instead.", self.name or 'invoice',
            ))
        if self.eh_peppol_transmission_status not in ('queued', 'error'):
            raise UserError(_(
                "Invoice %(name)s has no unconfirmed Peppol transmission to "
                "void (status %(status)s). Only a 'queued' or 'error' "
                "transmission may be reset; a confirmed 'sent'/'delivered' "
                "e-invoice cannot be re-cut.",
                name=self.name or 'invoice',
                status=self.eh_peppol_transmission_status,
            ))
        prior_status = self.eh_peppol_transmission_status
        prior_tid = self.eh_peppol_transmission_id or 'n/a'
        self._eh_write_sealed_metadata({
                'eh_peppol_transmission_status': 'not_sent',
                'eh_peppol_transmission_id': False,
                'eh_peppol_transmission_message': False,
                'eh_peppol_ap_key': False,
                'eh_peppol_sent_at': False,
                'eh_peppol_payload_attachment_id': False,
                'eh_peppol_payload_hash': False,
                'eh_peppol_recipient_endpoint_snapshot': False,
                'eh_peppol_recipient_scheme_snapshot': False,
            }, _EH_PEPPOL_METADATA_FIELDS)
        self._eh_message_post_sealed_metadata(body=_(
            "Peppol transmission voided for re-cut by %(user)s (was "
            "%(status)s, id %(tid)s). Any file already routed for this "
            "invoice must NOT be treated as the final e-invoice; the next "
            "Send supersedes it.",
            user=self.env.user.display_name,
            status=prior_status,
            tid=prior_tid,
        ))
        return True

    def action_eh_create_peppol_credit_note(self):
        """Create one linked draft credit note for a confirmed transmission.

        A sent/delivered legal invoice is never reset, cancelled, edited, or
        re-emitted.  Its correction is a new ``out_refund`` linked through
        core's immutable ``reversed_entry_id`` provenance.  The credit-note UBL
        serializes that original invoice number as ``BillingReference``.
        """
        self.ensure_one()
        self._eh_authorize_peppol_action()
        actor_move = self.sudo(False)
        actor_move.flush_recordset([
            'state', 'move_type', 'eh_peppol_transmission_status',
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE",
            [actor_move.id],
        )
        actor_move.invalidate_recordset([
            'state', 'move_type', 'eh_peppol_transmission_status',
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])
        if (
            actor_move.state != 'posted'
            or actor_move.move_type != 'out_invoice'
            or actor_move.eh_peppol_transmission_status not in ('sent', 'delivered')
        ):
            raise UserError(_(
                "A Peppol credit note can only correct a posted customer "
                "invoice whose transmission is sent or delivered."
            ))
        if actor_move.eh_legacy_unverified_seal:
            raise UserError(_(
                "This invoice also carries a legacy accounting-seal quarantine. "
                "Use Reverse Reviewed Legacy Entry first; it cannot enter a "
                "normal credit-note workflow."
            ))
        existing = self.env['account.move'].search([
            ('reversed_entry_id', '=', actor_move.id),
            ('move_type', '=', 'out_refund'),
        ], order='id', limit=1)
        if existing:
            correction = existing
        else:
            today = fields.Date.context_today(actor_move)
            values = {
                'date': today,
                'invoice_date': today,
                'invoice_date_due': today,
                'invoice_origin': actor_move.invoice_origin,
                'ref': _(
                    "Peppol correction of %(invoice)s / transmission %(tid)s",
                    invoice=actor_move.name or '/',
                    tid=actor_move.eh_peppol_transmission_id or 'n/a',
                ),
            }
            if actor_move.eh_sealed:
                correction = actor_move._eh_reverse_with_verified_capability(
                    [values], cancel=False,
                )
            else:
                # Odoo 16/17 write computed invoice-line balances while core
                # copies a move for reversal.  Admit that nested write only
                # for this locked, authorised correction corridor.  Object
                # identity keeps an RPC context value from forging access.
                guarded = actor_move.with_context(**{
                    _EH_PEPPOL_CORRECTION_INTERNAL:
                        _EH_PEPPOL_CORRECTION_CAPABILITY,
                })
                correction = guarded._reverse_moves(
                    default_values_list=[values], cancel=False,
                )
                clean_context = dict(correction.env.context)
                clean_context.pop(_EH_PEPPOL_CORRECTION_INTERNAL, None)
                correction = correction.with_context(clean_context)
            actor_move.message_post(body=_(
                "Peppol correction credit note created: %(credit)s",
                credit=correction._get_html_link(),
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Peppol Correction Credit Note"),
            'res_model': 'account.move',
            'res_id': correction.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _eh_build_peppol_payload(self):
        """Compose the UBL generator dict from this move.

        Override on a localization to map jurisdiction-specific tax
        codes (S/Z/E/AE/G/O) and partner identification schemes.
        The default implementation resolves each line's EN 16931 tax
        category from the tax configuration (the eh_edi_tax_category on
        account.tax, else the rate rule), carries the exemption reason
        for the no-tax categories, and surfaces an explicit error when
        the move has no exportable lines at all.
        """
        self.ensure_one()
        canonical = core_mapper.map_move(self).to_ubl_inputs()
        document_type = canonical['document_type']
        buyer_reference = canonical['buyer_reference']
        order_reference = canonical['order_reference'] or self.invoice_origin or ''
        if not buyer_reference and not order_reference:
            raise PeppolGeneratorError(
                "PEPPOL-EN16931-R003 requires BuyerReference or "
                "OrderReference. Set a customer purchase-order/origin "
                "reference before export."
            )
        return make_invoice_payload(
            invoice_number=canonical['invoice_number'],
            issue_date=canonical['issue_date'],
            due_date=canonical['due_date'],
            currency_code=canonical['currency_code'],
            tax_currency_code=canonical['tax_currency_code'],
            supplier=self._eh_build_peppol_party(self.company_id.partner_id),
            customer=self._eh_build_peppol_party(self.partner_id),
            lines=canonical['lines'],
            tax_categories=canonical['tax_categories'],
            document_type=document_type,
            note=html2plaintext(canonical['note']).strip(),
            payment_means=self._eh_peppol_payment_means(),
            buyer_reference=buyer_reference,
            order_reference=order_reference,
            contract_reference=canonical.get('contract_reference') or '',
            monetary_totals=canonical['monetary_totals'],
            billing_reference=(
                self.reversed_entry_id.name
                if document_type == 'credit_note' and self.reversed_entry_id
                else ''
            ),
        )

    def _eh_peppol_payment_means(self):
        """Return configured, evidenced Peppol payment instructions."""
        self.ensure_one()
        code = self.company_id.eh_peppol_payment_means_code or '1'
        result = {'code': code, 'payment_id': self.name or ''}
        if code in ('30', '58'):
            bank = self.partner_bank_id
            account = ''.join((bank.acc_number or '').split()) if bank else ''
            if not account:
                raise PeppolGeneratorError(
                    "PaymentMeansCode %s requires a payee bank account on "
                    "the invoice." % code
                )
            result['iban'] = account
            if bank.bank_id and bank.bank_id.bic:
                result['bic'] = ''.join(bank.bank_id.bic.split())
        return result

    # Country-based fallback scheme when the partner has not picked
    # one explicitly. Maps a country ISO-2 code to the OpenPeppol
    # scheme that participants from that country are typically registered
    # under. Unknown countries require an explicit scheme; silently calling
    # them Norwegian creates an invalid legal identifier.
    _EH_PEPPOL_SCHEME_BY_COUNTRY = {
        'AU': '0151',
        'NZ': '0088',
        'DK': '0184',
        'NO': '0192',
        'SE': '0007',
        'DE': '9930',
        'IS': '0196',
    }

    def _eh_build_peppol_party(self, partner):
        partner = partner.commercial_partner_id or partner
        country_code = (partner.country_id.code
                        or self.company_id.country_id.code or '')
        endpoint_id = (partner.eh_peppol_endpoint_id
                       or partner.vat or partner.email
                       or partner.name)
        default_scheme = self._EH_PEPPOL_SCHEME_BY_COUNTRY.get(country_code)
        scheme = partner.eh_peppol_endpoint_scheme or default_scheme
        # Validate the participant identifier against the OpenPeppol
        # scheme rules. A bad ABN, malformed GLN, or made-up scheme
        # would otherwise pass through to the XML and bounce at the
        # access point. Failing here surfaces a precise message naming
        # which scheme rule the partner data violates so the user can
        # fix the record before re-trying. The transformation also
        # canonicalises the identifier (strips spaces, dashes, dots).
        try:
            scheme, endpoint_id = validate_participant(scheme, endpoint_id)
        except PeppolIdentifierError as exc:
            raise UserError(_(
                "Peppol identifier on partner %(partner)s is invalid: "
                "%(reason)s. Fix the participant id on the partner form "
                "(scheme + endpoint id) and retry.",
                partner=partner.display_name,
                reason=str(exc),
            ))
        return {
            'name': partner.name,
            'endpoint_id': endpoint_id,
            'endpoint_scheme': scheme,
            'country_code': country_code,
            'vat_id': partner.vat or '',
            'legal_id': partner.eh_peppol_legal_id or partner.vat or '',
            'address': {
                'street': partner.street or '',
                'city': partner.city or '',
                'postcode': partner.zip or '',
                'country': country_code,
            },
        }

    def _eh_build_peppol_lines_and_tax(self):
        """Return (lines, tax_categories) tuples for the generator.

        Shared EN 16931 mapper owns line prices, stable unit codes, tax
        categories, and booked document-currency tax buckets. This adapter
        performs no second account.move interpretation.
        """
        canonical = core_mapper.map_move(self).to_ubl_inputs()
        lines = canonical['lines']
        tax_buckets = canonical['tax_categories']
        if not lines:
            raise PeppolGeneratorError(
                "Move %s has no exportable invoice lines." % self.name,
            )
        return lines, tax_buckets


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def _eh_refuse_transmitted_peppol_line_change(self, moves):
        # Odoo 16/17 synchronise invoice lines during metadata-only writes
        # and reversal creation. Admit only exact object-identity accounting
        # capabilities; an RPC context string/boolean cannot forge them.
        peppol_correction = (
            self.env.context.get(_EH_PEPPOL_CORRECTION_INTERNAL)
            is _EH_PEPPOL_CORRECTION_CAPABILITY
        )
        if peppol_correction or moves._eh_has_sealed_mutation_capability():
            return
        moves = moves.exists()
        if not moves:
            return
        frozen = moves._eh_lock_transmitted_peppol_documents()
        if frozen:
            raise UserError(_(
                "Peppol document %(moves)s has already been queued or sent. "
                "Its journal and invoice lines are immutable; correct it with "
                "a linked credit note.",
                moves=', '.join(frozen.mapped('display_name')),
            ))

    @api.model_create_multi
    def create(self, vals_list):
        move_ids = {
            int(vals['move_id']) for vals in vals_list if vals.get('move_id')
        }
        if move_ids:
            self._eh_refuse_transmitted_peppol_line_change(
                self.env['account.move'].browse(sorted(move_ids)),
            )
        return super().create(vals_list)

    def write(self, vals):
        if _EH_PEPPOL_DOCUMENT_LINE_FIELDS.intersection(vals):
            self._eh_check_access('write')
            moves = self.mapped('move_id')
            if vals.get('move_id'):
                moves |= self.env['account.move'].browse(int(vals['move_id']))
            self._eh_refuse_transmitted_peppol_line_change(moves)
        return super().write(vals)

    def unlink(self):
        # Core accounting paths may call ``unlink()`` on an empty filtered
        # journal-item recordset (for example, no COGS rows during invoice
        # cancellation).  No protected Peppol evidence exists in that case;
        # do not turn the no-op into an unrelated model-ACL requirement.
        if not self:
            return super().unlink()
        self._eh_check_access('unlink')
        self._eh_refuse_transmitted_peppol_line_change(self.mapped('move_id'))
        return super().unlink()


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    eh_peppol_outbound_evidence = fields.Boolean(
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Server-owned marker for exact UBL bytes used in an outbound "
            "Peppol attempt. Marked evidence is append-only."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        marking = any(
            vals.get('eh_peppol_outbound_evidence') for vals in vals_list
        )
        if marking and self.env.context.get(
            _EH_PEPPOL_OUTBOUND_ATTACHMENT_INTERNAL
        ) is not _EH_PEPPOL_OUTBOUND_ATTACHMENT_CAPABILITY:
            raise AccessError(_(
                "Outbound Peppol evidence can only be created by the "
                "controlled transmission workflow."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_peppol_outbound_evidence' in vals or self.filtered(
            'eh_peppol_outbound_evidence'
        ):
            raise AccessError(_(
                "Submitted Peppol UBL evidence is immutable. Create a new "
                "credit note or void an unconfirmed attempt; do not replace "
                "the historical bytes."
            ))
        return super().write(vals)

    def unlink(self):
        if self.filtered('eh_peppol_outbound_evidence'):
            raise AccessError(_(
                "Submitted Peppol UBL evidence cannot be deleted."
            ))
        return super().unlink()


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_peppol_endpoint_id = fields.Char(
        string="Peppol endpoint id",
        help=(
            "Partner's Peppol participant id. Defaults at export time to "
            "the partner's VAT or email."
        ),
    )
    eh_peppol_endpoint_scheme = fields.Char(
        string="Peppol endpoint scheme",
        help=(
            "OpenPeppol scheme id for the endpoint. Examples: '0192' "
            "Norway organisation number, '0088' GLN, '0151' Australian "
            "Business Number, '9930' German VAT-id."
        ),
    )
    eh_peppol_legal_id = fields.Char(
        string="Peppol legal entity id",
        help="Company registration number used in PartyLegalEntity/CompanyID.",
    )
    eh_peppol_endpoint_id_normalized = fields.Char(
        compute='_compute_eh_peppol_normalized_ids', store=True, index=True,
    )
    eh_peppol_vat_normalized = fields.Char(
        compute='_compute_eh_peppol_normalized_ids', store=True, index=True,
    )
    eh_peppol_legal_id_normalized = fields.Char(
        compute='_compute_eh_peppol_normalized_ids', store=True, index=True,
    )

    @api.depends('eh_peppol_endpoint_id', 'vat', 'eh_peppol_legal_id')
    def _compute_eh_peppol_normalized_ids(self):
        def normalize(value):
            return ''.join(
                char for char in str(value or '').upper() if char.isalnum()
            )

        for partner in self:
            partner.eh_peppol_endpoint_id_normalized = normalize(
                partner.eh_peppol_endpoint_id
            )
            partner.eh_peppol_vat_normalized = normalize(partner.vat)
            partner.eh_peppol_legal_id_normalized = normalize(
                partner.eh_peppol_legal_id
            )
