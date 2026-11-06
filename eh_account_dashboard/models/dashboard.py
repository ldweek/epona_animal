# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.dashboard: per-company financial KPI dashboard.

A single record per (company, user) holds the user's selected period
and serves as the anchor for the form view's KPI tiles. KPIs are
computed on read so the figures are always live; switching the period
recomputes everything in one form refresh.

Design notes:

* Every KPI is one SQL pass against an indexed column. Cash position
  uses the existing MoveLineQuery. Receivables / payables use a
  correlated subquery only on amount_residual. P&L uses MoveLineQuery
  with account-type filters.
* Optional integrations (approval, collections, budget) probe the
  module registry at compute time. If a module is not installed the
  KPI returns 0 and the view hides the tile via the corresponding
  'has_*_module' boolean.
* The model is a normal Model (not Transient) so users can save a
  bookmark to their personal dashboard configuration.
"""

import logging
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

_logger = logging.getLogger(__name__)


_PERIOD_MTD = 'mtd'
_PERIOD_QTD = 'qtd'
_PERIOD_YTD = 'ytd'
_PERIOD_LAST_30 = 'last_30'
_PERIOD_LAST_90 = 'last_90'
_PERIOD_CUSTOM = 'custom'

_INCOME_ACCOUNT_TYPES = ('income', 'income_other')
_EXPENSE_ACCOUNT_TYPES = (
    'expense', 'expense_other', 'expense_depreciation',
    'expense_direct_cost',
)


class EhAccountDashboard(models.Model):
    _name = 'eh.account.dashboard'
    _description = "Financial dashboard"
    _rec_name = 'name'

    name = fields.Char(default='Dashboard', required=True)

    user_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
        index=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True,
    )

    # ---- period selector ----
    period_mode = fields.Selection(
        [
            (_PERIOD_MTD, "Month to date"),
            (_PERIOD_QTD, "Quarter to date"),
            (_PERIOD_YTD, "Year to date"),
            (_PERIOD_LAST_30, "Last 30 days"),
            (_PERIOD_LAST_90, "Last 90 days"),
            (_PERIOD_CUSTOM, "Custom range"),
        ],
        default=_PERIOD_MTD, required=True,
    )
    period_date_from = fields.Date(
        compute='_compute_period_dates', store=True,
        readonly=False,
    )
    period_date_to = fields.Date(
        compute='_compute_period_dates', store=True,
        readonly=False,
    )

    posted_only = fields.Boolean(default=True)

    # ---- always-on KPIs ----
    cash_position = fields.Monetary(compute='_compute_cash_position')
    cash_account_count = fields.Integer(compute='_compute_cash_position')
    cash_journal_count = fields.Integer(compute='_compute_cash_position')

    receivable_total = fields.Monetary(compute='_compute_receivables')
    receivable_overdue = fields.Monetary(compute='_compute_receivables')
    receivable_days_overdue_max = fields.Integer(compute='_compute_receivables')

    payable_total = fields.Monetary(compute='_compute_payables')
    payable_overdue = fields.Monetary(compute='_compute_payables')

    period_revenue = fields.Monetary(compute='_compute_period_pl')
    period_expense = fields.Monetary(compute='_compute_period_pl')
    period_net = fields.Monetary(compute='_compute_period_pl')

    # ---- document counts (draft backlog + late dunning surface) ----
    draft_invoice_count = fields.Integer(
        compute='_compute_document_counts',
        help="Customer invoices still in draft.",
    )
    late_invoice_count = fields.Integer(
        compute='_compute_document_counts',
        help="Posted customer invoices past their due date and not "
             "fully paid.",
    )
    draft_bill_count = fields.Integer(
        compute='_compute_document_counts',
        help="Vendor bills still in draft.",
    )
    late_bill_count = fields.Integer(
        compute='_compute_document_counts',
        help="Posted vendor bills past their due date and not fully paid.",
    )

    # ---- ledger integrity signals ----
    sequence_hole_count = fields.Integer(
        compute='_compute_integrity_signals',
        help="Gaps in the posted-entry numbering per journal sequence "
             "prefix. A non-zero value means a posted number is missing "
             "(deleted or reset), which auditors flag.",
    )
    unhashed_entry_count = fields.Integer(
        compute='_compute_integrity_signals',
        help="Posted entries with no inalterability hash on journals that "
             "require secure posting.",
    )

    # ---- bank operations backlog ----
    to_reconcile_count = fields.Integer(
        compute='_compute_bank_ops',
        help="Bank/cash statement lines not yet reconciled.",
    )
    to_check_count = fields.Integer(
        compute='_compute_bank_ops',
        help="Posted entries flagged for review (not yet checked).",
    )

    # ---- optional KPIs ----
    has_approval_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    pending_approval_count = fields.Integer(
        compute='_compute_pending_approvals',
    )

    has_collections_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    active_collections_count = fields.Integer(
        compute='_compute_active_collections',
    )
    active_collections_total = fields.Monetary(
        compute='_compute_active_collections',
    )

    has_budget_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    active_budget_count = fields.Integer(
        compute='_compute_active_budgets',
    )
    overrun_budget_count = fields.Integer(
        compute='_compute_active_budgets',
    )

    # ---- continuous control monitoring ----
    has_credit_limit_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    credit_limit_breach_count = fields.Integer(
        compute='_compute_control_signals',
    )
    credit_limit_override_count_30d = fields.Integer(
        compute='_compute_control_signals',
    )

    has_sepa_dd_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    dormant_mandate_count = fields.Integer(
        compute='_compute_control_signals',
        help="Mandates approaching the 36-month dormancy cliff (>33 months idle).",
    )

    has_year_end_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    open_year_end_run_count = fields.Integer(
        compute='_compute_control_signals',
    )

    has_close_workflow = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    in_progress_close_count = fields.Integer(
        compute='_compute_control_signals',
    )

    has_fx_module = fields.Boolean(
        compute='_compute_has_optional_modules',
    )
    pending_fx_run_count = fields.Integer(
        compute='_compute_control_signals',
    )

    control_signal_total = fields.Integer(
        compute='_compute_control_signals',
        help="Sum of every control-signal counter; the headline figure.",
    )

    @api.depends('company_id')
    def _compute_has_optional_modules(self):
        # Probe the registry rather than ir.module.module so the result
        # is consistent with what the env actually exposes; a module
        # can be 'installed' in ir.module.module but absent from the
        # registry during a partial reload.
        for rec in self:
            rec.has_approval_module = 'eh.approval.policy' in self.env
            rec.has_collections_module = 'eh.collections.case' in self.env
            rec.has_budget_module = 'eh.budget.budget' in self.env
            rec.has_credit_limit_module = (
                'eh.credit.override.log' in self.env
            )
            rec.has_sepa_dd_module = 'eh.sepa.mandate' in self.env
            rec.has_year_end_module = 'eh.year.end.run' in self.env
            rec.has_close_workflow = 'eh.close.run' in self.env
            rec.has_fx_module = 'eh.fx.revaluation.run' in self.env

    # ---- period dates ----

    @api.depends('period_mode')
    def _compute_period_dates(self):
        for rec in self:
            today = fields.Date.context_today(rec)
            if rec.period_mode == _PERIOD_MTD:
                rec.period_date_from = today.replace(day=1)
                rec.period_date_to = today
            elif rec.period_mode == _PERIOD_QTD:
                quarter_first_month = ((today.month - 1) // 3) * 3 + 1
                rec.period_date_from = today.replace(
                    month=quarter_first_month, day=1,
                )
                rec.period_date_to = today
            elif rec.period_mode == _PERIOD_YTD:
                rec.period_date_from = today.replace(month=1, day=1)
                rec.period_date_to = today
            elif rec.period_mode == _PERIOD_LAST_30:
                rec.period_date_from = today - timedelta(days=30)
                rec.period_date_to = today
            elif rec.period_mode == _PERIOD_LAST_90:
                rec.period_date_from = today - timedelta(days=90)
                rec.period_date_to = today
            elif rec.period_mode == _PERIOD_CUSTOM:
                # Leave the dates alone; the user picks them.
                if not rec.period_date_from:
                    rec.period_date_from = today.replace(day=1)
                if not rec.period_date_to:
                    rec.period_date_to = today

    @api.constrains(
        'period_mode', 'period_date_from', 'period_date_to',
    )
    def _check_custom_period_order(self):
        """Keep direct ORM/form writes under the same range invariant as RPC.

        ``update_period`` validates its payload for the Owl board, but the
        stored form fields are also writable.  Enforce the invariant at the
        model boundary so imports and direct writes cannot persist a reversed
        custom range.
        """
        for rec in self:
            if (
                rec.period_mode == _PERIOD_CUSTOM
                and rec.period_date_from
                and rec.period_date_to
                and rec.period_date_from > rec.period_date_to
            ):
                raise ValidationError(_(
                    "The custom dashboard start date must be on or before "
                    "the end date."
                ))

    def _eh_refresh_relative_period_window(self):
        """Advance stored relative windows before serving live figures.

        Stored computed dates do not wake up at midnight. Dashboard refresh
        RPCs are its clock, so recompute every non-custom window first and
        invalidate dependent non-stored KPIs when either bound advances.
        """
        relative = self.filtered(
            lambda record: record.period_mode != _PERIOD_CUSTOM
        )
        before = {
            record.id: (record.period_date_from, record.period_date_to)
            for record in relative
        }
        relative._compute_period_dates()
        changed = relative.filtered(
            lambda record: before.get(record.id) != (
                record.period_date_from, record.period_date_to,
            )
        )
        if changed:
            changed.flush_recordset(['period_date_from', 'period_date_to'])
            changed.invalidate_recordset()
        return changed

    # ---- always-on KPIs ----

    @api.depends('company_id', 'posted_only')
    def _compute_cash_position(self):
        for rec in self:
            today = fields.Date.context_today(rec)
            Account = self.env['account.account']
            company_field = (
                'company_ids' if 'company_ids' in Account._fields
                else 'company_id'
            )
            cash_accounts = Account.search([
                (company_field, 'in', [rec.company_id.id]),
                ('account_type', '=', 'asset_cash'),
            ])
            rec.cash_account_count = len(cash_accounts)
            # Compatibility alias retained for callers of the pre-1.5.4
            # snapshot.  UI and current payload use cash_account_count because
            # the scalar is an asset_cash account balance, not a journal sum.
            rec.cash_journal_count = len(cash_accounts)
            # Cash position = balance of the liquidity (asset_cash) ACCOUNTS,
            # NOT the sum of a cash journal's move lines. Every journal entry
            # is balanced, so SUM(balance) over all of a bank/cash journal's
            # lines is structurally zero; the real figure is the balance
            # sitting on the cash/bank accounts. account_type asset_cash also
            # matches the prior-period delta baseline
            # (_eh_sum_balance_cumulative_to(('asset_cash',))), so the KPI and
            # its delta agree instead of comparing a broken 0 to a real prior.
            query = MoveLineQuery(
                self.env, company_ids=[rec.company_id.id],
            )
            query.where_account_types(['asset_cash'])
            query.where_date_range(date_to=today)
            if rec.posted_only:
                query.where_posted_only()
            query.select(SQL("SUM(aml.balance)"), 'balance')
            rows = query.execute()
            rec.cash_position = float(
                rows[0].get('balance') or 0.0,
            ) if rows else 0.0

    @api.depends('company_id', 'posted_only')
    def _compute_receivables(self):
        for rec in self:
            today = fields.Date.context_today(rec)
            base_domain = [
                ('company_id', '=', rec.company_id.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('amount_residual', '!=', 0),
            ]
            if rec.posted_only:
                base_domain.append(('parent_state', '=', 'posted'))
            AML = self.env['account.move.line']
            # One SQL pass for the total via _read_group; no Python
            # materialisation of every open AR line.
            total_rows = read_group_compat(AML, 
                base_domain, [], ['amount_residual:sum'],
            )
            rec.receivable_total = total_rows[0][0] if total_rows else 0.0
            # One SQL pass for overdue: domain narrows by date.
            overdue_domain = base_domain + [
                ('date_maturity', '<', today),
                ('date_maturity', '!=', False),
            ]
            overdue_rows = read_group_compat(AML, 
                overdue_domain, [], ['amount_residual:sum', 'date_maturity:min'],
            )
            if overdue_rows and overdue_rows[0][0]:
                rec.receivable_overdue = overdue_rows[0][0]
                oldest = overdue_rows[0][1]
                rec.receivable_days_overdue_max = (today - oldest).days if oldest else 0
            else:
                rec.receivable_overdue = 0.0
                rec.receivable_days_overdue_max = 0

    @api.depends('company_id', 'posted_only')
    def _compute_payables(self):
        for rec in self:
            today = fields.Date.context_today(rec)
            base_domain = [
                ('company_id', '=', rec.company_id.id),
                ('account_id.account_type', '=', 'liability_payable'),
                ('amount_residual', '!=', 0),
            ]
            if rec.posted_only:
                base_domain.append(('parent_state', '=', 'posted'))
            AML = self.env['account.move.line']
            total_rows = read_group_compat(AML, 
                base_domain, [], ['amount_residual:sum'],
            )
            # Payable balances are stored signed (credit-side negative);
            # absolute value is what the user expects to see.
            rec.payable_total = abs(total_rows[0][0] if total_rows else 0.0)
            overdue_domain = base_domain + [
                ('date_maturity', '<', today),
                ('date_maturity', '!=', False),
            ]
            overdue_rows = read_group_compat(AML, 
                overdue_domain, [], ['amount_residual:sum'],
            )
            rec.payable_overdue = abs(overdue_rows[0][0] if overdue_rows and overdue_rows[0][0] else 0.0)

    @api.depends(
        'company_id', 'posted_only',
        'period_date_from', 'period_date_to',
    )
    def _compute_period_pl(self):
        for rec in self:
            df = rec.period_date_from
            dt = rec.period_date_to
            if not df or not dt:
                rec.period_revenue = 0.0
                rec.period_expense = 0.0
                rec.period_net = 0.0
                continue
            # Revenue: account_type IN ('income', 'income_other').
            # Expense: account_type IN ('expense', 'expense_other',
            # 'expense_depreciation', 'expense_direct_cost').
            # Income balances are credit-side (negative balance); flip
            # sign to display positively.
            rev_query = MoveLineQuery(
                self.env, company_ids=[rec.company_id.id],
            )
            rev_query.where_account_types(_INCOME_ACCOUNT_TYPES)
            rev_query.where_date_range(date_from=df, date_to=dt)
            rec._eh_apply_operational_move_scope(rev_query)
            if rec.posted_only:
                rev_query.where_posted_only()
            rev_query.select(SQL("SUM(aml.balance)"), 'balance')
            rev_rows = rev_query.execute()
            rec.period_revenue = -float(
                rev_rows[0].get('balance') or 0.0,
            ) if rev_rows else 0.0

            exp_query = MoveLineQuery(
                self.env, company_ids=[rec.company_id.id],
            )
            exp_query.where_account_types(_EXPENSE_ACCOUNT_TYPES)
            exp_query.where_date_range(date_from=df, date_to=dt)
            rec._eh_apply_operational_move_scope(exp_query)
            if rec.posted_only:
                exp_query.where_posted_only()
            exp_query.select(SQL("SUM(aml.balance)"), 'balance')
            exp_rows = exp_query.execute()
            rec.period_expense = float(
                exp_rows[0].get('balance') or 0.0,
            ) if exp_rows else 0.0

            rec.period_net = rec.period_revenue - rec.period_expense

    # ---- optional KPIs ----

    @api.depends('company_id')
    def _compute_document_counts(self):
        """Draft backlog and overdue (late) counts for invoices and bills.

        Late = posted, past due date, and not fully paid. Drives the
        'work waiting' tiles and the dunning surface on the board.
        """
        Move = self.env['account.move']
        today = fields.Date.context_today(self)
        for rec in self:
            base = [('company_id', '=', rec.company_id.id)]
            late = [
                ('state', '=', 'posted'),
                ('payment_state', 'in', ('not_paid', 'partial')),
                ('invoice_date_due', '<', today),
            ]
            rec.draft_invoice_count = Move.search_count(
                base + [('move_type', '=', 'out_invoice'),
                        ('state', '=', 'draft')])
            rec.draft_bill_count = Move.search_count(
                base + [('move_type', '=', 'in_invoice'),
                        ('state', '=', 'draft')])
            rec.late_invoice_count = Move.search_count(
                base + [('move_type', '=', 'out_invoice')] + late)
            rec.late_bill_count = Move.search_count(
                base + [('move_type', '=', 'in_invoice')] + late)

    @api.depends('company_id')
    def _compute_bank_ops(self):
        """Reconciliation and review backlog counts. The 'checked' review
        flag is probed for existence so the dashboard tolerates Odoo
        builds where the field is absent."""
        StatementLine = self.env['account.bank.statement.line']
        Move = self.env['account.move']
        has_checked = 'checked' in Move._fields
        for rec in self:
            company = rec.company_id
            rec.to_reconcile_count = StatementLine.search_count([
                ('company_id', '=', company.id),
                ('is_reconciled', '=', False),
            ])
            if has_checked:
                rec.to_check_count = Move.search_count([
                    ('company_id', '=', company.id),
                    ('state', '=', 'posted'),
                    ('checked', '=', False),
                ])
            else:
                rec.to_check_count = 0

    @api.depends('company_id')
    def _compute_integrity_signals(self):
        """Posted-sequence gaps and unhashed secure entries.

        Holes are derived per (journal, sequence prefix) as
        (max - min + 1) - count over posted numbers, so a deleted or
        reset posting in the middle of a run surfaces. Unhashed counts
        posted entries lacking an inalterability hash on journals that
        require one. Both probe SQL aggregates, not per-record loops.
        """
        Move = self.env['account.move']
        Journal = self.env['account.journal']
        for rec in self:
            company = rec.company_id
            groups = read_group_compat(Move, 
                [('company_id', '=', company.id),
                 ('state', '=', 'posted'),
                 ('sequence_prefix', '!=', False)],
                groupby=['journal_id', 'sequence_prefix'],
                aggregates=['sequence_number:max', 'sequence_number:min',
                            '__count'],
            )
            holes = 0
            for _journal, _prefix, seq_max, seq_min, count in groups:
                if seq_max is not None and seq_min is not None:
                    holes += max(0, (seq_max - seq_min + 1) - count)
            rec.sequence_hole_count = holes

            hash_journals = Journal.search([
                ('company_id', '=', company.id),
                ('restrict_mode_hash_table', '=', True),
            ])
            unhashed = 0
            if hash_journals:
                unhashed = Move.search_count([
                    ('company_id', '=', company.id),
                    ('state', '=', 'posted'),
                    ('journal_id', 'in', hash_journals.ids),
                    ('inalterable_hash', '=', False),
                ])
            rec.unhashed_entry_count = unhashed

    @api.depends('has_approval_module', 'company_id')
    def _compute_pending_approvals(self):
        for rec in self:
            if not rec.has_approval_module:
                rec.pending_approval_count = 0
                continue
            rec.pending_approval_count = self.env['eh.approval.request'].search_count([
                ('state', '=', 'in_review'),
                ('company_id', '=', rec.company_id.id),
            ])

    @api.depends('has_collections_module', 'company_id')
    def _compute_active_collections(self):
        for rec in self:
            if not rec.has_collections_module:
                rec.active_collections_count = 0
                rec.active_collections_total = 0.0
                continue
            # One SQL aggregation (count + sum) instead of loading every
            # open case into a recordset to count and sum in Python, which
            # is wasteful for a company with a large collections backlog.
            rows = read_group_compat(self.env['eh.collections.case'], 
                [
                    ('is_resolved', '=', False),
                    ('company_id', '=', rec.company_id.id),
                ],
                [],
                ['__count', 'total_overdue_amount:sum'],
            )
            count, total = rows[0] if rows else (0, 0.0)
            rec.active_collections_count = count or 0
            rec.active_collections_total = total or 0.0

    @api.depends('has_budget_module', 'company_id')
    def _compute_active_budgets(self):
        for rec in self:
            if not rec.has_budget_module:
                rec.active_budget_count = 0
                rec.overrun_budget_count = 0
                continue
            today = fields.Date.context_today(rec)
            active = self.env['eh.budget.budget'].search([
                ('state', '=', 'confirmed'),
                ('company_id', '=', rec.company_id.id),
                ('date_from', '<=', today),
                ('date_to', '>=', today),
            ])
            rec.active_budget_count = len(active)
            rec.overrun_budget_count = len(active.filtered(
                lambda b: b.total_actual > b.total_budgeted,
            ))

    @api.depends(
        'has_credit_limit_module', 'has_sepa_dd_module',
        'has_year_end_module', 'has_close_workflow', 'has_fx_module',
        'has_approval_module', 'has_budget_module',
        'company_id',
    )
    def _compute_control_signals(self):
        """Aggregate every continuous-control counter for the
        executive read.

        Each signal probes the registry first so a partial install does
        not raise AttributeError; absent modules contribute zero. The
        result is the headline `control_signal_total` plus the
        per-source breakdown the form view shows.
        """
        for rec in self:
            company = rec.company_id
            today = fields.Date.context_today(rec)

            credit_breaches = 0
            credit_overrides = 0
            if rec.has_credit_limit_module:
                # Credit policy itself (when set) defines the partner
                # breach predicate; the override log lists managerial
                # waivers in the last 30 days.
                CreditLog = self.env['eh.credit.override.log']
                credit_overrides = CreditLog.search_count([
                    ('company_id', '=', company.id),
                    ('create_date', '>=', today - timedelta(days=30)),
                ])
                # Partners flagged as over-limit live on res.partner;
                # we count partners whose total residual exceeds their
                # configured limit. This is a Python aggregate so it
                # tolerates multi-currency scopes; it is only run once
                # per dashboard render.
                Partner = self.env['res.partner'].with_company(
                    company,
                ).with_context(allowed_company_ids=[company.id])
                if 'eh_credit_limit' in Partner._fields:
                    over = Partner.search([
                        ('eh_credit_limit', '>', 0),
                        ('parent_id', '=', False),
                        '|',
                        ('company_id', '=', False),
                        ('company_id', '=', company.id),
                    ])
                    credit_breaches = len(over.filtered(
                        lambda p: (
                            p.credit and p.credit > p.eh_credit_limit
                        ),
                    ))
            rec.credit_limit_breach_count = credit_breaches
            rec.credit_limit_override_count_30d = credit_overrides

            dormant = 0
            if rec.has_sepa_dd_module:
                # Approaching the 36-month dormancy cliff: last
                # collection 33+ months ago and still active.
                Mandate = self.env['eh.sepa.mandate']
                cutoff = today - timedelta(days=33 * 30)
                dormant = Mandate.search_count([
                    ('state', '=', 'active'),
                    ('last_collection_date', '!=', False),
                    ('last_collection_date', '<', cutoff),
                    ('company_id', '=', company.id),
                ])
            rec.dormant_mandate_count = dormant

            year_end_open = 0
            if rec.has_year_end_module:
                year_end_open = self.env['eh.year.end.run'].search_count([
                    ('state', 'in', ('draft', 'computed')),
                    ('company_id', '=', company.id),
                ])
            rec.open_year_end_run_count = year_end_open

            close_open = 0
            if rec.has_close_workflow:
                close_open = self.env['eh.close.run'].search_count([
                    ('state', 'in', ('in_progress', 'pending_approval')),
                    ('company_id', '=', company.id),
                ])
            rec.in_progress_close_count = close_open

            fx_pending = 0
            if rec.has_fx_module:
                fx_pending = self.env['eh.fx.revaluation.run'].search_count([
                    ('state', 'in', ('draft', 'computed')),
                    ('company_id', '=', company.id),
                ])
            rec.pending_fx_run_count = fx_pending

            rec.control_signal_total = (
                rec.pending_approval_count
                + rec.active_collections_count
                + rec.overrun_budget_count
                + credit_breaches
                + credit_overrides
                + dormant
                + year_end_open
                + close_open
                + fx_pending
            )

    # ---- actions ----

    def action_refresh(self):
        self.ensure_one()
        self._eh_refresh_relative_period_window()
        # Client reload starts a fresh request/environment, so invalidating a
        # partial field list here only burns work in the transaction that is
        # about to end.  Relative stored dates above are the sole stateful step.
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_drilldown_receivables(self):
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('amount_residual', '!=', 0),
        ]
        if self.posted_only:
            domain.append(('parent_state', '=', 'posted'))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Open Receivables"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
        }

    def action_drilldown_payables(self):
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('account_id.account_type', '=', 'liability_payable'),
            ('amount_residual', '!=', 0),
        ]
        if self.posted_only:
            domain.append(('parent_state', '=', 'posted'))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Open Payables"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
        }

    def action_drilldown_cash(self):
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('account_id.account_type', '=', 'asset_cash'),
            ('date', '<=', fields.Date.context_today(self)),
        ]
        if self.posted_only:
            domain.append(('parent_state', '=', 'posted'))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Cash-account journal items"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {'search_default_group_account': 1},
        }

    def _eh_invoice_drilldown(self, name, domain):
        """Shared helper for the document-backlog drilldowns."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('company_id', '=', self.company_id.id)] + domain,
        }

    def action_drilldown_late_invoices(self):
        """Posted customer invoices past due and not fully paid."""
        return self._eh_invoice_drilldown(_("Overdue customer invoices"), [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('invoice_date_due', '<', fields.Date.context_today(self)),
        ])

    def action_drilldown_late_bills(self):
        """Posted vendor bills past due and not fully paid."""
        return self._eh_invoice_drilldown(_("Overdue vendor bills"), [
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
            ('invoice_date_due', '<', fields.Date.context_today(self)),
        ])

    def action_drilldown_draft_invoices(self):
        """Customer invoices still in draft (the billing backlog)."""
        return self._eh_invoice_drilldown(_("Draft customer invoices"), [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'draft'),
        ])

    def action_drilldown_draft_bills(self):
        """Vendor bills still in draft (the entry backlog)."""
        return self._eh_invoice_drilldown(_("Draft vendor bills"), [
            ('move_type', '=', 'in_invoice'),
            ('state', '=', 'draft'),
        ])

    def action_drilldown_to_reconcile(self):
        """Bank statement lines not yet reconciled."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Statement lines to reconcile"),
            'res_model': 'account.bank.statement.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('is_reconciled', '=', False),
            ],
        }

    def action_drilldown_to_check(self):
        """Posted entries flagged for review (checked = false)."""
        self.ensure_one()
        if 'checked' not in self.env['account.move']._fields:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Entries to review"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('state', '=', 'posted'),
                ('checked', '=', False),
            ],
        }

    def action_drilldown_pending_approvals(self):
        self.ensure_one()
        if not self.has_approval_module:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Pending Approvals"),
            'res_model': 'eh.approval.request',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', '=', 'in_review'),
                ('company_id', '=', self.company_id.id),
            ],
        }

    def action_drilldown_active_collections(self):
        self.ensure_one()
        if not self.has_collections_module:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Active Collections Cases"),
            'res_model': 'eh.collections.case',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [
                ('is_resolved', '=', False),
                ('company_id', '=', self.company_id.id),
            ],
        }

    def action_drilldown_overrun_budgets(self):
        self.ensure_one()
        if not self.has_budget_module:
            return False
        today = fields.Date.context_today(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Active Budgets"),
            'res_model': 'eh.budget.budget',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', '=', 'confirmed'),
                ('company_id', '=', self.company_id.id),
                ('date_from', '<=', today),
                ('date_to', '>=', today),
            ],
        }

    def action_drilldown_credit_overrides(self):
        """Open the exact rolling 30-day set counted by the control tile."""
        self.ensure_one()
        if not self.has_credit_limit_module:
            return False
        today = fields.Date.context_today(self)
        domain = [
            ('company_id', '=', self.company_id.id),
            ('create_date', '>=', today - timedelta(days=30)),
        ]
        return {
            'type': 'ir.actions.act_window',
            'name': _("Credit-limit overrides (last 30 days)"),
            'res_model': 'eh.credit.override.log',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
        }

    def action_drilldown_period_revenue(self):
        """Open posted revenue journal items within the dashboard period."""
        self.ensure_one()
        return self._eh_period_pl_drilldown(account_type='income')

    def action_drilldown_period_expense(self):
        """Open posted expense journal items within the dashboard period."""
        self.ensure_one()
        return self._eh_period_pl_drilldown(account_type='expense')

    def _eh_period_pl_drilldown(self, account_type):
        """Build an act_window for P&L journal-item drill.

        Filters by company, account_type, posted state, and the
        dashboard period date range. Used by both the revenue and
        expense tile buttons so the period scoping logic stays
        in one place.
        """
        self.ensure_one()
        account_types = {
            'income': _INCOME_ACCOUNT_TYPES,
            'expense': _EXPENSE_ACCOUNT_TYPES,
        }.get(account_type, (account_type,))
        domain = [
            ('company_id', '=', self.company_id.id),
            ('account_id.account_type', 'in', account_types),
        ]
        if self.period_date_from:
            domain.append(('date', '>=', self.period_date_from))
        if self.period_date_to:
            domain.append(('date', '<=', self.period_date_to))
        if self.posted_only:
            domain.append(('parent_state', '=', 'posted'))
        excluded_move_ids = self._eh_operational_excluded_move_ids()
        if excluded_move_ids:
            domain.append(('move_id', 'not in', excluded_move_ids))
        title_map = {
            'income': _("Revenue (period)"),
            'expense': _("Expense (period)"),
        }
        return {
            'type': 'ir.actions.act_window',
            'name': title_map.get(account_type, _("Period detail")),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {
                'search_default_group_account': 1,
            },
        }

    def action_drilldown_dormant_mandates(self):
        self.ensure_one()
        if not self.has_sepa_dd_module:
            return False
        cutoff = fields.Date.context_today(self) - timedelta(days=33 * 30)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Dormant mandates (>33 months idle)"),
            'res_model': 'eh.sepa.mandate',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', '=', 'active'),
                ('last_collection_date', '!=', False),
                ('last_collection_date', '<', cutoff),
                ('company_id', '=', self.company_id.id),
            ],
        }

    def action_drilldown_open_year_end(self):
        self.ensure_one()
        if not self.has_year_end_module:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Year-end runs in progress"),
            'res_model': 'eh.year.end.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', 'in', ('draft', 'computed')),
                ('company_id', '=', self.company_id.id),
            ],
        }

    def action_drilldown_open_close_runs(self):
        self.ensure_one()
        if not self.has_close_workflow:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("Period-close runs in progress"),
            'res_model': 'eh.close.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', 'in', ('in_progress', 'pending_approval')),
                ('company_id', '=', self.company_id.id),
            ],
        }

    def action_drilldown_pending_fx(self):
        self.ensure_one()
        if not self.has_fx_module:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': _("FX revaluation runs pending"),
            'res_model': 'eh.fx.revaluation.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('state', 'in', ('draft', 'computed')),
                ('company_id', '=', self.company_id.id),
            ],
        }

    @api.model
    def open_for_current_user(self):
        """Open (or create) the dashboard record for the current user.

        Routes to the Owl client action so each user gets the live,
        reactive dashboard. The legacy form view is still reachable via
        the action menu under Settings, but the default entry point is
        the Owl board because it auto-refreshes, draws the cash trend
        sparkline, and renders KPI deltas as colour-coded badges.
        """
        record = self._eh_get_or_create_for_current()
        return {
            'type': 'ir.actions.client',
            'tag': 'eh_account_dashboard.board',
            'name': _("Financial Dashboard"),
            'target': 'current',
            'context': {
                'eh_dashboard_id': record.id,
            },
        }

    @api.model
    def open_form_for_current_user(self):
        """Legacy form-view entry point.

        Kept for users who want to bookmark or directly edit the
        underlying record (period preset, custom dates, posted-only
        flag). The Owl board is the default; this is the escape hatch.
        """
        record = self._eh_get_or_create_for_current()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Financial Dashboard (form)"),
            'res_model': self._name,
            'res_id': record.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }

    @api.model
    def _eh_get_or_create_for_current(self):
        """Single-source helper: one dashboard record per (user, company)."""
        existing = self.search(
            [
                ('user_id', '=', self.env.user.id),
                ('company_id', '=', self.env.company.id),
            ],
            limit=1,
        )
        if existing:
            return existing
        return self.create({
            'user_id': self.env.user.id,
            'company_id': self.env.company.id,
        })

    # ---- Owl board RPC ----

    def get_dashboard_snapshot(self):
        """Return the full dashboard payload as a flat dict.

        The Owl board calls this once on mount and again whenever the
        period changes. Every figure is computed against the record's
        existing fields so the SQL passes already battle-tested by the
        form view stay the source of truth. Module-availability flags
        gate the optional tiles; the JS hides anything tagged absent.
        """
        self.ensure_one()
        self._eh_refresh_relative_period_window()
        currency = self.currency_id or self.company_id.currency_id
        return {
            'record_id': self.id,
            'period': {
                'mode': self.period_mode,
                'date_from': self._iso(self.period_date_from),
                'date_to': self._iso(self.period_date_to),
                'posted_only': bool(self.posted_only),
            },
            'currency': {
                'id': currency.id,
                'name': currency.name,
                'symbol': currency.symbol,
                'position': currency.position,
                'decimal_places': currency.decimal_places,
            },
            'company': {
                'id': self.company_id.id,
                'name': self.company_id.display_name,
            },
            'liquidity': {
                'cash_position': float(self.cash_position or 0.0),
                'cash_account_count': int(self.cash_account_count or 0),
                # Compatibility alias for integrations using the old key.
                'cash_journal_count': int(self.cash_journal_count or 0),
                'receivable_total': float(self.receivable_total or 0.0),
                'receivable_overdue': float(self.receivable_overdue or 0.0),
                'receivable_days_overdue_max':
                    int(self.receivable_days_overdue_max or 0),
                'payable_total': float(self.payable_total or 0.0),
                'payable_overdue': float(self.payable_overdue or 0.0),
            },
            'pnl': {
                'revenue': float(self.period_revenue or 0.0),
                'expense': float(self.period_expense or 0.0),
                'net': float(self.period_net or 0.0),
            },
            'documents': {
                'draft_invoice_count': int(self.draft_invoice_count or 0),
                'late_invoice_count': int(self.late_invoice_count or 0),
                'draft_bill_count': int(self.draft_bill_count or 0),
                'late_bill_count': int(self.late_bill_count or 0),
            },
            'integrity': {
                'sequence_hole_count': int(self.sequence_hole_count or 0),
                'unhashed_entry_count': int(self.unhashed_entry_count or 0),
            },
            'bank_ops': {
                'to_reconcile_count': int(self.to_reconcile_count or 0),
                'to_check_count': int(self.to_check_count or 0),
            },
            'modules': {
                'approval': bool(self.has_approval_module),
                'collections': bool(self.has_collections_module),
                'budget': bool(self.has_budget_module),
                'credit_limit': bool(self.has_credit_limit_module),
                'sepa_dd': bool(self.has_sepa_dd_module),
                'close_workflow': bool(self.has_close_workflow),
                'year_end': bool(self.has_year_end_module),
                'fx': bool(self.has_fx_module),
            },
            'operations': {
                'pending_approval_count':
                    int(self.pending_approval_count or 0),
                'active_collections_count':
                    int(self.active_collections_count or 0),
                'active_collections_total':
                    float(self.active_collections_total or 0.0),
                'active_budget_count': int(self.active_budget_count or 0),
                'overrun_budget_count': int(self.overrun_budget_count or 0),
            },
            'controls': {
                'total': int(self.control_signal_total or 0),
                'credit_limit_breach_count':
                    int(self.credit_limit_breach_count or 0),
                'credit_limit_override_count_30d':
                    int(self.credit_limit_override_count_30d or 0),
                'dormant_mandate_count':
                    int(self.dormant_mandate_count or 0),
                'in_progress_close_count':
                    int(self.in_progress_close_count or 0),
                'open_year_end_run_count':
                    int(self.open_year_end_run_count or 0),
                'pending_fx_run_count':
                    int(self.pending_fx_run_count or 0),
            },
            'cash_trend': self._eh_cash_trend_series(days=30),
            'revenue_trend': self._eh_pl_trend_series(
                'income', days=30,
            ),
            'expense_trend': self._eh_pl_trend_series(
                'expense', days=30,
            ),
            'deltas': self._eh_compute_prior_period_deltas(),
        }

    def update_period(self, period_mode, date_from=None, date_to=None,
                      posted_only=True):
        """Persist a period selector change from the Owl board.

        The board sends the user's pick here; we update the record so
        the next snapshot reflects the new window. Custom mode requires
        explicit date_from / date_to; the other modes derive their dates
        from the existing period compute.

        After the write we explicitly invalidate the recordset so every
        non-stored compute (cash position, receivables, payables,
        period P&L, operations + controls counts) re-runs on the next
        read in get_dashboard_snapshot. Without the invalidation the
        env transaction cache can keep returning the pre-toggle values
        when the user flips the Posted-only checkbox or switches the
        period mode and switches back.
        """
        self.ensure_one()
        allowed_modes = {
            _PERIOD_MTD, _PERIOD_QTD, _PERIOD_YTD, _PERIOD_LAST_30,
            _PERIOD_LAST_90, _PERIOD_CUSTOM,
        }
        if period_mode not in allowed_modes:
            raise ValidationError(_("Select a valid dashboard period."))
        vals = {
            'period_mode': period_mode,
            'posted_only': bool(posted_only),
        }
        if period_mode == _PERIOD_CUSTOM:
            if not (date_from and date_to):
                raise ValidationError(_(
                    "Select both a start date and an end date for the "
                    "custom dashboard range."
                ))
            date_from = fields.Date.to_date(date_from)
            date_to = fields.Date.to_date(date_to)
            if date_from > date_to:
                raise ValidationError(_(
                    "The custom dashboard start date must be on or before "
                    "the end date."
                ))
            vals['period_date_from'] = date_from
            vals['period_date_to'] = date_to
        self.write(vals)
        self.invalidate_recordset()
        return self.get_dashboard_snapshot()

    def _eh_cash_trend_series(self, days=30):
        """30-day daily cash position series for the sparkline.

        Uses the same MoveLineQuery scoping as the cash KPI so the
        endpoint of the trend matches the cash_position scalar exactly.
        Each point is the cumulative balance on cash journals up to and
        including that day; the series is dense (one point per day).

        Returns a list of {date: ISO, value: float} sorted ascending.
        Tolerates an empty cash-journal set by returning an empty list.
        """
        self.ensure_one()
        company = self.company_id
        Account = self.env['account.account']
        company_field = (
            'company_ids' if 'company_ids' in Account._fields
            else 'company_id'
        )
        if not Account.search_count([
            (company_field, 'in', [company.id]),
            ('account_type', '=', 'asset_cash'),
        ]):
            return []
        end = fields.Date.context_today(self)
        start = end - timedelta(days=max(days - 1, 0))

        # One SQL pass: group SUM(balance) by date inside the window. Sum the
        # liquidity (asset_cash) ACCOUNTS, not the cash journals' lines, for
        # the same reason as the cash KPI: a journal's lines net to zero, so
        # journal-scoped sums produce a flat-zero series.
        query = MoveLineQuery(self.env, company_ids=[company.id])
        query.where_account_types(['asset_cash'])
        query.where_raw(SQL("aml.date <= %s", end))
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.select_field('date')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.date"))
        rows = query.execute()

        # Build a cumulative running balance up to each day in the
        # window. Days strictly before `start` contribute to the opening
        # balance only; days inside the window each add their delta to
        # the running total.
        deltas = {}
        opening = 0.0
        for row in rows:
            d = row.get('date')
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            amount = float(row.get('balance') or 0.0)
            if d < start:
                opening += amount
            else:
                deltas[d] = deltas.get(d, 0.0) + amount

        series = []
        running = opening
        cursor = start
        while cursor <= end:
            running += deltas.get(cursor, 0.0)
            series.append({
                'date': cursor.isoformat(),
                'value': round(running, 2),
            })
            cursor += timedelta(days=1)
        return series

    def _eh_pl_trend_series(self, account_type, days=30):
        """Daily P&L trend series for the sparkline.

        Returns one point per day in the trailing window of the
        requested account_type's signed daily total. Income is sign-
        flipped so revenue plots positive. Expense plots positive.

        :param account_type: 'income' or 'expense'.
        :param days: window length, defaults to 30.
        """
        self.ensure_one()
        company = self.company_id
        end = fields.Date.context_today(self)
        start = end - timedelta(days=max(days - 1, 0))
        account_types = {
            'income': _INCOME_ACCOUNT_TYPES,
            'expense': _EXPENSE_ACCOUNT_TYPES,
        }.get(account_type, (account_type,))
        query = MoveLineQuery(self.env, company_ids=[company.id])
        query.where_account_types(account_types)
        query.where_raw(SQL("aml.date <= %s", end))
        query.where_raw(SQL("aml.date >= %s", start))
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.select_field('date')
        query.select(SQL("SUM(aml.balance)"), 'balance')
        query.group_by(SQL("aml.date"))
        rows = query.execute()
        # income posts as negative balance (credit), expense as positive
        # (debit). Flip income so the sparkline plots a positive trend.
        sign = -1.0 if account_type == 'income' else 1.0
        per_day = {}
        for row in rows:
            d = row.get('date')
            if isinstance(d, str):
                d = date.fromisoformat(d[:10])
            per_day[d] = sign * float(row.get('balance') or 0.0)
        series = []
        cursor = start
        while cursor <= end:
            series.append({
                'date': cursor.isoformat(),
                'value': round(per_day.get(cursor, 0.0), 2),
            })
            cursor += timedelta(days=1)
        return series

    def _eh_compute_prior_period_deltas(self):
        """Return prior-period equivalents and deltas for every KPI.

        The prior period is the same length as the current dashboard
        window, ending immediately before period_date_from. So if the
        current window is May 1 to May 14, the prior is April 17 to
        April 30; if the current window is FY YTD (Jan 1 to May 14),
        the prior is the equivalent slice of the previous fiscal year.

        Each KPI dict carries:
          {'current': float, 'prior': float, 'delta': float, 'pct': float}

        pct is None when prior is zero (no comparable baseline).
        """
        self.ensure_one()
        if not self.period_date_from or not self.period_date_to:
            return {}
        window_days = (self.period_date_to - self.period_date_from).days
        prior_to = self.period_date_from - timedelta(days=1)
        prior_from = prior_to - timedelta(days=max(window_days, 0))

        # P&L deltas
        prior_revenue = self._eh_sum_balance(
            _INCOME_ACCOUNT_TYPES, prior_from, prior_to,
        )
        prior_expense = self._eh_sum_balance(
            _EXPENSE_ACCOUNT_TYPES, prior_from, prior_to,
        )
        prior_net = -prior_revenue - prior_expense
        # Cash position is point-in-time; compare end-of-prior vs today.
        prior_cash = self._eh_sum_balance_cumulative_to(
            ('asset_cash',), prior_to,
        )
        prior_receivable = self._eh_sum_balance_cumulative_to(
            ('asset_receivable',), prior_to,
        )
        prior_payable = self._eh_sum_balance_cumulative_to(
            ('liability_payable',), prior_to,
        )

        return {
            'window': {
                'prior_from': prior_from.isoformat(),
                'prior_to': prior_to.isoformat(),
                'days': window_days,
            },
            'revenue': self._make_delta(
                float(self.period_revenue or 0.0), -prior_revenue,
            ),
            'expense': self._make_delta(
                float(self.period_expense or 0.0), prior_expense,
            ),
            'net': self._make_delta(
                float(self.period_net or 0.0), prior_net,
            ),
            'cash_position': self._make_delta(
                float(self.cash_position or 0.0), prior_cash,
            ),
            'receivable_total': self._make_delta(
                float(self.receivable_total or 0.0), prior_receivable,
            ),
            'payable_total': self._make_delta(
                # payable_total is displayed absolute (see _compute_payables),
                # while prior_payable sums signed (credit-side negative)
                # amount_residual; take abs so the delta compares like for like.
                float(self.payable_total or 0.0), abs(prior_payable),
            ),
        }

    def _eh_sum_balance(self, account_types, date_from, date_to):
        company = self.company_id
        query = MoveLineQuery(self.env, company_ids=[company.id])
        if isinstance(account_types, str):
            account_types = (account_types,)
        query.where_account_types(account_types)
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        return float(rows[0].get('balance') or 0.0) if rows else 0.0

    def _eh_sum_balance_cumulative_to(self, account_types, cutoff_date):
        """Cumulative sum of balances on the given account types up to
        and including cutoff_date.

        For a historical AR/AP point-in-time baseline, sum ledger balance,
        not today's ``amount_residual``. Payments and other clearing entries
        dated after the cutoff are thereby excluded, while payments through
        the cutoff naturally reduce the balance. Filtering current residuals
        would erase invoices that were open then but have since been paid.
        """
        company = self.company_id
        query = MoveLineQuery(self.env, company_ids=[company.id])
        query.where_account_types(account_types)
        query.where_date_range(date_to=cutoff_date)
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        return float(rows[0].get('balance') or 0.0) if rows else 0.0

    def _eh_operational_excluded_move_ids(self):
        """Return proven year-end close/reversal moves for this company.

        Year-end entries deliberately zero and later restore P&L accounts.
        They are ledger-valid, but not operating revenue/expense.  Provenance
        comes from run links/history, never journal names or free-text refs.
        """
        self.ensure_one()
        if 'eh.year.end.run' not in self.env:
            return []
        runs = self.env['eh.year.end.run'].sudo().search([
            ('company_id', '=', self.company_id.id),
        ])
        moves = (
            runs.mapped('move_id')
            | runs.mapped('reversal_move_id')
            | runs.mapped('closing_move_ids')
            | runs.mapped('reversal_move_ids')
        )
        return sorted(set(moves.ids))

    def _eh_apply_operational_move_scope(self, query):
        """Exclude engine-proven close/reversal entries from a KPI query."""
        self.ensure_one()
        if 'eh.year.end.run' in self.env:
            # Keep provenance filtering inside each aggregate instead of first
            # issuing a separate ORM search for every P&L/trend/ratio pass.  All
            # four links are engine-owned and company-scoped; free-text move
            # labels never classify an entry as a close.
            query.where_raw(SQL(
                "NOT EXISTS ("
                "SELECT 1 FROM eh_year_end_run dashboard_yer "
                "WHERE dashboard_yer.company_id = %s AND ("
                "dashboard_yer.move_id = aml.move_id "
                "OR dashboard_yer.reversal_move_id = aml.move_id "
                "OR EXISTS ("
                "SELECT 1 FROM eh_year_end_run_closing_move_rel close_rel "
                "WHERE close_rel.run_id = dashboard_yer.id "
                "AND close_rel.move_id = aml.move_id"
                ") OR EXISTS ("
                "SELECT 1 FROM eh_year_end_run_reversal_move_rel reverse_rel "
                "WHERE reverse_rel.run_id = dashboard_yer.id "
                "AND reverse_rel.move_id = aml.move_id"
                ")"
                "))",
                self.company_id.id,
            ))
        return query

    @staticmethod
    def _make_delta(current, prior):
        """Return a {current, prior, delta, pct} dict.

        pct is None when prior is zero (avoids divide-by-zero and
        signals "no comparable baseline" to the renderer).
        """
        delta = current - prior
        pct = None
        if prior:
            pct = round((delta / abs(prior)) * 100.0, 2)
        return {
            'current': round(current, 2),
            'prior': round(prior, 2),
            'delta': round(delta, 2),
            'pct': pct,
        }

    @staticmethod
    def _iso(value):
        return value.isoformat() if hasattr(value, 'isoformat') else (value or False)
