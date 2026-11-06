# -*- encoding: utf-8 -*-
"""Retire withdrawn COR1 configuration without rewriting bank evidence."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    for table, column in (
        ('res_company', 'eh_sepa_dd_default_instrument'),
        ('eh_sepa_creditor', 'default_local_instrument'),
        ('eh_sepa_mandate', 'local_instrument'),
    ):
        cr.execute(
            "UPDATE %s SET %s = 'CORE' WHERE %s = 'COR1'"
            % (table, column, column),
        )
        if cr.rowcount:
            _logger.warning(
                "eh_account_sepa_dd 19.0.1.2.0: migrated %d withdrawn "
                "COR1 values to CORE in %s.%s.",
                cr.rowcount, table, column,
            )

    # Historical exports/payment snapshots remain byte-truthful. Retire live
    # COR1 files so they cannot be submitted or re-cut under withdrawn rules.
    cr.execute("""
        UPDATE eh_sepa_dd_export
           SET state = 'superseded'
         WHERE local_instrument = 'COR1'
           AND state IN ('generated', 'downloaded')
    """)
    if cr.rowcount:
        _logger.warning(
            "eh_account_sepa_dd 19.0.1.2.0: retired %d live historic COR1 "
            "exports; immutable files and snapshots were preserved.",
            cr.rowcount,
        )
