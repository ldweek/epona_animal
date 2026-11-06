# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Quarantine legacy maturity rows whose provenance cannot be reconstructed.

The pre-1.1.0 schema stored neither origin nor class. Read-only web
presentation and run contents are not proof of either. Preserve every actual
legacy value, mark both new operational labels as ambiguous, and let a manager
later attest the row as manual or retain it as excluded evidence.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE eh_fin_maturity_line l
           SET origin = 'legacy_ambiguous',
               item_class = 'legacy_unknown',
               legacy_claimed_origin = NULL,
               legacy_claimed_item_class = NULL
    """)
    cr.execute("""
        UPDATE eh_fin_maturity_run r
           SET total_undiscounted = COALESCE((
               SELECT SUM(l.undiscounted_amount)
                 FROM eh_fin_maturity_line l
                WHERE l.run_id = r.id
                  AND l.origin IN ('manual', 'extracted')
           ), 0.0)
    """)
