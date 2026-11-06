# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Race-safe company discriminator for purchase-budget governance."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


_GOVERNANCE_LOCK_PREFIX = 'eh_account_budget_pro:company:'


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_budget_governance_active = fields.Boolean(
        readonly=True,
        copy=False,
        index=True,
        help=(
            "Server-owned discriminator indicating that confirmed or "
            "superseded budget policy can govern purchase commitments. "
            "Purchase source hooks use it under a transaction advisory lock "
            "to bypass expensive source-row locks in companies that have no "
            "live budget governance."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any('eh_budget_governance_active' in vals for vals in vals_list):
            raise AccessError(_(
                "Budget-governance status is maintained by the budget "
                "engine and cannot be supplied directly."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_budget_governance_active' in vals:
            raise AccessError(_(
                "Budget-governance status is maintained by the budget "
                "engine and cannot be edited directly."
            ))
        return super().write(vals)

    @api.private
    def _eh_lock_budget_governance(self, *, shared):
        """Serialize policy transitions against purchase source mutations."""
        for company_id in sorted(set(self.ids)):
            function = (
                'pg_advisory_xact_lock_shared'
                if shared else 'pg_advisory_xact_lock'
            )
            self.env.cr.execute(
                "SELECT %s(hashtextextended(%%s, 0))" % function,
                (_GOVERNANCE_LOCK_PREFIX + str(company_id),),
            )
        return self

    @api.private
    def _eh_refresh_budget_governance(self):
        """Refresh authoritative discriminator while exclusive lock is held."""
        company_ids = tuple(sorted(set(self.ids)))
        if not company_ids:
            return self
        # Odoo 19 can defer the workflow assignment until flush. Raw SQL must
        # never compute the discriminator from the pre-transition state.
        self.env['eh.budget.budget'].flush_model([
            'company_id', 'state', 'overrun_policy',
        ])
        self.env.cr.execute(
            """
            UPDATE res_company AS company
               SET eh_budget_governance_active = EXISTS (
                   SELECT 1
                     FROM eh_budget_budget AS budget
                    WHERE budget.company_id = company.id
                      AND budget.state IN ('confirmed', 'revised')
                      AND budget.overrun_policy != 'off'
               )
             WHERE company.id IN %s
            """,
            (company_ids,),
        )
        self.invalidate_recordset(['eh_budget_governance_active'])
        return self
