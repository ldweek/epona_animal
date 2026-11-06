# -*- encoding: utf-8 -*-
"""Invalidate pre-1.7 report payloads after accounting-policy corrections."""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['res.company']._eh_bump_global_report_version()
