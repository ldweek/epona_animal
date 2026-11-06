# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: linked-asset and lease counters.

Surfaces stat-buttons on the partner form for vendors / lessors:

  * "Assets supplied" - count of fixed assets where this partner is
    the vendor (eh.asset.partner_id).
  * "Leases" - count of IFRS-16 lease contracts where this partner
    is the lessor.

Both buttons hide when the count is zero so a regular customer's
partner form stays clean.
"""

from odoo import _, api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_asset_count = fields.Integer(
        compute='_compute_eh_asset_count',
        groups='eh_account_base.group_eh_user',
        help="Fixed assets supplied by this partner (vendor link).",
    )
    eh_lease_count = fields.Integer(
        compute='_compute_eh_lease_count',
        groups='eh_account_base.group_eh_user',
        help="IFRS-16 lease contracts with this partner as lessor.",
    )

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_eh_asset_count(self):
        if (
            not self
            or not self.env.user.has_group('eh_account_base.group_eh_user')
        ):
            for partner in self:
                partner.eh_asset_count = 0
            return
        # Keep the caller's company rules. sudo() here leaked how many assets
        # another company bought from the same shared partner.
        Asset = self.env['eh.asset']
        groups = read_group_compat(
            Asset,
            [('partner_id', 'in', self.ids), ('state', '!=', 'disposed')],
            groupby=['partner_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_asset_count = counts.get(partner.id, 0)

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_eh_lease_count(self):
        if (
            not self
            or not self.env.user.has_group('eh_account_base.group_eh_user')
        ):
            for partner in self:
                partner.eh_lease_count = 0
            return
        Lease = self.env['eh.lease.contract']
        groups = read_group_compat(
            Lease,
            [('lessor_id', 'in', self.ids)],
            groupby=['lessor_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_lease_count = counts.get(partner.id, 0)

    def _compute_application_statistics_hook(self):
        statistics = super()._compute_application_statistics_hook()
        if (
            not self
            or not self.env.user.has_group('eh_account_base.group_eh_user')
        ):
            return statistics
        for partner in self:
            if partner.eh_asset_count:
                statistics[partner.id].append({
                    'iconClass': 'fa-cube',
                    'value': partner.eh_asset_count,
                    'label': _('Assets'),
                    'tagClass': 'o_tag_color_2',
                    'actionMethod': 'action_view_eh_assets_list',
                })
            if partner.eh_lease_count:
                statistics[partner.id].append({
                    'iconClass': 'fa-file-text-o',
                    'value': partner.eh_lease_count,
                    'label': _('Leases'),
                    'tagClass': 'o_tag_color_4',
                    'actionMethod': 'action_view_eh_leases',
                })
        return statistics

    def action_view_eh_assets_list(self):
        """Open the badge drill-down with the same scope as its count."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Assets supplied"),
            'res_model': 'eh.asset',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('state', '!=', 'disposed'),
                ('company_id', 'in', self.env.companies.ids),
            ],
        }

    def action_view_eh_assets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Assets supplied"),
            'res_model': 'eh.asset',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [('partner_id', '=', self.id)],
        }

    def action_view_eh_leases(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Lease contracts"),
            'res_model': 'eh.lease.contract',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('lessor_id', '=', self.id)],
        }
