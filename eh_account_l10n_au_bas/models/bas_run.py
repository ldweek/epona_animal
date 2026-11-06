# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.bas.run -- one BAS computation per (company, period).

Lifecycle: draft -> computed -> lodged. A user creates a run for a
quarter, runs Compute, reviews the figures, then marks Lodged when the
agent has lodged the form with the ATO. Lodged runs are read-only:
re-running compute on a lodged run raises an explicit error rather
than silently overwriting figures the ATO already received.

Audit: every compute appends a row to `eh.account.report.execution`
through the same audit pipeline as the standard reports, so an
auditor can trace which run produced which figure on which date.
"""

import logging
import re
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

from .bas_label import _AGGREGATION_CHOICES


_logger = logging.getLogger(__name__)


_QUARTERS = [
    ('q1', "Q1 (Jul-Sep)"),
    ('q2', "Q2 (Oct-Dec)"),
    ('q3', "Q3 (Jan-Mar)"),
    ('q4', "Q4 (Apr-Jun)"),
]

BAS_ENGINE_CTX = 'eh_bas_engine'
BAS_MIGRATION_CTX = 'eh_bas_migration'
BAS_AMENDMENT_CTX = 'eh_bas_amendment_capability'
_BAS_AMENDMENT_CAPABILITY = object()

_BAS_INPUT_FIELDS = (
    'company_id', 'fy_label', 'quarter', 'reporting_basis',
    'is_simpler_bas',
)

_BAS_LODGED_EVIDENCE_FIELDS = _BAS_INPUT_FIELDS + (
    'date_from', 'date_to', 'line_ids', 'notes',
)


class EhBasRun(models.Model):
    _name = 'eh.bas.run'
    _description = "BAS run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'fy_label desc, quarter desc'

    # State and the lodgement stamps may only change through this model's own
    # actions (which run under sudo), never a direct RPC/ORM write. Without
    # this a plain user could write({'state': 'lodged'}) to skip action_compute
    # and its audit trail, or repoint lodged_at/lodged_by. See
    # eh.workflow.guard.
    _eh_guarded_fields = (
        'state', 'lodged_at', 'lodged_by', 'name', 'date_from', 'date_to',
        'amendment_no', 'amends_run_id', 'tax_lock_date_snapshot',
    )

    name = fields.Char(
        compute='_compute_name', store=True,
        help="Display label '<company> <FY> <quarter>'.",
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
        help="Reporting entity. One BAS run per company per quarter.",
    )
    fy_label = fields.Char(
        required=True, default=lambda self: self._default_fy_label(),
        help=(
            "Australian financial year label in YYYY-YY form (the "
            "Australian FY runs 1 July to 30 June). Example: '2025-26' "
            "covers 1 Jul 2025 to 30 Jun 2026."
        ),
    )
    quarter = fields.Selection(
        _QUARTERS, required=True, default=lambda self: self._default_quarter(),
        tracking=True,
        help=(
            "BAS quarter within the financial year. Q1 covers Jul-Sep, "
            "Q2 Oct-Dec, Q3 Jan-Mar, Q4 Apr-Jun."
        ),
    )
    amendment_no = fields.Integer(
        string="Amendment", default=0, readonly=True, copy=False, index=True,
        help="Zero for original return; positive sequence for amendments.",
    )
    amends_run_id = fields.Many2one(
        'eh.bas.run', string="Amends BAS", readonly=True, copy=False,
        ondelete='restrict', index=True,
    )
    date_from = fields.Date(
        compute='_compute_period_dates', store=True,
        help="Inclusive start date derived from fy_label + quarter.",
    )
    date_to = fields.Date(
        compute='_compute_period_dates', store=True,
        help="Inclusive end date derived from fy_label + quarter.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('computed', "Computed"),
        ('lodged', "Lodged"),
    ], default='draft', tracking=True, required=True,
        help=(
            "draft: just instantiated, no figures yet. computed: the "
            "Compute action populated label totals. lodged: the agent "
            "has filed the BAS with the ATO; the run is read-only "
            "from this point so re-running compute cannot overwrite "
            "lodged figures."
        ),
    )
    lodged_at = fields.Datetime(
        readonly=True, copy=False,
        help="Timestamp recorded when the run was marked lodged.",
    )
    lodged_by = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help="User who marked the run lodged.",
    )
    tax_lock_date_snapshot = fields.Date(
        string="Tax Lock Date at Lodgement", readonly=True, copy=False,
        help="Company tax lock date proven and recorded at lodgement.",
    )
    line_ids = fields.One2many(
        'eh.bas.run.line', 'run_id', copy=False,
        help=(
            "One immutable snapshot line for each active row in the "
            "33-label G, GST, PAYG-W, PAYG-I, FBT, fuel-credit, WET and "
            "LCT worksheet. Populated by the Compute action."
        ),
    )
    line_count = fields.Integer(
        compute='_compute_line_count',
        help="Number of populated label rows on this run.",
    )
    notes = fields.Text(
        help=(
            "Free-form notes for the agent or auditor: assumptions, "
            "manual adjustments, reconciliation steps."
        ),
    )

    reporting_basis = fields.Selection(
        [
            ('accruals', "Accruals"),
            ('cash', "Cash"),
        ],
        default='accruals',
        required=True,
        help=(
            "ATO reporting basis. Accruals (the default) recognises "
            "GST on the date the invoice is posted. Cash recognises "
            "GST on the date the invoice is paid, apportioned by the "
            "paid fraction of each invoice, using the payment date for "
            "period membership; small businesses with turnover under "
            "the cash-basis threshold can elect this to defer GST until "
            "receipt."
        ),
    )
    is_simpler_bas = fields.Boolean(
        string="Simpler BAS",
        default=False,
        help=(
            "Simpler BAS available for businesses with GST turnover "
            "under the ATO threshold (currently AUD 10 million). "
            "Reports only G1, 1A, 1B and PAYG-W; the additional G "
            "labels are still computed for internal review but not "
            "lodged."
        ),
    )

    _unique_period = models.Constraint(
        'unique(company_id, fy_label, quarter, amendment_no)',
        'This BAS original/amendment sequence already exists for the period.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        supplied_amendment = any(
            vals.get('amendment_no') or vals.get('amends_run_id')
            for vals in vals_list
        )
        if supplied_amendment and self.env.context.get(
            BAS_AMENDMENT_CTX
        ) is not _BAS_AMENDMENT_CAPABILITY:
            raise AccessError(_(
                "BAS amendment provenance is server-owned; use Create "
                "Amendment on the latest lodged return."
            ))
        company_ids = set()
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            company_ids.add(company_id)
            self._eh_assert_company_allowed(company_id)
            self._eh_lock_period_key(
                company_id, vals.get('fy_label') or self._default_fy_label(),
                vals.get('quarter') or self._default_quarter(),
            )
            if not vals.get('amendment_no') and self.search_count([
                ('company_id', '=', company_id),
                ('fy_label', '=', vals.get('fy_label') or self._default_fy_label()),
                ('quarter', '=', vals.get('quarter') or self._default_quarter()),
            ], limit=1):
                raise UserError(_(
                    "A BAS already exists for this period. Open the latest "
                    "lodged run and use Create Amendment."
                ))
        for company_id in company_ids:
            self._eh_assert_company_allowed(company_id)
        runs = super().create(vals_list)
        runs._eh_validate_company_closure()
        for run in runs.filtered('amends_run_id'):
            source = run.amends_run_id
            if (
                source.state != 'lodged'
                or source.company_id != run.company_id
                or source.fy_label != run.fy_label
                or source.quarter != run.quarter
                or run.amendment_no != source.amendment_no + 1
            ):
                raise ValidationError(_(
                    "A BAS amendment must follow the latest lodged return "
                    "for the exact same company and period."
                ))
        return runs

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A BAS run may only use a company enabled in the caller's "
                "current company context."
            ))

    def _eh_validate_company_closure(self):
        if not self.env.su:
            self._eh_check_access('write')
        for run in self:
            self._eh_assert_company_allowed(run.company_id.id)
            # Read the persisted child rows as the caller. A hidden child
            # attached through a guessed id must fail before any elevation.
            if not self.env.su:
                run.line_ids._eh_check_access('read')
        return self

    @api.model
    def _eh_lock_period_key(self, company_id, fy_label, quarter):
        key = "eh.bas.period:%s:%s:%s" % (company_id, fy_label, quarter)
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (key,),
        )

    def _eh_lock_for_transition(self, extra_company_ids=()):
        # Existing-run transitions serialize on exact run rows. New-period
        # phantoms serialize on _eh_lock_period_key; no res_company row lock.
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_bas_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return self

    def _eh_validate_compute_source_access(self):
        if self.env.su:
            return self
        AML = self.env['account.move.line']
        Partial = self.env['account.partial.reconcile']
        Tax = self.env['account.tax']
        AML._eh_check_access('read')
        Partial._eh_check_access('read')
        Tax._eh_check_access('read')
        self.env['account.tax.repartition.line']._eh_check_access('read')
        self.env['account.account']._eh_check_access('read')
        self.env['eh.bas.label']._eh_check_access('read')
        for run in self:
            if not run.date_from or not run.date_to:
                continue
            aml_domain = [
                ('company_id', '=', run.company_id.id),
                ('parent_state', '=', 'posted'),
                ('date', '>=', run.date_from),
                ('date', '<=', run.date_to),
            ]
            if AML.search_count(aml_domain) != AML.sudo().search_count(
                aml_domain
            ):
                raise AccessError(_(
                    "The BAS ledger basis contains rows the caller is not "
                    "authorised to read."
                ))
            tax_domain = [('company_id', '=', run.company_id.id)]
            if Tax.search_count(tax_domain) != Tax.sudo().search_count(
                tax_domain
            ):
                raise AccessError(_(
                    "The BAS tax configuration contains rows the caller is "
                    "not authorised to read."
                ))
            if run.reporting_basis != 'cash':
                continue
            # Cash BAS period membership follows settlement max_date, so its
            # source invoices may predate the reporting window. Prove access
            # to every qualifying reconciliation edge and every journal item
            # on each source move before elevated set-based SQL reads them.
            # Checking only AML.date in the BAS period would let a restricted
            # actor render hidden historical invoices paid this quarter.
            edge_domain = [
                ('max_date', '>=', run.date_from),
                ('max_date', '<=', run.date_to),
                '|',
                '&',
                ('debit_move_id.company_id', '=', run.company_id.id),
                ('debit_move_id.account_id.account_type', 'in', (
                    'asset_receivable', 'liability_payable',
                )),
                '&',
                ('credit_move_id.company_id', '=', run.company_id.id),
                ('credit_move_id.account_id.account_type', 'in', (
                    'asset_receivable', 'liability_payable',
                )),
            ]
            if Partial.search_count(edge_domain) != Partial.sudo().search_count(
                edge_domain
            ):
                raise AccessError(_(
                    "The BAS settlement basis contains reconciliation rows "
                    "the caller is not authorised to read."
                ))
            edges = Partial.search(edge_domain).sudo()
            terms = (edges.debit_move_id | edges.credit_move_id).filtered(
                lambda line: (
                    line.company_id == run.company_id
                    and line.account_id.account_type in (
                        'asset_receivable', 'liability_payable',
                    )
                )
            )
            move_ids = tuple(terms.move_id.ids)
            if move_ids:
                source_domain = [
                    ('company_id', '=', run.company_id.id),
                    ('move_id', 'in', move_ids),
                ]
                if AML.search_count(source_domain) != AML.sudo().search_count(
                    source_domain
                ):
                    raise AccessError(_(
                        "The BAS cash-basis source contains journal items "
                        "the caller is not authorised to read."
                    ))
        return self

    def _eh_prepare_transition(self, source_access=False):
        caller = self
        caller._eh_validate_company_closure()
        if source_access:
            caller._eh_validate_compute_source_access()
        caller._eh_lock_for_transition()
        caller._eh_validate_company_closure()
        if source_access:
            caller._eh_validate_compute_source_access()
        return caller._eh_workflow_action().with_context(
            **{BAS_ENGINE_CTX: True})

    def write(self, vals):
        if {'amendment_no', 'amends_run_id'}.intersection(vals) and not (
            self.env.su
            and self.env.context.get(BAS_AMENDMENT_CTX)
            is _BAS_AMENDMENT_CAPABILITY
        ):
            raise AccessError(_("BAS amendment provenance is immutable."))
        for run in self:
            self._eh_assert_company_allowed(
                vals.get('company_id', run.company_id.id))
        changed = set(vals)
        # Direct form/RPC edits must take the same company -> run lock as
        # compute/lodge/reset.  Otherwise a manual worksheet or notes update
        # can pass the pre-lodgement state check while action_mark_lodged is
        # concurrently freezing the evidence.
        engine = self.env.su and self.env.context.get(BAS_ENGINE_CTX)
        migration = self.env.su and self.env.context.get(BAS_MIGRATION_CTX)
        if changed.intersection(_BAS_LODGED_EVIDENCE_FIELDS) and not (
            engine or migration
        ):
            self._eh_validate_company_closure()
            for run in self:
                self._eh_lock_period_key(
                    vals.get('company_id', run.company_id.id),
                    vals.get('fy_label', run.fy_label),
                    vals.get('quarter', run.quarter),
                )
            target_company_ids = {
                vals['company_id']
                for run in self
                if vals.get('company_id')
                and vals['company_id'] != run.company_id.id
            }
            self._eh_lock_for_transition(target_company_ids)
            self.invalidate_recordset()
            self._eh_validate_company_closure()
        settled = self.filtered(lambda run: run.state in ('computed', 'lodged'))
        if settled and changed.intersection(_BAS_INPUT_FIELDS) and not (
            self.env.su and self.env.context.get(BAS_ENGINE_CTX)
        ):
            raise UserError(_(
                "A computed BAS basis is frozen. Reset it to draft before "
                "changing the company, period, or reporting options."
            ))
        lodged = self.filtered(lambda run: run.state == 'lodged')
        if lodged and changed.intersection(_BAS_LODGED_EVIDENCE_FIELDS):
            raise UserError(_(
                "A lodged BAS is immutable evidence. Create an amendment "
                "run instead of changing the lodged worksheet."
            ))
        result = super().write(vals)
        if changed.intersection(_BAS_INPUT_FIELDS):
            self._eh_validate_company_closure()
        return result

    def unlink(self):
        self._eh_validate_company_closure()
        self._eh_lock_for_transition()
        self._eh_validate_company_closure()
        if self.filtered(lambda run: run.state == 'lodged'):
            raise UserError(_(
                "A lodged BAS is retained as immutable evidence and cannot "
                "be deleted."
            ))
        return super().unlink()

    @api.model
    def _default_fy_label(self):
        today = fields.Date.context_today(self)
        # Australian FY runs Jul to Jun; the FY label is YYYY-YY.
        if today.month >= 7:
            return "%d-%02d" % (today.year, (today.year + 1) % 100)
        return "%d-%02d" % (today.year - 1, today.year % 100)

    @api.model
    def _default_quarter(self):
        month = fields.Date.context_today(self).month
        if month >= 7 and month <= 9:
            return 'q1'
        if month >= 10:
            return 'q2'
        if month <= 3:
            return 'q3'
        return 'q4'

    @api.depends('fy_label', 'quarter')
    def _compute_period_dates(self):
        # Map the quarter to month ranges; fy_label format is YYYY-YY.
        for rec in self:
            try:
                start_year = int((rec.fy_label or '').split('-')[0])
            except (ValueError, IndexError):
                rec.date_from = False
                rec.date_to = False
                continue
            mapping = {
                'q1': (date(start_year, 7, 1), date(start_year, 9, 30)),
                'q2': (date(start_year, 10, 1), date(start_year, 12, 31)),
                'q3': (date(start_year + 1, 1, 1), date(start_year + 1, 3, 31)),
                'q4': (date(start_year + 1, 4, 1), date(start_year + 1, 6, 30)),
            }
            df, dt = mapping.get(rec.quarter, (False, False))
            rec.date_from = df
            rec.date_to = dt

    @api.constrains('fy_label')
    def _check_fy_label_format(self):
        # fy_label drives the period dates; a malformed value leaves
        # date_from/date_to unset and would crash the run. Enforce the
        # YYYY-YY shape (e.g. 2025-26) at write time.
        for rec in self:
            if not re.match(r'^\d{4}-\d{2}$', rec.fy_label or ''):
                raise ValidationError(_(
                    "Financial-year label must be in YYYY-YY form "
                    "(for example 2025-26); got %r.",
                ) % rec.fy_label)
            start_year, suffix = rec.fy_label.split('-')
            expected = (int(start_year) + 1) % 100
            if int(suffix) != expected:
                raise ValidationError(_(
                    "Financial-year suffix must follow the starting year; "
                    "%(label)s must end in %(suffix)02d.",
                    label=rec.fy_label,
                    suffix=expected,
                ))

    @api.depends('company_id', 'fy_label', 'quarter', 'amendment_no')
    def _compute_name(self):
        for rec in self:
            rec.name = "BAS %s %s [%s]%s" % (
                rec.fy_label or '',
                dict(_QUARTERS).get(rec.quarter, ''),
                rec.company_id.name or '',
                " amendment %s" % rec.amendment_no
                if rec.amendment_no else '',
            )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    # ---- actions ----

    def action_compute(self):
        self = self._eh_prepare_transition(source_access=True)
        for rec in self:
            if rec.state == 'lodged':
                raise UserError(_(
                    "BAS %s is lodged; recompute is refused. Create a "
                    "new run if you need to amend.",
                ) % rec.display_name)
            rec._compute_lines()
            rec.state = 'computed'
            rec.message_post(body=_(
                "BAS computed for %(start)s to %(end)s; %(count)s "
                "labels populated.",
                start=rec.date_from, end=rec.date_to,
                count=len(rec.line_ids),
            ))
        return True

    def action_mark_lodged(self):
        if (
            not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager')
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager may attest that a BAS was "
                "lodged."
            ))
        self = self._eh_prepare_transition(source_access=True)
        for rec in self:
            if rec.state != 'computed':
                raise UserError(_(
                    "BAS run must be computed before it is lodged "
                    "(state is %s).",
                ) % rec.state)
            rec._eh_validate_lodgement_evidence()
            rec._eh_freeze_lodged_tax_period()
            rec.write({
                'state': 'lodged',
                'lodged_at': fields.Datetime.now(),
                'lodged_by': self.env.user.id,
                'tax_lock_date_snapshot': rec.company_id.tax_lock_date,
            })
            rec.message_post(body=_(
                "BAS marked lodged by %s.",
            ) % self.env.user.display_name)
        return True

    def action_create_amendment(self):
        self.ensure_one()
        if (
            not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'
            )
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager may create a BAS amendment."
            ))
        self._eh_validate_company_closure()
        self._eh_lock_period_key(self.company_id.id, self.fy_label, self.quarter)
        latest = self.search([
            ('company_id', '=', self.company_id.id),
            ('fy_label', '=', self.fy_label),
            ('quarter', '=', self.quarter),
        ], order='amendment_no desc, id desc', limit=1)
        if self != latest or self.state != 'lodged':
            raise UserError(_(
                "Only the latest lodged BAS in the period can start the next "
                "amendment."
            ))
        amendment = self.with_context(
            **{BAS_AMENDMENT_CTX: _BAS_AMENDMENT_CAPABILITY}
        ).create({
            'company_id': self.company_id.id,
            'fy_label': self.fy_label,
            'quarter': self.quarter,
            'reporting_basis': self.reporting_basis,
            'is_simpler_bas': self.is_simpler_bas,
            'amendment_no': self.amendment_no + 1,
            'amends_run_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("BAS Amendment"),
            'res_model': 'eh.bas.run',
            'view_mode': 'form',
            'res_id': amendment.id,
            'target': 'current',
        }

    def action_reset_draft(self):
        self = self._eh_prepare_transition()
        for rec in self:
            if rec.state == 'lodged':
                raise UserError(_(
                    "Lodged BAS runs cannot be reset; create a new run "
                    "for amendment instead."
                ))
            # Keep reviewer-entered manual amounts and all worksheet notes.
            # Recompute replaces automatic amounts and snapshots atomically.
            rec.state = 'draft'
        return True

    def _eh_freeze_lodged_tax_period(self):
        """Reject pending tax evidence and advance company tax lock."""
        Move = self.env['account.move']
        for run in self:
            domain = [
                ('company_id', '=', run.company_id.id),
                ('state', '=', 'draft'),
                ('date', '>=', run.date_from),
                ('date', '<=', run.date_to),
                '|', '|', ('line_ids.tax_line_id', '!=', False),
                ('line_ids.tax_ids', '!=', False),
                ('line_ids.tax_tag_ids', '!=', False),
            ]
            visible = Move.search_count(domain)
            total = Move.sudo().search_count(domain)
            if visible != total:
                raise AccessError(_(
                    "Draft tax entries exist outside caller visibility; BAS "
                    "lodgement is blocked."
                ))
            if total:
                raise UserError(_(
                    "BAS cannot be lodged while %(count)s draft tax entries "
                    "remain in %(start)s to %(end)s.",
                    count=total, start=run.date_from, end=run.date_to,
                ))
            current = run.company_id.tax_lock_date
            if not current or current < run.date_to:
                run.company_id.sudo().write({'tax_lock_date': run.date_to})
            run.company_id.invalidate_recordset(['tax_lock_date'])
            if run.company_id.tax_lock_date < run.date_to:
                raise UserError(_(
                    "Company tax lock did not reach BAS period end; "
                    "lodgement is blocked."
                ))
        return True

    def _eh_validate_lodgement_evidence(self):
        for run in self:
            if not run.line_ids:
                raise UserError(_(
                    "A BAS cannot be marked lodged without worksheet lines."
                ))
            label_ids = run.line_ids.mapped('label_id').ids
            if len(label_ids) != len(set(label_ids)):
                raise UserError(_(
                    "A BAS cannot be lodged with duplicate worksheet labels."
                ))
            wrong_currency = run.line_ids.filtered(
                lambda line: line.currency_id != run.company_id.currency_id
            )
            if wrong_currency:
                raise UserError(_(
                    "A BAS worksheet line has a currency different from "
                    "the run company's immutable currency snapshot."
                ))
            missing_snapshot = run.line_ids.filtered(
                lambda line: not line.label_code
                or not line.label_name
                or not line.label_aggregation
            )
            if missing_snapshot:
                raise UserError(_(
                    "A BAS cannot be lodged because one or more worksheet "
                    "rows lack immutable label-definition evidence. Recompute "
                    "the run first."
                ))
        return self

    def action_open_manual_inputs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("BAS manual inputs"),
            'res_model': 'eh.bas.run.line',
            'view_mode': 'list,form',
            'views': [
                (self.env.ref(
                    'eh_account_l10n_au_bas.view_eh_bas_run_line_manual_list'
                ).id, 'list'),
                (self.env.ref(
                    'eh_account_l10n_au_bas.view_eh_bas_run_line_manual_form'
                ).id, 'form'),
            ],
            'domain': [
                ('run_id', '=', self.id),
                ('label_aggregation', '=', 'manual'),
            ],
            'context': {
                'default_run_id': self.id,
                'search_default_run_id': self.id,
            },
            'target': 'current',
        }

    def action_reconcile_gst_control(self):
        """Open the GST control account reconciliation view.

        Compares the 1A and 1B label totals against the period balance
        movement on each company's GST collected and GST paid control
        accounts. A non-zero difference signals either a missing tag
        on a journal item or a manual entry on the control account
        that bypassed the tax engine. Common bookkeeping error;
        catching it pre-lodge avoids an ATO query.
        """
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_(
                "Compute the BAS first; reconciliation needs label "
                "totals to compare against.",
            ))
        report = self.compute_gst_control_reconciliation()
        # Render result as a transient report record the user can inspect
        # through a transient form.
        Recon = self.env['eh.bas.gst.recon.result']
        result = Recon.create({
            'run_id': self.id,
            'label_1a': report['label_1a'],
            'label_1b': report['label_1b'],
            'gst_collected_movement': report['gst_collected_movement'],
            'gst_paid_movement': report['gst_paid_movement'],
            'collected_diff': report['collected_diff'],
            'paid_diff': report['paid_diff'],
            'unclassified_movement': report['unclassified_movement'],
            'detail_lines': report['detail_text'],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("GST control reconciliation"),
            'res_model': 'eh.bas.gst.recon.result',
            'res_id': result.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def compute_gst_control_reconciliation(self):
        """Compute the 1A/1B vs GL control account variance.

        Walks the company's tax control accounts (the accounts marked
        as the GST clearing accounts on account.tax records) and
        sums the period balance movement. Compares to the BAS line
        for 1A and 1B. Returns a dict suitable for either the wizard
        view or programmatic callers.
        """
        self.ensure_one()
        # This public method elevates tax/account-move-line reads below.
        # Establish record-rule access and allowed-company scope first.
        self._eh_check_access('read')
        if self.company_id not in self.env.companies:
            raise AccessError(_(
                "You cannot reconcile a BAS run outside your companies.",
            ))
        self._eh_validate_compute_source_access()
        Line = self.env['eh.bas.run.line']
        line_1a = Line.search([
            ('run_id', '=', self.id),
            ('label_id.code', '=', '1A'),
        ], limit=1)
        line_1b = Line.search([
            ('run_id', '=', self.id),
            ('label_id.code', '=', '1B'),
        ], limit=1)
        # Resolve every account that appears as a tax repartition
        # destination for taxes on this company. Sales and purchases commonly
        # share one GST control account, so its net balance cannot be reused
        # for 1A and 1B: doing so subtracts 1B from 1A. Attribute the posted
        # tax journal items by their authoritative AU tax grids instead.
        Tax = self.env['account.tax'].sudo()
        taxes = Tax.search([('company_id', '=', self.company_id.id)])
        repartition = taxes.mapped('invoice_repartition_line_ids') \
            + taxes.mapped('refund_repartition_line_ids')
        tax_accounts = repartition.filtered(
            lambda r: r.repartition_type == 'tax',
        ).mapped('account_id')
        collected_movement = 0.0
        paid_movement = 0.0
        unclassified_movement = 0.0
        if tax_accounts:
            AML = self.env['account.move.line'].sudo()
            base_domain = [
                ('account_id', 'in', tax_accounts.ids),
                ('parent_state', '=', 'posted'),
                ('company_id', '=', self.company_id.id),
                ('date', '>=', self.date_from),
                ('date', '<=', self.date_to),
            ]

            def ledger_sides(extra_domain=()):
                rows = read_group_compat(
                    AML, base_domain + list(extra_domain), [],
                    ['credit:sum', 'debit:sum'],
                )
                if not rows:
                    return 0.0, 0.0
                return float(rows[0][0] or 0.0), float(rows[0][1] or 0.0)

            grid_tags = self._bas_grid_tags()
            sale_tags = (
                grid_tags['G1'] | grid_tags['G7']
            )
            purchase_tags = (
                grid_tags['G10'] | grid_tags['G11']
                | grid_tags['G18'] | grid_tags['ONLY']
            )
            purchase_exclusions = (
                grid_tags['G13'] | grid_tags['G14'] | grid_tags['G15']
            )
            sale_credit, sale_debit = ledger_sides([
                ('tax_line_id', '!=', False),
                ('tax_line_id.type_tax_use', '=', 'sale'),
                ('tax_tag_ids', 'in', sale_tags.ids),
            ])
            purchase_credit, purchase_debit = ledger_sides([
                ('tax_line_id', '!=', False),
                ('tax_line_id.type_tax_use', '=', 'purchase'),
                ('tax_tag_ids', 'in', purchase_tags.ids),
                ('tax_tag_ids', 'not in', purchase_exclusions.ids),
            ])
            total_credit, total_debit = ledger_sides()
            collected_movement = round(sale_credit - sale_debit, 2)
            paid_movement = round(purchase_debit - purchase_credit, 2)
            classified_balance = (
                (sale_debit - sale_credit)
                + (purchase_debit - purchase_credit)
            )
            unclassified_movement = round(
                (total_debit - total_credit) - classified_balance, 2,
            )
        amount_1a = float(line_1a.amount or 0.0)
        amount_1b = float(line_1b.amount or 0.0)
        collected_diff = round(collected_movement - amount_1a, 2)
        paid_diff = round(paid_movement - amount_1b, 2)
        detail_lines = [
            "Tax control accounts considered: %d" % len(tax_accounts),
            "Unclassified movement (debit positive): %.2f"
            % unclassified_movement,
        ]
        if tax_accounts:
            detail_lines.append("Accounts:")
            for acc in tax_accounts:
                detail_lines.append(
                    "  %s %s" % (acc.code or '', acc.name or ''),
                )
        return {
            'label_1a': amount_1a,
            'label_1b': amount_1b,
            'gst_collected_movement': collected_movement,
            'gst_paid_movement': paid_movement,
            'collected_diff': collected_diff,
            'paid_diff': paid_diff,
            'unclassified_movement': unclassified_movement,
            'detail_text': "\n".join(detail_lines),
        }

    # ---- compute ----

    def _compute_lines(self):
        self.ensure_one()
        if not (self.date_from and self.date_to):
            raise UserError(_(
                "This BAS run has no reporting period. Set a valid "
                "financial-year label (YYYY-YY) and quarter before "
                "computing the lines.",
            ))
        # Serialize definition edits with a transaction-scoped advisory lock.
        # Label create/write/unlink takes the same key below.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ('eh_account_l10n_au_bas:labels',),
        )
        executor = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'au_bas')], limit=1,
        )
        # The audit trail is a soft dependency on dynamic_reports; if
        # the report record is not registered we still emit the lines
        # (the user has installed l10n_au_bas without dynamic_reports
        # for some reason). We log a chatter note so the absence is
        # visible.
        Execution = self.env['eh.account.report.execution']
        audit = False
        audit_options = {
            'date_from': self.date_from.isoformat(),
            'date_to': self.date_to.isoformat(),
            'company_ids': [self.company_id.id],
            'fy_label': self.fy_label,
            'quarter': self.quarter,
            'reporting_basis': self.reporting_basis,
            'is_simpler_bas': self.is_simpler_bas,
        }
        if executor:
            audit = Execution.start_execution(
                report_code='au_bas', name='BAS Run',
                options=audit_options,
                company_ids=[self.company_id.id],
                result_format='json',
            )
        try:
            with self.env.cr.savepoint():
                Label = self.env['eh.bas.label']
                labels = Label.search([('active', '=', True)])
                prior_by_label = {
                    line.label_id.id: {
                        'amount': line.amount,
                        'notes': line.notes,
                    }
                    for line in self.line_ids
                }
                # Rebuild definition snapshots atomically while carrying only
                # reviewer-owned manual amounts and notes.
                self.line_ids.unlink()
                new_vals = []
                amount_by_code = {}
                automatic_amounts = self._bas_automatic_amounts()
                for label in labels:
                    prior = prior_by_label.get(label.id, {})
                    if label.aggregation == 'manual':
                        amount = prior.get('amount', 0.0)
                    elif label.aggregation == 'payg_withheld_total':
                        amount = sum(
                            amount_by_code.get(code, 0.0)
                            for code in ('W2', 'W3', 'W4')
                        )
                    else:
                        amount = self._aggregate(label, automatic_amounts)
                    amount_by_code[label.code] = amount
                    new_vals.append({
                        'run_id': self.id,
                        'label_id': label.id,
                        'label_code': label.code,
                        'label_name': label.name,
                        'label_sequence': label.sequence,
                        'label_aggregation': label.aggregation,
                        'amount': amount,
                        'currency_id': self.company_id.currency_id.id,
                        'is_lodgeable': (
                            not self.is_simpler_bas
                            or label.code in ('G1', '1A', '1B')
                            or label.code.startswith('W')
                        ),
                        'notes': prior.get('notes') or False,
                    })
                self.env['eh.bas.run.line'].create(new_vals)
            if audit:
                audit.complete_execution(
                    row_count=len(new_vals),
                )
        except Exception as exc:
            # Preserve original compute error even when best-effort audit
            # persistence fails. Independent evidence runs only after the
            # savepoint restored a usable request cursor.
            try:
                Execution.record_failure_durable(
                    report_code='au_bas',
                    name='BAS Run',
                    options=audit_options,
                    company_ids=[self.company_id.id],
                    result_format='json',
                    error_message=str(exc),
                    definition_id=executor.id if executor else None,
                )
                if audit and audit.state == 'running':
                    audit.fail_execution(str(exc))
            except Exception:  # noqa: BLE001 - preserve original error
                _logger.exception(
                    "Could not persist BAS failure audit for run %s",
                    self.id,
                )
            raise

    def _aggregate(self, label, automatic_amounts=None):
        if label.aggregation == 'manual':
            return 0.0
        # Tax-type driven modes resolve the relevant account.tax records at
        # compute time from their type_tax_use, so a fresh install produces
        # non-zero figures for any company that carries standard GST taxes,
        # with no pre-seeded account tags. The tag-driven modes below remain
        # available for bespoke labels that map to a specific chart tag.
        if label.aggregation in (
            'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
        ):
            amounts = automatic_amounts or self._bas_automatic_amounts()
            return amounts[label.aggregation]
        if not label.tag_ids:
            return 0.0
        sign_filter, balance_expr = self._aggregation_to_sql(label.aggregation)
        # Cross join with the m2m of tax tags via the upstream
        # account_account_tag_account_move_line_rel relation. The
        # MoveLineQuery composer does not yet expose tag joins so we
        # use where_raw with an EXISTS clause.
        query = MoveLineQuery(
            self.env, company_ids=[self.company_id.id],
        )
        query.where_date_range(
            date_from=self.date_from, date_to=self.date_to,
        )
        query.where_posted_only()
        query.where_raw(SQL(
            "EXISTS (SELECT 1 FROM account_account_tag_account_move_line_rel "
            "rel WHERE rel.account_move_line_id = aml.id AND "
            "rel.account_account_tag_id IN %s)",
            tuple(label.tag_ids.ids),
        ))
        if sign_filter is not None:
            query.where_raw(SQL(sign_filter))
        query.select(SQL(balance_expr), 'agg_amount')
        rows = query.execute()
        return float(rows[0]['agg_amount'] or 0.0) if rows else 0.0

    _GST_GRID_CODES = (
        'G1', 'G2', 'G3', 'G4', 'G7',
        'G10', 'G11', 'G13', 'G14', 'G15', 'G18', 'ONLY',
    )

    def _bas_grid_tags(self):
        """Return ``{grid code: signed tags}`` for Australian tax grids.

        ``type_tax_use`` only says whether a tax is offered on sales or
        purchases. It does not say whether its amount is GST, GST-free,
        input-taxed, WET, LCT, PAYG, private-use or an adjustment. Using that
        field for 1A/1B therefore puts unrelated sale/purchase taxes into GST.

        Odoo's AU localisation already encodes the ATO classification on tax
        repartition lines through G-grid tags. Use that authoritative contract
        and fail closed when an AU chart is incomplete.
        """
        self.ensure_one()
        country = (
            self.company_id.account_fiscal_country_id
            or self.company_id.country_id
        )
        if not country or country.code != 'AU':
            raise UserError(_(
                "Automatic G1/1A/1B calculation requires an Australian "
                "fiscal country and the Odoo Australian tax grids."
            ))
        Tag = self.env['account.account.tag'].sudo()
        tags = {}
        missing = []
        for code in self._GST_GRID_CODES:
            signed_tags = Tag._get_tax_tags(code, country.id)
            if signed_tags:
                # Tax-tag expressions create a pair: ``+CODE`` for invoices
                # and ``-CODE`` for refunds.  Standard AU tax repartition
                # lines use only the matching sign, so dropping either tag
                # silently drops one population from the BAS.
                tags[code] = signed_tags
            else:
                missing.append(code)
        if missing:
            raise UserError(_(
                "Australian BAS tax grids are incomplete for %(company)s. "
                "Missing: %(codes)s. Update/reload the l10n_au chart before "
                "computing the BAS.",
                company=self.company_id.display_name,
                codes=', '.join(missing),
            ))
        return tags

    def _bas_grid_totals(self, *, per_move=False):
        """Return posted tax-grid balances for the run company.

        Accrual mode restricts journal-item dates to the BAS period and returns
        ``{code: balance}``. Cash mode needs all invoice dates because period
        membership is the settlement date; with ``per_move=True`` it returns
        ``{move_id: {code: balance}}`` for later payment apportionment.
        """
        tags = self._bas_grid_tags()
        code_by_tag = {
            tag.id: code
            for code, signed_tags in tags.items()
            for tag in signed_tags
        }
        select_move = "aml.move_id," if per_move else ""
        group_move = "aml.move_id," if per_move else ""
        date_clause = SQL()
        exigible_clause = SQL()
        if not per_move:
            date_clause = SQL(
                "AND aml.date >= %s AND aml.date <= %s",
                self.date_from, self.date_to,
            )
            # Mirror account.move.line._get_tax_exigible_domain. Original
            # on-payment tax/base lines are not reportable; generated CABA
            # lines are. Without this gate accrual BAS counts both copies.
            exigible_clause = SQL(
                """
                AND (
                    move.always_tax_exigible
                    OR (
                        aml.tax_line_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1
                              FROM account_move_line_account_tax_rel tr0
                             WHERE tr0.account_move_line_id = aml.id
                        )
                    )
                    OR move.tax_cash_basis_rec_id IS NOT NULL
                    OR EXISTS (
                        SELECT 1 FROM account_tax tl
                         WHERE tl.id = aml.tax_line_id
                           AND tl.tax_exigibility != 'on_payment'
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM account_move_line_account_tax_rel tr1
                          JOIN account_tax t1 ON t1.id = tr1.account_tax_id
                         WHERE tr1.account_move_line_id = aml.id
                           AND t1.tax_exigibility != 'on_payment'
                    )
                )
                """
            )
        self.env.cr.execute(SQL(
            """
            SELECT %s rel.account_account_tag_id,
                   SUM(aml.balance)
              FROM account_move_line aml
              JOIN account_move move ON move.id = aml.move_id
              JOIN account_account_tag_account_move_line_rel rel
                ON rel.account_move_line_id = aml.id
             WHERE move.company_id = %s
               AND move.state = 'posted'
               AND rel.account_account_tag_id IN %s
               %s
               %s
             GROUP BY %s rel.account_account_tag_id
            """,
            SQL(select_move),
            self.company_id.id,
            tuple(code_by_tag),
            date_clause,
            exigible_clause,
            SQL(group_move),
        ))
        if per_move:
            totals = {}
            for move_id, tag_id, amount in self.env.cr.fetchall():
                code = code_by_tag[tag_id]
                move_totals = totals.setdefault(move_id, {})
                move_totals[code] = (
                    move_totals.get(code, 0.0) + (amount or 0.0)
                )
            return totals
        totals = {}
        for tag_id, amount in self.env.cr.fetchall():
            code = code_by_tag[tag_id]
            totals[code] = totals.get(code, 0.0) + (amount or 0.0)
        return totals

    def _bas_amount_from_grid_totals(self, mode, totals):
        """Evaluate Odoo l10n_au's canonical G1/G8/G19 calculations."""
        currency = self.company_id.currency_id

        def sale(code):
            # AU sale-grid formulas are negative because revenue is credit.
            return -(totals.get(code, 0.0) or 0.0)

        def purchase(code):
            return totals.get(code, 0.0) or 0.0

        g1 = sale('G1')
        if mode == 'total_sales_incl':
            return currency.round(g1)
        if mode == 'gst_on_sales':
            g8 = (
                g1 - sale('G2') - sale('G3') - sale('G4')
                + sale('G7')
            )
            return currency.round(g8 / 11.0)
        if mode == 'gst_on_purchases':
            g19 = (
                purchase('G10') + purchase('G11')
                - purchase('G13') - purchase('G14') - purchase('G15')
                + purchase('G18')
            )
            return currency.round(g19 / 11.0 + purchase('ONLY'))
        return 0.0

    def _aggregate_by_tax_type(self, mode):
        """Compatibility entry point backed by authoritative AU tax grids."""
        return self._bas_amount_from_grid_totals(
            mode, self._bas_grid_totals(),
        )

    def _bas_automatic_amounts(self):
        modes = (
            'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
        )
        if self.reporting_basis == 'cash':
            return self._bas_cash_amounts()
        totals = self._bas_grid_totals()
        amounts = {
            mode: self._bas_amount_from_grid_totals(mode, totals)
            for mode in modes
        }
        # Odoo's AU G-grid tags cover GST-classified sales. G1 also includes
        # genuinely untaxed sales. Add only posted customer revenue lines
        # with no tax relation, so zero-rated/GST-free lines already tagged
        # G2/G3 are not double counted.
        amounts['total_sales_incl'] = self.company_id.currency_id.round(
            amounts['total_sales_incl'] + self._bas_untaxed_sales()
        )
        return amounts

    def _aggregate_cash_by_tax_type(self, mode):
        """Compatibility entry point backed by one set-based cash pass."""
        return self._bas_cash_amounts()[mode]

    def _bas_cash_amounts(self):
        """Compute all cash labels in one bounded SQL pass.

        Partial reconciliations are date-pruned first. Only invoice/bill/credit
        moves settled in this BAS period reach tax-grid aggregation. This avoids
        loading full GST history and repeated O(moves x lines) ORM filtering for
        G1, 1A and 1B.
        """
        self.ensure_one()
        tags = self._bas_grid_tags()
        code_by_tag = {
            tag.id: code
            for code, signed_tags in tags.items()
            for tag in signed_tags
        }
        self.env.cr.execute(SQL(
            """
            WITH paid_edges AS (
                SELECT term.move_id, apr.amount
                  FROM account_partial_reconcile apr
                  JOIN account_move_line term ON term.id = apr.debit_move_id
                  JOIN account_account acc ON acc.id = term.account_id
                 WHERE apr.max_date >= %s
                   AND apr.max_date <= %s
                   AND term.company_id = %s
                   AND acc.account_type IN (
                       'asset_receivable', 'liability_payable'
                   )
                UNION ALL
                SELECT term.move_id, apr.amount
                  FROM account_partial_reconcile apr
                  JOIN account_move_line term ON term.id = apr.credit_move_id
                  JOIN account_account acc ON acc.id = term.account_id
                 WHERE apr.max_date >= %s
                   AND apr.max_date <= %s
                   AND term.company_id = %s
                   AND acc.account_type IN (
                       'asset_receivable', 'liability_payable'
                   )
            ),
            paid AS (
                SELECT move_id, SUM(amount) AS amount
                  FROM paid_edges
                 GROUP BY move_id
            ),
            terms AS (
                SELECT aml.move_id, SUM(ABS(aml.balance)) AS total
                  FROM account_move_line aml
                  JOIN account_account acc ON acc.id = aml.account_id
                  JOIN paid ON paid.move_id = aml.move_id
                 WHERE aml.company_id = %s
                   AND acc.account_type IN (
                       'asset_receivable', 'liability_payable'
                   )
                 GROUP BY aml.move_id
            ),
            grid_rows AS (
                SELECT aml.move_id,
                       rel.account_account_tag_id AS tag_id,
                       SUM(aml.balance) AS amount
                  FROM paid
                  JOIN account_move move ON move.id = paid.move_id
                  JOIN account_move_line aml ON aml.move_id = move.id
                  JOIN account_account_tag_account_move_line_rel rel
                    ON rel.account_move_line_id = aml.id
                 WHERE move.company_id = %s
                   AND move.state = 'posted'
                   AND rel.account_account_tag_id IN %s
                 GROUP BY aml.move_id, rel.account_account_tag_id
            ),
            untaxed_rows AS (
                SELECT aml.move_id, 0 AS tag_id,
                       -SUM(aml.balance) AS amount
                  FROM paid
                  JOIN account_move move ON move.id = paid.move_id
                  JOIN account_move_line aml ON aml.move_id = move.id
                  JOIN account_account account ON account.id = aml.account_id
                 WHERE move.company_id = %s
                   AND move.state = 'posted'
                   AND move.move_type IN ('out_invoice', 'out_refund')
                   AND account.account_type IN ('income', 'income_other')
                   AND aml.tax_line_id IS NULL
                   AND NOT EXISTS (
                        SELECT 1
                          FROM account_move_line_account_tax_rel tax_rel
                         WHERE tax_rel.account_move_line_id = aml.id
                   )
                 GROUP BY aml.move_id
            ),
            report_rows AS (
                SELECT * FROM grid_rows
                UNION ALL
                SELECT * FROM untaxed_rows
            )
            SELECT report_rows.move_id,
                   report_rows.tag_id,
                   report_rows.amount,
                   paid.amount,
                   terms.total
              FROM report_rows
              JOIN paid ON paid.move_id = report_rows.move_id
              JOIN terms ON terms.move_id = report_rows.move_id
            """,
            self.date_from,
            self.date_to,
            self.company_id.id,
            self.date_from,
            self.date_to,
            self.company_id.id,
            self.company_id.id,
            self.company_id.id,
            tuple(code_by_tag),
            self.company_id.id,
        ))
        totals_by_move = {}
        untaxed_by_move = {}
        fractions = {}
        for move_id, tag_id, amount, paid, total in self.env.cr.fetchall():
            if not total or not paid or paid <= 0:
                continue
            if tag_id:
                code = code_by_tag[tag_id]
                move_totals = totals_by_move.setdefault(move_id, {})
                move_totals[code] = (
                    move_totals.get(code, 0.0) + (amount or 0.0)
                )
            else:
                untaxed_by_move[move_id] = amount or 0.0
            fractions[move_id] = min(float(paid) / float(total), 1.0)
        currency = self.company_id.currency_id
        modes = (
            'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
        )
        amounts = dict.fromkeys(modes, 0.0)
        for move_id, totals in totals_by_move.items():
            fraction = fractions[move_id]
            for mode in modes:
                recognised = self._bas_amount_from_grid_totals(mode, totals)
                amounts[mode] += currency.round(recognised * fraction)
        for move_id, amount in untaxed_by_move.items():
            amounts['total_sales_incl'] += currency.round(
                amount * fractions[move_id]
            )
        return {
            mode: currency.round(amount) for mode, amount in amounts.items()
        }

    def _bas_untaxed_sales(self):
        """Posted untaxed customer revenue in the accrual BAS period."""
        self.ensure_one()
        self.env.cr.execute(SQL(
            """
            SELECT COALESCE(-SUM(aml.balance), 0)
              FROM account_move_line aml
              JOIN account_move move ON move.id = aml.move_id
              JOIN account_account account ON account.id = aml.account_id
             WHERE move.company_id = %s
               AND move.state = 'posted'
               AND move.move_type IN ('out_invoice', 'out_refund')
               AND aml.date >= %s
               AND aml.date <= %s
               AND account.account_type IN ('income', 'income_other')
               AND aml.tax_line_id IS NULL
               AND NOT EXISTS (
                    SELECT 1
                      FROM account_move_line_account_tax_rel tax_rel
                     WHERE tax_rel.account_move_line_id = aml.id
               )
            """,
            self.company_id.id,
            self.date_from,
            self.date_to,
        ))
        return float(self.env.cr.fetchone()[0] or 0.0)

    @staticmethod
    def _aggregation_to_sql(mode):
        # Tax tags may be present on both base and tax journal items. Split
        # those populations explicitly, then sum signed balance so refunds
        # reduce the reported amount instead of disappearing behind a
        # debit/credit filter.
        if mode == 'base_credit':
            return ("aml.tax_line_id IS NULL", "-SUM(aml.balance)")
        if mode == 'base_debit':
            return ("aml.tax_line_id IS NULL", "SUM(aml.balance)")
        if mode == 'tax_credit':
            return ("aml.tax_line_id IS NOT NULL", "-SUM(aml.balance)")
        if mode == 'tax_debit':
            return ("aml.tax_line_id IS NOT NULL", "SUM(aml.balance)")
        return (None, "0")


class EhBasRunLine(models.Model):
    _name = 'eh.bas.run.line'
    _description = "BAS run line"
    _order = 'label_sequence, label_code'

    run_id = fields.Many2one(
        'eh.bas.run', required=True, ondelete='cascade', index=True,
    )
    label_id = fields.Many2one(
        'eh.bas.label', required=True, ondelete='restrict',
    )
    # These are deliberate snapshots, not related fields. A label definition
    # may be revised after lodgement; the worksheet must keep exactly the code,
    # translated name, ordering and aggregation rule used at compute time.
    label_code = fields.Char(readonly=True, index=True)
    label_name = fields.Char(readonly=True)
    label_sequence = fields.Integer(readonly=True)
    label_aggregation = fields.Selection(
        _AGGREGATION_CHOICES, readonly=True,
    )
    amount = fields.Monetary(currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency', readonly=True,
        help="Run-company currency captured when the worksheet was computed.",
    )
    is_lodgeable = fields.Boolean(
        readonly=True,
        help=(
            "True when this snapshot row belongs to the selected BAS "
            "lodgement set. Simpler BAS retains all rows for review but "
            "exports only G1, 1A, 1B and PAYG-W rows."
        ),
    )
    run_state = fields.Selection(
        related='run_id.state', readonly=True,
    )
    notes = fields.Char()

    def _eh_check_engine_create_unlink(self):
        if not (self.env.su and self.env.context.get(BAS_ENGINE_CTX)):
            raise AccessError(_(
                "BAS worksheet rows are created and removed by the compute "
                "engine, not by direct RPC calls."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_check_engine_create_unlink()
        runs = self.env['eh.bas.run'].browse(
            [vals.get('run_id') for vals in vals_list if vals.get('run_id')]
        )
        runs._eh_validate_company_closure()
        runs._eh_lock_for_transition()
        runs._eh_validate_company_closure()
        lines = super().create(vals_list)
        lines._eh_validate_provenance()
        return lines

    def _eh_validate_provenance(self):
        for line in self:
            if not line.run_id or not line.label_id:
                raise UserError(_(
                    "Every BAS worksheet row needs its run and label."
                ))
            if line.currency_id != line.run_id.company_id.currency_id:
                raise UserError(_(
                    "Every BAS worksheet row must snapshot its run-company "
                    "currency."
                ))
        return self

    def write(self, vals):
        runs = self.mapped('run_id') | self.env['eh.bas.run'].browse(
            [vals.get('run_id')] if vals.get('run_id') else [])
        if not self.env.su:
            self._eh_check_access('write')
            runs._eh_validate_company_closure()
        runs._eh_lock_for_transition()
        self.invalidate_recordset()
        runs.invalidate_recordset()
        if not self.env.su:
            self._eh_check_access('write')
        runs._eh_validate_company_closure()
        provenance_fields = {
            'run_id', 'label_id', 'label_code', 'label_name',
            'label_sequence', 'label_aggregation', 'currency_id',
            'is_lodgeable',
        }
        if provenance_fields.intersection(vals):
            if not (self.env.su and self.env.context.get(BAS_ENGINE_CTX)):
                raise AccessError(_(
                    "BAS worksheet provenance is server-owned."
                ))
        if self.filtered(lambda line: line.run_id.state == 'lodged') and not (
            self.env.su and self.env.context.get(BAS_MIGRATION_CTX)
        ):
            raise UserError(_(
                "The worksheet of a lodged BAS is immutable evidence."
            ))
        if 'amount' in vals and not (
            self.env.su and self.env.context.get(BAS_ENGINE_CTX)
        ):
            automatic = self.filtered(
                lambda line: line.label_aggregation != 'manual')
            if automatic:
                raise AccessError(_(
                    "Automatically computed BAS amounts cannot be edited "
                    "directly. Recompute the BAS instead."
                ))
        result = super().write(vals)
        self._eh_validate_provenance()
        return result

    def unlink(self):
        self._eh_check_engine_create_unlink()
        runs = self.mapped('run_id')
        runs._eh_lock_for_transition()
        self.invalidate_recordset()
        runs.invalidate_recordset()
        if self.filtered(lambda line: line.run_id.state == 'lodged'):
            raise UserError(_(
                "The worksheet of a lodged BAS is immutable evidence."
            ))
        return super().unlink()
