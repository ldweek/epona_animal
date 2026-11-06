# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Install lifecycle hooks for Dynamic Reports Pro."""

from odoo import SUPERUSER_ID, api


def uninstall_hook(env_or_cr, registry=None):
    """Remove runtime report definitions whose handler is being uninstalled.

    Odoo 16 passes ``(cr, registry)``; Odoo 17+ passes one Environment.
    Builder definitions have no XML ID, so normal module-data cleanup cannot
    own them. Removing them here prevents broken report-menu entries after
    uninstall while preserving every accounting ledger record.
    """
    env = (
        env_or_cr
        if registry is None
        else api.Environment(env_or_cr, SUPERUSER_ID, {})
    )
    attachments = env['ir.attachment'].sudo()
    if 'eh_report_schedule_delivery' in attachments._fields:
        attachments.search([
            ('eh_report_schedule_delivery', '=', True),
        ]).unlink()
    env['eh.account.dynamic.report'].sudo().search([
        (
            'handler_model', '=',
            'eh.account.dynamic.report.handler.builder',
        ),
    ]).unlink()
