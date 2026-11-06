# -*- encoding: utf-8 -*-
"""Seed missing company-scoped AP tolerance defaults on upgrade."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    companies = env['res.company'].sudo().search([])
    created = env[
        'eh.ap.tolerance.profile'
    ]._eh_ensure_company_defaults(companies)
    _logger.warning(
        "AP automation 19.0.1.4.7 seeded %d missing company tolerance "
        "profile(s).",
        len(created),
    )
