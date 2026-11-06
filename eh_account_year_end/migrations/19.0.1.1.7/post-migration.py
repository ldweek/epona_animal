# -*- encoding: utf-8 -*-
"""Preserve legacy close evidence and activate fiscal-period references."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    main_company = env.ref('base.main_company')
    legacy_sequence = env.ref(
        'eh_account_year_end.seq_eh_year_end_run',
        raise_if_not_found=False,
    )
    Sequence = env['ir.sequence'].sudo().with_context(active_test=False)
    company_sequence = Sequence.search([
        ('code', '=', 'eh.year.end.run'),
        ('company_id', '=', main_company.id),
    ], limit=1)
    if not company_sequence and legacy_sequence:
        company_sequence = legacy_sequence
    if company_sequence:
        company_sequence.write({
            'company_id': main_company.id,
            'prefix': 'YE/%(year)s/',
            'use_date_range': True,
        })

    Line = env['eh.year.end.line'].sudo().with_context(
        eh_year_end_engine=True,
    )
    lines = Line.search([
        ('source_account_label', '=', False),
        ('account_id', '!=', False),
    ])
    for line in lines:
        scoped = line.account_id.with_company(line.run_id.company_id)
        line.source_account_label = (
            scoped.code or scoped.name or line.account_id.display_name
        )

    _logger.warning(
        "Year End 19.0.1.1.7 enabled a company/fiscal-date sequence and "
        "backfilled %d stable source-account label(s) without changing any "
        "journal entry or amount.",
        len(lines),
    )
