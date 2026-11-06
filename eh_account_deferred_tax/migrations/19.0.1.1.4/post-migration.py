# -*- coding: utf-8 -*-
"""Backfill stable producer identity for historical gathered ECL rows."""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE eh_deferred_tax_line line
           SET source_model = 'eh.ecl.run',
               source_key = 'company:' || run.company_id::text
                            || ':ecl_loss_allowance'
          FROM eh_deferred_tax_run run
         WHERE run.id = line.run_id
           AND line.eh_auto_gathered IS TRUE
           AND line.category = 'receivable_ecl'
           AND (line.source_model IS NULL OR line.source_model = '')
           AND (line.source_key IS NULL OR line.source_key = '')
        """
    )
