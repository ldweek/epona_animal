# -*- coding: utf-8 -*-
"""Name compound forecast growth truthfully on stored scenarios."""


def migrate(cr, version):
    cr.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'eh_report_forecast'"
    )
    columns = {row[0] for row in cr.fetchall()}
    if 'growth_method' not in columns:
        return
    cr.execute(
        "UPDATE eh_report_forecast SET growth_method = 'compound' "
        "WHERE growth_method = 'linear'"
    )
