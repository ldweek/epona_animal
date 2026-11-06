# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Wizard to apply an IFRS 15.18-21 contract modification from the UI. It is a
thin front end over eh.revenue.contract._apply_modification: the manager picks
the treatment, optionally revises the transaction price and lists the added
distinct goods or services, and the wizard delegates the balanced posting to
the contract.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhRevenueModificationWizard(models.TransientModel):
    _name = 'eh.revenue.modification.wizard'
    _description = "Apply revenue contract modification"

    contract_id = fields.Many2one(
        'eh.revenue.contract', required=True, ondelete='cascade')
    currency_id = fields.Many2one(
        related='contract_id.currency_id', readonly=True)
    method = fields.Selection(
        [('separate', "Separate contract (15.20)"),
         ('prospective', "Prospective reallocation (15.21a)"),
         ('catch_up', "Cumulative catch-up (15.21b)")],
        required=True, default='prospective')
    description = fields.Char()
    effective_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help="Exact contract-modification and catch-up journal date.")
    set_new_price = fields.Boolean(
        string="Revise transaction price",
        help="Change the total transaction price as part of the "
             "modification.")
    new_transaction_price = fields.Monetary(
        currency_field='currency_id',
        help="The revised total transaction price. For a separate contract "
             "this is the price of the added goods or services.")
    line_ids = fields.One2many(
        'eh.revenue.modification.wizard.line', 'wizard_id',
        string="Added obligations")

    def action_apply(self):
        self.ensure_one()
        added = [{
            'name': line.name,
            'standalone_price': line.standalone_price,
            'satisfaction': line.satisfaction,
            'progress_method': line.progress_method,
            'method_basis': line.method_basis,
        } for line in self.line_ids]
        if self.method == 'separate' and not added:
            raise UserError(_(
                "List the added distinct goods or services for a separate "
                "contract."))
        price = self.new_transaction_price if self.set_new_price else None
        result = self.contract_id._apply_modification(
            method=self.method,
            added_obligations=added,
            new_transaction_price=price,
            description=self.description,
            effective_date=self.effective_date,
        )
        if self.method == 'separate':
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'eh.revenue.contract',
                'res_id': result.id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {'type': 'ir.actions.act_window_close'}


class EhRevenueModificationWizardLine(models.TransientModel):
    _name = 'eh.revenue.modification.wizard.line'
    _description = "Added obligation on a modification"

    wizard_id = fields.Many2one(
        'eh.revenue.modification.wizard', required=True, ondelete='cascade')
    currency_id = fields.Many2one(
        related='wizard_id.currency_id', readonly=True)
    name = fields.Char(required=True)
    standalone_price = fields.Monetary(
        currency_field='currency_id', required=True)
    satisfaction = fields.Selection(
        [('point_in_time', "Point in time"), ('over_time', "Over time")],
        default='point_in_time', required=True)
    progress_method = fields.Selection(
        [('output_milestones', "Output: milestones reached"),
         ('output_units', "Output: units delivered"),
         ('input_cost', "Input: costs incurred"),
         ('input_time', "Input: time elapsed")],
        default='output_milestones')
    method_basis = fields.Text(
        help="Why this method faithfully depicts transfer of control. "
             "Required for an over-time added obligation (IFRS 15.B14).")
