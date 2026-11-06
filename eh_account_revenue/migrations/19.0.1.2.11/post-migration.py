# -*- encoding: utf-8 -*-
"""Scope contract sequences and preserve only legacy basis placeholders."""


def migrate(cr, version):
    if not version:
        return

    # New records must document their progress basis. Preserve upgrade
    # compatibility only for pre-existing over-time obligations whose former
    # schema supplied no basis field/value.
    cr.execute("""
        UPDATE eh_revenue_obligation
           SET method_basis = 'migrated'
         WHERE satisfaction = 'over_time'
           AND NULLIF(BTRIM(COALESCE(method_basis, '')), '') IS NULL
    """)

    # amount_recognised used to mirror nominal obligation anchors. Rebuild it
    # from posted, same-company revenue-account lines so upgraded financed
    # contracts expose the same present-value ledger measure as new records.
    cr.execute("""
        WITH posted_revenue AS (
            SELECT c.id,
                   COALESCE(-SUM(aml.balance) FILTER (
                       WHERE am.state = 'posted'
                         AND am.company_id = c.company_id
                         AND aml.account_id = c.revenue_account_id
                   ), 0.0) AS recognised
              FROM eh_revenue_contract c
              LEFT JOIN account_move am
                ON am.eh_revenue_contract_id = c.id
              LEFT JOIN account_move_line aml
                ON aml.move_id = am.id
             GROUP BY c.id
        )
        UPDATE eh_revenue_contract c
           SET amount_recognised = p.recognised
          FROM posted_revenue p
         WHERE p.id = c.id
    """)

    # Retire the formerly global XML sequence. If another company-scoped
    # sequence already exists, preserve its counter and move the old XML row
    # to an inert legacy code; otherwise scope the existing counter to the
    # main company so numbering continues without reset.
    cr.execute("""
        SELECT seq.id, main_company.res_id
          FROM ir_model_data seq_xml
          JOIN ir_sequence seq ON seq.id = seq_xml.res_id
          JOIN ir_model_data main_company
            ON main_company.module = 'base'
           AND main_company.name = 'main_company'
           AND main_company.model = 'res.company'
         WHERE seq_xml.module = 'eh_account_revenue'
           AND seq_xml.name = 'seq_eh_revenue_contract'
           AND seq_xml.model = 'ir.sequence'
    """)
    row = cr.fetchone()
    if not row:
        return
    sequence_id, company_id = row
    cr.execute("""
        SELECT id
          FROM ir_sequence
         WHERE code = 'eh.revenue.contract'
           AND company_id = %s
           AND id != %s
         ORDER BY id
         LIMIT 1
    """, (company_id, sequence_id))
    if cr.fetchone():
        cr.execute("""
            UPDATE ir_sequence
               SET code = 'eh.revenue.contract.legacy.global',
                   name = name || ' (retired global)'
             WHERE id = %s
        """, (sequence_id,))
    else:
        cr.execute("""
            UPDATE ir_sequence
               SET company_id = %s
             WHERE id = %s
        """, (company_id, sequence_id))
