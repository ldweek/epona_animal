# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Install lifecycle for company-scoped AP defaults."""

import logging


_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Seed a default tolerance profile for every company on fresh install."""
    companies = env['res.company'].sudo().search([])
    legacy_invoice = r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)'
    legacy_total = r'(?im)Total[:\s]+([0-9][0-9,\.]*)'
    for company in companies:
        values = {}
        if company.eh_ap_invoice_ref_regex == legacy_invoice:
            values['eh_ap_invoice_ref_regex'] = False
        if company.eh_ap_total_regex == legacy_total:
            values['eh_ap_total_regex'] = False
        if values:
            company.write(values)
    created = env[
        'eh.ap.tolerance.profile'
    ]._eh_ensure_company_defaults(companies)
    _logger.info(
        "AP automation install retired permissive parser defaults and seeded "
        "%d missing company tolerance profile(s).", len(created),
    )
