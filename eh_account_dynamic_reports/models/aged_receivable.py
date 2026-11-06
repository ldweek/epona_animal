# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Aged Receivable handler.

Open balances on receivable accounts (asset_receivable, asset_current that
participates in invoicing) bucketed by days overdue. Sign is +1 because a
receivable is naturally a positive amount the customer owes us.

Inherits all bucket / rendering logic from the aged base; only the account
type tuple and SIGN differ from Aged Payable.
"""

from odoo import models
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)


class EhAgedReceivableHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.aged_receivable'
    _inherit = 'eh.account.dynamic.report.handler.aged_base'
    _description = "Aged Receivable report handler"

    REPORT_CODE = 'aged_receivable'
    REPORT_NAME = _lt("Aged Receivable")

    ACCOUNT_TYPES = ('asset_receivable',)
    SIGN = 1
