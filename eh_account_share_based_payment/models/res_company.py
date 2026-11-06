# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Protect the currency identity of SBP measurement evidence."""

from odoo import _, models
from odoo.exceptions import AccessError, UserError

from .sbp_plan import (
    EH_SBP_ENGINE_CAPABILITY,
    EH_SBP_ENGINE_CONTEXT_KEY,
    _lock_sbp_companies,
)


class ResCompany(models.Model):
    _inherit = 'res.company'

    def write(self, vals):
        requested_currency_id = vals.get('currency_id')
        changed = self.filtered(
            lambda company: company.currency_id.id != requested_currency_id
        ) if 'currency_id' in vals else self.env['res.company']
        if not changed:
            return super().write(vals)

        # Preserve a genuine sudo caller.  For a normal caller, ordinary
        # company write access is checked before the elevated SBP evidence
        # lookup.  The company row is locked first so plan creation cannot race
        # the authoritative "does SBP data exist?" decision.
        actor_companies = changed
        if not actor_companies.env.su:
            actor_companies._eh_check_access('write')
        _lock_sbp_companies(
            actor_companies,
            actor_companies,
            access_mode='write',
            require_allowed=False,
        )

        plans = self.env['eh.sbp.plan'].sudo().with_context(
            active_test=False,
        ).search([('company_id', 'in', changed.ids)])
        valuations = self.env['eh.sbp.valuation'].sudo().search([
            ('company_id', 'in', changed.ids),
        ])
        if not plans and not valuations:
            return super().write(vals)
        if not self.env.su:
            foreign = changed.filtered(
                lambda company: company.id not in self.env.companies.ids,
            )
            if foreign:
                raise AccessError(_(
                    "A company with share-based-payment evidence must be "
                    "one of your allowed companies before its currency can "
                    "change."
                ))

        approved = plans.filtered(lambda plan: plan.state != 'draft')
        if approved:
            raise UserError(_(
                "A company currency cannot change while it has an activated "
                "or terminal share-based-payment plan. The approved currency "
                "is immutable historical measurement evidence."
            ))
        populated_drafts = plans.filtered(lambda plan: (
            plan.grant_ids
            or plan.tranche_ids
            or plan.modification_ids
            or plan.run_ids
            or bool(plan.settlement_amount)
            or bool(plan.recognised_cumulative)
        ))
        if populated_drafts:
            raise UserError(_(
                "A company currency cannot change while a draft "
                "share-based-payment plan contains monetary terms. Delete or "
                "recreate those draft terms in the new currency first."
            ))

        valuations._eh_lock_valuations()
        result = super().write(vals)
        if valuations:
            valuations.with_context(**{
                EH_SBP_ENGINE_CONTEXT_KEY: EH_SBP_ENGINE_CAPABILITY,
            }).write({
                'result_value': 0.0,
                'result_provenance_version': 0,
                'result_currency_id': False,
            })
        return result
