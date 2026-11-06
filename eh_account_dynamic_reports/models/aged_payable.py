# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Aged Payable handler.

Open balances on payable accounts (liability_payable) bucketed by days
overdue. Sign is -1 because a payable is naturally a credit balance, and
we want the report to display positive amounts the company owes vendors.

Inherits all bucket / rendering logic from the aged base.
"""

from odoo import models
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)


class EhAgedPayableHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.aged_payable'
    _inherit = 'eh.account.dynamic.report.handler.aged_base'
    _description = "Aged Payable report handler"

    REPORT_CODE = 'aged_payable'
    REPORT_NAME = _lt("Aged Payable")

    ACCOUNT_TYPES = ('liability_payable',)
    SIGN = -1
