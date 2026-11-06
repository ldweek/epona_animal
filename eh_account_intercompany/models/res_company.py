# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import _, fields, models
from odoo.exceptions import UserError


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ic_consolidation_only = fields.Boolean(
        string="Consolidation-only elimination entity",
        groups='eh_account_base.group_eh_manager',
        help=(
            "Marks this company as a non-trading consolidation workspace. "
            "Inter-company elimination journals may be posted only here, "
            "never in either member company's statutory general ledger. Do "
            "not enable this on a legal entity that issues separate financial "
            "statements."
        ),
    )

    def write(self, vals):
        if vals.get('eh_ic_consolidation_only') is False:
            used = self.env['eh.ic.elimination.batch'].sudo().search([
                ('elimination_company_id', 'in', self.ids),
                '|', ('move_history_ids', '!=', False),
                ('state', 'in', ('computed', 'posted')),
            ], limit=1)
            if used:
                raise UserError(_(
                    "Company %(company)s cannot be converted back to a "
                    "trading/legal-entity workspace while elimination batch "
                    "%(batch)s still depends on it.",
                    company=used.elimination_company_id.display_name,
                    batch=used.display_name,
                ))
        return super().write(vals)
