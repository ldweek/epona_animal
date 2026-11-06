# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Append-only AP exception decisions.

Current workflow pointers may be invalidated when bill evidence changes.  The
rows below are never edited or deleted, so every duplicate override and line
decision survives that invalidation as durable audit evidence.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


_MAX_REASON_LENGTH = 8000


class EhApDuplicateOverrideLog(models.Model):
    _name = 'eh.ap.duplicate.override.log'
    _description = "AP Duplicate Override Audit"
    _rec_name = 'intake_id'
    _order = 'overridden_at desc, id desc'

    intake_id = fields.Many2one(
        'eh.ap.intake', required=True, ondelete='restrict', index=True,
    )
    company_id = fields.Many2one(
        related='intake_id.company_id', store=True, readonly=True, index=True,
    )
    duplicate_intake_id = fields.Many2one(
        'eh.ap.intake', ondelete='restrict', readonly=True,
    )
    duplicate_move_id = fields.Many2one(
        'account.move', ondelete='restrict', readonly=True,
    )
    reason = fields.Text(required=True, readonly=True)
    evidence_fingerprint = fields.Char(required=True, readonly=True, index=True)
    overridden_by_id = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict',
    )
    overridden_at = fields.Datetime(required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Duplicate override audit rows are server-owned. Use the "
                "intake override action."
            ))
        for vals in vals_list:
            reason = (vals.get('reason') or '').strip()
            if not reason or len(reason) > _MAX_REASON_LENGTH:
                raise AccessError(_(
                    "Duplicate override audit reason is missing or too long."
                ))
            fingerprint = vals.get('evidence_fingerprint') or ''
            if len(fingerprint) != 64:
                raise AccessError(_(
                    "Duplicate override audit fingerprint is invalid."
                ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("Duplicate override audit rows are append-only."))

    def unlink(self):
        raise AccessError(_("Duplicate override audit rows are append-only."))


class EhApLineDecisionLog(models.Model):
    _name = 'eh.ap.intake.line.decision'
    _description = "AP Intake Line Decision Audit"
    _rec_name = 'line_id'
    _order = 'decided_at desc, id desc'

    line_id = fields.Many2one(
        'eh.ap.intake.line', required=True, ondelete='restrict', index=True,
    )
    intake_id = fields.Many2one(
        related='line_id.intake_id', store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        related='line_id.company_id', store=True, readonly=True, index=True,
    )
    decision = fields.Selection([
        ('override', "Override"),
        ('reject', "Reject"),
        ('reopen', "Reopen"),
    ], required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    evidence_fingerprint = fields.Char(required=True, readonly=True, index=True)
    decided_by_id = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict',
    )
    decided_at = fields.Datetime(required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Line decision audit rows are server-owned. Use the line "
                "override or reject action."
            ))
        for vals in vals_list:
            reason = (vals.get('reason') or '').strip()
            if not reason or len(reason) > _MAX_REASON_LENGTH:
                raise AccessError(_(
                    "Line decision audit reason is missing or too long."
                ))
            fingerprint = vals.get('evidence_fingerprint') or ''
            if len(fingerprint) != 64:
                raise AccessError(_(
                    "Line decision audit fingerprint is invalid."
                ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("Line decision audit rows are append-only."))

    def unlink(self):
        raise AccessError(_("Line decision audit rows are append-only."))


class EhApPaymentHoldLog(models.Model):
    _name = 'eh.ap.payment.hold.log'
    _description = "AP Payment Hold Audit"
    _rec_name = 'move_id'
    _order = 'action_at desc, id desc'

    move_id = fields.Many2one(
        'account.move', required=True, ondelete='restrict', index=True,
    )
    company_id = fields.Many2one(
        related='move_id.company_id', store=True, readonly=True, index=True,
    )
    action = fields.Selection([
        ('hold', "Hold"),
        ('release', "Release"),
        ('exception_waive', "Waive Billing Exception"),
    ], required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    actor_id = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict',
    )
    action_at = fields.Datetime(required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Payment hold audit rows are server-owned. Use the hold or "
                "release action."
            ))
        for vals in vals_list:
            reason = (vals.get('reason') or '').strip()
            if not reason or len(reason) > _MAX_REASON_LENGTH:
                raise AccessError(_(
                    "Payment-control audit reason is missing or too long."
                ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("Payment hold audit rows are append-only."))

    def unlink(self):
        raise AccessError(_("Payment hold audit rows are append-only."))
