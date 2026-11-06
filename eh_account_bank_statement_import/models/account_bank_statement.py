# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import models

from .ir_attachment import (
    _SOURCE_STATEMENT_DELETE_CAPABILITY,
    _SOURCE_STATEMENT_DELETE_CONTEXT,
)


class AccountBankStatement(models.Model):
    _inherit = 'account.bank.statement'

    def unlink(self):
        return super(AccountBankStatement, self.with_context(**{
            _SOURCE_STATEMENT_DELETE_CONTEXT:
                _SOURCE_STATEMENT_DELETE_CAPABILITY,
        })).unlink()
