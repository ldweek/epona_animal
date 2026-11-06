# -*- encoding: utf-8 -*-
"""Allow the former catalog-only BAS definition to receive its handler."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE ir_model_data
           SET noupdate = FALSE
         WHERE module = 'eh_account_l10n_au_bas'
           AND name = 'report_au_bas'
           AND model = 'eh.account.dynamic.report'
    """)
