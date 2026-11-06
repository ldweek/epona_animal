# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Audited, manager-only entry point for low-value-pool transfers."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhAssetLvpTransferWizard(models.TransientModel):
    _name = 'eh.asset.lvp.transfer.wizard'
    _description = "Low-Value Pool Asset Transfer Wizard"

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    pool_id = fields.Many2one(
        'eh.asset.lvp.pool', required=True, check_company=True,
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
    )
    asset_id = fields.Many2one(
        'eh.asset', required=True, check_company=True,
        domain="[('company_id', '=', company_id), "
               "('deferred_type', '=', 'asset'), "
               "('is_under_construction', '=', False), "
               "('lvp_pool_id', '=', False), "
               "('state', 'in', ('draft', 'running', 'paused'))]",
    )
    transfer_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help=(
            "Accounting and pool-allocation date. It cannot precede the "
            "asset's acquisition or latest sealed accounting evidence."
        ),
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True,
    )
    opening_value = fields.Monetary(
        related='asset_id.net_book_value', currency_field='currency_id',
        readonly=True,
        help="Current adjustable value that will be transferred to the pool.",
    )
    processed = fields.Boolean(readonly=True, copy=False)

    @api.constrains('company_id', 'pool_id', 'asset_id')
    def _check_company_integrity(self):
        for wizard in self:
            if (wizard.pool_id
                    and wizard.pool_id.company_id != wizard.company_id):
                raise ValidationError(_(
                    "The low-value pool must belong to the wizard company."
                ))
            if (wizard.asset_id
                    and wizard.asset_id.company_id != wizard.company_id):
                raise ValidationError(_(
                    "The asset must belong to the wizard company."
                ))

    def action_transfer(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_lvp_transfer_wizard '
            'WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset([
            'processed', 'company_id', 'pool_id', 'asset_id', 'transfer_date',
        ])
        if self.processed:
            raise UserError(_(
                "This low-value-pool transfer wizard has already been applied."
            ))
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can transfer an asset into a "
                "low-value pool."
            ))
        pool = self.pool_id
        asset = self.asset_id
        # Re-prove source access before the pool method reaches its guarded
        # workflow write; a guessed foreign-company id must not cross sudo.
        pool._eh_check_access('write')
        asset._eh_check_access('write')
        self._check_company_integrity()
        pool.action_transfer_asset(asset, transfer_date=self.transfer_date)
        self.sudo().write({'processed': True})
        return {'type': 'ir.actions.act_window_close'}

    def write(self, vals):
        if 'processed' in vals and not self.env.su:
            raise UserError(_("Processed status is server-owned."))
        return super().write(vals)
