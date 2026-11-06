# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Vendor bill intake line.

Stores one extracted bill line plus the result of the 3 way match
against the matching purchase order line and the goods receipt
(stock moves) on that PO line.
"""

import hashlib
import json
import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_MAX_CODE_LENGTH = 256
_MAX_DESCRIPTION_LENGTH = 2000
_MAX_DECISION_REASON_LENGTH = 8000
_LINE_SOURCE_FIELDS = frozenset({
    'sequence', 'product_code', 'product_id', 'description', 'invoice_qty',
    'invoice_price', 'subtotal', 'tax_ids',
})
_LINE_SERVER_FIELDS = frozenset({
    'company_id',
    'purchase_order_line_id', 'po_qty', 'po_price', 'received_qty',
    'match_status', 'qty_diff', 'price_diff', 'qty_diff_pct',
    'price_diff_pct', 'match_notes', 'overridden_at', 'overridden_by_id',
    'decision_log_id', 'match_evidence_fingerprint',
    'extractor_confidence', 'extractor_metadata_json', 'tax_provenance',
})


class EhApIntakeLine(models.Model):
    _name = 'eh.ap.intake.line'
    _description = "Vendor Bill Intake Line"
    _inherit = ['eh.workflow.guard']
    _order = 'intake_id, sequence, id'

    # match_status is the per-line match outcome and a manager-gated field:
    # the matcher sets it (inside the intake's sudo action_match) and only a
    # manager may flip an exception line to 'overridden'/'rejected' through the
    # actions below. A plain user must not RPC-write match_status='overridden'
    # to push an out-of-tolerance line into the postable set and skip the
    # override gate, so it is guarded here.
    _eh_guarded_fields = ('match_status',)

    intake_id = fields.Many2one(
        'eh.ap.intake', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    product_code = fields.Char(
        help="Vendor or internal code as captured.",
    )
    product_id = fields.Many2one(
        'product.product', string="Product",
    )
    description = fields.Char(
        help="Optional free text description as captured.",
    )

    invoice_qty = fields.Float(
        digits='Product Unit', default=0.0,
    )
    invoice_price = fields.Float(
        digits='Product Price', default=0.0,
    )
    subtotal = fields.Float(
        digits='Account', default=0.0,
    )
    extractor_confidence = fields.Float(readonly=True, copy=False)
    extractor_metadata_json = fields.Text(readonly=True, copy=False)

    tax_ids = fields.Many2many(
        'account.tax',
        'eh_ap_intake_line_tax_rel',
        'line_id', 'tax_id',
        string="Taxes",
        domain="[('type_tax_use', '=', 'purchase')]",
        help=(
            "Taxes applied to this line on the posted vendor bill. "
            "Auto-populated from the matched PO line, or from the "
            "product's supplier taxes when no PO line is matched. "
            "Can be edited manually before posting."
        ),
    )
    tax_provenance = fields.Selection([
        ('none', "None"),
        ('po', "Purchase Order"),
        ('product', "Product Default"),
        ('extractor', "Document Extractor"),
        ('manual', "Manual"),
    ], default='none', readonly=True, copy=False,
        help=(
            "Identifies whether the reviewed taxes came from the PO, product, "
            "extractor, or a manual accounting decision. Automatic taxes are "
            "discarded and derived again whenever matching is invalidated."
        ),
    )

    purchase_order_line_id = fields.Many2one(
        'purchase.order.line', string="PO Line", readonly=True,
        ondelete='set null',
    )
    po_qty = fields.Float(readonly=True, digits='Product Unit')
    po_price = fields.Float(readonly=True, digits='Product Price')
    received_qty = fields.Float(readonly=True, digits='Product Unit')

    match_status = fields.Selection([
        ('pending', "Pending"),
        ('ok', "OK"),
        ('qty_exception', "Qty Exception"),
        ('price_exception', "Price Exception"),
        ('over_received', "Over Received"),
        ('no_match', "No Match"),
        ('overridden', "Overridden"),
        ('rejected', "Rejected"),
    ], default='pending')
    qty_diff = fields.Float(readonly=True, digits='Product Unit')
    price_diff = fields.Float(readonly=True, digits='Product Price')
    qty_diff_pct = fields.Float(readonly=True)
    price_diff_pct = fields.Float(readonly=True)
    match_notes = fields.Char(readonly=True)

    override_reason = fields.Text(groups='eh_account_base.group_eh_manager')
    overridden_at = fields.Datetime(readonly=True, copy=False)
    overridden_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    decision_log_id = fields.Many2one(
        'eh.ap.intake.line.decision', string="Current Decision",
        readonly=True, copy=False, ondelete='restrict',
    )
    decision_log_ids = fields.One2many(
        'eh.ap.intake.line.decision', 'line_id', string="Decision History",
        readonly=True,
    )
    match_evidence_fingerprint = fields.Char(
        readonly=True, copy=False, index=True,
        help=(
            "SHA-256 over the reviewed line, PO, receipt, tolerance and "
            "decision evidence. Posting recomputes it while sources are "
            "locked."
        ),
    )

    company_id = fields.Many2one(
        related='intake_id.company_id', store=True, readonly=True,
    )

    @api.onchange('invoice_qty', 'invoice_price')
    def _onchange_compute_subtotal(self):
        for line in self:
            line.subtotal = line.invoice_qty * line.invoice_price

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(vals or {}) for vals in vals_list]
        if not self.env.su:
            forged = sorted(set().union(*(
                set(vals) & _LINE_SERVER_FIELDS for vals in vals_list
            )))
            if forged:
                raise AccessError(_(
                    "AP line match and audit fields are server-owned: %s.",
                    ', '.join(forged),
                ))
            for vals in vals_list:
                if 'tax_ids' in vals:
                    vals['tax_provenance'] = 'manual'
        self._eh_validate_input_values(vals_list)
        intake_ids = sorted({
            vals.get('intake_id') for vals in vals_list
            if vals.get('intake_id')
        })
        intakes = self.env['eh.ap.intake'].browse(intake_ids).exists()
        if len(intakes) != len(intake_ids):
            raise ValidationError(_("Every AP line must reference a valid intake."))
        if not self.env.su and intakes:
            intakes._eh_check_access('write')
            intakes._eh_lock_rows()
            self._eh_assert_intakes_editable(intakes)
        lines = super().create(vals_list)
        lines._eh_validate_scope()
        if not self.env.su:
            lines.mapped('intake_id').sudo()._eh_invalidate_review(
                _("Bill lines changed; previous match and duplicate approval "
                  "were invalidated."),
            )
        return lines

    def write(self, vals):
        vals = dict(vals or {})
        if not self.env.su:
            forged = sorted(set(vals) & _LINE_SERVER_FIELDS)
            if forged:
                raise AccessError(_(
                    "AP line match and audit fields are server-owned: %s.",
                    ', '.join(forged),
                ))
            if 'tax_ids' in vals:
                vals['tax_provenance'] = 'manual'
            if 'intake_id' in vals:
                raise AccessError(_(
                    "An AP intake line cannot be moved to another intake."
                ))
            if 'override_reason' in vals and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only ERP Heritage managers can prepare line decisions."
                ))
        self._eh_validate_input_values([vals])
        source_change = bool(set(vals) & _LINE_SOURCE_FIELDS)
        staging_change = 'override_reason' in vals
        if source_change or staging_change:
            if not self.env.su:
                self._eh_check_access('write')
            self._eh_lock_parent_and_lines()
            self._eh_assert_intakes_editable(self.mapped('intake_id'))
        result = super().write(vals)
        self._eh_validate_scope()
        if source_change and not self.env.su:
            self.sudo().write({
                'extractor_confidence': 0.0,
                'extractor_metadata_json': False,
            })
            self.mapped('intake_id').sudo()._eh_invalidate_review(
                _("Bill lines changed; previous match and duplicate approval "
                  "were invalidated."),
            )
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        intakes = self.mapped('intake_id')
        self._eh_lock_parent_and_lines()
        self._eh_assert_intakes_editable(intakes)
        result = super().unlink()
        if not self.env.su:
            intakes.sudo()._eh_invalidate_review(
                _("Bill lines changed; previous match and duplicate approval "
                  "were invalidated."),
            )
        return result

    @api.model
    def _eh_validate_input_values(self, vals_list):
        for vals in vals_list:
            code = vals.get('product_code')
            description = vals.get('description')
            reason = vals.get('override_reason')
            if code and len(code) > _MAX_CODE_LENGTH:
                raise ValidationError(_(
                    "Captured product code exceeds %s characters.",
                    _MAX_CODE_LENGTH,
                ))
            if description and len(description) > _MAX_DESCRIPTION_LENGTH:
                raise ValidationError(_(
                    "Captured line description exceeds %s characters.",
                    _MAX_DESCRIPTION_LENGTH,
                ))
            if reason and len(reason) > _MAX_DECISION_REASON_LENGTH:
                raise ValidationError(_(
                    "Line decision reason exceeds %s characters.",
                    _MAX_DECISION_REASON_LENGTH,
                ))
            for field_name in ('invoice_qty', 'invoice_price', 'subtotal'):
                if field_name not in vals or vals[field_name] in (False, None):
                    continue
                try:
                    value = float(vals[field_name])
                except (TypeError, ValueError) as exc:
                    raise ValidationError(_(
                        "%s must be a finite number.", field_name,
                    )) from exc
                if not math.isfinite(value):
                    raise ValidationError(_(
                        "%s must be a finite number.", field_name,
                    ))

    @api.constrains('invoice_qty', 'invoice_price', 'subtotal')
    def _check_finite_amounts(self):
        for line in self:
            if not all(math.isfinite(value or 0.0) for value in (
                    line.invoice_qty, line.invoice_price, line.subtotal)):
                raise ValidationError(_(
                    "AP line quantities and amounts must be finite numbers."
                ))

    @api.model
    def _eh_assert_intakes_editable(self, intakes):
        retained = intakes.filtered(
            lambda intake: intake.state in ('posted', 'rejected'),
        )
        if retained:
            raise UserError(_(
                "Posted or rejected AP intake lines are retained evidence and "
                "cannot be edited: %s.",
                ', '.join(retained.mapped('display_name')),
            ))

    def _eh_lock_parent_and_lines(self):
        intakes = self.mapped('intake_id')
        intakes._eh_lock_rows()
        ids = sorted(set(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_ap_intake_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(ids)],
            )
            self.invalidate_recordset()

    def _eh_validate_scope(self):
        for line in self:
            intake = line.intake_id
            company = intake.company_id
            if line.company_id != company:
                raise ValidationError(_(
                    "AP line company must be inherited from its intake."
                ))
            if not self.env.su and company not in self.env.companies:
                raise AccessError(_(
                    "AP line company %s is outside the user's active "
                    "companies.", company.display_name,
                ))
            if not self.env.su:
                intake._eh_check_access('read')
                if line.product_id:
                    line.product_id._eh_check_access('read')
                if line.tax_ids:
                    line.tax_ids._eh_check_access('read')
            product_company = line.product_id.product_tmpl_id.company_id
            if product_company and product_company != company:
                raise ValidationError(_(
                    "Line product must be shared or belong to intake company."
                ))
            bad_taxes = line.tax_ids.filtered(
                lambda tax: tax.company_id != company
                or tax.type_tax_use not in ('purchase', 'none'),
            )
            if bad_taxes:
                raise ValidationError(_(
                    "Line taxes must be purchase taxes in intake company."
                ))
            po_line = line.purchase_order_line_id
            if po_line and (
                    po_line.company_id != company
                    or po_line.order_id.partner_id.commercial_partner_id
                    != intake.partner_id.commercial_partner_id
                    or (line.product_id and po_line.product_id != line.product_id)):
                raise ValidationError(_(
                    "Matched purchase-order line does not belong to intake "
                    "company, vendor and product."
                ))

    # ---- match ----

    def _evaluate_match(self, profile, consumed_by_po_line=None):
        consumed_by_po_line = (
            consumed_by_po_line
            if consumed_by_po_line is not None else {}
        )
        for line in self:
            line._eh_clear_match_derived_taxes()
            line._reset_match_fields()
            po_line = line._find_po_line()
            if not po_line:
                line.match_status = 'no_match'
                line.match_notes = _("No matching PO line found.")
                # Even without a PO match, fall back to the product's
                # supplier taxes so the posted bill carries tax. A bill
                # with no taxes on a tax-aware company silently breaks
                # tax reporting; better to default and let the user
                # adjust than to drop the field on the floor.
                if line.tax_provenance not in ('manual', 'extractor'):
                    taxes = line.product_id.supplier_taxes_id.filtered(
                        lambda t: t.company_id == line.intake_id.company_id
                    ) if line.product_id else self.env['account.tax']
                    line.write({
                        'tax_ids': [(6, 0, taxes.ids)],
                        'tax_provenance': 'product' if taxes else 'none',
                    })
                continue
            line.purchase_order_line_id = po_line.id
            # Compare effective pre-tax unit prices in the bill currency.
            # Extractors normalise invoice_price to line_total / quantity, so
            # it is net of any document line discount.  The PO comparator must
            # therefore apply the PO discount after currency conversion; using
            # the gross PO price accepts an undiscounted vendor invoice and
            # rejects the economically correct discounted price.
            po_price = line._eh_po_price_in_bill_currency(po_line)
            line.po_qty = po_line.product_qty
            line.po_price = po_price
            # Inherit taxes from the matched PO line. Users can still
            # edit before posting, but the default is the PO's tax
            # treatment which is the auditable reference.
            if line.tax_provenance != 'manual':
                line.write({
                    'tax_ids': [(6, 0, po_line.tax_ids.ids)],
                    'tax_provenance': 'po',
                })
            received = line._compute_received_qty(po_line)
            line.received_qty = received
            # Bill only the quantity still un-invoiced against this PO line:
            # remaining billable = received goods less what earlier bills
            # already invoiced (po_line.qty_invoiced). Without this the same
            # goods receipt can be billed on every intake, so the same
            # delivery gets paid repeatedly. qty_invoiced is 0 for a first
            # bill, so single-bill behaviour is unchanged.
            prior_intake_qty = consumed_by_po_line.get(po_line.id, 0.0)
            control_quantity = line._eh_bill_control_quantity(po_line)
            billable = max(
                control_quantity - po_line.qty_invoiced - prior_intake_qty,
                0.0,
            )
            # Every captured sibling consumes capacity, including an
            # exception which may later receive a manager override.  A bad
            # first line must never leave the same receipt fully available to
            # a later sibling in this intake.
            consumed_by_po_line[po_line.id] = (
                prior_intake_qty + max(line.invoice_qty, 0.0)
            )

            qty_diff = line.invoice_qty - billable
            price_diff = line.invoice_price - po_price
            line.qty_diff = qty_diff
            line.price_diff = price_diff
            line.qty_diff_pct = (
                (qty_diff / billable * 100.0) if billable else 0.0
            )
            line.price_diff_pct = (
                (price_diff / po_price * 100.0) if po_price else 0.0
            )

            # Over receipt: invoiced qty exceeds PO qty beyond tolerance.
            over_receipt_limit = max(
                max(po_line.product_qty - po_line.qty_invoiced, 0.0)
                * (1 + profile.over_receipt_pct / 100.0)
                - prior_intake_qty,
                0.0,
            )
            if line.invoice_qty > over_receipt_limit:
                line.match_status = 'over_received'
                line.match_notes = _(
                    "Invoice qty %(inv).4f exceeds PO qty %(po).4f beyond "
                    "tolerance %(pct).2f%%.",
                    inv=line.invoice_qty,
                    po=po_line.product_qty,
                    pct=profile.over_receipt_pct,
                )
                continue

            # Qty mismatch vs received.
            qty_tolerance = max(
                billable * profile.qty_tolerance_pct / 100.0,
                0.0,
            )
            if abs(qty_diff) > qty_tolerance:
                line.match_status = 'qty_exception'
                line.match_notes = _(
                    "Qty delta %(diff).4f exceeds tolerance %(tol).4f.",
                    diff=qty_diff, tol=qty_tolerance,
                )
                continue

            # Price mismatch vs PO (compared in the bill currency).
            price_tolerance = max(
                po_price * profile.price_tolerance_pct / 100.0,
                0.0,
            )
            line_amount_tolerance = line._eh_amount_tolerance_evidence(
                profile,
            )['converted_amount']
            if (
                abs(price_diff) > price_tolerance
                and abs(price_diff * line.invoice_qty) > line_amount_tolerance
            ):
                line.match_status = 'price_exception'
                line.match_notes = _(
                    "Price delta %(diff).4f exceeds tolerance "
                    "%(tol).4f and amount tolerance %(amt).2f.",
                    diff=price_diff, tol=price_tolerance,
                    amt=line_amount_tolerance,
                )
                continue

            line.match_status = 'ok'
            line.match_notes = _("Within tolerance.")

    @staticmethod
    def _eh_bill_control_quantity(po_line):
        """Ordered services/manual-control lines do not require a receipt."""
        return (
            po_line.qty_received
            if po_line.product_id.purchase_method == 'receive'
            else po_line.product_qty
        )

    def _eh_amount_tolerance_evidence(self, profile):
        """Absolute tolerance converted from profile-company to bill currency."""
        self.ensure_one()
        intake = self.intake_id
        source_currency = (
            profile.amount_tolerance_currency_id
            or profile.company_id.currency_id
        )
        bill_currency = intake.currency_id or intake.company_id.currency_id
        conversion_date = intake.bill_date or fields.Date.context_today(self)
        converted = source_currency._convert(
            profile.amount_tolerance, bill_currency, intake.company_id,
            conversion_date, round=False,
        )
        factor = source_currency._get_conversion_rate(
            source_currency, bill_currency, intake.company_id,
            conversion_date,
        )
        return {
            'source_currency_id': source_currency.id,
            'bill_currency_id': bill_currency.id,
            'company_id': intake.company_id.id,
            'date': str(conversion_date),
            'factor': self._eh_number(factor),
            'source_amount': self._eh_number(profile.amount_tolerance),
            'converted_amount': converted,
            'converted_amount_number': self._eh_number(converted),
        }

    def _eh_po_price_in_bill_currency(self, po_line):
        """Return the PO line unit price expressed in the intake (bill)
        currency.

        The PO line gross price is stated in the PO's currency. When the bill
        is raised in a different currency, convert at the bill date, then
        apply the PO percentage discount.  AP ``invoice_price`` is the
        effective pre-tax price (the extraction pipeline back-solves
        ``line_total / quantity``), so gross-to-net is required for an
        economically like-for-like comparison.
        """
        self.ensure_one()
        po_price = po_line.price_unit
        po_currency = po_line.currency_id or po_line.order_id.currency_id
        intake = self.intake_id
        bill_currency = intake.currency_id or intake.company_id.currency_id
        if po_currency and bill_currency and po_currency != bill_currency:
            conversion_date = (
                intake.bill_date or fields.Date.context_today(self)
            )
            po_price = po_currency._convert(
                po_price, bill_currency, intake.company_id, conversion_date,
                round=False,
            )
        discount = self._eh_po_discount(po_line)
        return po_price * (1.0 - discount / 100.0)

    @staticmethod
    def _eh_po_discount(po_line):
        """Return a finite PO discount or block ambiguous economics."""
        discount = float(getattr(po_line, 'discount', 0.0) or 0.0)
        if not math.isfinite(discount):
            raise UserError(_(
                "Purchase-order line %(line)s has an invalid discount "
                "(%(discount)s%%). Use a finite percentage before matching "
                "the vendor bill.",
                line=po_line.display_name,
                discount=discount,
            ))
        return discount

    def _eh_fx_evidence(self, po_line):
        """Return the effective, unrounded rate used for this PO comparison."""
        self.ensure_one()
        intake = self.intake_id
        po_currency = po_line.currency_id or po_line.order_id.currency_id
        bill_currency = intake.currency_id or intake.company_id.currency_id
        conversion_date = (
            intake.bill_date or fields.Date.context_today(self)
        )
        factor = po_currency._get_conversion_rate(
            po_currency, bill_currency, intake.company_id, conversion_date,
        )
        return {
            'from_currency_id': po_currency.id,
            'to_currency_id': bill_currency.id,
            'company_id': intake.company_id.id,
            'date': str(conversion_date),
            'factor': self._eh_number(factor),
        }

    def _reset_match_fields(self):
        for line in self:
            line.write({
                'purchase_order_line_id': False,
                'po_qty': 0.0,
                'po_price': 0.0,
                'received_qty': 0.0,
                'qty_diff': 0.0,
                'price_diff': 0.0,
                'qty_diff_pct': 0.0,
                'price_diff_pct': 0.0,
                'match_notes': False,
                'override_reason': False,
                'overridden_at': False,
                'overridden_by_id': False,
                'decision_log_id': False,
                'match_evidence_fingerprint': False,
            })

    def _eh_clear_automatic_taxes(self):
        automatic = self.filtered(
            lambda line: line.tax_provenance in (
                'po', 'product', 'extractor', 'none', False,
            ),
        )
        if automatic:
            automatic.sudo().write({
                'tax_ids': [(5, 0, 0)],
                'tax_provenance': 'none',
            })

    def _eh_clear_match_derived_taxes(self):
        derived = self.filtered(
            lambda line: line.tax_provenance in ('po', 'product'),
        )
        if derived:
            derived.sudo().write({
                'tax_ids': [(5, 0, 0)],
                'tax_provenance': 'none',
            })

    def _eh_reset_review_evidence(self):
        """Clear current review pointers; append-only decisions remain."""
        if not self:
            return
        self._eh_clear_automatic_taxes()
        self.sudo().write({
            'purchase_order_line_id': False,
            'po_qty': 0.0,
            'po_price': 0.0,
            'received_qty': 0.0,
            'match_status': 'pending',
            'qty_diff': 0.0,
            'price_diff': 0.0,
            'qty_diff_pct': 0.0,
            'price_diff_pct': 0.0,
            'match_notes': False,
            'override_reason': False,
            'overridden_at': False,
            'overridden_by_id': False,
            'decision_log_id': False,
            'match_evidence_fingerprint': False,
        })

    def _eh_match_lock_key(self):
        self.ensure_one()
        intake = self.intake_id
        return 'eh-ap-match|%s|%s|%s|%s' % (
            intake.company_id.id,
            intake.partner_id.commercial_partner_id.id or 0,
            intake.purchase_order_id.id or 0,
            self.product_id.id or 0,
        )

    def _eh_lock_match_sources(self):
        """Serialize AP matches and lock every selected PO/receipt source."""
        if not self:
            return self
        self._eh_lock_parent_and_lines()
        for key in sorted({line._eh_match_lock_key() for line in self}):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key],
            )
        candidate_ids = set(self.mapped('purchase_order_line_id').ids)
        for line in self:
            candidate_ids.update(line._find_po_line().ids)
        if candidate_ids:
            ordered_ids = tuple(sorted(candidate_ids))
            candidate_lines = self.env['purchase.order.line'].browse(
                ordered_ids,
            )
            currency_ids = set(self.mapped('intake_id.currency_id').ids)
            currency_ids.update(candidate_lines.mapped('currency_id').ids)
            currency_ids.update(
                candidate_lines.mapped('order_id.currency_id').ids,
            )
            self.env['res.currency.rate']._eh_ap_lock_currency_ids(
                currency_ids,
            )
            self.env.cr.execute(
                "SELECT id FROM purchase_order_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [ordered_ids],
            )
            self.env.cr.execute(
                "SELECT id FROM stock_move WHERE purchase_line_id IN %s "
                "ORDER BY id FOR UPDATE",
                [ordered_ids],
            )
            candidate_lines.invalidate_recordset()
            self.env['stock.move'].search([
                ('purchase_line_id', 'in', list(ordered_ids)),
            ]).invalidate_recordset()
        self.invalidate_recordset()
        return self

    @staticmethod
    def _eh_number(value):
        return format(float(value or 0.0), '.16g')

    def _eh_match_evidence_payload(self, profile):
        self.ensure_one()
        intake = self.intake_id
        po_line = self.purchase_order_line_id
        po_payload = False
        if po_line:
            moves = po_line.move_ids.sorted('id')
            po_payload = {
                'id': po_line.id,
                'write_date': str(po_line.write_date or ''),
                'order_id': po_line.order_id.id,
                'company_id': po_line.company_id.id,
                'commercial_partner_id': (
                    po_line.order_id.partner_id.commercial_partner_id.id
                ),
                'product_id': po_line.product_id.id,
                'currency_id': po_line.currency_id.id,
                'product_qty': self._eh_number(po_line.product_qty),
                'price_unit': self._eh_number(po_line.price_unit),
                'discount': self._eh_number(getattr(po_line, 'discount', 0.0)),
                'purchase_method': po_line.product_id.purchase_method or '',
                'qty_received': self._eh_number(po_line.qty_received),
                'qty_invoiced': self._eh_number(po_line.qty_invoiced),
                'fx': self._eh_fx_evidence(po_line),
                'moves': [
                    (
                        move.id,
                        str(move.write_date or ''),
                        move.state or '',
                        self._eh_number(move.product_uom_qty),
                        move.location_id.usage or '',
                        move.location_dest_id.usage or '',
                    )
                    for move in moves
                ],
            }
        decision = self.decision_log_id
        return {
            'schema': 'eh-ap-match-v2',
            'intake': {
                'id': intake.id,
                'company_id': intake.company_id.id,
                'commercial_partner_id': (
                    intake.partner_id.commercial_partner_id.id
                ),
                'purchase_order_id': intake.purchase_order_id.id or 0,
                'currency_id': intake.currency_id.id,
                'bill_date': str(intake.bill_date or ''),
                'vendor_reference': intake._eh_normalize_reference(
                    intake.vendor_reference,
                ),
                'extracted_total': self._eh_number(intake.extracted_total),
                'source_fingerprint': intake.source_fingerprint or '',
            },
            'line': {
                'id': self.id,
                'sequence': self.sequence,
                'product_code': self.product_code or '',
                'product_id': self.product_id.id or 0,
                'description': self.description or '',
                'invoice_qty': self._eh_number(self.invoice_qty),
                'invoice_price': self._eh_number(self.invoice_price),
                'subtotal': self._eh_number(self.subtotal),
                'extractor_confidence': self._eh_number(
                    self.extractor_confidence,
                ),
                'extractor_metadata_json': (
                    self.extractor_metadata_json or ''
                ),
                'taxes': [
                    (
                        tax.id,
                        str(tax.write_date or ''),
                        tax.company_id.id,
                        tax.type_tax_use or '',
                        tax.amount_type or '',
                        self._eh_number(tax.amount),
                        bool(tax.price_include),
                    )
                    for tax in self.tax_ids.sorted('id')
                ],
                'tax_provenance': self.tax_provenance or 'none',
                'match_status': self.match_status or '',
                'po_qty': self._eh_number(self.po_qty),
                'po_price': self._eh_number(self.po_price),
                'received_qty': self._eh_number(self.received_qty),
                'qty_diff': self._eh_number(self.qty_diff),
                'price_diff': self._eh_number(self.price_diff),
                'qty_diff_pct': self._eh_number(self.qty_diff_pct),
                'price_diff_pct': self._eh_number(self.price_diff_pct),
            },
            'profile': {
                'id': profile.id,
                'write_date': str(profile.write_date or ''),
                'company_id': profile.company_id.id,
                'qty_tolerance_pct': self._eh_number(
                    profile.qty_tolerance_pct,
                ),
                'price_tolerance_pct': self._eh_number(
                    profile.price_tolerance_pct,
                ),
                'amount_tolerance': self._eh_number(
                    profile.amount_tolerance,
                ),
                'amount_tolerance_evidence': (
                    self._eh_amount_tolerance_evidence(profile)
                ),
                'over_receipt_pct': self._eh_number(
                    profile.over_receipt_pct,
                ),
            },
            'purchase_order_line': po_payload,
            'same_po_intake_lines': [
                (
                    sibling.id,
                    sibling.sequence,
                    sibling.product_id.id or 0,
                    self._eh_number(sibling.invoice_qty),
                    self._eh_number(sibling.invoice_price),
                    self._eh_number(sibling.subtotal),
                    tuple(sorted(sibling.tax_ids.ids)),
                    sibling.purchase_order_line_id.id or 0,
                )
                for sibling in intake.line_ids.filtered(
                    lambda candidate: candidate.purchase_order_line_id
                    == po_line
                ).sorted(lambda candidate: (candidate.sequence, candidate.id))
            ] if po_line else [],
            'decision': (
                {
                    'id': decision.id,
                    'decision': decision.decision,
                    'reason': decision.reason,
                    'evidence_fingerprint': decision.evidence_fingerprint,
                    'decided_by_id': decision.decided_by_id.id,
                    'decided_at': str(decision.decided_at or ''),
                }
                if decision else False
            ),
        }

    def _eh_current_match_fingerprint(self, profile):
        self.ensure_one()
        payload = self._eh_match_evidence_payload(profile)
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _eh_stamp_match_fingerprint(self, profile):
        self.ensure_one()
        fingerprint = self._eh_current_match_fingerprint(profile)
        self.sudo().write({'match_evidence_fingerprint': fingerprint})
        return fingerprint

    def _eh_validate_posting_evidence(self):
        for line in self:
            intake = line.intake_id
            profile = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
                intake.partner_id, company=intake.company_id,
            )
            line._eh_validate_scope()
            values = (
                line.invoice_qty, line.invoice_price, line.subtotal,
                line.po_qty, line.po_price, line.received_qty,
            )
            if not all(math.isfinite(value or 0.0) for value in values):
                raise UserError(_(
                    "Line %s contains a non-finite quantity or amount.",
                    line.display_name,
                ))
            if line.invoice_qty <= 0:
                raise UserError(_(
                    "Line %s must have a positive invoice quantity.",
                    line.display_name,
                ))
            expected_subtotal = line.invoice_qty * line.invoice_price
            if intake.currency_id.compare_amounts(
                    line.subtotal, expected_subtotal) != 0:
                raise UserError(_(
                    "Line %(line)s subtotal %(subtotal)s does not match "
                    "quantity × unit price %(expected)s.",
                    line=line.display_name,
                    subtotal=line.subtotal,
                    expected=expected_subtotal,
                ))
            current_po_line = line._find_po_line()
            if current_po_line != line.purchase_order_line_id:
                raise UserError(_(
                    "Purchase-order availability changed after line %s was "
                    "reviewed. Match the intake again.", line.display_name,
                ))
            if line.match_status not in ('ok', 'overridden'):
                raise UserError(_(
                    "Line %(line)s is not approved for posting (%(status)s).",
                    line=line.display_name, status=line.match_status,
                ))
            if line.match_status == 'overridden' and (
                    not line.decision_log_id
                    or line.decision_log_id.decision != 'override'):
                raise UserError(_(
                    "Line %s has no append-only override decision.",
                    line.display_name,
                ))
            expected = line._eh_current_match_fingerprint(profile)
            if not line.match_evidence_fingerprint \
                    or line.match_evidence_fingerprint != expected:
                raise UserError(_(
                    "Match evidence for line %s changed after review. Match "
                    "the intake again.", line.display_name,
                ))

    def _find_po_line(self):
        self.ensure_one()
        if not self.product_id:
            return self.env['purchase.order.line']
        domain = [
            ('product_id', '=', self.product_id.id),
            ('order_id.partner_id.commercial_partner_id', '=',
             self.intake_id.partner_id.commercial_partner_id.id),
            ('order_id.state', 'in', ['purchase', 'done']),
            ('order_id.company_id', '=', self.intake_id.company_id.id),
        ]
        if self.intake_id.purchase_order_id:
            domain.append(
                ('order_id', '=', self.intake_id.purchase_order_id.id),
            )
        candidates = self.env['purchase.order.line'].search(
            domain, order='create_date asc',
        )
        # Prefer a PO line that still has un-billed quantity so a fully
        # invoiced line is not reused to bill the same receipt again. Fall
        # back to the oldest line when every candidate is fully invoiced, so
        # the match still runs and flags the over-receipt / qty exception.
        open_lines = candidates.filtered(
            lambda pl: (
                self._eh_bill_control_quantity(pl) - pl.qty_invoiced
            ) > 0,
        )
        return (open_lines[:1] or candidates[:1])

    @staticmethod
    def _compute_received_qty(po_line):
        # Odoo's stored/computed value accounts for returns and UoM
        # conversion. Summing inbound moves alone overstates receipts after a
        # supplier return and can release payment for goods no longer held.
        return po_line.qty_received

    # ---- override ----

    def action_override(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can override match exceptions."
            ))
        actor = self.env.user
        self._eh_lock_match_sources()
        for intake in self.mapped('intake_id'):
            intake._eh_verify_source_evidence()
        lines = self._eh_workflow_action()
        for line in lines:
            if line.match_status not in (
                'qty_exception', 'price_exception',
                'over_received', 'no_match',
            ):
                raise UserError(_(
                    "Only exception lines can be overridden.",
                ))
            reason = (line.override_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Provide an override reason before approving.",
                ))
            if len(reason) > _MAX_DECISION_REASON_LENGTH:
                raise UserError(_(
                    "Line decision reason exceeds %s characters.",
                    _MAX_DECISION_REASON_LENGTH,
                ))
            profile = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
                line.intake_id.partner_id, company=line.company_id,
            )
            current = line._eh_current_match_fingerprint(profile)
            if not line.match_evidence_fingerprint \
                    or line.match_evidence_fingerprint != current:
                raise UserError(_(
                    "Match evidence for line %s changed. Match again before "
                    "overriding.", line.display_name,
                ))
            decided_at = fields.Datetime.now()
            log = self.env['eh.ap.intake.line.decision'].sudo().create({
                'line_id': line.id,
                'decision': 'override',
                'reason': reason,
                'evidence_fingerprint': current,
                'decided_by_id': actor.id,
                'decided_at': decided_at,
            })
            line.write({
                'match_status': 'overridden',
                'override_reason': False,
                'overridden_at': decided_at,
                'overridden_by_id': actor.id,
                'decision_log_id': log.id,
            })
            line._eh_stamp_match_fingerprint(profile)
            line.intake_id._compute_match_summary()
            if line.intake_id.exception_count == 0:
                # 'state' is guarded by eh.workflow.guard on the intake; this
                # sanctioned transition (all exceptions cleared) writes as su.
                line.intake_id._eh_workflow_write({'state': 'matched'})

    def action_reject_line(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can reject intake lines."
            ))
        actor = self.env.user
        self._eh_lock_match_sources()
        for intake in self.mapped('intake_id'):
            intake._eh_verify_source_evidence()
        lines = self._eh_workflow_action()
        for line in lines:
            if line.match_status not in (
                    'qty_exception', 'price_exception', 'over_received',
                    'no_match'):
                raise UserError(_(
                    "Only exception lines can be rejected."
                ))
            reason = (line.override_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Provide a decision reason before rejecting the line."
                ))
            profile = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
                line.intake_id.partner_id, company=line.company_id,
            )
            current = line._eh_current_match_fingerprint(profile)
            if not line.match_evidence_fingerprint \
                    or line.match_evidence_fingerprint != current:
                raise UserError(_(
                    "Match evidence for line %s changed. Match again before "
                    "rejecting.", line.display_name,
                ))
            decided_at = fields.Datetime.now()
            log = self.env['eh.ap.intake.line.decision'].sudo().create({
                'line_id': line.id,
                'decision': 'reject',
                'reason': reason,
                'evidence_fingerprint': current,
                'decided_by_id': actor.id,
                'decided_at': decided_at,
            })
            line.write({
                'match_status': 'rejected',
                'override_reason': False,
                'overridden_at': decided_at,
                'overridden_by_id': actor.id,
                'decision_log_id': log.id,
            })
            line._eh_stamp_match_fingerprint(profile)

    def action_reopen_line(self):
        """Manager-audited exit from a rejected-line dead end."""
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can reopen rejected intake lines."
            ))
        actor = self.env.user
        self._eh_lock_match_sources()
        for intake in self.mapped('intake_id'):
            intake._eh_verify_source_evidence()
        lines = self._eh_workflow_action()
        for line in lines:
            if line.match_status != 'rejected':
                raise UserError(_("Only rejected lines can be reopened."))
            reason = (line.override_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "Provide a decision reason before reopening the line."
                ))
            if len(reason) > _MAX_DECISION_REASON_LENGTH:
                raise UserError(_(
                    "Line decision reason exceeds %s characters.",
                    _MAX_DECISION_REASON_LENGTH,
                ))
            profile = self.env['eh.ap.tolerance.profile'].resolve_for_partner(
                line.intake_id.partner_id, company=line.company_id,
            )
            current = line._eh_current_match_fingerprint(profile)
            if not line.match_evidence_fingerprint \
                    or line.match_evidence_fingerprint != current:
                raise UserError(_(
                    "Match evidence for line %s changed. Match again before "
                    "reopening.", line.display_name,
                ))
            decided_at = fields.Datetime.now()
            self.env['eh.ap.intake.line.decision'].sudo().create({
                'line_id': line.id,
                'decision': 'reopen',
                'reason': reason,
                'evidence_fingerprint': current,
                'decided_by_id': actor.id,
                'decided_at': decided_at,
            })
            line.intake_id._eh_invalidate_review(_(
                "Rejected line reopened by %(user)s. Reason: %(reason)s",
                user=actor.name, reason=reason,
            ))

    # ---- build move line ----

    def _build_invoice_line_vals(self):
        self.ensure_one()
        vals = {
            'name': (
                self.description
                or (self.product_id and self.product_id.display_name)
                or self.product_code
                or '/'
            ),
            'quantity': self.invoice_qty,
            'price_unit': self.invoice_price,
        }
        if self.product_id:
            vals['product_id'] = self.product_id.id
        if self.purchase_order_line_id:
            po_line = self.purchase_order_line_id
            vals['purchase_line_id'] = po_line.id
            # Matching quantities and unit prices are expressed in the PO
            # line UoM. Persist that same UoM on the bill; otherwise core
            # defaults to the product base UoM and changes the economic
            # quantity while the three-way-match evidence still says PO UoM.
            vals['product_uom_id'] = po_line.product_uom_id.id
            # invoice_price is already the captured effective unit price.  Do
            # not reapply the PO discount on the bill: that would discount the
            # vendor document twice.  The original PO gross price + discount
            # remain in the sealed match fingerprint and purchase-line link.
            if 'discount' in self.env['account.move.line']._fields:
                self._eh_po_discount(po_line)
                vals['discount'] = 0.0
        elif self.product_id:
            vals['product_uom_id'] = self.product_id.uom_id.id
        # An explicit empty command is significant: omitting the field lets
        # account.move.line's product precompute silently restore supplier
        # taxes even when the reviewed PO line is tax-free.
        vals['tax_ids'] = [(6, 0, self.tax_ids.ids)]
        return vals
