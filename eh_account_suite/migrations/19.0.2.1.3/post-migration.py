"""Apply country-aware BAS onboarding to existing suite companies."""

from odoo import SUPERUSER_ID, api

from odoo.addons.eh_account_suite.hooks import finalize_bas_onboarding


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    finalize_bas_onboarding(env)
