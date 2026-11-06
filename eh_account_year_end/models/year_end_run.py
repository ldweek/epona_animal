# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.year.end.run: per-(company, fiscal year) closing run.

Compute step:
* Validates the exact company-configured fiscal year, then aggregates posted
  account.move.line records by income and expense account. On Odoo's shared
  branch chart (18+), a run includes the selected company and its accounting
  branch subtree; the caller must have every source branch enabled.
* Produces one breakdown row per contributing account with its closing
  balance. Income balances are credit-side (negative); expense are
  debit-side (positive).

AOCI sub-reserve step (IAS 1.106), inside compute:
* When the company carries AOCI sub-reserve mapping rows
  (eh.aoci.reserve.map), each mapped OCI flow account's NET posted
  movement over the fiscal year becomes an OCI reclassification row.
  Net movement only: an amount recycled to P&L during the year (e.g. a
  CTA disposal reclassification, IAS 21.48) already left the flow
  account, so the close never double-moves it.
* Known OCI flow accounts (discovered from installed suite modules, or
  listed on an incomplete mapping row) that moved in the period but
  have no complete mapping are collected on a warning list; posting is
  blocked unless a manager overrides with a documented reason.
* With no mapping rows configured the behaviour is exactly the
  original pure P&L close.

Post step:
* Builds an account.move with one line per breakdown row plus one or
  two lines for retained earnings, plus one debit/credit pair per OCI
  reclassification row (flow account -> AOCI sub-reserve), ensuring
  the entry balances by construction. Retained earnings receives ONLY
  the P&L result; OCI components go to their sub-reserves, never to
  retained earnings. Posts the move on the fiscal year end date so
  the closing belongs to the fiscal year.
* Chronology guard: posting is refused while a later fiscal year's
  close already stands posted for the company; closes must post
  oldest-first so each year's equity roll builds on the prior one.

Reverse step:
* Generates the inverse entry dated one day after the fiscal year end
  so the income and expense accounts re-open for re-classification. The
  reversal is refused if Odoo's effective fiscal/hard/parent lock would
  silently move that exact date. Newer closes must be reversed first. A
  verified reversed run can then be recomputed and re-posted without losing
  any close/reversal history.

Lock step:
* Posting checks the effective fiscal, hard and inherited parent-company
  lock date before creating an entry. lock_after_post defaults to True and
  advances res.company.fiscalyear_lock_date to the fiscal year end without
  ever rewinding a later lock. Disabling it requires a documented reason
  logged in chatter; reversing does not weaken or rewind a lock.
"""

from datetime import timedelta
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

from .aoci_reserve_map import AOCI_KINDS
from .year_end_sequence import next_company_reference

_logger = logging.getLogger(__name__)


_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = (
    'expense', 'expense_depreciation', 'expense_direct_cost', 'expense_other',
)

# Input / measurement fields on the run that become the basis of a posted
# closing entry. Once the run is posted or reversed they are frozen at the ORM
# write layer. 'state' is deliberately NOT listed: the action methods write
# only state + the audit stamps (posted_at/posted_by_id, ...) + the move
# links, so a pure state-transition write carries no frozen field and always
# passes. The breakdown lines are guarded on their own model; line_ids appears
# here so a raw reassignment of the whole set on a posted run is also refused.
_FROZEN_AFTER_POST = (
    'line_ids', 'fiscal_year_start', 'fiscal_year_end', 'company_id',
    'journal_id', 'retained_earnings_account_id', 'lock_after_post',
    'no_lock_reason', 'override_unmapped_oci', 'override_unmapped_reason',
)

_FROZEN_AFTER_COMPUTE = (
    'line_ids', 'fiscal_year_start', 'fiscal_year_end', 'company_id',
    'journal_id', 'retained_earnings_account_id',
)

YEAR_END_ENGINE_CTX = 'eh_year_end_engine'


class EhYearEndRun(models.Model):
    _name = 'eh.year.end.run'
    _description = "Year-end closing run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'fiscal_year_end desc, id desc'
    _rec_name = 'name'

    # The state machine is enforced in the UI (readonly statusbar + header
    # buttons) and by the write() freeze below, but a draft run's state is not
    # frozen: without this guard a plain user could RPC write({'state':
    # 'posted'}) straight past action_post and its journal entry. eh.workflow.
    # guard blocks any direct (non-action, non-superuser) write to 'state'; the
    # action_* methods flag their own state writes via self._eh_workflow_action.
    _eh_guarded_fields = (
        'state', 'move_id', 'reversal_move_id',
        'closing_move_ids', 'reversal_move_ids',
        'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
        'reversed_at', 'reversed_by_id',
        'has_unmapped_oci', 'unmapped_oci_note',
    )

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('posted', "Posted"),
            ('reversed', "Reversed"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
        help=(
            "Company whose year is closed. On Odoo 18+ a run also includes "
            "that company's active accounting-branch subtree; every source "
            "branch must be enabled in the caller's company context."
        ),
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True, store=True,
    )

    fiscal_year_start = fields.Date(
        required=True, tracking=True,
        help=(
            "Start of the fiscal year being closed. The closing entry "
            "aggregates posted moves whose date falls in [start, end]."
        ),
    )
    fiscal_year_end = fields.Date(
        required=True, tracking=True,
        help="End of the fiscal year. Posting date for the closing entry.",
    )

    journal_id = fields.Many2one(
        'account.journal', required=True, tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
        check_company=True,
    )
    retained_earnings_account_id = fields.Many2one(
        'account.account', required=True, tracking=True,
        domain=(
            "[('account_type', '=', 'equity'),"
            " ('company_ids', 'parent_of', company_id)]"
        ),
        check_company=True,
        help=(
            "Equity account that absorbs the net profit / loss for the "
            "fiscal year. Most charts call this Retained Earnings or "
            "Accumulated Profit and Loss."
        ),
    )
    lock_after_post = fields.Boolean(
        default=True, tracking=True,
        help=(
            "When set, posting bumps res.company.fiscalyear_lock_date "
            "to the fiscal year end so prior period entries cannot be "
            "edited. Disabling this control requires a documented "
            "reason; the reason is logged in the chatter when the close "
            "posts without advancing the lock date."
        ),
    )
    no_lock_reason = fields.Text(
        string="Reason For Not Locking",
        help=(
            "Documented reason for posting the year-end close without "
            "advancing the fiscal-year lock date. Required when 'Lock "
            "After Post' is disabled; logged in the chatter on post."
        ),
    )

    line_ids = fields.One2many(
        'eh.year.end.line', 'run_id', copy=False,
    )
    line_count = fields.Integer(compute='_compute_totals', store=True)

    total_income = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    total_expense = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
    )
    net_profit = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="total_income minus total_expense; positive = profit. "
             "P&L only: OCI reclassification rows never contribute, so "
             "retained earnings receives exactly this amount.",
    )
    total_oci_reclass = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="Net OCI movement of the fiscal year swept into AOCI "
             "sub-reserves by the closing entry, credit-positive "
             "(positive = net OCI gain for the year). Zero when no AOCI "
             "mapping rows are configured.",
    )

    # ---- AOCI governance (IAS 1.106) ----
    has_unmapped_oci = fields.Boolean(
        readonly=True, copy=False,
        help="True when known OCI flow accounts moved during the fiscal "
             "year but carry no complete AOCI sub-reserve mapping. "
             "Posting is blocked unless a manager overrides with a "
             "documented reason.",
    )
    unmapped_oci_note = fields.Text(
        string="Unmapped OCI Accounts", readonly=True, copy=False,
        help="Known OCI flow accounts (discovered from installed suite "
             "modules or listed on incomplete mapping rows) whose period "
             "movement would stay commingled in equity because no AOCI "
             "sub-reserve account is mapped.",
    )
    override_unmapped_oci = fields.Boolean(
        string="Override Unmapped OCI", default=False, copy=False,
        tracking=True,
        help="Post the close even though OCI flow accounts lack an AOCI "
             "sub-reserve mapping. Requires a documented reason, logged "
             "in the chatter on post.",
    )
    override_unmapped_reason = fields.Text(
        string="Override Reason",
        help="Documented basis for closing with unmapped OCI accounts. "
             "Required when the override is ticked; logged in the "
             "chatter on post.",
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )
    closing_move_ids = fields.Many2many(
        'account.move', 'eh_year_end_run_closing_move_rel',
        'run_id', 'move_id', string="Closing Entry History",
        readonly=True, copy=False,
        help="Every closing entry posted by this run, including entries from "
             "earlier close/reverse/re-close cycles.",
    )
    reversal_move_ids = fields.Many2many(
        'account.move', 'eh_year_end_run_reversal_move_rel',
        'run_id', 'move_id', string="Reversal Entry History",
        readonly=True, copy=False,
        help="Every reversal entry posted by this run, including reversals "
             "from earlier close/reverse/re-close cycles.",
    )

    # ---- audit ----
    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True, tracking=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True, tracking=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True, tracking=True)

    notes = fields.Text()

    _unique_company_year = models.Constraint(
        'unique(company_id, fiscal_year_end)',
        'Only one year-end run per company per fiscal year end date.',
    )
    _check_year_dates = models.Constraint(
        'CHECK (fiscal_year_start <= fiscal_year_end)',
        'fiscal_year_start must be on or before fiscal_year_end.',
    )

    @api.depends(
        'line_ids', 'line_ids.income_balance', 'line_ids.expense_balance',
        'line_ids.line_kind', 'line_ids.oci_balance',
    )
    def _compute_totals(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            # OCI rows carry zero income/expense balances by construction,
            # so the P&L totals (and with them net_profit and the retained
            # earnings amount) are structurally untouched by AOCI rows.
            rec.total_income = sum(rec.line_ids.mapped('income_balance'))
            rec.total_expense = sum(rec.line_ids.mapped('expense_balance'))
            rec.net_profit = rec.total_income - rec.total_expense
            # oci_balance is ledger-signed (debit positive); an OCI gain
            # accumulates as a credit, so negate to report gain-positive.
            oci_lines = rec.line_ids.filtered(
                lambda l: l.line_kind == 'oci')
            rec.total_oci_reclass = -sum(oci_lines.mapped('oci_balance'))

    # ---- onchange (live form feedback) ----

    @api.onchange('company_id', 'fiscal_year_end')
    def _onchange_fiscal_year_end_derive_start(self):
        """Derive the authoritative configured fiscal-year start."""
        for rec in self:
            if not rec.company_id or not rec.fiscal_year_end:
                continue
            dates = rec.company_id.compute_fiscalyear_dates(
                rec.fiscal_year_end,
            )
            rec.fiscal_year_start = dates['date_from']

    @api.constrains(
        'company_id', 'fiscal_year_start', 'fiscal_year_end',
    )
    def _check_configured_fiscal_window(self):
        for run in self:
            run._validate_fiscal_window()

    def _validate_fiscal_window(self):
        """Require the exact fiscal year configured on ``res.company``."""
        for run in self:
            if not (
                run.company_id
                and run.fiscal_year_start
                and run.fiscal_year_end
            ):
                continue
            expected = run.company_id.compute_fiscalyear_dates(
                run.fiscal_year_end,
            )
            if (
                run.fiscal_year_start != expected['date_from']
                or run.fiscal_year_end != expected['date_to']
            ):
                raise ValidationError(_(
                    "The selected period %(start)s to %(end)s is not the "
                    "configured fiscal year for %(company)s. Use "
                    "%(expected_start)s to %(expected_end)s.",
                    start=run.fiscal_year_start,
                    end=run.fiscal_year_end,
                    company=run.company_id.display_name,
                    expected_start=expected['date_from'],
                    expected_end=expected['date_to'],
                ))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            if vals.get('name', '/') == '/':
                fiscal_year_end = fields.Date.to_date(
                    vals.get('fiscal_year_end'),
                )
                if fiscal_year_end:
                    company = self.env['res.company'].browse(company_id)
                    vals['name'] = next_company_reference(
                        self.env, company, fiscal_year_end,
                    ) or '/'
        runs = super().create(vals_list)
        runs._eh_validate_references()
        return runs

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A year-end run may only use a company enabled in the caller's "
                "current company context."
            ))

    def _source_companies(self, require_access=False):
        """Companies whose ledger rows form this run's economic close.

        Odoo 18 introduced a shared branch chart. In that schema a selected
        company closes its active branch subtree as one legal-entity result;
        older single-company account schemas retain exact-company scope.
        Requiring the complete subtree in ``allowed_company_ids`` prevents a
        superficially successful close built from only the visible branches.
        """
        self.ensure_one()
        companies = self.company_id
        if (
            'company_ids' in self.env['account.account']._fields
            and 'root_id' in self.company_id._fields
        ):
            companies = self.env['res.company'].sudo().search([
                ('id', 'child_of', self.company_id.id),
            ], order='id')
        if require_access and not self.env.su:
            missing_ids = set(companies.ids) - set(self.env.companies.ids)
            missing = self.env['res.company'].sudo().browse(missing_ids)
            if missing:
                raise AccessError(_(
                    "The year-end source includes accounting branch(es) not "
                    "enabled in the caller's current company context: "
                    "%(branches)s. Enable the complete branch subtree before "
                    "computing or posting.",
                    branches=', '.join(missing.mapped('display_name')),
                ))
        return companies

    def _scope_family_company_ids(self):
        """Full branch family used to find potentially overlapping runs."""
        self.ensure_one()
        company = self.company_id
        if 'root_id' not in company._fields:
            return [company.id]
        root = company.root_id or company
        return self.env['res.company'].sudo().search([
            ('id', 'child_of', root.id),
        ]).ids

    def _scopes_overlap(self, other):
        self.ensure_one()
        other.ensure_one()
        return bool(
            set(self._source_companies().ids)
            & set(other._source_companies().ids)
        )

    def _scope_related_runs(self, domain=None):
        """Sudo only the governance search after caller access was proved."""
        self.ensure_one()
        candidates = self.sudo().search([
            ('id', '!=', self.id),
            ('company_id', 'in', self._scope_family_company_ids()),
            *(domain or []),
        ])
        return candidates.filtered(lambda run: self._scopes_overlap(run))

    @api.model
    def _eh_account_matches_company(self, account, company):
        """Use chart model's authoritative branch-company contract."""
        checker = getattr(account, '_check_company_domain', None)
        if checker:
            try:
                return bool(account.filtered_domain(checker(company)))
            except (AttributeError, TypeError):
                # Odoo 16 has no hierarchy-aware account-company API.
                pass
        if 'company_ids' in account._fields:
            return company.id in account.company_ids.ids
        return not account.company_id or account.company_id == company

    @api.model
    def _eh_validate_posting_account(self, account, company, label):
        if not account:
            return
        archived = (
            ('active' in account._fields and not account.active)
            or (
                'deprecated' in account._fields
                and account.deprecated
            )
        )
        if archived:
            raise UserError(_(
                "%(label)s '%(account)s' is archived. Restore or replace it "
                "before computing or posting the year-end close.",
                label=label, account=account.display_name,
            ))
        if not self._eh_account_matches_company(account, company):
            raise UserError(_(
                "%(label)s '%(account)s' is not available to the run "
                "company or its accounting branch.",
                label=label, account=account.display_name,
            ))
        forced_currency = (
            account.currency_id if 'currency_id' in account._fields
            else self.env['res.currency']
        )
        if forced_currency and forced_currency != company.currency_id:
            raise UserError(_(
                "%(label)s '%(account)s' forces %(currency)s. Year-end "
                "closing amounts are measured in %(company_currency)s and "
                "cannot be posted to a secondary-currency account without "
                "inventing an amount-currency basis. Use a company-currency "
                "closing account.",
                label=label,
                account=account.display_name,
                currency=forced_currency.display_name,
                company_currency=company.currency_id.display_name,
            ))

    def _eh_validate_references(self, vals=None, check_source=False):
        vals = vals or {}
        if not self.env.su:
            self._eh_check_access('write')
            self.env['account.move.line']._eh_check_access('read')
            self.env['account.account']._eh_check_access('read')
            self.env['eh.aoci.reserve.map']._eh_check_access('read')
        for run in self:
            company_id = vals.get('company_id', run.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            if (
                check_source
                and not self.env.su
                and run.fiscal_year_start
                and run.fiscal_year_end
            ):
                source_companies = run._source_companies(require_access=True)
                Line = self.env['account.move.line']
                source_domain = [
                    ('company_id', 'in', source_companies.ids),
                    ('date', '>=', run.fiscal_year_start),
                    ('date', '<=', run.fiscal_year_end),
                ]
                if Line.search_count(source_domain) != Line.sudo().search_count(
                    source_domain
                ):
                    raise AccessError(_(
                        "The year-end ledger basis contains rows the caller is "
                        "not authorised to read."
                    ))
            journal = self.env['account.journal'].browse(
                vals.get('journal_id', run.journal_id.id))
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id != company:
                    raise UserError(_(
                        "The year-end journal must belong to the run company."
                    ))
                if 'active' in journal._fields and not journal.active:
                    raise UserError(_(
                        "The year-end journal is archived. Restore or replace "
                        "it before computing or posting the close."
                    ))
            retained = self.env['account.account'].browse(vals.get(
                'retained_earnings_account_id',
                run.retained_earnings_account_id.id,
            ))
            if retained:
                if not self.env.su:
                    retained._eh_check_access('read')
                self._eh_validate_posting_account(
                    retained, company, _("Retained-earnings account"))
            for line in run.line_ids:
                account_labels = (
                    (line.account_id, _("Year-end breakdown account")),
                    (line.reserve_account_id, _("AOCI reserve account")),
                )
                for account, label in account_labels:
                    if not account:
                        continue
                    if not self.env.su:
                        account._eh_check_access('read')
                    self._eh_validate_posting_account(
                        account, company, label)
        return self

    def _eh_lock_for_transition(
        self, lock_companies=False, serialize_close=False,
    ):
        if serialize_close:
            keys = set()
            for run in self:
                company = run.company_id
                root = (
                    company.root_id
                    if 'root_id' in company._fields and company.root_id
                    else company
                )
                keys.add(root.id)
            for company_id in sorted(keys):
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ('eh.year.end.close:%s' % company_id,),
                )
        locked_companies = self.env['res.company']
        if lock_companies:
            company_ids = set()
            for run in self:
                company_ids.update(run._source_companies().ids)
            company_ids = tuple(sorted(company_ids))
            locked_companies = self.env['res.company'].browse(company_ids)
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (company_ids,),
            )
            lock_fields = [
                field_name
                for field_name in (
                    'fiscalyear_lock_date', 'hard_lock_date',
                    'period_lock_date', 'eh_move_version',
                )
                if field_name in locked_companies._fields
            ]
            if lock_fields:
                locked_companies.invalidate_recordset(lock_fields)
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_year_end_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return self

    def _eh_prepare_transition(
        self, engine=False, check_source=False,
        lock_companies=False, serialize_close=False,
    ):
        caller = self
        if not caller.env.su:
            caller._eh_check_access('write')
        caller._eh_lock_for_transition(
            lock_companies=lock_companies,
            serialize_close=serialize_close,
        )
        caller._eh_validate_references(check_source=check_source)
        caller._validate_fiscal_window()
        records = caller._eh_workflow_action()
        if engine:
            records = records.with_context(**{YEAR_END_ENGINE_CTX: True})
        return records

    # ---- integrity: freeze input once the close is posted ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is posted or
        reversed; they are the basis of a posted closing entry.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps and move links) carries no frozen
        field and passes. A write touching a frozen figure while any record is
        posted or reversed is always blocked. 'state' is never frozen, so
        recompute in draft / computed and the legitimate transitions keep
        working.
        """
        frozen = [f for f in _FROZEN_AFTER_POST if f in vals]
        compute_frozen = [f for f in _FROZEN_AFTER_COMPUTE if f in vals]
        reference_fields = {
            'company_id', 'journal_id', 'retained_earnings_account_id',
            'fiscal_year_start', 'fiscal_year_end',
        }
        if frozen or compute_frozen or 'state' in vals \
                or reference_fields.intersection(vals):
            if not self.env.su:
                self._eh_check_access('write')
            self._eh_lock_for_transition()
            self._eh_validate_references(vals)
        confirmed = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        computed = self.filtered(lambda r: r.state == 'computed')
        engine = self.env.su and self.env.context.get(YEAR_END_ENGINE_CTX)
        if (
            (frozen and confirmed)
            or (compute_frozen and computed and not engine)
        ):
            blocked_fields = sorted(set(
                (frozen if confirmed else [])
                + (compute_frozen if computed and not engine else [])
            ))
            blocked_states = ', '.join(sorted(set(
                (confirmed | computed).mapped('state')
            )))
            raise UserError(_(
                "Year-end field(s) %(fields)s cannot be changed while the "
                "run is %(states)s. Cancel a computed run and set it back "
                "to draft, or reverse a posted run through the controlled "
                "manager action.",
                fields=', '.join(blocked_fields),
                states=blocked_states,
            ))
        # The state of a posted / reversed run is itself a control point:
        # resetting it to draft would silently lift the figure freeze above.
        # A raw ORM state write on such a run without the sanctioned-transition
        # context flag must be manager-gated, so a plain user cannot un-freeze
        # a GL-backed run. action_reverse sets the flag after its own manager
        # check and move handling.
        if 'state' in vals and not (
                self.env.su
                and self.env.context.get('eh_year_end_state_change')):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed year-end run."))

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        self._eh_validate_references()
        posted = self.filtered(
            lambda r: r.state in ('computed', 'posted', 'reversed')
            or r.move_id or r.reversal_move_id)
        if posted:
            raise UserError(_(
                "A posted or reversed year-end run cannot be deleted; it "
                "carries a posted GL closing entry. Reverse it first."))
        return super().unlink()

    @api.constrains('retained_earnings_account_id', 'company_id')
    def _check_retained_earnings_company(self):
        for rec in self:
            if not rec.retained_earnings_account_id:
                continue
            account = rec.retained_earnings_account_id
            if not rec._eh_account_matches_company(account, rec.company_id):
                raise ValidationError(_(
                    "Retained earnings account must be available to the "
                    "company or accounting branch being closed.",
                ))

    # ---- transitions ----

    def action_compute(self):
        self = self._eh_prepare_transition(
            engine=True,
            check_source=True,
            serialize_close=True,
        )
        for run in self:
            if run.state not in ('draft', 'computed', 'reversed'):
                raise UserError(_(
                    "Compute is only available in draft, computed, or "
                    "reversed state.",
                ))
            if run.state == 'reversed':
                run._check_manager()
                run._check_reclose_provenance()
                # Seed the durable history for runs created before entry
                # history was introduced.  The latest links remain available
                # for the form while the corrected close is recomputed.
                run.write({
                    'closing_move_ids': [(4, run.move_id.id)],
                    'reversal_move_ids': [(4, run.reversal_move_id.id)],
                })
            run.line_ids.unlink()
            run._build_lines()
            run._build_oci_lines()
            run._refresh_unmapped_oci()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })

    def action_post(self):
        self = self._eh_prepare_transition(
            engine=True,
            check_source=True,
            lock_companies=True,
            serialize_close=True,
        )
        for run in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can post a year-end "
                    "closing run.",
                ))
            if run.state != 'computed':
                raise UserError(_(
                    "Run must be computed before posting.",
                ))
            if not run.line_ids:
                raise UserError(_(
                    "No income or expense balances to close. "
                    "Recompute or cancel the run.",
                ))
            # Rebuild this engine-owned governance snapshot under the same
            # company/run lock used for posting; a stale or forged flag can
            # never bypass the OCI gate.
            run._refresh_unmapped_oci()
            run._check_prior_period_pl_cleared()
            run._check_post_lock()
            run._check_oci_governance()
            move = run._build_closing_move()
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
                'reversal_move_id': False,
                'closing_move_ids': [(4, move.id)],
            })
            if run.has_unmapped_oci and run.override_unmapped_oci:
                run.message_post(body=_(
                    "Year-end close posted with unmapped OCI accounts "
                    "(manager override). Reason: %(reason)s\n%(detail)s",
                    reason=run.override_unmapped_reason,
                    detail=run.unmapped_oci_note or '',
                ))
            if run.lock_after_post:
                run._maybe_advance_lock_date()
            else:
                run.message_post(body=_(
                    "Fiscal-year lock date NOT advanced on post "
                    "(lock disabled on this run). Reason: %s",
                    run.no_lock_reason,
                ))

    def action_reverse(self):
        self = self._eh_prepare_transition(
            lock_companies=True,
            serialize_close=True,
        )
        for run in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse a "
                    "year-end closing run.",
                ))
            if run.state != 'posted':
                raise UserError(_(
                    "Only posted runs can be reversed.",
                ))
            if not run.move_id:
                raise UserError(_(
                    "Run has no posted move to reverse.",
                ))
            if (
                run.move_id.company_id != run.company_id
                or run.move_id.state != 'posted'
                or run.move_id.move_type != 'entry'
                or not run.move_id.eh_sealed
            ):
                raise UserError(_(
                    "The linked year-end move failed its sealed provenance "
                    "check and will not be reversed."
                ))
            later = run._scope_related_runs([
                ('state', '=', 'posted'),
                ('fiscal_year_end', '>', run.fiscal_year_end),
            ]).sorted(
                key=lambda candidate: (
                    candidate.fiscal_year_end, candidate.id,
                ),
                reverse=True,
            )[:1]
            if later:
                raise UserError(_(
                    "Year-end close %(current)s cannot be reversed while the "
                    "later close %(later)s (year ending %(end)s) remains "
                    "posted. Reverse closes newest-first so a reversal never "
                    "enters an already-closed successor period.",
                    current=run.name,
                    later=later.name,
                    end=later.fiscal_year_end,
                ))
            reversal = run._build_reversal_move()
            run.with_context(eh_year_end_state_change=True).write({
                'state': 'reversed',
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
                'reversal_move_id': reversal.id,
                'closing_move_ids': [(4, run.move_id.id)],
                'reversal_move_ids': [(4, reversal.id)],
            })

    def action_cancel(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state in ('posted', 'reversed'):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run.",
                ))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_prepare_transition(engine=True)
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_(
                    "Only cancelled runs can return to draft.",
                ))
            run.line_ids.unlink()
            # The unmapped-OCI snapshot belongs to the discarded compute.
            run.write({
                'state': 'draft',
                'has_unmapped_oci': False,
                'unmapped_oci_note': False,
            })

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No closing entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def action_seed_aoci_map(self):
        """Discover known OCI flow accounts from the installed suite modules
        and seed the company's AOCI sub-reserve mapping rows, then open the
        mapping so the manager can assign the sub-reserve accounts."""
        self.ensure_one()
        caller = self
        if not caller.env.su:
            caller._eh_check_access('write')
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self.env['eh.aoci.reserve.map'].action_seed_from_modules(
            self.company_id)
        return {
            'type': 'ir.actions.act_window',
            'name': _("AOCI Sub-Reserve Mapping"),
            'res_model': 'eh.aoci.reserve.map',
            'view_mode': 'list,form',
            'domain': [('company_id', '=', self.company_id.id)],
            'context': {'default_company_id': self.company_id.id},
        }

    # ---- helpers ----

    def _check_reclose_provenance(self):
        """Prove the current close was fully reversed before recomputing.

        A reversed workflow state alone is not sufficient evidence.  Re-close
        is allowed only when the run still points to one verified sealed,
        posted closing entry and its exact verified sealed, posted reversal.
        """
        self.ensure_one()
        close = self.move_id
        reversal = self.reversal_move_id
        if (
            not close
            or not reversal
            or close.company_id != self.company_id
            or reversal.company_id != self.company_id
            or close.state != 'posted'
            or reversal.state != 'posted'
            or close.move_type != 'entry'
            or reversal.move_type != 'entry'
            or not close.eh_sealed
            or not reversal.eh_sealed
            or reversal.reversed_entry_id != close
        ):
            raise UserError(_(
                "The reversed run does not carry one intact verified closing "
                "entry and its exact verified reversal. Re-close is blocked "
                "until the provenance is repaired."
            ))
        return True

    def _year_end_entry_move_ids(self):
        """All closing/reversal move ids whose economic scopes overlap.

        Current ``move_id`` links cover runs created before the durable
        many-to-many history fields existed.  Excluding both close and
        reversal entries keeps a corrected re-close based on operating P&L
        only and prevents a next-day reversal from becoming next year's P&L.
        """
        self.ensure_one()
        runs = self.sudo().search([
            ('company_id', 'in', self._scope_family_company_ids()),
        ]).filtered(lambda run: self._scopes_overlap(run))
        moves = (
            runs.mapped('move_id')
            | runs.mapped('reversal_move_id')
            | runs.mapped('closing_move_ids')
            | runs.mapped('reversal_move_ids')
        )
        return moves.ids

    def _account_source_label(self, account):
        """Resolve company-dependent account metadata in the run company."""
        self.ensure_one()
        scoped = account.with_company(self.company_id)
        return scoped.code or scoped.name or account.display_name

    def _build_lines(self):
        """Aggregate posted journal lines per account in [start, end].

        Income and expense accounts both contribute: income lines
        produce credit-side (negative) balances and expense lines
        produce debit-side (positive) balances. The breakdown row
        stores both fields so the totals view can sum them
        independently.
        """
        self.ensure_one()
        Line = self.env['eh.year.end.line']
        rows = self._fetch_account_balances()
        if not rows:
            return
        # Read every contributing account's type in one prefetch rather
        # than browsing one account per row, then create all breakdown
        # lines in a single batched insert. A full chart can carry
        # hundreds of income and expense accounts; the per-row browse
        # plus per-row create was two round-trips per account.
        accounts = self.env['account.account'].browse(
            [row['account_id'] for row in rows]
        )
        account_type_by_id = {acc.id: acc.account_type for acc in accounts}
        account_label_by_id = {
            acc.id: self._account_source_label(acc) for acc in accounts
        }
        line_vals = []
        for row in rows:
            account_id = row['account_id']
            account_type = account_type_by_id.get(account_id)
            if account_type in _INCOME_TYPES:
                # Income balance is naturally credit-side; flip sign.
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account_id,
                    'source_account_label': account_label_by_id[account_id],
                    'income_balance': -float(row['balance']),
                    'expense_balance': 0.0,
                })
            elif account_type in _EXPENSE_TYPES:
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account_id,
                    'source_account_label': account_label_by_id[account_id],
                    'income_balance': 0.0,
                    'expense_balance': float(row['balance']),
                })
        if line_vals:
            Line.create(line_vals)

    def _fetch_account_balances(self):
        """Return [{'account_id': X, 'balance': Y}] per income/expense
        account with non-zero balance in the fiscal year.
        """
        self.ensure_one()
        query = MoveLineQuery(
            self.env, company_ids=self._source_companies().ids,
        )
        query.where_account_types(_INCOME_TYPES + _EXPENSE_TYPES)
        query.where_date_range(
            date_from=self.fiscal_year_start,
            date_to=self.fiscal_year_end,
        )
        query.where_posted_only()
        excluded_move_ids = self._year_end_entry_move_ids()
        if excluded_move_ids:
            query.where_raw(SQL(
                "aml.move_id NOT IN %s", tuple(excluded_move_ids),
            ))
        query.select_field('account_id')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.account_id"))
        return [
            row for row in query.execute()
            if not self.currency_id.is_zero(
                float(row.get('balance') or 0.0),
            )
        ]

    def _check_prior_period_pl_cleared(self):
        """Block a new close while any earlier P&L account still carries.

        A standing prior close nets each income/expense account to zero. A
        missing or reversed prior close leaves at least one non-zero account;
        merely netting all P&L accounts together is intentionally insufficient.
        """
        self.ensure_one()
        query = MoveLineQuery(
            self.env,
            company_ids=self._source_companies().ids,
        )
        query.where_account_types(_INCOME_TYPES + _EXPENSE_TYPES)
        query.where_date_range(
            date_to=self.fiscal_year_start - timedelta(days=1),
        )
        query.where_posted_only()
        query.select_field('account_id')
        query.select_balance_sum()
        query.group_by(SQL("aml.account_id"))
        residuals = [
            row for row in query.execute()
            if not self.currency_id.is_zero(
                float(row.get('balance') or 0.0),
            )
        ]
        if not residuals:
            return
        accounts = self.env['account.account'].browse(
            [row['account_id'] for row in residuals],
        )
        labels = ', '.join(
            self._account_source_label(account)
            for account in accounts[:8]
        )
        if len(accounts) > 8:
            labels = _("%(labels)s and %(count)s more", labels=labels,
                       count=len(accounts) - 8)
        raise UserError(_(
            "Profit-and-loss balances exist before fiscal year start "
            "%(start)s (%(accounts)s). Post and retain the immediately "
            "preceding year-end close, or reconcile the opening equity "
            "basis, before posting this close.",
            start=self.fiscal_year_start,
            accounts=labels,
        ))

    # ---- AOCI sub-reserve reclassification (IAS 1.106) ----

    def _fetch_period_balances(self, accounts):
        """Net posted movement per account over [start, end], ledger-signed
        (debit positive), as {account_id: balance}.

        NET movement is the whole point: when a recyclable component
        recycles during the year (e.g. a CTA disposal reclassification
        posted by the FX module under IAS 21.48, debiting the flow account
        against P&L), the recycled amount has already left the flow
        account, so the net figure excludes it and the close never moves
        it twice.
        """
        self.ensure_one()
        if not accounts:
            return {}
        query = MoveLineQuery(
            self.env, company_ids=self._source_companies().ids,
        )
        query.where_accounts(accounts.ids)
        query.where_date_range(
            date_from=self.fiscal_year_start,
            date_to=self.fiscal_year_end,
        )
        query.where_posted_only()
        excluded_move_ids = self._year_end_entry_move_ids()
        if excluded_move_ids:
            query.where_raw(SQL(
                "aml.move_id NOT IN %s", tuple(excluded_move_ids),
            ))
        query.select_field('account_id')
        query.select_balance_sum()
        query.group_by(SQL("aml.account_id"))
        return {
            row['account_id']: float(row.get('balance') or 0.0)
            for row in query.execute()
        }

    def _build_oci_lines(self):
        """One OCI reclassification row per mapped flow account with a
        non-zero net period movement.

        Only complete mapping rows (reserve account AND source accounts)
        produce reclassification rows; incomplete rows feed the
        unmapped-OCI warning list instead. With no mapping rows configured
        this is a no-op and the close stays a pure P&L close.
        """
        self.ensure_one()
        rows = self.env['eh.aoci.reserve.map'].search([
            ('company_id', '=', self.company_id.id),
        ]).filtered(lambda r: r.reserve_account_id and r.source_account_ids)
        if not rows:
            return
        balances = self._fetch_period_balances(
            rows.mapped('source_account_ids'),
        )
        line_vals = []
        for row in rows:
            for account in row.source_account_ids:
                net = balances.get(account.id, 0.0)
                if self.currency_id.is_zero(net):
                    continue
                line_vals.append({
                    'run_id': self.id,
                    'account_id': account.id,
                    'source_account_label': self._account_source_label(
                        account,
                    ),
                    'line_kind': 'oci',
                    'oci_kind': row.kind,
                    'oci_balance': net,
                    'reserve_account_id': row.reserve_account_id.id,
                    'income_balance': 0.0,
                    'expense_balance': 0.0,
                })
        if line_vals:
            self.env['eh.year.end.line'].create(line_vals)

    def _refresh_unmapped_oci(self):
        """Rebuild the unmapped-OCI warning list.

        Candidates are the union of (a) known OCI flow accounts discovered
        from the installed suite modules and (b) source accounts listed on
        incomplete mapping rows (no reserve account yet), minus accounts on
        complete rows. A candidate only warns when it actually moved in
        the period; a silent account cannot commingle anything.
        """
        self.ensure_one()
        MapModel = self.env['eh.aoci.reserve.map']
        rows = MapModel.search([('company_id', '=', self.company_id.id)])
        complete = rows.filtered(
            lambda r: r.reserve_account_id and r.source_account_ids)
        candidates = (rows - complete).mapped('source_account_ids')
        for accounts in MapModel._discover_oci_sources(
                self.company_id).values():
            candidates |= accounts
        candidates -= complete.mapped('source_account_ids')
        # Retained earnings is the P&L destination, never an OCI flow.
        candidates -= self.retained_earnings_account_id
        balances = self._fetch_period_balances(candidates)
        problems = []
        for account in sorted(
            candidates,
            key=lambda candidate: self._account_source_label(candidate),
        ):
            net = balances.get(account.id, 0.0)
            if self.currency_id.is_zero(net):
                continue
            scoped = account.with_company(self.company_id)
            problems.append(_(
                "%(code)s %(name)s: period movement %(amount)s has no "
                "AOCI sub-reserve mapping",
                code=scoped.code or '', name=scoped.name,
                # Report gain-positive (credit-positive), the reading a
                # preparer expects for an OCI reserve.
                amount=("%.*f" % (
                    self.currency_id.decimal_places, -net,
                )),
            ))
        self.write({
            'has_unmapped_oci': bool(problems),
            'unmapped_oci_note': '\n'.join(problems) if problems else False,
        })

    def _check_oci_governance(self):
        """Post-time gates for the AOCI architecture and lock governance.

        * Unmapped OCI accounts block posting unless the manager override
          is ticked with a documented reason (suite-standard
          blocking-with-override).
        * Disabling lock_after_post requires a documented reason; the
          reason is logged in the chatter after the post succeeds.
        """
        self.ensure_one()
        if self.has_unmapped_oci:
            if not self.override_unmapped_oci:
                raise UserError(_(
                    "OCI accounts moved this fiscal year without a "
                    "complete AOCI sub-reserve mapping; closing now would "
                    "leave accumulated OCI commingled in equity "
                    "(IAS 1.106):\n%(detail)s\n\nMap them via 'Discover "
                    "OCI Reserves' / the AOCI Sub-Reserve Mapping, or "
                    "tick the override with a documented reason.",
                    detail=self.unmapped_oci_note or ''))
            if not (self.override_unmapped_reason or '').strip():
                raise UserError(_(
                    "Posting with unmapped OCI accounts requires a "
                    "documented override reason; it is logged in the "
                    "chatter."))
        if not self.lock_after_post \
                and not (self.no_lock_reason or '').strip():
            raise UserError(_(
                "Posting without advancing the fiscal-year lock date "
                "requires a documented reason ('Reason For Not Locking'); "
                "it is logged in the chatter."))

    def _build_closing_move(self):
        """Build the closing journal entry.

        Income accounts: balance is credit-side; debit each by
        income_balance to bring them to zero. Expense accounts:
        balance is debit-side; credit each by expense_balance to
        bring them to zero. The retained earnings account absorbs
        the net: credit when net_profit > 0, debit when < 0.

        OCI reclassification rows add one debit/credit pair each,
        moving the flow account's net period movement into its AOCI
        sub-reserve. Retained earnings receives ONLY the P&L result:
        no OCI amount ever posts against it (IAS 1.106), which the
        guard below enforces structurally.
        """
        self.ensure_one()
        line_vals = []
        pl_lines = self.line_ids.filtered(lambda l: l.line_kind != 'oci')
        oci_lines = self.line_ids.filtered(lambda l: l.line_kind == 'oci')
        for line in pl_lines:
            account = line.account_id
            source_label = (
                line.source_account_label
                or self._account_source_label(account)
            )
            if not self.currency_id.is_zero(line.income_balance):
                # Income closes with a debit.
                line_vals.append((0, 0, {
                    'name': _("Year-end close: %s", source_label),
                    'account_id': account.id,
                    'debit': line.income_balance,
                    'credit': 0.0,
                }))
            if not self.currency_id.is_zero(line.expense_balance):
                # Expense closes with a credit.
                line_vals.append((0, 0, {
                    'name': _("Year-end close: %s", source_label),
                    'account_id': account.id,
                    'debit': 0.0,
                    'credit': line.expense_balance,
                }))
        # Retained earnings line.
        net = self.net_profit
        if self.currency_id.compare_amounts(net, 0.0) > 0:
            line_vals.append((0, 0, {
                'name': _("Net profit transferred to retained earnings"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': 0.0,
                'credit': net,
            }))
        elif self.currency_id.compare_amounts(net, 0.0) < 0:
            line_vals.append((0, 0, {
                'name': _("Net loss transferred from retained earnings"),
                'account_id': self.retained_earnings_account_id.id,
                'debit': -net,
                'credit': 0.0,
            }))
        # net == 0 produces no retained earnings line; the close still
        # balances because income == expense exactly.
        # ---- AOCI sub-reserve reclassifications (IAS 1.106) ----
        kind_labels = dict(AOCI_KINDS)
        for line in oci_lines:
            source = line.account_id
            reserve = line.reserve_account_id
            source_label = (
                line.source_account_label
                or self._account_source_label(source)
            )
            if not reserve:
                raise UserError(_(
                    "OCI reclassification row for %(code)s carries no AOCI "
                    "sub-reserve account. Recompute the run after "
                    "completing the AOCI mapping.",
                    code=source_label))
            # Retained-earnings purity: RE receives ONLY the P&L result;
            # an OCI component routed through RE would silently commingle
            # accumulated OCI with retained earnings (the audit finding
            # this architecture exists to close).
            if self.retained_earnings_account_id in (source, reserve):
                raise UserError(_(
                    "Retained earnings cannot take part in an OCI "
                    "reclassification (source or sub-reserve). OCI "
                    "components are carried in their own AOCI "
                    "sub-reserves, never in retained earnings "
                    "(IAS 1.106)."))
            amount = line.oci_balance
            label = kind_labels.get(line.oci_kind, line.oci_kind or '')
            amount_sign = self.currency_id.compare_amounts(amount, 0.0)
            if amount_sign < 0:
                # Accumulated credit (net OCI gain): debit the flow
                # account to zero its period movement, credit the reserve.
                line_vals.append((0, 0, {
                    'name': _("AOCI reclass (%(kind)s): %(code)s",
                              kind=label, code=source_label),
                    'account_id': source.id,
                    'debit': -amount,
                    'credit': 0.0,
                }))
                line_vals.append((0, 0, {
                    'name': _("AOCI sub-reserve (%(kind)s)", kind=label),
                    'account_id': reserve.id,
                    'debit': 0.0,
                    'credit': -amount,
                }))
            elif amount_sign > 0:
                # Accumulated debit (net OCI loss): credit the flow
                # account, debit the reserve.
                line_vals.append((0, 0, {
                    'name': _("AOCI reclass (%(kind)s): %(code)s",
                              kind=label, code=source_label),
                    'account_id': source.id,
                    'debit': 0.0,
                    'credit': amount,
                }))
                line_vals.append((0, 0, {
                    'name': _("AOCI sub-reserve (%(kind)s)", kind=label),
                    'account_id': reserve.id,
                    'debit': amount,
                    'credit': 0.0,
                }))
        if not line_vals:
            raise UserError(_(
                "The closing entry has no lines; nothing to post.",
            ))
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.fiscal_year_end,
            'journal_id': self.journal_id.id,
            'ref': _("Year-end close %s", self.name),
            'line_ids': line_vals,
            'eh_sealed': True,
        })
        move.action_post()
        if move.date != self.fiscal_year_end:
            raise UserError(_(
                "Odoo changed the requested year-end posting date from "
                "%(requested)s to %(actual)s. The close was rolled back; "
                "resolve the effective lock before retrying.",
                requested=self.fiscal_year_end,
                actual=move.date,
            ))
        return move

    def _build_reversal_move(self):
        self.ensure_one()
        if not self.move_id:
            return self.env['account.move']
        reversal_date = self.fiscal_year_end + timedelta(days=1)
        lock_date = self._effective_lock_date()
        if lock_date and lock_date >= reversal_date:
            raise UserError(_(
                "The required reversal date %(date)s is locked by an "
                "effective fiscal, hard, or inherited parent-company lock "
                "dated %(lock)s. The reversal was not created and the lock "
                "was not weakened.",
                date=reversal_date,
                lock=lock_date,
            ))
        defaults = {
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("Year-end close reversal %s", self.name),
        }
        rev = self.move_id._eh_reverse_with_verified_capability(
            [defaults], cancel=False,
        )
        rev._eh_post_verified_reversal()
        self._eh_seal_reversal(rev)
        if rev.date != reversal_date:
            raise UserError(_(
                "Odoo changed the requested reversal date from %(requested)s "
                "to %(actual)s. The reversal was rolled back; resolve the "
                "effective lock before retrying.",
                requested=reversal_date,
                actual=rev.date,
            ))
        return rev

    def _effective_lock_date(self):
        """Maximum fiscal/hard/parent lock across the source branch scope."""
        self.ensure_one()
        lock_dates = []
        for company in self._source_companies():
            if 'hard_lock_date' in company._fields:
                lock_date = company.with_context(
                    ignore_exceptions=True,
                )._get_user_fiscal_lock_date(
                    self.journal_id,
                    ignore_exceptions=True,
                )
            else:
                # Odoo 16/17 expose a no-argument helper which already folds
                # the fiscal/period and inherited parent-company locks.
                lock_date = company._get_user_fiscal_lock_date()
            if lock_date:
                lock_dates.append(lock_date)
        return max(lock_dates) if lock_dates else False

    def _check_post_lock(self):
        """Pre-post overlap / lock guard.

        Reject posting a year-end close when governance would be
        breached:

        * Another already-posted (or reversed) run exists for the same
          company whose fiscal year overlaps this run's [start, end]
          window. Two closings that overlap the same period would
          double-close income and expense, so a second post is blocked
          while a sibling posted run stands.
        * The company's fiscal year lock date is already at or past this
          run's fiscal year end, i.e. the year is closed and frozen.
          Posting a fresh closing entry into a locked year would either
          be rejected by the ledger or silently mis-dated, so it is
          blocked here with a clear message.
        * Chronology: a LATER fiscal year's close already stands posted
          for the company. Closes must post oldest-first: the later
          year's retained earnings and AOCI roll were built on this
          year's then-unclosed balances, so posting an earlier close
          afterwards would silently restate the later year's opening
          equity. Reverse the later close first. A reversed later run
          no longer stands, so it does not block.

        Opt-in-safe: the sibling check only fires when a genuinely
        posted run overlaps, the chronology check only when a later
        posted run stands, and the lock check only fires when the lock
        date is actually set at or past the year end. A first, clean
        close is never affected.
        """
        self.ensure_one()
        # 1. Overlapping posted (or reversed) economic scope.
        sibling = self._scope_related_runs([
            ('state', 'in', ('posted', 'reversed')),
            ('fiscal_year_start', '<=', self.fiscal_year_end),
            ('fiscal_year_end', '>=', self.fiscal_year_start),
        ])[:1]
        if sibling:
            raise UserError(_(
                "A year-end close (%s) has already been posted over an "
                "overlapping company/branch scope and fiscal year. Reverse "
                "it before posting another close for the same period.",
                sibling.name,
            ))
        # 2. Chronology: a later fiscal year is already closed.
        later = self._scope_related_runs([
            ('state', '=', 'posted'),
            ('fiscal_year_start', '>', self.fiscal_year_end),
        ]).sorted(
            key=lambda candidate: (
                candidate.fiscal_year_start, candidate.id,
            ),
        )[:1]
        if later:
            raise UserError(_(
                "A later fiscal year has already been closed for this "
                "company (%(name)s, year ending %(end)s). Year-end closes "
                "must post oldest-first so each year's equity roll builds "
                "on the prior close; reverse the later close before "
                "posting this one.",
                name=later.name, end=later.fiscal_year_end,
            ))
        # 3. Effective fiscal/hard/parent lock already closes this date.
        lock_date = self._effective_lock_date()
        if lock_date and lock_date >= self.fiscal_year_end:
            raise UserError(_(
                "The fiscal year ending %(end)s is already covered by an "
                "effective fiscal, hard, or inherited parent-company lock "
                "dated %(lock)s. Posting a year-end close into a locked "
                "period is not allowed.",
                end=self.fiscal_year_end,
                lock=lock_date,
            ))

    def _maybe_advance_lock_date(self):
        """Bump res.company.fiscalyear_lock_date to the fiscal year end.

        Idempotent: if the company's lock date is already at or past
        the fiscal year end, do nothing. Done via sudo() because the
        lock date update requires admin-level access; the post-time
        manager guard already gated the call.
        """
        self.ensure_one()
        company = self.company_id
        # _eh_prepare_transition already holds the company row lock. Use ORM
        # write so core lock-date validations, dependent-field invalidation,
        # and any installed audit hooks all run. Narrow sudo is warranted only
        # after the real caller passed run access, company scope, manager, and
        # posting-provenance gates.
        company.invalidate_recordset(['fiscalyear_lock_date'])
        current = company.fiscalyear_lock_date
        if not current or current < self.fiscal_year_end:
            company.sudo().write({
                'fiscalyear_lock_date': self.fiscal_year_end,
            })
            self.message_post(body=_(
                "Fiscal year lock date advanced to %s.",
                self.fiscal_year_end,
            ))
