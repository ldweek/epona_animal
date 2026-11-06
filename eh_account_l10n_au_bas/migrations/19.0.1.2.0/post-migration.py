# -*- encoding: utf-8 -*-
"""Backfill BAS worksheet currency/lodgeability and synchronized ABNs."""


_BAS_REPORT_DESCRIPTION = (
    'Australian Business Activity Statement snapshot for one '
    'company and quarter. Renders the exact computed or lodged '
    '33-label worksheet; Simpler BAS renders its lodgeable '
    'G1, 1A, 1B and PAYG-W subset.'
)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE eh_bas_run_line line
           SET currency_id = company.currency_id,
               is_lodgeable = (
                   NOT run.is_simpler_bas
                   OR line.label_code IN ('G1', '1A', '1B')
                   OR line.label_code LIKE 'W%'
               )
          FROM eh_bas_run run
          JOIN res_company company ON company.id = run.company_id
         WHERE line.run_id = run.id
           AND (line.currency_id IS NULL OR line.is_lodgeable IS NULL)
    """)
    cr.execute("""
        UPDATE eh_bas_label
           SET aggregation = 'payg_withheld_total'
         WHERE code = 'W5'
    """)
    cr.execute("""
        UPDATE eh_account_dynamic_report report
           SET handler_model = 'eh.account.dynamic.report.handler.au_bas',
               description = jsonb_set(
                   COALESCE(report.description, '{}'::jsonb),
                   '{en_US}',
                   to_jsonb(%s::text),
                   TRUE
               )
          FROM ir_model_data data
         WHERE data.module = 'eh_account_l10n_au_bas'
           AND data.name = 'report_au_bas'
           AND data.model = 'eh.account.dynamic.report'
           AND report.id = data.res_id
    """, (_BAS_REPORT_DESCRIPTION,))
    cr.execute("""
        UPDATE eh_bas_run_line w5
           SET label_aggregation = 'payg_withheld_total',
               amount = COALESCE(parts.amount, 0)
          FROM eh_bas_run run
          LEFT JOIN (
              SELECT run_id, SUM(amount) AS amount
                FROM eh_bas_run_line
               WHERE label_code IN ('W2', 'W3', 'W4')
               GROUP BY run_id
          ) parts ON parts.run_id = run.id
         WHERE w5.run_id = run.id
           AND w5.label_code = 'W5'
           AND run.state != 'lodged'
    """)
    # Existing aliases passed the ORM ABN constraint. Fill only empty standard
    # VAT values; never overwrite a separately reviewed tax identifier.
    cr.execute("""
        UPDATE res_partner
           SET vat = eh_au_abn
         WHERE eh_au_abn IS NOT NULL
           AND eh_au_abn != ''
           AND (vat IS NULL OR vat = '')
    """)
