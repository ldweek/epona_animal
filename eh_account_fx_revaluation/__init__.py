# -*- encoding: utf-8 -*-
from . import models


def post_init_hook(cr_or_env, registry=None):
    """Seed existing monetary accounts on Odoo 16-19 installations."""
    if registry is not None:
        from odoo import SUPERUSER_ID, api
        env = api.Environment(cr_or_env, SUPERUSER_ID, {})
    else:
        env = cr_or_env
    env['account.account']._eh_seed_existing_monetary_accounts()
