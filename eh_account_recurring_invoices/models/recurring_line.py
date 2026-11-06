# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.recurring.invoice.line: line item on a recurring invoice template.

Each line carries enough information to materialise an
account.move.line on a generated customer invoice: optional product,
description, income account, quantity, unit price, and taxes. When a
product is set its taxes and account default through; the explicit
fields override.
"""

from odoo import api, fields, models


class EhRecurringInvoiceLine(models.Model):
    _name = 'eh.recurring.invoice.line'
    _description = "Recurring invoice template line"
    _order = 'sequence, id'
    _check_company_auto = True

    template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    sequence = fields.Integer(default=10)
    # Non-stored parent anchor lets Odoo's automatic company checker validate
    # product/account/tax targets without introducing a forgeable stored
    # company column on the child row.
    company_id = fields.Many2one(
        related='template_id.company_id', store=False, readonly=True,
    )

    product_id = fields.Many2one(
        'product.product',
        check_company=True,
        help="Optional. When set, defaults price, taxes, and account.",
    )
    name = fields.Char(required=True)
    account_id = fields.Many2one(
        'account.account',
        check_company=True,
        help=(
            "Income account for this line. When blank, the product's "
            "income account or fallback is used at generation time."
        ),
    )

    quantity = fields.Float(required=True, default=1.0)
    price_unit = fields.Float(required=True, default=0.0)

    tax_ids = fields.Many2many(
        'account.tax',
        'eh_recurring_line_tax_rel',
        'line_id', 'tax_id',
        string="Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
        check_company=True,
    )

    currency_id = fields.Many2one(
        related='template_id.currency_id',
        store=False,
    )
    subtotal = fields.Monetary(
        compute='_compute_subtotal',
        currency_field='currency_id',
        store=False,
    )

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if not self.product_id:
            return
        prod = self.product_id
        if not self.name:
            self.name = prod.name
        if not self.price_unit:
            self.price_unit = prod.list_price
        if not self.account_id and prod.property_account_income_id:
            self.account_id = prod.property_account_income_id
        if not self.tax_ids and prod.taxes_id:
            self.tax_ids = [(6, 0, prod.taxes_id.ids)]

    @api.depends('quantity', 'price_unit')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.quantity or 0.0) * (line.price_unit or 0.0)

    @api.model
    def _eh_lock_templates(self, template_ids):
        templates = self.env['eh.recurring.invoice.template'].browse(
            sorted({template_id for template_id in template_ids if template_id})
        )
        templates._eh_lock_for_integrity()
        templates._eh_assert_integrity_trusted()
        return templates

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_lock_templates([
            vals.get('template_id')
            or self.env.context.get('default_template_id')
            for vals in vals_list
        ])
        return super().create(vals_list)

    def write(self, vals):
        target_ids = self.mapped('template_id').ids
        if vals.get('template_id'):
            target_ids.append(vals['template_id'])
        self._eh_lock_templates(target_ids)
        return super().write(vals)

    def unlink(self):
        self._eh_lock_templates(self.mapped('template_id').ids)
        return super().unlink()
