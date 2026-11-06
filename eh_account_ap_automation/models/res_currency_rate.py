# -*- encoding: utf-8 -*-
"""Serialize AP FX evidence with mutations of the effective rate table."""

from odoo import api, models


class ResCurrencyRate(models.Model):
    _inherit = 'res.currency.rate'

    @api.model
    def _eh_ap_lock_currency_ids(self, currency_ids):
        for currency_id in sorted(set(currency_ids or ())):
            if not currency_id:
                continue
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ['eh-ap-fx|%s' % currency_id],
            )

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_ap_lock_currency_ids(
            vals.get('currency_id') for vals in vals_list
        )
        return super().create(vals_list)

    def write(self, vals):
        currency_ids = set(self.mapped('currency_id').ids)
        if vals.get('currency_id'):
            currency_ids.add(vals['currency_id'])
        self._eh_ap_lock_currency_ids(currency_ids)
        return super().write(vals)

    def unlink(self):
        self._eh_ap_lock_currency_ids(self.mapped('currency_id').ids)
        return super().unlink()
