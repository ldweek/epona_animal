# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import _, fields, models
from odoo.exceptions import ValidationError


class EhIcManualFulfillmentWizard(models.TransientModel):
    _name = 'eh.ic.manual.fulfillment.wizard'
    _description = "Record manual inter-company fulfilment"

    sale_line_id = fields.Many2one('sale.order.line', readonly=True)
    purchase_line_id = fields.Many2one('purchase.order.line', readonly=True)
    quantity = fields.Float(required=True)

    def action_apply(self):
        self.ensure_one()
        if bool(self.sale_line_id) == bool(self.purchase_line_id):
            raise ValidationError(_(
                "Open manual fulfilment from exactly one order line."
            ))
        line = self.sale_line_id or self.purchase_line_id
        line.action_eh_ic_set_fulfilled_qty(self.quantity)
        return {'type': 'ir.actions.act_window_close'}
