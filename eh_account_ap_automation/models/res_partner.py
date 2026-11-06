# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inherit res.partner to attach an AP tolerance profile override and a
parser regex profile.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .regex_policy import compile_safe_regex


_EH_AP_PARTNER_QUARANTINE_INTERNAL = 'eh_ap_partner_quarantine_internal'


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_ap_tolerance_profile_id = fields.Many2one(
        'eh.ap.tolerance.profile',
        string="AP Tolerance Profile",
        company_dependent=True,
        check_company=True,
        groups='eh_account_base.group_eh_manager',
        help="Override the company default tolerance profile for this "
             "vendor's bills.",
    )
    eh_ap_invoice_ref_regex = fields.Char(
        string="Invoice Ref Regex",
        company_dependent=True,
        groups='eh_account_base.group_eh_manager',
        help="Regular expression with one capture group used to extract "
             "the vendor invoice reference from incoming bill text. "
             "Falls back to a generic 'Invoice[: #]+([A-Z0-9-]+)' "
             "pattern if not set.",
    )
    eh_ap_total_regex = fields.Char(
        string="Total Regex",
        company_dependent=True,
        groups='eh_account_base.group_eh_manager',
        help="Regular expression with one capture group used to extract "
             "the total amount from incoming bill text. Falls back to a "
             "generic 'Total[: ]+([0-9.,]+)' pattern.",
    )
    eh_ap_legacy_config_quarantine = fields.Text(
        string="Legacy AP Configuration Quarantine",
        readonly=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "Preserved pre-company-scope AP settings from a shared vendor. "
            "They are evidence only and must be reviewed into each intended "
            "company rather than applied globally."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(
                'eh_ap_legacy_config_quarantine' in vals
                for vals in vals_list) and not (
                    self.env.su and self.env.context.get(
                        _EH_AP_PARTNER_QUARANTINE_INTERNAL)):
            raise AccessError(_(
                "Legacy AP configuration quarantine is server-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_ap_legacy_config_quarantine' in vals and not (
                self.env.su and self.env.context.get(
                    _EH_AP_PARTNER_QUARANTINE_INTERNAL)):
            raise AccessError(_(
                "Legacy AP configuration quarantine is server-owned."
            ))
        return super().write(vals)

    @api.constrains('eh_ap_invoice_ref_regex', 'eh_ap_total_regex')
    def _check_eh_ap_regexes(self):
        for partner in self:
            for pattern in (
                    partner.eh_ap_invoice_ref_regex,
                    partner.eh_ap_total_regex):
                if not pattern:
                    continue
                try:
                    compile_safe_regex(pattern)
                except ValueError as exc:
                    raise ValidationError(_(
                        "Unsafe AP parser regular expression: %s.", exc,
                    )) from exc
