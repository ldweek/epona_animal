# -*- encoding: utf-8 -*-
"""Initialize the new IAS 1 disclosure totals on existing worksheets."""


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE eh_soci_line
           SET net_amount = COALESCE(amount, 0) + COALESCE(tax_amount, 0)
    """)
    cr.execute("""
        UPDATE eh_soce_line
           SET restated_opening_balance =
                   COALESCE(opening_balance, 0)
                   + COALESCE(retrospective_adjustment, 0),
               oci_analysis_total =
                   COALESCE(oci_revaluation, 0)
                   + COALESCE(oci_fvoci, 0)
                   + COALESCE(oci_cashflow_hedge, 0)
                   + COALESCE(oci_translation, 0)
                   + COALESCE(oci_actuarial, 0)
                   + COALESCE(oci_associates_jv, 0)
                   + COALESCE(oci_other, 0),
               oci_analysis_residual =
                   COALESCE(oci_movement, 0)
                   - (COALESCE(oci_revaluation, 0)
                      + COALESCE(oci_fvoci, 0)
                      + COALESCE(oci_cashflow_hedge, 0)
                      + COALESCE(oci_translation, 0)
                      + COALESCE(oci_actuarial, 0)
                      + COALESCE(oci_associates_jv, 0)
                      + COALESCE(oci_other, 0))
    """)
