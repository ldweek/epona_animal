# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company- and fiscal-period-scoped year-end references."""

_SEQUENCE_CODE = 'eh.year.end.run'
_SEQUENCE_PREFIX = 'YE/%(year)s/'


def ensure_company_sequence(env, company):
    """Return one exact-company sequence without a global fallback.

    First use is serialized on a dedicated PostgreSQL advisory key.  This
    avoids locking ``res.company`` merely to allocate a draft reference while
    still preventing duplicate company sequences under concurrent creates.
    """
    Sequence = env['ir.sequence'].sudo().with_context(active_test=False)
    domain = [
        ('code', '=', _SEQUENCE_CODE),
        ('company_id', '=', company.id),
    ]
    sequence = Sequence.search(domain, limit=1)
    if not sequence:
        env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ('eh.year.end.sequence:%s' % company.id,),
        )
        sequence = Sequence.search(domain, limit=1)
        if not sequence:
            sequence = Sequence.create({
                'name': 'EH Year-End Run Reference - %s'
                        % company.display_name,
                'code': _SEQUENCE_CODE,
                'prefix': _SEQUENCE_PREFIX,
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
    # ``noupdate`` protected legacy databases can retain the old global
    # metadata.  An exact-company sequence is safe to normalize in place;
    # already-issued references remain immutable.
    updates = {}
    if sequence.prefix != _SEQUENCE_PREFIX:
        updates['prefix'] = _SEQUENCE_PREFIX
    if not sequence.use_date_range:
        updates['use_date_range'] = True
    if updates:
        sequence.write(updates)
    return sequence


def next_company_reference(env, company, fiscal_year_end):
    """Allocate the annual counter using the fiscal year-end business date."""
    sequence = ensure_company_sequence(env, company)
    return sequence.with_context(
        ir_sequence_date=fiscal_year_end,
    ).next_by_id(sequence_date=fiscal_year_end)
