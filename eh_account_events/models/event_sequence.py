# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company- and business-date-scoped references for IAS 8 / IAS 10."""


_SEQUENCE_SPECS = {
    'eh.accounting.change': {
        'name': "EH Accounting Change Reference",
        'prefix': "IAS8/%(year)s/",
    },
    'eh.subsequent.event': {
        'name': "EH Subsequent Event Reference",
        'prefix': "IAS10/%(year)s/",
    },
}


def ensure_company_sequence(env, code, company):
    """Return (and concurrency-safely create) company's exact sequence.

    ``ir.sequence.next_by_code`` falls back to a global sequence. That makes
    otherwise isolated companies consume one counter. This helper never uses
    that fallback. It serializes first-use creation on the company row.
    """
    spec = _SEQUENCE_SPECS[code]
    sequences = env['ir.sequence'].sudo()
    domain = [('code', '=', code), ('company_id', '=', company.id)]
    sequence = sequences.search(domain, limit=1)
    if not sequence:
        company.flush_recordset()
        env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (company.id,),
        )
        sequence = sequences.search(domain, limit=1)
        if not sequence:
            sequence = sequences.create({
                'name': spec['name'],
                'code': code,
                'prefix': spec['prefix'],
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
    return sequence


def next_company_reference(env, code, company, sequence_date):
    """Return reference from one sequence owned by ``company``.

    Year interpolation and annual counter range use ``sequence_date`` rather
    than wall-clock/RPC time.
    """
    sequence = ensure_company_sequence(env, code, company)
    # Odoo 19 forwards sequence_date into prefix interpolation; 16-18 use it
    # for date-range selection but still read ir_sequence_date for %(year)s.
    # Supplying both contracts keeps reference text and annual counter range
    # anchored to the same business date on every supported series.
    return sequence.with_context(
        ir_sequence_date=sequence_date,
    ).next_by_id(sequence_date=sequence_date)
