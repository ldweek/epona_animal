"""Keep suite-only onboarding applicability current for company lifecycle."""

from odoo import api, models

from ..hooks import finalize_bas_onboarding


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        finalize_bas_onboarding(self.env, companies)
        return companies

    def write(self, vals):
        result = super().write(vals)
        applicability_fields = {
            'active', 'country_id', 'account_fiscal_country_id',
        }
        if applicability_fields.intersection(vals):
            finalize_bas_onboarding(self.env, self)
        return result
