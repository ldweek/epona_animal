# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Controlled down-payment corridor for sealed inter-company orders."""

from odoo import models

from ..models.intercompany_order_mixin import (
    IC_MIRROR_CAPABILITY,
    IC_MIRROR_CONTEXT,
)


class SaleAdvancePaymentInv(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'

    def create_invoices(self):
        self.mapped('sale_order_ids')._eh_ic_assert_accounting_ready()
        engine_self = self.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })
        return super(SaleAdvancePaymentInv, engine_self).create_invoices()
