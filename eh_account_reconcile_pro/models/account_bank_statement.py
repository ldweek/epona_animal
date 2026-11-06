# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.bank.statement extension: live unmatched-line counter.

Surfaces a stat-button on the bank statement form: "Unmatched: N"
where N is the number of statement lines on this statement that
haven't been reconciled yet. One click drills into the workspace
filtered to those lines.

Without this the operator has to scroll the line list and eyeball
the reconciled column; with the counter, the priority of the
statement is visible at a glance.
"""

from odoo import _, api, fields, models


class AccountBankStatement(models.Model):
    _inherit = 'account.bank.statement'

    eh_unmatched_line_count = fields.Integer(
        compute='_compute_eh_unmatched_line_count',
        help=(
            "Statement lines on this statement that have not yet "
            "been reconciled. Refreshes on every form load."
        ),
    )

    @api.depends('line_ids', 'line_ids.is_reconciled')
    def _compute_eh_unmatched_line_count(self):
        for statement in self:
            statement.eh_unmatched_line_count = len(
                statement.line_ids.filtered(
                    lambda l: not getattr(l, 'is_reconciled', False),
                ),
            )

    def action_view_eh_unmatched_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Unmatched lines"),
            'res_model': 'account.bank.statement.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('statement_id', '=', self.id),
                ('is_reconciled', '=', False),
            ],
        }
