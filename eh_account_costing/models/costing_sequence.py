# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company- and business-date-scoped references for costing documents."""

from odoo import _
from odoo.exceptions import AccessError, UserError


_SEQUENCE_SPECS = {
    'eh.cost.card': {
        'name': "EH Standard Cost Card",
        'prefix': "SCC/%(year)s/",
    },
    'eh.cost.actual': {
        'name': "EH Cost Actuals",
        'prefix': "CACT/%(year)s/",
    },
    'eh.cost.variance.run': {
        'name': "EH Cost Variance Run",
        'prefix': "CVAR/%(year)s/",
    },
    'eh.contribution.report': {
        'name': "EH Contribution Report",
        'prefix': "CMR/%(year)s/",
    },
}


def ensure_company_sequence(env, code, company):
    """Return the exact-company sequence, creating it under a narrow lock.

    ``next_by_code`` falls back to a global sequence and therefore lets one
    company consume another company's counter.  Exact lookup avoids that
    fallback.  The company row is locked only during first-use provisioning,
    never during ordinary costing writes.
    """
    if code not in _SEQUENCE_SPECS:
        raise UserError(_("Unknown costing sequence code: %s") % code)
    if not env.su and company.id not in env.companies.ids:
        raise AccessError(_(
            "A costing reference can only be assigned for an enabled "
            "company."
        ))
    Sequence = env['ir.sequence'].sudo().with_company(company)
    domain = [('code', '=', code), ('company_id', '=', company.id)]
    sequence = Sequence.search(domain, limit=1)
    if not sequence:
        company.flush_recordset()
        env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (company.id,),
        )
        sequence = Sequence.search(domain, limit=1)
        if not sequence:
            spec = _SEQUENCE_SPECS[code]
            sequence = Sequence.create({
                'name': '%s - %s' % (spec['name'], company.display_name),
                'code': code,
                'prefix': spec['prefix'],
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
    return sequence


def next_company_reference(env, code, company, sequence_date):
    """Consume the company/year counter anchored to ``sequence_date``."""
    sequence = ensure_company_sequence(env, code, company)
    # Odoo 19 forwards sequence_date into interpolation.  Earlier supported
    # series use the context key for %(year)s and the argument for range
    # selection, so supply both contracts.
    return sequence.with_context(
        ir_sequence_date=sequence_date,
    ).next_by_id(sequence_date=sequence_date)
