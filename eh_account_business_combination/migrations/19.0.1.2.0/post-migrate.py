# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Seed new equity-method lifecycle anchors on existing active records."""


def migrate(cr, version):
    del version
    cr.execute(
        """
        UPDATE eh_equity_investment
           SET current_ownership_pct = ownership_pct,
               activation_date = COALESCE(activation_date, accounting_date)
         WHERE state = 'active'
           AND COALESCE(current_ownership_pct, 0) = 0
        """
    )
