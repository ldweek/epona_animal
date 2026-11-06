# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company-, direction-, and payment-date-scoped batch references."""


_SEQUENCE_SPECS = {
    'inbound': {
        'code': 'eh.batch.payment.inbound',
        'name': "EH Inbound Batch Payment Reference",
        'prefix': "BATCH-IN/%(year)s/",
    },
    'outbound': {
        'code': 'eh.batch.payment.outbound',
        'name': "EH Outbound Batch Payment Reference",
        'prefix': "BATCH-OUT/%(year)s/",
    },
}


def ensure_company_sequence(env, batch_type, company):
    """Return the exact company/direction sequence, creating it safely."""
    spec = _SEQUENCE_SPECS[batch_type]
    sequences = env['ir.sequence'].sudo()
    domain = [
        ('code', '=', spec['code']),
        ('company_id', '=', company.id),
    ]
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
                'code': spec['code'],
                'prefix': spec['prefix'],
                'padding': 5,
                'company_id': company.id,
                'use_date_range': True,
            })
    return sequence


def next_batch_reference(env, batch_type, company, payment_date):
    """Consume the counter whose company, direction, and year are reviewed."""
    sequence = ensure_company_sequence(env, batch_type, company)
    return sequence.with_context(
        ir_sequence_date=payment_date,
    ).next_by_id(sequence_date=payment_date)
