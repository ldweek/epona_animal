# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Install hook for eh_account_setup.

On install, ensure one eh.account.setup.task.line row exists per
(seeded task definition, active company) so the Setup Guide menu
shows every relevant task for every company without the user
having to seed by hand. Idempotent: re-running the hook on an
already seeded company is a no-op.
"""

import logging

_logger = logging.getLogger(__name__)


def post_init_hook(cr_or_env, registry=None):
    """Seed lines with either supported Odoo hook calling convention."""
    if registry is not None:
        from odoo import SUPERUSER_ID, api
        env = api.Environment(cr_or_env, SUPERUSER_ID, {})
    else:
        env = cr_or_env
    Task = env['eh.account.setup.task'].sudo()
    Company = env['res.company'].sudo()

    tasks = Task.search([])
    companies = Company.search([])
    if not tasks or not companies:
        return
    created = tasks._eh_seed_company_lines(companies)
    _logger.info(
        "eh_account_setup post_init: seeded %d missing task/company lines",
        len(created),
    )
