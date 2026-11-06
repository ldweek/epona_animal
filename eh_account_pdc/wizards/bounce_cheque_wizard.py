# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Wizard to record a cheque bounce with reason, dishonour date and bank
charges. The bounce reversal is dated at the bank dishonour date (the day
the bank refused the cheque), not the day the operator records the bounce;
the model validates the date against the presentation date and the
accounting lock dates."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhChequeBounceWizard(models.TransientModel):
    _name = 'eh.cheque.bounce.wizard'
    _description = "Bounce Cheque Wizard"

    cheque_id = fields.Many2one(
        'eh.cheque', required=True, readonly=True,
    )
    reason_id = fields.Many2one(
        'eh.cheque.bounce.reason', required=True, string="Reason",
    )
    dishonour_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help="Date the bank actually dishonoured the cheque, from the bank "
             "advice. The bounce reversal and any charge entry are dated "
             "here.",
    )
    force_current_date = fields.Boolean(
        string="Post at Current Date",
        help="When the dishonour date falls in a locked period, post the "
             "bounce reversal at today's date instead. The dishonour date "
             "is still stored on the cheque for disclosure.",
    )
    bounce_charges = fields.Monetary()
    currency_id = fields.Many2one(
        related='cheque_id.currency_id', readonly=True,
    )
    notes = fields.Text()

    @api.constrains('bounce_charges')
    def _check_bounce_charges(self):
        for wizard in self:
            if wizard.bounce_charges < 0.0:
                raise ValidationError(_(
                    "Bounce charges cannot be negative.",
                ))

    def action_confirm(self):
        self.ensure_one()
        self._eh_check_access('write')
        cheque = self.cheque_id
        # The wizard id and cheque_id are client-controlled. Authorize the
        # real target before reading its state or entering the sudo workflow.
        cheque._eh_authorize_cheque_action()
        if cheque.state != 'presented':
            raise UserError(_(
                "Only presented cheques can be bounced.",
            ))
        cheque._mark_bounced(
            reason=self.reason_id,
            charges=self.bounce_charges,
            notes=self.notes,
            dishonour_date=self.dishonour_date,
            force_current_date=self.force_current_date,
        )
        return {'type': 'ir.actions.act_window_close'}
