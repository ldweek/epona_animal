# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Populate new multi-statement evidence from legacy primary links.

    Exact source bytes were not retained before 1.3.10, so this migration
    never invents an attachment or reconstructs a historical upload.
    """
    cr.execute(
        """
        INSERT INTO eh_bsi_log_statement_rel (log_id, statement_id)
        SELECT id, statement_id
          FROM eh_account_bank_statement_import_log
         WHERE statement_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
