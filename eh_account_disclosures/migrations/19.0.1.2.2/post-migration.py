# -*- coding: utf-8 -*-
"""Provision exact-company disclosure sequences for every company."""

from odoo import SUPERUSER_ID, api


SEQUENCES = {
    'eh.segment.report': (
        'seq_eh_segment_report', 'EH Segment Report Reference',
        'SEG/%(year)s/', 4),
    'eh.fin.maturity.run': (
        'seq_eh_fin_maturity_run', 'EH Maturity Analysis Run Reference',
        'MAT/%(year)s/', 4),
    'eh.fin.credit.note': (
        'seq_eh_fin_credit_note', 'EH Credit-Risk Note Reference',
        'CRN/%(year)s/', 4),
    'eh.fin.sensitivity': (
        'seq_eh_fin_sensitivity', 'EH Sensitivity Analysis Reference',
        'SENS/%(year)s/', 4),
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sequence = env['ir.sequence']
    companies = env['res.company'].search([])
    main_company = env.ref('base.main_company')
    module_data = env['ir.model.data']

    for code, (xml_name, label, prefix, padding) in SEQUENCES.items():
        global_sequences = Sequence.search([
            ('code', '=', code), ('company_id', '=', False),
        ], order='id')
        exact = {
            sequence.company_id.id: sequence
            for sequence in Sequence.search([
                ('code', '=', code), ('company_id', '!=', False),
            ], order='id')
        }
        if main_company.id not in exact and global_sequences:
            main_sequence = global_sequences[0]
            main_sequence.write({'company_id': main_company.id})
            exact[main_company.id] = main_sequence
            global_sequences -= main_sequence
        for company in companies:
            if company.id not in exact:
                exact[company.id] = Sequence.create({
                    'name': '%s - %s' % (label, company.display_name),
                    'code': code,
                    'prefix': prefix,
                    'padding': padding,
                    'company_id': company.id,
                })
        xmlid = module_data.search([
            ('module', '=', 'eh_account_disclosures'),
            ('name', '=', xml_name),
        ], limit=1)
        if xmlid:
            xmlid.write({'res_id': exact[main_company.id].id})
        global_sequences.unlink()
