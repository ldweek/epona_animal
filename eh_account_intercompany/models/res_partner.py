# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Adds an explicit "represented company" link to res.partner.

The intercompany mirror walks `partner.commercial_partner_id.company_id`
to find the destination company. Setting `res.partner.company_id`
directly to the represented company forbids the partner from being
used on invoices in any other company (Odoo's `_check_company`
guard). The dedicated field below sidesteps that by leaving
`partner.company_id` global (False) while still recording which
company the partner represents.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_represented_company_id = fields.Many2one(
        'res.company', string="Represented Company",
        index=True, copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "When set, posting an out_invoice for this partner triggers "
            "an inter-company mirror as a vendor bill in the named "
            "company. Leave blank if the partner is a regular customer."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any('eh_represented_company_id' in vals for vals in vals_list):
            self._eh_authorize_represented_company_change(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_represented_company_id' in vals:
            self._eh_authorize_represented_company_change([vals])
        return super().write(vals)

    def _eh_authorize_represented_company_change(self, vals_list):
        """Treat intercompany routing as accounting configuration.

        A contact writer must not be able to turn an ordinary customer into
        a privileged cross-company document target.  Only an EH manager may
        configure the route, and the destination must be one of that actor's
        explicitly allowed companies before any mirror engine can elevate.
        """
        if self.env.su:
            return
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can configure the company "
                "represented by a contact."))
        company_ids = {
            vals.get('eh_represented_company_id')
            for vals in vals_list
            if vals.get('eh_represented_company_id')
        }
        forbidden = company_ids - set(self.env.companies.ids)
        if forbidden:
            raise AccessError(_(
                "The represented company must be in your allowed-company "
                "scope."))
        if company_ids:
            self.env['res.company'].browse(company_ids)._eh_check_access('read')
