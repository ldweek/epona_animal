# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset categories.

A category groups assets sharing the same default useful life, salvage
treatment, depreciation method and posting accounts. Asset records inherit
from a category at creation; values can be overridden per asset before the
schedule is generated.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

from .accounting_integrity import _eh_validate_accounting_company


class EhAssetCategory(models.Model):
    _name = 'eh.asset.category'
    _description = "Fixed Asset Category"
    _order = 'name'

    name = fields.Char(required=True)
    code = fields.Char(
        help="Short internal code, e.g. ITHW for IT Hardware.",
    )
    active = fields.Boolean(default=True)

    method = fields.Selection([
        ('straight_line', "Straight Line"),
        ('reducing_balance', "Reducing Balance"),
        ('prime_cost', "Prime Cost (AU tax)"),
        ('diminishing_value', "Diminishing Value (AU tax)"),
        ('manual', "Manual"),
    ], required=True, default='straight_line')

    useful_life_months = fields.Integer(
        string="Useful Life (months)", default=60,
        help="Default useful life used to seed assets in this category.",
    )
    salvage_rate = fields.Float(
        string="Salvage Rate (0-1)", default=0.0,
        help="Default salvage value as a fraction of acquisition cost.",
    )
    declining_factor = fields.Float(
        default=2.0,
        help="Multiplier on the straight line rate when method is "
             "reducing balance. 2.0 yields double declining balance.",
    )
    prorate_first_period = fields.Boolean(
        default=True,
        help="If set, the first period is prorated based on days in "
             "service. Otherwise the first period charges the full amount.",
    )
    prorata_mode = fields.Selection(
        [
            ('none', "Full first period"),
            ('daily', "By days in service"),
            ('half', "Half first period (mid-period convention)"),
        ],
        help="Default first-period proration mode seeded onto assets in "
             "this category. When blank, the Prorate First Period switch "
             "applies.",
    )

    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account",
        check_company=True,
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
        help="Where the asset is capitalised on the balance sheet.",
    )
    depreciation_account_id = fields.Many2one(
        'account.account', string="Depreciation Expense Account",
        check_company=True,
        domain="[('account_type', '=', 'expense_depreciation')]",
        help="P/L account that the depreciation charge is booked to.",
    )
    accumulated_depreciation_account_id = fields.Many2one(
        'account.account', string="Accumulated Depreciation Account",
        check_company=True,
        domain="[('account_type', 'in', ['asset_fixed', 'asset_non_current'])]",
        help="Contra asset account on the balance sheet.",
    )
    disposal_gain_account_id = fields.Many2one(
        'account.account', string="Disposal Gain Account",
        check_company=True,
        domain="[('account_type', '=', 'income_other')]",
    )
    disposal_loss_account_id = fields.Many2one(
        'account.account', string="Disposal Loss Account",
        check_company=True,
        domain="[('account_type', '=', 'expense')]",
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account', string="Revaluation Reserve Account",
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Depreciation Journal",
        check_company=True,
        domain="[('type', '=', 'general')]",
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    asset_count = fields.Integer(compute='_compute_asset_count')

    _uniq_code_company = models.Constraint(
        'unique(code, company_id)',
        'Category code must be unique per company.',
    )
    _check_useful_life_positive = models.Constraint(
        'CHECK (useful_life_months > 0)',
        'Useful life must be greater than zero.',
    )
    _check_salvage_rate_range = models.Constraint(
        'CHECK (salvage_rate >= 0 AND salvage_rate < 1)',
        'Salvage rate must be in [0, 1).',
    )

    @api.depends()
    def _compute_asset_count(self):
        Asset = self.env['eh.asset']
        counts = dict(read_group_compat(Asset, 
            [('category_id', 'in', self.ids)],
            ['category_id'],
            ['__count'],
        ))
        for category in self:
            category.asset_count = counts.get(category, 0)

    @api.constrains('declining_factor', 'method')
    def _check_declining_factor(self):
        for category in self:
            if category.method == 'reducing_balance' and category.declining_factor <= 0:
                raise ValidationError(_(
                    "Declining factor must be positive for reducing "
                    "balance method (category %s).", category.name,
                ))

    @api.constrains(
        'company_id', 'asset_account_id', 'depreciation_account_id',
        'accumulated_depreciation_account_id', 'disposal_gain_account_id',
        'disposal_loss_account_id', 'revaluation_reserve_account_id',
        'journal_id',
    )
    def _check_accounting_company(self):
        _eh_validate_accounting_company(self, (
            'asset_account_id', 'depreciation_account_id',
            'accumulated_depreciation_account_id',
            'disposal_gain_account_id', 'disposal_loss_account_id',
            'revaluation_reserve_account_id',
            'journal_id',
        ))
        for category in self.filtered('revaluation_reserve_account_id'):
            if category.revaluation_reserve_account_id.account_type != 'equity':
                raise ValidationError(_(
                    "Revaluation Reserve Account on category %(category)s "
                    "must be an equity account.",
                    category=category.display_name,
                ))

    def action_view_assets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Assets in %s", self.name),
            'res_model': 'eh.asset',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('category_id', '=', self.id)],
            'context': {'default_category_id': self.id},
        }
