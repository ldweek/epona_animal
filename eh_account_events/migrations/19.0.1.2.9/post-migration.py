# -*- encoding: utf-8 -*-
"""Backfill explicit dates/references and provision company sequences."""

from odoo import api, SUPERUSER_ID

from odoo.addons.eh_account_events.models.event_sequence import (
    ensure_company_sequence,
    next_company_reference,
)


def migrate(cr, version):
    if not version:
        return
    # Registry initialization gives a required Date its technical default.
    # Legacy restatements must instead retain their old posting date exactly.
    cr.execute(
        "UPDATE eh_accounting_change "
        "SET restatement_date = change_date "
        "WHERE change_date IS NOT NULL"
    )
    env = api.Environment(cr, SUPERUSER_ID, {})
    changes = env['eh.accounting.change'].search([])
    events = env['eh.subsequent.event'].search([])
    companies = (changes.mapped('company_id') | events.mapped('company_id'))
    for company in companies:
        for code in ('eh.accounting.change', 'eh.subsequent.event'):
            ensure_company_sequence(env, code, company)
    legacy_events = env['eh.subsequent.event'].search([
        '|', ('reference', '=', False), ('reference', '=', '/'),
    ])
    legacy_events.invalidate_recordset(['reference'])
    for event in legacy_events:
        event.reference = next_company_reference(
            env, 'eh.subsequent.event', event.company_id, event.event_date,
        ) or '/'
