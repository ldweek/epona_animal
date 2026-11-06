# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.account.setup.task: a setup checklist task definition.

Definitions are seeded by XML data and shared across companies.
Per company progress lives on eh.account.setup.task.line, which
holds the state machine and the audit fields. Splitting the two
keeps module upgrades from blowing away user state when a task
description is reworded.
"""

import operator as py_operator

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_TASK_ENGINE_FIELDS = frozenset({
    'line_ids', 'line_count', 'line_done_count', 'line_todo_count',
    'line_skipped_count', 'completion_ratio',
})


class EhAccountSetupTask(models.Model):
    _name = 'eh.account.setup.task'
    _description = "Accounting setup task"
    _order = 'category, sequence, id'

    key = fields.Char(
        required=True,
        index=True,
        copy=False,
        help=(
            "Stable technical identifier. Referenced from data XML "
            "external ids and from any future cross module rules. "
            "Renaming a key after release is a breaking change."
        ),
    )
    name = fields.Char(
        required=True,
        translate=True,
        help="Short verb led label, e.g. 'Configure approval policy'.",
    )
    description = fields.Text(
        translate=True,
        help=(
            "One paragraph explaining what completing this task "
            "achieves. Shown on the form view and the kanban card."
        ),
    )
    sequence = fields.Integer(default=10)
    category = fields.Selection(
        [
            ('foundations', "Foundations"),
            ('currencies', "Currencies"),
            ('receivables', "Receivables"),
            ('payables', "Payables"),
            ('approvals', "Approvals"),
            ('reporting', "Reporting"),
            ('close', "Period Close"),
        ],
        required=True,
        default='foundations',
    )
    required_modules = fields.Char(
        help=(
            "Whitespace separated list of technical module names. "
            "The task only renders for a company when every listed "
            "module is installed in the database. Empty means the "
            "task always renders."
        ),
    )
    action_xmlid = fields.Char(
        required=True,
        help=(
            "External id of the ir.actions.* record to open when the "
            "user clicks Open on the task. Resolved at click time, "
            "so the definition does not break if the target action "
            "is renamed; an unresolved id surfaces a clear error "
            "naming the missing xmlid."
        ),
    )
    icon = fields.Char(
        help=(
            "Optional font awesome class, without the leading 'fa fa-'. "
            "Rendered on the kanban card as a small category glyph."
        ),
    )

    line_ids = fields.One2many(
        'eh.account.setup.task.line',
        'task_id',
        help="Per company progress lines for this task.",
    )

    line_count = fields.Integer(
        compute='_compute_line_counts', compute_sudo=False,
        help="Progress lines visible in the caller's allowed companies.",
    )
    line_done_count = fields.Integer(
        compute='_compute_line_counts',
        compute_sudo=False,
        search='_search_line_done_count',
        help="Allowed companies that have marked this task done.",
    )
    line_todo_count = fields.Integer(
        compute='_compute_line_counts',
        compute_sudo=False,
        help="Allowed companies that still have this task to do.",
    )
    line_skipped_count = fields.Integer(
        compute='_compute_line_counts',
        compute_sudo=False,
        help="Allowed companies that explicitly skipped this task.",
    )
    completion_ratio = fields.Float(
        compute='_compute_line_counts',
        compute_sudo=False,
        search='_search_completion_ratio',
        help=(
            "Percent of companies that have marked this task done. "
            "Skipped companies are excluded from the denominator."
        ),
    )

    _key_unique = models.Constraint(
        'unique(key)',
        "Setup task keys must be unique.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
                _TASK_ENGINE_FIELDS.intersection(vals) for vals in vals_list):
            raise AccessError(_(
                "Setup task progress totals are server-owned."
            ))
        tasks = super().create(vals_list)
        tasks._eh_seed_company_lines()
        return tasks

    def _eh_seed_company_lines(self, companies=None):
        """Create every missing task/company pair without touching progress."""
        tasks = self.sudo()
        if not tasks:
            return self.env['eh.account.setup.task.line']
        companies = (
            companies.sudo()
            if companies is not None
            else self.env['res.company'].sudo().search([])
        )
        if not companies:
            return self.env['eh.account.setup.task.line']
        # Task definitions and companies can be created concurrently. One
        # short, transaction-scoped advisory lock prevents two seeders from
        # colliding with the unique(task, company) evidence constraint.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ('eh.account.setup.seed',),
        )
        Line = self.env['eh.account.setup.task.line'].sudo()
        existing = set(Line.search([
            ('task_id', 'in', tasks.ids),
            ('company_id', 'in', companies.ids),
        ]).mapped(lambda line: (line.task_id.id, line.company_id.id)))
        vals_list = [
            {'task_id': task.id, 'company_id': company.id, 'state': 'todo'}
            for task in tasks
            for company in companies
            if (task.id, company.id) not in existing
        ]
        return Line.create(vals_list) if vals_list else Line

    def write(self, vals):
        if not self.env.su and _TASK_ENGINE_FIELDS.intersection(vals):
            raise AccessError(_(
                "Setup task progress totals are server-owned."
            ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            if self.sudo().filtered('line_ids'):
                raise UserError(_(
                    "Setup task definitions with company progress cannot be "
                    "deleted. Archive or retain them so audit history "
                    "remains."
                ))
        return super().unlink()

    @api.depends('line_ids', 'line_ids.state')
    @api.depends_context('allowed_company_ids')
    def _compute_line_counts(self):
        counts = {
            task_id: {'done': 0, 'todo': 0, 'skipped': 0}
            for task_id in self.ids
        }
        lines = self.env['eh.account.setup.task.line'].search([
            ('task_id', 'in', self.ids),
            ('company_id', 'in', self.env.companies.ids),
        ])
        for line in lines:
            counts[line.task_id.id][line.state] += 1
        for task in self:
            task_counts = counts.get(task.id, {})
            done = task_counts.get('done', 0)
            todo = task_counts.get('todo', 0)
            skipped = task_counts.get('skipped', 0)
            task.line_count = done + todo + skipped
            task.line_done_count = done
            task.line_todo_count = todo
            task.line_skipped_count = skipped
            task.completion_ratio = (
                100.0 * done / (done + todo) if done + todo else 0.0
            )

    @api.model
    def _search_line_done_count(self, operator, value):
        comparators = {
            '=': py_operator.eq,
            '!=': py_operator.ne,
            '<': py_operator.lt,
            '<=': py_operator.le,
            '>': py_operator.gt,
            '>=': py_operator.ge,
        }
        compare = comparators.get(operator)
        try:
            threshold = int(value)
        except (TypeError, ValueError, OverflowError):
            threshold = None
        if compare is None or threshold is None:
            raise UserError(_(
                "Unsupported completed-company search: %(operator)s "
                "%(value)r",
                operator=operator,
                value=value,
            ))
        tasks = self.search([])
        return [('id', 'in', tasks.filtered(
            lambda task: compare(task.line_done_count, threshold)
        ).ids)]

    @api.model
    def _search_completion_ratio(self, operator, value):
        comparators = {
            '=': py_operator.eq,
            '!=': py_operator.ne,
            '<': py_operator.lt,
            '<=': py_operator.le,
            '>': py_operator.gt,
            '>=': py_operator.ge,
        }
        compare = comparators.get(operator)
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            threshold = None
        if compare is None or threshold is None:
            raise UserError(_(
                "Unsupported completion-ratio search: %(operator)s "
                "%(value)r",
                operator=operator,
                value=value,
            ))
        tasks = self.search([])
        return [
            ('id', 'in', tasks.filtered(
                lambda task: compare(task.completion_ratio, threshold)
            ).ids),
        ]

    def action_view_lines(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state=None)

    def action_view_lines_done(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='done')

    def action_view_lines_todo(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='todo')

    def action_view_lines_skipped(self):
        self.ensure_one()
        return self._action_view_lines_with_state(state='skipped')

    def _action_view_lines_with_state(self, state):
        domain = [('task_id', '=', self.id)]
        if state:
            domain.append(('state', '=', state))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Per company progress: %(task)s", task=self.name),
            'res_model': 'eh.account.setup.task.line',
            'view_mode': 'kanban,list,form',
            'domain': domain,
            'context': {'search_default_group_company': 1},
        }
