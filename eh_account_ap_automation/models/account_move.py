# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Release-to-pay governance on vendor bills.

A native, stored status that tells AP whether a posted vendor bill is
cleared for payment. It folds two controls:

* A manual payment hold (eh_payment_hold) with a reason, so a controller
  can park a bill regardless of matching.
* A purchase bill-control check that honours each product's purchase
  method: an 'ordered' product may be billed up to the ordered quantity,
  a 'received' product only up to what has actually been received.
  Billing beyond that allowance flags an exception.

The status is queryable for the journal dashboard (count of bills awaiting
release, on hold, or in exception).
"""

import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_EH_AP_PROVENANCE_INTERNAL = 'eh_ap_provenance_internal'
_EH_AP_PAYMENT_CONTROL_INTERNAL = 'eh_ap_payment_control_internal'
_EH_AP_REASON_INTERNAL = 'eh_ap_payment_reason_internal'
_EH_AP_REASON_CAPABILITY = object()
_EH_AP_PROVENANCE_FIELDS = frozenset({'eh_ap_intake_id'})
_EH_AP_PAYMENT_CONTROL_FIELDS = frozenset({
    'eh_payment_hold', 'eh_hold_reason', 'eh_held_at', 'eh_held_by_id',
    'eh_released_at', 'eh_released_by_id', 'eh_release_to_pay',
    'eh_exception_waived', 'eh_exception_waiver_reason',
    'eh_exception_waiver_fingerprint', 'eh_exception_waived_at',
    'eh_exception_waived_by_id',
})
_MAX_PAYMENT_REASON_LENGTH = 2000


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_ap_intake_id = fields.Many2one(
        'eh.ap.intake', string="AP Intake Source", readonly=True, copy=False,
        ondelete='restrict', index=True,
    )
    eh_payment_hold = fields.Boolean(
        string="Payment Hold", copy=False, tracking=True, readonly=True,
        help="When set, the bill is parked and never reports as released "
             "to pay, whatever the matching outcome.",
    )
    eh_payment_action_reason = fields.Char(
        string="Payment Control Reason", copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "Required input for the next Hold Payment, Release Payment, or "
            "Waive Billing Exception action."
        ),
    )
    eh_hold_reason = fields.Char(
        string="Current Hold Reason", copy=False, readonly=True,
    )
    eh_held_at = fields.Datetime(readonly=True, copy=False)
    eh_held_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    eh_released_at = fields.Datetime(readonly=True, copy=False)
    eh_released_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    eh_payment_hold_log_ids = fields.One2many(
        'eh.ap.payment.hold.log', 'move_id', readonly=True,
        string="Payment Hold History",
    )
    eh_exception_waived = fields.Boolean(
        string="Billing Exception Waived", readonly=True, copy=False,
        help=(
            "Manager approval of the exact current purchase-control "
            "exception. Any change to bill, PO, receipt, or purchase method "
            "makes the waiver stale and blocks settlement again."
        ),
    )
    eh_exception_waiver_reason = fields.Text(readonly=True, copy=False)
    eh_exception_waiver_fingerprint = fields.Char(
        readonly=True, copy=False, index=True,
    )
    eh_exception_waived_at = fields.Datetime(readonly=True, copy=False)
    eh_exception_waived_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    eh_release_to_pay = fields.Selection(
        [
            ('not_ready', "Not Ready"),
            ('hold', "On Hold"),
            ('exception', "Billing Exception"),
            ('exception_waived', "Exception Waived"),
            ('released', "Released to Pay"),
            ('settled', "Settled"),
        ],
        string="Release to Pay", compute='_compute_eh_release_to_pay',
        store=True,
        help="Released only when the bill is posted, not on hold, and "
             "within the purchase bill-control allowance.",
    )

    _eh_ap_intake_unique = models.Constraint(
        'UNIQUE(eh_ap_intake_id)',
        'An AP intake can be linked to only one journal entry.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(
                any(vals.get(field_name)
                    for field_name in _EH_AP_PROVENANCE_FIELDS)
                for vals in vals_list) and not (
                    self.env.su
                    and self.env.context.get(_EH_AP_PROVENANCE_INTERNAL)):
            raise AccessError(_(
                "AP intake journal-entry provenance is server-owned."
            ))
        if any(
                any(
                    vals.get(field_name) not in (False, None, 'not_ready')
                    for field_name in _EH_AP_PAYMENT_CONTROL_FIELDS
                )
                for vals in vals_list) and not (
                    self.env.su
                    and self.env.context.get(
                        _EH_AP_PAYMENT_CONTROL_INTERNAL)):
            raise AccessError(_(
                "AP payment-control audit fields are server-owned."
            ))
        if any(vals.get('eh_payment_action_reason') for vals in vals_list) \
                and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can prepare payment-control "
                "reasons."
            ))
        for vals in vals_list:
            reason = vals.get('eh_payment_action_reason')
            if reason and len(reason) > _MAX_PAYMENT_REASON_LENGTH:
                raise UserError(_(
                    "Payment-control reason exceeds %s characters.",
                    _MAX_PAYMENT_REASON_LENGTH,
                ))
        return super().create(vals_list)

    def write(self, vals):
        if _EH_AP_PROVENANCE_FIELDS.intersection(vals) and not (
                self.env.su
                and self.env.context.get(_EH_AP_PROVENANCE_INTERNAL)):
            raise AccessError(_(
                "AP intake journal-entry provenance is server-owned."
            ))
        if _EH_AP_PAYMENT_CONTROL_FIELDS.intersection(vals) and not (
                self.env.su
                and self.env.context.get(
                    _EH_AP_PAYMENT_CONTROL_INTERNAL)):
            raise AccessError(_(
                "AP payment-control audit fields can only change through "
                "the Hold Payment and Release Payment actions."
            ))
        if 'eh_payment_action_reason' in vals and not self.env.su:
            self._eh_check_access('write')
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only ERP Heritage managers can prepare payment hold "
                    "or release reasons."
                ))
            invalid = self.filtered(
                lambda move: move.move_type != 'in_invoice'
                or move.state != 'posted',
            )
            if invalid:
                raise UserError(_(
                    "Payment-control reasons can only be prepared on posted "
                    "vendor bills."
                ))
        reason = vals.get('eh_payment_action_reason')
        if reason and len(reason) > _MAX_PAYMENT_REASON_LENGTH:
            raise UserError(_(
                "Payment-control reason exceeds %s characters.",
                _MAX_PAYMENT_REASON_LENGTH,
            ))
        # The reason is the one manager-entered input to the hold/release
        # action. On a sealed AP bill it still has to pass Base's fail-closed
        # stored-field guard, but only through this already ACL/group/state
        # checked, exact-field corridor. The object identity cannot be forged
        # by an RPC context.
        if (
            set(vals) == {'eh_payment_action_reason'}
            and self._eh_frozen_by_seal()
            and self.env.context.get(_EH_AP_REASON_INTERNAL)
            is not _EH_AP_REASON_CAPABILITY
        ):
            guarded = self.with_context(**{
                _EH_AP_REASON_INTERNAL: _EH_AP_REASON_CAPABILITY,
            })
            return guarded._eh_write_sealed_metadata(
                vals,
                {'eh_payment_action_reason'},
            )
        return super().write(vals)

    @api.private
    def _eh_attach_ap_intake(self, intake):
        self.ensure_one()
        intake.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        if self.state != 'draft' or self.move_type != 'in_invoice':
            raise UserError(_(
                "AP intake provenance can only be attached to a draft vendor "
                "bill."
            ))
        if self.eh_ap_intake_id and self.eh_ap_intake_id != intake:
            raise UserError(_(
                "Journal entry already belongs to another AP intake."
            ))
        if intake.move_id and intake.move_id != self:
            raise UserError(_(
                "AP intake already belongs to another journal entry."
            ))
        if self.company_id != intake.company_id \
                or self.partner_id.commercial_partner_id \
                != intake.partner_id.commercial_partner_id:
            raise UserError(_(
                "AP intake and journal entry company/vendor do not match."
            ))
        self.sudo().with_context(
            **{_EH_AP_PROVENANCE_INTERNAL: True}
        )._eh_write_sealed_metadata(
            {'eh_ap_intake_id': intake.id},
            _EH_AP_PROVENANCE_FIELDS,
        )

    @api.depends(
        'state', 'move_type', 'payment_state', 'eh_payment_hold',
        'eh_exception_waived', 'eh_exception_waiver_fingerprint',
        'invoice_line_ids.quantity',
        'invoice_line_ids.purchase_line_id',
        'invoice_line_ids.purchase_line_id.qty_received',
        'invoice_line_ids.purchase_line_id.qty_invoiced',
        'invoice_line_ids.purchase_line_id.product_qty',
        'invoice_line_ids.purchase_line_id.product_id.purchase_method',
    )
    def _compute_eh_release_to_pay(self):
        for move in self:
            if move.move_type != 'in_invoice' or move.state != 'posted':
                move.eh_release_to_pay = 'not_ready'
            elif move.payment_state in ('in_payment', 'paid', 'reversed'):
                move.eh_release_to_pay = 'settled'
            elif move.eh_payment_hold:
                move.eh_release_to_pay = 'hold'
            elif move._eh_has_billing_exception():
                move.eh_release_to_pay = (
                    'exception_waived'
                    if move._eh_exception_waiver_is_current()
                    else 'exception'
                )
            else:
                move.eh_release_to_pay = 'released'

    def _eh_has_billing_exception(self, tolerance=0.0):
        """True when any line is billed beyond its purchase-method
        allowance (ordered qty for 'purchase' products, received qty for
        'receive' products). Lines with no purchase link never flag."""
        self.ensure_one()
        for line in self.invoice_line_ids:
            po_line = line.purchase_line_id
            if not po_line:
                continue
            method = po_line.product_id.purchase_method
            if method == 'receive':
                allowance = po_line.qty_received
            else:
                allowance = po_line.product_qty
            if (po_line.qty_invoiced or 0.0) > allowance + tolerance:
                return True
        return False

    @staticmethod
    def _eh_control_number(value):
        return format(float(value or 0.0), '.16g')

    def _eh_exception_waiver_evidence_fingerprint(self):
        """Fingerprint the live exception evidence under row locks."""
        self.ensure_one()
        payload = {
            'schema': 'eh-ap-payment-exception-v1',
            'move': (
                self.id, self.company_id.id, self.state or '',
                self.move_type or '', str(self.date or ''),
            ),
            'lines': [
                {
                    'id': line.id,
                    'quantity': self._eh_control_number(line.quantity),
                    'purchase_line_id': line.purchase_line_id.id,
                    'purchase_line_write_date': str(
                        line.purchase_line_id.write_date or '',
                    ),
                    'purchase_method': (
                        line.purchase_line_id.product_id.purchase_method or ''
                    ),
                    'ordered': self._eh_control_number(
                        line.purchase_line_id.product_qty,
                    ),
                    'received': self._eh_control_number(
                        line.purchase_line_id.qty_received,
                    ),
                    'invoiced': self._eh_control_number(
                        line.purchase_line_id.qty_invoiced,
                    ),
                }
                for line in self.invoice_line_ids.filtered(
                    'purchase_line_id',
                ).sorted('id')
            ],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _eh_exception_waiver_is_current(self):
        self.ensure_one()
        return bool(
            self.eh_exception_waived
            and self.eh_exception_waiver_fingerprint
            and self._eh_has_billing_exception()
            and self.eh_exception_waiver_fingerprint
            == self._eh_exception_waiver_evidence_fingerprint()
        )

    def _eh_lock_billing_sources(self):
        """Lock bills and purchase evidence before release decisions."""
        bills = self.filtered(
            lambda move: move.move_type == 'in_invoice'
            and move.state == 'posted',
        )
        if not bills:
            return bills
        bills.flush_recordset(['state', 'move_type', 'eh_payment_hold'])
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            [tuple(sorted(bills.ids))],
        )
        po_lines = bills.invoice_line_ids.purchase_line_id
        if po_lines:
            po_lines.flush_recordset([
                'product_qty', 'qty_received', 'qty_invoiced',
            ])
            self.env.cr.execute(
                "SELECT id FROM purchase_order_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(sorted(po_lines.ids))],
            )
            po_lines.invalidate_recordset([
                'product_qty', 'qty_received', 'qty_invoiced', 'write_date',
            ])
        bills.invalidate_recordset([
            'state', 'move_type', 'payment_state', 'eh_payment_hold',
            'eh_exception_waived', 'eh_exception_waiver_fingerprint',
            'eh_release_to_pay',
        ])
        return bills

    def _eh_assert_released_for_payment(self):
        """Reject settlement of posted vendor bills that are not released.

        Never trust the stored badge here: it is a display/search aid, while
        the hold flag and live purchase quantities are the authoritative
        controls.  Locking the bills also serializes this check with the hold
        and release actions, so a payment cannot race a newly-applied hold.
        """
        bills = self.filtered(
            lambda move: move.move_type == 'in_invoice'
            and move.state == 'posted',
        )
        if not bills:
            return True
        if not self.env.su:
            bills._eh_check_access('read')
        bills = bills._eh_lock_billing_sources()
        blocked = bills.filtered(
            lambda move: move.eh_payment_hold
            or (
                move._eh_has_billing_exception()
                and not move._eh_exception_waiver_is_current()
            ),
        )
        if blocked:
            raise UserError(_(
                "Payment is blocked until these vendor bills are released: "
                "%s.",
                ', '.join(blocked.mapped('display_name')),
            ))
        return True

    def action_eh_waive_billing_exception(self):
        """Permit settlement of this exact, manager-reviewed exception."""
        actor = self.env.user
        moves = self._eh_authorize_payment_control()._eh_lock_billing_sources()
        for move in moves:
            if move.eh_payment_hold:
                raise UserError(_(
                    "Release the payment hold on bill %s before waiving its "
                    "billing exception.", move.display_name,
                ))
            if not move._eh_has_billing_exception():
                raise UserError(_(
                    "Bill %s has no current billing exception to waive.",
                    move.display_name,
                ))
            reason = (move.eh_payment_action_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "A reason is required before waiving the billing "
                    "exception on bill %s.", move.display_name,
                ))
            now = fields.Datetime.now()
            fingerprint = move._eh_exception_waiver_evidence_fingerprint()
            move.with_context(
                **{_EH_AP_PAYMENT_CONTROL_INTERNAL: True}
            )._eh_write_sealed_metadata(
                {
                    'eh_exception_waived': True,
                    'eh_exception_waiver_reason': reason,
                    'eh_exception_waiver_fingerprint': fingerprint,
                    'eh_exception_waived_at': now,
                    'eh_exception_waived_by_id': actor.id,
                    'eh_payment_action_reason': False,
                },
                _EH_AP_PAYMENT_CONTROL_FIELDS
                | {'eh_payment_action_reason'},
            )
            self.env['eh.ap.payment.hold.log'].sudo().create({
                'move_id': move.id,
                'action': 'exception_waive',
                'reason': reason,
                'actor_id': actor.id,
                'action_at': now,
            })
            move.message_post(body=_(
                "Billing exception waived by %(user)s for the current "
                "purchase evidence. Reason: %(reason)s",
                user=actor.name, reason=reason,
            ))

    def action_eh_hold_payment(self):
        actor = self.env.user
        moves = self._eh_authorize_payment_control()
        for move in moves:
            if move.eh_payment_hold:
                raise UserError(_("Bill %s is already on hold.", move.display_name))
            reason = (move.eh_payment_action_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "A reason is required before holding bill %s.",
                    move.display_name,
                ))
            now = fields.Datetime.now()
            move.with_context(
                **{_EH_AP_PAYMENT_CONTROL_INTERNAL: True}
            )._eh_write_sealed_metadata(
                {
                    'eh_payment_hold': True,
                    'eh_hold_reason': reason,
                    'eh_held_at': now,
                    'eh_held_by_id': actor.id,
                    'eh_released_at': False,
                    'eh_released_by_id': False,
                    'eh_payment_action_reason': False,
                },
                _EH_AP_PAYMENT_CONTROL_FIELDS
                | {'eh_payment_action_reason'},
            )
            self.env['eh.ap.payment.hold.log'].sudo().create({
                'move_id': move.id,
                'action': 'hold',
                'reason': reason,
                'actor_id': actor.id,
                'action_at': now,
            })
            move.message_post(body=_(
                "Payment held by %(user)s. Reason: %(reason)s",
                user=actor.name, reason=reason,
            ))

    def action_eh_release_payment(self):
        actor = self.env.user
        moves = self._eh_authorize_payment_control()
        for move in moves:
            if not move.eh_payment_hold:
                raise UserError(_("Bill %s is not on hold.", move.display_name))
            reason = (move.eh_payment_action_reason or '').strip()
            if not reason:
                raise UserError(_(
                    "A reason is required before releasing bill %s.",
                    move.display_name,
                ))
            now = fields.Datetime.now()
            move.with_context(
                **{_EH_AP_PAYMENT_CONTROL_INTERNAL: True}
            )._eh_write_sealed_metadata(
                {
                    'eh_payment_hold': False,
                    'eh_hold_reason': False,
                    'eh_released_at': now,
                    'eh_released_by_id': actor.id,
                    'eh_payment_action_reason': False,
                },
                _EH_AP_PAYMENT_CONTROL_FIELDS
                | {'eh_payment_action_reason'},
            )
            self.env['eh.ap.payment.hold.log'].sudo().create({
                'move_id': move.id,
                'action': 'release',
                'reason': reason,
                'actor_id': actor.id,
                'action_at': now,
            })
            move.message_post(body=_(
                "Payment released by %(user)s. Reason: %(reason)s",
                user=actor.name, reason=reason,
            ))

    def _eh_authorize_payment_control(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can hold or release payments."
            ))
        ids = sorted(set(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                [tuple(ids)],
            )
            self.invalidate_recordset([
                'state', 'move_type', 'eh_payment_hold',
                'eh_payment_action_reason', 'eh_exception_waived',
                'eh_exception_waiver_fingerprint',
            ])
        invalid = self.filtered(
            lambda move: move.move_type != 'in_invoice'
            or move.state != 'posted',
        )
        if invalid:
            raise UserError(_(
                "Payment hold controls apply only to posted vendor bills."
            ))
        return self.sudo()
