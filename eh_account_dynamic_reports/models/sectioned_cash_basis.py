# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cash-basis scalar parity for Dynamic Reports.

The shared sectioned handler already recognises grouped P&L rows on settlement
dates.  Balance Sheet current-year earnings and Executive Summary use its
scalar helper, so keep that scalar on the identical recognition path whenever
the requested account perimeter is a P&L flow.
"""

from odoo import api, models


class EhDynamicSectionedCashBasis(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.sectioned'

    _FLOW_ACCOUNT_TYPES = frozenset({
        'income', 'income_other',
        'expense', 'expense_other', 'expense_depreciation',
        'expense_direct_cost',
    })

    @api.model
    def _fetch_aggregate_balance(
        self, account_types=None, company_ids=None,
        date_from=None, date_to=None, posted_only=True, options=None,
        sign=1, currency_table=None,
    ):
        options = options or {}
        account_types = tuple(account_types or ())
        if (options.get('cash_basis') and account_types
                and set(account_types).issubset(self._FLOW_ACCOUNT_TYPES)):
            company_ids = company_ids or [self.env.company.id]
            rows = self._cash_basis_grouped_totals(
                account_types=account_types,
                company_ids=company_ids,
                date_from=date_from,
                date_to=date_to,
                posted_only=posted_only,
                options=options,
                sign=sign,
                currency_table=currency_table,
            )
            return sum(float(row.get('amount') or 0.0) for row in rows)
        return super()._fetch_aggregate_balance(
            account_types=account_types,
            company_ids=company_ids,
            date_from=date_from,
            date_to=date_to,
            posted_only=posted_only,
            options=options,
            sign=sign,
            currency_table=currency_table,
        )
