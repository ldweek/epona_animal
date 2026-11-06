# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Customer-portal accounting settings."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


DEFAULT_MAX_STATEMENT_DAYS = 366
MAX_CONFIGURED_STATEMENT_DAYS = 3660
STATEMENT_DAYS_CONFIG_KEY = (
    'eh_account_portal_extra.max_statement_days'
)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    eh_portal_max_statement_days = fields.Integer(
        string="Maximum Portal Statement Days",
        config_parameter=STATEMENT_DAYS_CONFIG_KEY,
        default=DEFAULT_MAX_STATEMENT_DAYS,
        help=(
            "Maximum inclusive date span a customer may request from the "
            "portal statement form. Choose 1 to 3660 days."
        ),
    )

    @api.constrains('eh_portal_max_statement_days')
    def _check_eh_portal_max_statement_days(self):
        for settings in self:
            days = settings.eh_portal_max_statement_days
            if not 1 <= days <= MAX_CONFIGURED_STATEMENT_DAYS:
                raise ValidationError(_(
                    "Maximum portal statement days must be between 1 and "
                    "%s.",
                    MAX_CONFIGURED_STATEMENT_DAYS,
                ))
