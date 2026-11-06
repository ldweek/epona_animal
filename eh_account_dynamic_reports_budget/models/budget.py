# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
from odoo import _, api, models
from odoo.exceptions import AccessError


class EhBudget(models.Model):
    _inherit = 'eh.budget.budget'

    @api.model
    def eh_report_budget_choices(
        self, report_code=None, company_ids=None, state_filter='live',
    ):
        """Budgets selectable in the P&L dynamic-report budget filter.

        Server decides whether the active report supports this extension, so
        the client does not hard-code a report code.  Choices are scoped to
        the report companies, include lifecycle/version identity, and never
        use sudo.
        """
        report = self.env['eh.account.dynamic.report'].search([
            ('code', '=', report_code or ''),
            ('handler_model', '=',
             'eh.account.dynamic.report.handler.profit_and_loss'),
        ], limit=1)
        if not report:
            return {'supported': False, 'choices': []}

        # Financial-report access is broader than Budget Pro access: EH
        # auditors may render reports but deliberately cannot read operational
        # budgets.  Treat absent Budget ACLs as no picker choices so this
        # optional extension cannot prevent the base viewer from mounting.
        # Never sudo: authorised users remain constrained by record rules and
        # active-company scope.
        try:
            self._eh_check_access('read')
        except AccessError:
            return {'supported': True, 'choices': []}

        allowed_company_ids = set(self.env.companies.ids)
        requested_company_ids = {
            int(company_id) for company_id in (company_ids or ())
            if str(company_id).isdigit()
        }
        if not requested_company_ids:
            requested_company_ids = allowed_company_ids
        requested_company_ids &= allowed_company_ids
        if not requested_company_ids:
            return {'supported': True, 'choices': []}

        domain = [
            ('company_id', 'in', sorted(requested_company_ids)),
        ]
        if state_filter != 'all':
            domain.append(('state', 'in', ('draft', 'confirmed')))
        budgets = self.search(domain, order='name, id')
        state_labels = {
            'draft': _("Draft"),
            'confirmed': _("Confirmed"),
            'revised': _("Revised"),
            'closed': _("Closed"),
        }
        choices = []
        for budget in budgets:
            state_label = state_labels.get(
                budget.state, budget.state or _("Unknown"),
            )
            version_label = budget.version_label or _("Unversioned")
            choices.append({
                'id': budget.id,
                'name': budget.display_name,
                'company_id': budget.company_id.id,
                'state': budget.state,
                'state_label': state_label,
                'version_label': version_label,
                'label': _(
                    "%(name)s — %(state)s — %(version)s",
                    name=budget.display_name,
                    state=state_label,
                    version=version_label,
                ),
            })
        return {'supported': True, 'choices': choices}
