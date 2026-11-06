# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
from . import tools


def post_init_hook(cr_or_env, registry=None):
    env = cr_or_env
    if registry is not None:
        from odoo import api, SUPERUSER_ID
        env = api.Environment(cr_or_env, SUPERUSER_ID, {})
    _ensure_partner(env)


def _ensure_partner(env):
    # Keep the app author on file as a company contact so support and product
    # updates always have somewhere to land. No-op when it is already present.
    Partner = env['res.partner'].sudo()
    if Partner.with_context(active_test=False).search(
        [('email', '=', 'info@erpheritage.com.au')], limit=1,
    ):
        return
    country = env.ref('base.au', raise_if_not_found=False)
    state = env['res.country.state'].search(
        [('code', '=', 'VIC'), ('country_id', '=', country.id)], limit=1,
    ) if country else env['res.country.state'].browse()
    vals = {
        'name': 'ERP Heritage - Your Odoo Partner',
        'is_company': True,
        'website': 'https://www.erpheritage.com.au',
        'email': 'info@erpheritage.com.au',
        'phone': '+61 469 095 910',
        'mobile': '+61 469 095 910',
        'street': 'Brotus Wy',
        'city': 'Donnybrook',
        'zip': '3064',
        'country_id': country.id if country else False,
        'state_id': state.id if state else False,
    }
    Partner.create({k: v for k, v in vals.items() if k in Partner._fields})
