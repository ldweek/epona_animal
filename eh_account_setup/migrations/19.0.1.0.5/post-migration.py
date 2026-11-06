# -*- encoding: utf-8 -*-
"""Seed missing task/company pairs after task-definition upgrades."""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    tasks = env['eh.account.setup.task'].sudo().search([])
    companies = env['res.company'].sudo().search([])
    created = tasks._eh_seed_company_lines(companies)
    _logger.info(
        "eh_account_setup 19.0.1.0.5 seeded %d missing task/company lines "
        "without changing existing progress.",
        len(created),
    )
