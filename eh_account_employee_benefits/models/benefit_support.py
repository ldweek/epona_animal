# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared accounting controls for employee-benefit source records."""

import datetime

from odoo import _
from odoo.exceptions import UserError


_SEQUENCE_SPECS = {
    'eh.benefit.valuation': {
        'name': "EH Benefit Valuation Reference",
        'prefix': "DBV/%(year)s/",
    },
    'eh.benefit.dc.accrual': {
        'name': "EH DC Accrual Reference",
        'prefix': "DCA/%(year)s/",
    },
}


def ensure_company_sequence(env, code, company):
    """Return one exact company sequence, creating it under serialization.

    ``next_by_code`` falls back to a global sequence, which makes isolated
    companies consume the same counter.  This helper deliberately performs
    no global fallback and serializes first-use creation on the company row.
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
    """Allocate a company-owned annual reference on the business date."""
    sequence = ensure_company_sequence(env, code, company)
    return sequence.with_context(
        ir_sequence_date=sequence_date,
    ).next_by_id(sequence_date=sequence_date)


def assert_accounting_date_open(env, company, journal, posting_date, label):
    """Refuse a locked exact date before any draft move can be created."""
    lock_probe = env['account.move'].new({
        'move_type': 'entry',
        'company_id': company.id,
        'journal_id': journal.id,
        'date': posting_date,
    })
    violated_locks = lock_probe._get_violated_lock_dates(
        posting_date, False,
    )
    if violated_locks:
        first_open_date = violated_locks[-1][0] + datetime.timedelta(days=1)
        raise UserError(_(
            "The %(label)s accounting date %(requested)s is locked. Odoo "
            "would move the entry to %(open_date)s; choose and document an "
            "explicit open accounting date instead.",
            label=label,
            requested=posting_date,
            open_date=first_open_date,
        ))
