# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: open post-dated cheque counter.

Adds a smart-button "Open cheques: N" on the partner form, where
"open" means cheques in registered, presented, or bounced state (i.e.
not yet cleared/replaced/cancelled). One click drills into the cheque
list filtered to this partner.
"""

from odoo import _, api, fields, models
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


_OPEN_STATES = ('registered', 'presented', 'bounced')


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_open_cheque_count = fields.Integer(
        compute='_compute_eh_open_cheque_count',
        groups='eh_account_base.group_eh_user',
        help=(
            "Number of cheques on this partner whose state is not yet "
            "cleared, replaced, or cancelled. Both incoming (received "
            "from customer) and outgoing (issued to vendor) cheques "
            "count."
        ),
    )

    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_eh_open_cheque_count(self):
        for partner in self:
            partner.eh_open_cheque_count = 0
        if (
            not self
            or not self.env.user.has_group('eh_account_base.group_eh_user')
        ):
            return
        Cheque = self.env['eh.cheque']
        groups = read_group_compat(
            Cheque,
            [
                ('partner_id', 'in', self.ids),
                ('company_id', 'in', self.env.companies.ids),
                ('state', 'in', list(_OPEN_STATES)),
            ],
            groupby=['partner_id'],
            aggregates=['__count'],
        )
        counts = {p.id: c for p, c in groups}
        for partner in self:
            partner.eh_open_cheque_count = counts.get(partner.id, 0)

    def _compute_application_statistics_hook(self):
        statistics = super()._compute_application_statistics_hook()
        if (
            not self
            or not self.env.user.has_group('eh_account_base.group_eh_user')
        ):
            return statistics
        for partner in self.filtered('eh_open_cheque_count'):
            statistics[partner.id].append({
                'iconClass': 'fa-money',
                'value': partner.eh_open_cheque_count,
                'label': _('Open Cheques'),
                'tagClass': 'o_tag_color_5',
                'actionMethod': 'action_view_eh_open_cheques_list',
            })
        return statistics

    def action_view_eh_open_cheques_list(self):
        """Open the badge drill-down as a filtered multi-record list."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Open cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('company_id', 'in', self.env.companies.ids),
                ('state', 'in', list(_OPEN_STATES)),
            ],
        }

    def action_view_eh_open_cheques(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Open cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [
                ('partner_id', '=', self.id),
                ('state', 'in', list(_OPEN_STATES)),
            ],
        }
