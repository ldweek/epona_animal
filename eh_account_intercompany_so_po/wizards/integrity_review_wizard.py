# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import _, fields, models
from odoo.exceptions import ValidationError


class EhIcIntegrityReviewWizard(models.TransientModel):
    _name = 'eh.ic.integrity.review.wizard'
    _description = "Inter-company SO/PO integrity review"

    sale_order_id = fields.Many2one('sale.order', readonly=True)
    purchase_order_id = fields.Many2one('purchase.order', readonly=True)
    review_note = fields.Text(
        required=True,
        string="Evidence and conclusion",
        help="Record the documents and checks used to attest this legacy pair.",
    )

    def action_review(self):
        self.ensure_one()
        if bool(self.sale_order_id) == bool(self.purchase_order_id):
            raise ValidationError(_(
                "Open integrity review from exactly one sales or purchase order."
            ))
        order = self.sale_order_id or self.purchase_order_id
        order._eh_ic_review_integrity_pair(self.review_note)
        return {'type': 'ir.actions.act_window_close'}
