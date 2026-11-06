# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Adopt explicit IR sources and separate maturity inflows/outflows.

Earlier versions added risk, maturity and fair-value surfaces together, even
though the same instrument is commonly represented in all three.  Historical
computed rows remain untouched as signed evidence.  Future recomputations use
one authoritative source; existing runs adopt the core IFRS 7 risk register.
Existing maturity rows are not rewritten: only their stored headlines are
recomputed so receivable inflows no longer inflate contractual outflows.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        UPDATE eh_fin_sensitivity
           SET ir_source = 'risk_register'
         WHERE ir_source IS NULL
        """,
    )
    cr.execute(
        """
        UPDATE eh_fin_maturity_run r
           SET total_undiscounted = COALESCE((
                   SELECT SUM(l.undiscounted_amount)
                     FROM eh_fin_maturity_line l
                    WHERE l.run_id = r.id
                      AND l.origin IN ('manual', 'extracted')
                      AND l.item_class != 'receivable'
               ), 0.0),
               total_receivable_inflows = COALESCE((
                   SELECT SUM(l.undiscounted_amount)
                     FROM eh_fin_maturity_line l
                    WHERE l.run_id = r.id
                      AND l.origin IN ('manual', 'extracted')
                      AND l.item_class = 'receivable'
               ), 0.0)
        """,
    )
