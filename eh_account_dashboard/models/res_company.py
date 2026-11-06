# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: warn thresholds for the dashboard ratio layer.

Each field is the level at which the matching ratio tile flips from
'ok' to 'warn' on the Owl board (see dashboard_ratios.py for the
direction each threshold applies in). Defaults are conventional
prudential levels:

* current ratio < 1.00 warns (current liabilities exceed current
  assets);
* quick ratio < 0.80 warns;
* cash ratio < 0.20 warns;
* interest cover < 2.00 warns (EBIT under twice the interest charge);
* net margin < 0.00 pct warns (loss-making period);
* debt-to-equity > 2.00 warns;
* cash conversion cycle > 90 days warns.

Edited on the company form (Ratio Thresholds tab); deliberately not
mirrored into res.config.settings.
"""

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_ratio_warn_current = fields.Float(
        string="Warn: Current Ratio Below",
        default=1.0,
        help="Dashboard warns when the current ratio falls below this "
             "level. Default 1.0: current liabilities exceed current "
             "assets.",
    )
    eh_ratio_warn_quick = fields.Float(
        string="Warn: Quick Ratio Below",
        default=0.8,
        help="Dashboard warns when the quick ratio (current assets "
             "less inventory over current liabilities) falls below "
             "this level. Default 0.8.",
    )
    eh_ratio_warn_cash = fields.Float(
        string="Warn: Cash Ratio Below",
        default=0.2,
        help="Dashboard warns when the cash ratio falls below this "
             "level. Default 0.2.",
    )
    eh_ratio_warn_interest_cover = fields.Float(
        string="Warn: Interest Cover Below",
        default=2.0,
        help="Dashboard warns when EBIT covers the interest expense "
             "fewer than this many times. Default 2.0.",
    )
    eh_ratio_warn_net_margin_pct = fields.Float(
        string="Warn: Net Margin Below (%)",
        default=0.0,
        help="Dashboard warns when the net margin percentage falls "
             "below this level. Default 0.0: any loss-making period "
             "warns.",
    )
    eh_ratio_warn_debt_equity = fields.Float(
        string="Warn: Debt-to-Equity Above",
        default=2.0,
        help="Dashboard warns when total debt (current plus "
             "non-current liabilities) exceeds this multiple of "
             "equity. Default 2.0.",
    )
    eh_ratio_warn_ccc_days = fields.Float(
        string="Warn: Cash Conversion Cycle Above (days)",
        default=90.0,
        help="Dashboard warns when the cash conversion cycle "
             "(DSO + DIO - DPO) exceeds this many days. Default 90.",
    )
