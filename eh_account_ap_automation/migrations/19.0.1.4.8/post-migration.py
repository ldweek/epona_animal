# -*- encoding: utf-8 -*-
"""Preserve reviewed legacy taxes as deliberate manual evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
            UPDATE res_company
               SET eh_ap_invoice_ref_regex = NULL
             WHERE eh_ap_invoice_ref_regex = %s
        """,
        [r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)'],
    )
    retired_invoice = cr.rowcount
    cr.execute(
        """
            UPDATE res_company
               SET eh_ap_total_regex = NULL
             WHERE eh_ap_total_regex = %s
        """,
        [r'(?im)Total[:\s]+([0-9][0-9,\.]*)'],
    )
    retired_total = cr.rowcount
    cr.execute(
        """
            UPDATE eh_ap_intake_line AS line
               SET tax_provenance = 'manual'
             WHERE EXISTS (
                       SELECT 1
                         FROM eh_ap_intake_line_tax_rel AS relation
                        WHERE relation.line_id = line.id
                   )
               AND COALESCE(tax_provenance, 'none') = 'none'
        """
    )
    preserved = cr.rowcount
    cr.execute(
        """
            UPDATE eh_ap_intake_line
               SET tax_provenance = 'none'
             WHERE tax_provenance IS NULL
        """
    )
    _logger.warning(
        "AP automation 19.0.1.4.8 retired %d/%d permissive company parser "
        "default(s) and preserved %d legacy taxed line(s) as manual tax "
        "evidence.", retired_invoice, retired_total, preserved,
    )
