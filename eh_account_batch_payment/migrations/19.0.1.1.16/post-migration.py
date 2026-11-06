# -*- encoding: utf-8 -*-
"""Provision isolated batch sequences and retire the legacy global counter."""

from odoo import SUPERUSER_ID, api

from odoo.addons.eh_account_batch_payment.models.batch_sequence import (
    ensure_company_sequence,
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    companies = env['res.company'].search([])
    for company in companies:
        for batch_type in ('inbound', 'outbound'):
            ensure_company_sequence(env, batch_type, company)
    legacy = env['ir.sequence'].search([
        ('code', '=', 'eh.batch.payment'),
        ('company_id', '=', False),
    ])
    if legacy and 'active' in legacy._fields:
        legacy.write({'active': False})
