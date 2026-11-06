# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget split wizard.

Generates budget lines as the cartesian product of selected accounts,
selected analytic accounts (the multi-dimensional axis), and the budget
period sliced by a chosen granularity (month / quarter / year). This is
the fast way to turn an empty budget into a fully dimensioned tracking
grid without hand-entering every cell.
"""

from calendar import monthrange
from datetime import date

from odoo import _, fields, models
from odoo.exceptions import UserError


class EhBudgetSplitWizard(models.TransientModel):
    _name = 'eh.budget.split.wizard'
    _description = "Budget split wizard"

    budget_id = fields.Many2one(
        'eh.budget.budget', required=True, ondelete='cascade',
    )
    granularity = fields.Selection(
        [('month', "Monthly"), ('quarter', "Quarterly"),
         ('year', "Yearly")],
        default='month', required=True,
    )
    account_ids = fields.Many2many(
        'account.account', string="Accounts", required=True,
    )
    analytic_account_ids = fields.Many2many(
        'account.analytic.account', string="Analytic accounts",
        help="Optional. Each selected analytic account multiplies the "
             "generated lines, giving one budget cell per "
             "account x analytic x period combination.",
    )
    amount_per_line = fields.Monetary(
        currency_field='currency_id',
        help="Budgeted amount placed on every generated line.",
    )
    currency_id = fields.Many2one(
        related='budget_id.currency_id', readonly=True,
    )
    replace_existing = fields.Boolean(
        string="Replace existing lines",
        help="Delete the budget's current lines before generating.",
    )

    def action_generate(self):
        self.ensure_one()
        budget = self.budget_id
        if budget.state != 'draft':
            raise UserError(_(
                "Only draft budgets can be split (got %s).") % budget.state)
        periods = self._slice_periods(
            budget.date_from, budget.date_to, self.granularity)
        if not periods:
            raise UserError(_("The budget period produced no slices."))
        analytic_ids = self.analytic_account_ids.ids or [False]
        Line = self.env['eh.budget.line']
        vals = []
        seq = 10
        for account in self.account_ids:
            for analytic_id in analytic_ids:
                for period_from, period_to in periods:
                    vals.append({
                        'budget_id': budget.id,
                        'sequence': seq,
                        'account_id': account.id,
                        'analytic_account_id': analytic_id,
                        'period_from': period_from,
                        'period_to': period_to,
                        'budgeted_amount': self.amount_per_line or 0.0,
                    })
                    seq += 1
        if self.replace_existing:
            budget.line_ids.unlink()
        Line.create(vals)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.budget.budget',
            'res_id': budget.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }

    @staticmethod
    def _slice_periods(date_from, date_to, granularity):
        """Slice [date_from, date_to] into (start, end) tuples."""
        if not date_from or not date_to or date_from > date_to:
            return []
        if granularity == 'year':
            return [(date_from, date_to)]
        step = 1 if granularity == 'month' else 3
        out = []
        cur = date_from.replace(day=1)
        while cur <= date_to:
            # End of this slice = last day of the (step-1)-th month ahead.
            end_month = cur.month + step - 1
            end_year = cur.year + (end_month - 1) // 12
            end_month = (end_month - 1) % 12 + 1
            slice_end = date(
                end_year, end_month, monthrange(end_year, end_month)[1])
            out.append((max(cur, date_from), min(slice_end, date_to)))
            nxt_month = end_month + 1
            nxt_year = end_year + (1 if nxt_month > 12 else 0)
            nxt_month = nxt_month if nxt_month <= 12 else 1
            cur = date(nxt_year, nxt_month, 1)
        return out
