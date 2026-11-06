# -*- encoding: utf-8 -*-
"""Retire the shared counter without restarting any consumed grant year."""

from collections import defaultdict
from datetime import date
import re

from odoo import SUPERUSER_ID, api


_REFERENCE_RE = re.compile(r'^GRANT/(?P<year>\d{4})/(?P<number>\d+)$')


def _date_range_floor(env, sequence, year, number_next):
    """Ensure ``year`` can never reuse a reference already in evidence."""
    DateRange = env['ir.sequence.date_range']
    date_from = date(year, 1, 1)
    date_to = date(year, 12, 31)
    sequence_range = DateRange.search([
        ('sequence_id', '=', sequence.id),
        ('date_from', '<=', date_from),
        ('date_to', '>=', date_to),
    ], limit=1)
    if not sequence_range:
        sequence_range = DateRange.create({
            'sequence_id': sequence.id,
            'date_from': date_from,
            'date_to': date_to,
            'number_next': max(number_next, 1),
        })
        # On standard sequences the PostgreSQL subsequence is created before
        # the computed ``number_next_actual`` cache is settled. An explicit
        # write keeps both the ORM row and PostgreSQL sequence at the floor.
        sequence_range.write({'number_next': max(number_next, 1)})
        return
    current = sequence_range.number_next_actual or 1
    if current < number_next:
        sequence_range.write({'number_next': number_next})


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Grant = env['eh.gov.grant']
    Sequence = env['ir.sequence'].with_context(active_test=False)
    main_company = env.ref('base.main_company')
    global_sequences = Sequence.search([
        ('code', '=', 'eh.gov.grant'),
        ('company_id', '=', False),
    ], order='id')
    main_sequence = Sequence.search([
        ('code', '=', 'eh.gov.grant'),
        ('company_id', '=', main_company.id),
    ], limit=1)
    # The XML data update may already have assigned the former global record to
    # the main company and enabled date ranges. The parent counter still holds
    # the consumed legacy next number, so capture it before normalising ranges.
    legacy_main_next = max(
        [sequence.number_next_actual for sequence in global_sequences]
        + ([main_sequence.number_next_actual] if main_sequence else [1])
    )
    if not main_sequence and global_sequences:
        main_sequence = global_sequences[0]
        main_sequence.write({
            'company_id': main_company.id,
            'active': True,
        })
        global_sequences -= main_sequence
    if not main_sequence:
        main_sequence = Grant._eh_company_sequence(main_company)

    sequence_by_company = {}
    for company in env['res.company'].search([]):
        sequence = Grant._eh_company_sequence(company)
        sequence_by_company[company.id] = sequence
        duplicates = Sequence.search([
            ('code', '=', 'eh.gov.grant'),
            ('company_id', '=', company.id),
            ('id', '!=', sequence.id),
        ])
        if duplicates:
            duplicates.write({'active': False})
    main_sequence = sequence_by_company[main_company.id]

    # Existing names are the authoritative collision evidence. Seed every
    # observed prefix year above its largest serial, even when the old code
    # took that year from server-today rather than grant_date.
    floors = defaultdict(lambda: 1)
    for grant in Grant.with_context(active_test=False).search([]):
        match = _REFERENCE_RE.fullmatch(grant.name or '')
        if match:
            key = (grant.company_id.id, int(match.group('year')))
            floors[key] = max(floors[key], int(match.group('number')) + 1)
    current_key = (main_company.id, date.today().year)
    floors[current_key] = max(floors[current_key], legacy_main_next)
    for (company_id, year), floor in floors.items():
        sequence = sequence_by_company.get(company_id)
        if sequence:
            _date_range_floor(env, sequence, year, floor)

    if global_sequences:
        global_sequences.write({'active': False})
    xmlid = env['ir.model.data'].search([
        ('module', '=', 'eh_account_grants'),
        ('name', '=', 'seq_eh_gov_grant'),
    ], limit=1)
    if xmlid:
        xmlid.write({'res_id': main_sequence.id})
