# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.revenue.modification: an audit record of an IFRS 15.18-21 contract
modification applied to a revenue contract.

A modification is a change to the scope or price of a contract approved by the
parties (IFRS 15.18). Depending on whether the added goods or services are
distinct and priced at their standalone selling prices, it is accounted for as
a separate contract (15.20), a prospective reallocation of the remaining
transaction price (15.21(a)) or a cumulative catch-up (15.21(b)). This model
records which treatment was applied, the price before and after, and, for a
separate-contract treatment, the new contract that was created.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .revenue_contract import _REVENUE_MODIFICATION_CAPABILITY


class EhRevenueModification(models.Model):
    _name = 'eh.revenue.modification'
    _description = "Revenue contract modification (IFRS 15.18-21)"
    _order = 'create_date desc, id desc'

    contract_id = fields.Many2one(
        'eh.revenue.contract', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='contract_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='contract_id.currency_id', store=True, readonly=True)

    method = fields.Selection(
        [('separate', "Separate contract (15.20)"),
         ('prospective', "Prospective reallocation (15.21a)"),
         ('catch_up', "Cumulative catch-up (15.21b)")],
        required=True, readonly=True)
    description = fields.Char(readonly=True)
    effective_date = fields.Date(
        readonly=True,
        help="Effective date supplied by the approved modification. Legacy "
             "rows created before date capture remain blank.")
    price_before = fields.Monetary(
        currency_field='currency_id', readonly=True)
    price_after = fields.Monetary(
        currency_field='currency_id', readonly=True)
    separate_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Separate Contract", readonly=True,
        ondelete='set null',
        help="For a separate-contract modification, the new contract created "
             "for the added distinct goods or services.")

    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get('eh_revenue_modification_capability')
            is not _REVENUE_MODIFICATION_CAPABILITY
        ):
            raise AccessError(_(
                "Revenue modification evidence can only be created by the "
                "contract modification workflow."
            ))
        for vals in vals_list:
            vals.pop('company_id', None)
            vals.pop('currency_id', None)
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_(
            "Revenue modification evidence is append-only and immutable."
        ))

    def unlink(self):
        raise AccessError(_(
            "Revenue modification evidence is append-only and cannot be "
            "deleted."
        ))
