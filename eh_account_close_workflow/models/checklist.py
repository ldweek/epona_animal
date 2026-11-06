# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.checklist: reusable close checklist template.

A checklist defines a sequence of task templates. Each instantiation
(eh.close.run) copies the checklist's task templates into per run task
records. The template stays untouched so subsequent closes start from the
same baseline.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_CODE_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')
_CHECKLIST_LOCK_NAMESPACE = 1162036326


class EhCloseChecklist(models.Model):
    _name = 'eh.close.checklist'
    _description = "Period close checklist template"
    _order = 'sequence, name'

    name = fields.Char(
        required=True,
        help=(
            "Display name of the checklist template. Convention: "
            "'<Cadence> Close', e.g. 'Monthly Close', 'Quarterly Close'."
        ),
    )
    code = fields.Char(
        required=True, copy=False,
        help=(
            "Unique programmatic identifier per company. Must match "
            "[a-zA-Z][a-zA-Z0-9_]*. Used for cross-record references "
            "(reports, automations) so a renamed name does not break "
            "external links."
        ),
    )
    description = fields.Text(
        help=(
            "Long-form description of when and how this checklist is "
            "used. Visible to preparers when they pick a checklist."
        ),
    )
    sequence = fields.Integer(
        default=10,
        help="Sort order in the checklist picker. Lower values render first.",
    )
    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive checklists do not appear in "
            "the run-creation picker but existing runs that reference "
            "them stay readable."
        ),
    )
    ledger_lock_policy = fields.Selection(
        [
            ('fiscalyear', "Advance Global Lock Date"),
            ('signoff_only', "Sign-off Only (No Ledger Lock)"),
        ],
        required=True, default='fiscalyear',
        help="Controls approval effect. Global Lock advances the company's "
             "fiscal-year/global lock through the run end date. Sign-off Only "
             "records governance evidence but leaves ledger lock dates alone.",
    )

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        index=True,
        help=(
            "Optional company scope. When set, the checklist is only "
            "selectable for that company; when blank, it is shared "
            "across all companies."
        ),
    )

    task_template_ids = fields.One2many(
        'eh.close.task.template',
        'checklist_id',
        copy=True,
        help=(
            "Ordered list of task templates instantiated on each new "
            "run. Editing a template here does not back-port to "
            "existing runs."
        ),
    )
    task_template_count = fields.Integer(
        compute='_compute_task_template_count',
        help="Number of task templates currently defined on this checklist.",
    )

    run_ids = fields.One2many(
        'eh.close.run', 'checklist_id', readonly=True,
        help="Past and current close runs created from this checklist.",
    )
    run_count = fields.Integer(
        compute='_compute_run_count',
        help="Total run count across the checklist's lifetime.",
    )

    _unique_code = models.Constraint(
        'unique(code, company_id)',
        'Checklist code must be unique per company.',
    )

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Checklist code must match [a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('code', 'company_id')
    def _check_unique_code_scope(self):
        """Cover shared checklists, where SQL UNIQUE treats NULL as distinct."""
        for rec in self:
            duplicate = self.sudo().with_context(active_test=False).search([
                ('id', '!=', rec.id),
                ('code', '=', rec.code),
                ('company_id', '=', rec.company_id.id or False),
            ], limit=1)
            if duplicate:
                raise ValidationError(_(
                    "Checklist code must be unique within its company scope."))

    def init(self):
        """Close PostgreSQL's NULL gap for shared checklist codes.

        The regular ``unique(code, company_id)`` constraint already protects
        company-owned rows, including archived rows. A partial unique index
        provides the equivalent invariant when company_id is NULL.
        """
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                eh_close_checklist_shared_code_uniq
            ON eh_close_checklist (code)
            WHERE company_id IS NULL
            """
        )

    @api.constrains('company_id')
    def _check_existing_run_companies(self):
        for rec in self:
            if rec.company_id and rec.sudo().run_ids.filtered(
                lambda run: run.company_id != rec.company_id
            ):
                raise ValidationError(_(
                    "Checklist company cannot exclude companies already "
                    "governed by its close runs."))

    @api.model
    def _eh_lock_code_scopes(self, pairs):
        normalized = {
            (int(company_id or 0), code or '')
            for company_id, code in pairs
        }
        for company_id, code in sorted(normalized):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (
                    _CHECKLIST_LOCK_NAMESPACE,
                    f"{company_id or 0}:{code or ''}",
                ),
            )

    @api.model
    def _eh_assert_code_scopes_available(self, values, exclude_ids=()):
        """Fail cleanly before PostgreSQL reports the durable unique index."""
        seen = set()
        Checklist = self.sudo().with_context(active_test=False)
        for vals in values:
            company_id = vals.get('company_id', self.env.company.id) or False
            key = (company_id, vals.get('code') or '')
            if key in seen:
                raise ValidationError(_(
                    "Checklist code must be unique within its company scope."))
            seen.add(key)
            domain = [
                ('code', '=', key[1]),
                ('company_id', '=', company_id),
            ]
            if exclude_ids:
                domain.append(('id', 'not in', list(exclude_ids)))
            if Checklist.search(domain, limit=1):
                raise ValidationError(_(
                    "Checklist code must be unique within its company scope."))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            self._eh_check_access('create')
        self._eh_lock_code_scopes(
            (
                vals.get('company_id', self.env.company.id),
                vals.get('code'),
            )
            for vals in vals_list
        )
        self._eh_assert_code_scopes_available(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        if {'code', 'company_id'} & set(vals):
            pairs = {
                (rec.company_id.id or 0, rec.code) for rec in self
            }
            pairs.update({
                (
                    vals.get('company_id', rec.company_id.id or 0),
                    vals.get('code', rec.code),
                )
                for rec in self
            })
            self._eh_lock_code_scopes(pairs)
            candidate_values = [{
                'company_id': vals.get(
                    'company_id', rec.company_id.id or False,
                ),
                'code': vals.get('code', rec.code),
            } for rec in self]
            self._eh_assert_code_scopes_available(
                candidate_values, exclude_ids=self.ids,
            )
        return super().write(vals)

    @api.depends('task_template_ids')
    def _compute_task_template_count(self):
        for rec in self:
            rec.task_template_count = len(rec.task_template_ids)

    @api.depends('run_ids')
    def _compute_run_count(self):
        for rec in self:
            rec.run_count = len(rec.run_ids)

    def action_create_run(self, name=None, period_from=None, period_to=None,
                          company_id=None):
        """Create a new run from this checklist with copied task instances.

        Returns the created run record.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        if not self.task_template_ids:
            raise UserError(_(
                "Checklist '%s' has no task templates; cannot start a run.",
            ) % self.name)
        Run = self.env['eh.close.run']
        company = (
            self.env['res.company'].browse(company_id).exists()
            if company_id else (self.company_id or self.env.company)
        )
        if len(company) != 1:
            raise UserError(_("Select one valid company for the close run."))
        if not self.env.su:
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "You cannot create a close run for a company outside "
                    "your active company scope."))
        if self.company_id and self.company_id != company:
            raise ValidationError(_(
                "Checklist '%(checklist)s' belongs to %(check_company)s and "
                "cannot create a run for %(run_company)s.",
                checklist=self.display_name,
                check_company=self.company_id.display_name,
                run_company=company.display_name,
            ))
        run_vals = {
            'name': name or _("Close from %s") % self.name,
            'checklist_id': self.id,
            'company_id': company.id,
            'period_from': period_from,
            'period_to': period_to,
            'ledger_lock_policy': self.ledger_lock_policy,
            'task_ids': [
                (0, 0, {
                    'sequence': tpl.sequence,
                    'name': tpl.name,
                    'description': tpl.description,
                    'responsible_role': tpl.responsible_role,
                    'is_required': tpl.is_required,
                })
                for tpl in self.task_template_ids.sorted('sequence')
            ],
        }
        return Run.create(run_vals)

    def action_create_run_ui(self):
        """Open a seeded run form without creating an abandoned shell."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        if not self.task_template_ids:
            raise UserError(_(
                "Checklist '%s' has no task templates; cannot start a run.",
            ) % self.name)
        company = self.company_id or self.env.company
        return {
            'type': 'ir.actions.act_window',
            'name': _("New Close Run"),
            'res_model': 'eh.close.run',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
            'context': {
                'default_checklist_id': self.id,
                'default_company_id': company.id,
                'default_name': _("Close from %s") % self.name,
            },
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Runs"),
            'res_model': 'eh.close.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('checklist_id', '=', self.id)],
            'context': {'default_checklist_id': self.id},
        }
