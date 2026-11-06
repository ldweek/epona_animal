# -*- encoding: utf-8 -*-
"""Provision isolated annual sequences and retire legacy global counters."""

import re

from odoo import SUPERUSER_ID, api

from odoo.addons.eh_account_costing.models.costing_sequence import (
    _SEQUENCE_SPECS,
    ensure_company_sequence,
)


_SEQUENCE_MODELS = {
    'eh.cost.card': 'eh.cost.card',
    'eh.cost.actual': 'eh.cost.actual',
    'eh.cost.variance.run': 'eh.cost.variance.run',
    'eh.contribution.report': 'eh.contribution.report',
}


def _seed_annual_ranges(env, sequence, code, company):
    """Continue each historical company/year counter without duplicates."""
    spec = _SEQUENCE_SPECS[code]
    pattern = re.compile(
        '^%s(?P<year>[0-9]{4})/(?P<number>[0-9]+)$'
        % re.escape(spec['prefix'].replace('%(year)s/', ''))
    )
    maximum_by_year = {}
    records = env[_SEQUENCE_MODELS[code]].sudo().with_context(
        active_test=False,
    ).search([('company_id', '=', company.id)])
    for name in records.mapped('name'):
        match = pattern.match(name or '')
        if not match:
            continue
        year = int(match.group('year'))
        maximum_by_year[year] = max(
            maximum_by_year.get(year, 0), int(match.group('number')),
        )

    DateRange = env['ir.sequence.date_range'].sudo()
    for year, maximum in maximum_by_year.items():
        date_from = '%04d-01-01' % year
        date_to = '%04d-12-31' % year
        date_range = DateRange.search([
            ('sequence_id', '=', sequence.id),
            ('date_from', '<=', date_from),
            ('date_to', '>=', date_to),
        ], limit=1)
        if not date_range:
            date_range = DateRange.create({
                'sequence_id': sequence.id,
                'date_from': date_from,
                'date_to': date_to,
            })
        next_number = maximum + 1
        if date_range.number_next_actual < next_number:
            # Writing number_next updates both no-gap storage and the
            # PostgreSQL sequence used by the standard implementation.
            date_range.write({'number_next': next_number})


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sequence = env['ir.sequence'].sudo()
    main_company = env.ref('base.main_company')

    # Preserve the main company's accumulated counter where possible by
    # adopting the legacy XML-backed global sequence before provisioning the
    # remaining companies. Global fallbacks are then retired.
    for code, spec in _SEQUENCE_SPECS.items():
        exact_main = Sequence.search([
            ('code', '=', code),
            ('company_id', '=', main_company.id),
        ], limit=1)
        global_sequences = Sequence.search([
            ('code', '=', code), ('company_id', '=', False),
        ], order='id')
        if not exact_main and global_sequences:
            exact_main = global_sequences[0]
            exact_main.write({'company_id': main_company.id})
            global_sequences -= exact_main
        if exact_main:
            exact_main.write({
                'prefix': spec['prefix'],
                'padding': 4,
                'use_date_range': True,
            })
        if global_sequences and 'active' in global_sequences._fields:
            global_sequences.write({'active': False})

    for company in env['res.company'].with_context(active_test=False).search([]):
        for code, spec in _SEQUENCE_SPECS.items():
            sequence = ensure_company_sequence(env, code, company)
            sequence.write({
                'prefix': spec['prefix'],
                'padding': 4,
                'use_date_range': True,
            })
            _seed_annual_ranges(env, sequence, code, company)
