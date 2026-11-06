# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.run: one consolidation pass for one period.

Lifecycle:

  draft -> computed -> reviewed -> closed
                    \\-> draft (manager reset)

draft: parameters being entered.
computed: per-member trial balances pulled, translated, summed; CTA
booked; NCI carved; eliminations applied. Lines on consol_run_line.
reviewed: a manager has signed off; eliminations are locked.
closed: the run is read-only and cited in audit.
"""

import logging
import math

from collections import defaultdict
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_is_zero, float_round

from odoo.addons.eh_account_base.tools.orm_compat import grouped_sum

from .consol_run_line import CONSOL_ENGINE_CAPABILITY, CONSOL_ENGINE_CTX


def _acc_company_field(env):
    # account.account is multi-company (company_ids) from Odoo 18;
    # single company_id before that.
    Account = env['account.account']
    return 'company_ids' if 'company_ids' in Account._fields else 'company_id'


_logger = logging.getLogger(__name__)


_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = (
    'expense', 'expense_other', 'expense_depreciation',
    'expense_direct_cost',
)
_EQUITY_TYPES = ('equity', 'equity_unaffected')


# States in which the run's consolidated figures back a posted / audited GL
# figure and are frozen at the ORM write layer.
_POSTED_STATES = ('computed', 'reviewed', 'closed')

# Identity fields that fix which entity and period a run consolidates. Once the
# run is computed / reviewed / closed they are frozen at the write layer; change
# them by resetting to draft. 'state' is deliberately NOT listed: the action
# methods write only state + audit stamps, so a pure state-transition write
# carries no frozen field and always passes.
_FROZEN_AFTER_CONFIRM = (
    'entity_id', 'period_from', 'period_to',
    'override_policy_checks', 'override_policy_reason',
    'notes',
)

# Kinds whose amounts are genuine consolidated-figure legs; the CTA plug is
# their negated sum. 'impairment' and 'cta_recycle' are excluded: they are
# balanced pairs added AFTER compute and never disturb the plug.
_CTA_BASE_KINDS = (
    'subsidiary_balance', 'parent_balance', 'elimination',
    'equity_pickup', 'nci', 'goodwill', 'bargain_purchase',
)

# IFRS 10.B93: the gap between a member's own reporting date and the group's
# may never exceed three months.
_MAX_REPORTING_OFFSET_MONTHS = 3

# Goodwill impairment-test fields. These are legitimately set by the manager
# during the IAS 36 impairment test, which runs on a computed / reviewed run
# BEFORE the consolidation move is posted (action_impair_goodwill refuses once
# the move is posted). They therefore freeze only once the consolidation move
# is posted, at which point goodwill_impairment_amount is a booked GL figure.
_FROZEN_AFTER_POSTED_MOVE = (
    'goodwill_recoverable_amount', 'goodwill_impairment_amount',
    'goodwill_impairment_account_id', 'goodwill_recoverable_confirmed',
)

_MANAGER_ONLY_INPUT_FIELDS = frozenset({
    'goodwill_recoverable_amount', 'goodwill_impairment_account_id',
    'goodwill_recoverable_confirmed',
    'override_policy_checks', 'override_policy_reason',
    'nci_period_profit_input', 'nci_period_oci_input',
    'nci_period_flow_confirmed', 'nci_period_flow_basis',
})

_NCI_PERIOD_FLOW_INPUT_FIELDS = frozenset({
    'nci_period_profit_input', 'nci_period_oci_input',
    'nci_period_flow_confirmed', 'nci_period_flow_basis',
})

_ENGINE_OWNED_FIELDS = (
    'move_id', 'reversal_move_id', 'move_history_ids',
    'posting_predecessor_run_id',
    'engine_provenance_version', 'source_move_version',
    'posting_currency_rounding',
    'legacy_evidence_quarantined',
    'legacy_evidence_quarantine_reason',
    'presentation_currency_id', 'cta_amount', 'nci_amount',
    'computed_at', 'computed_by_id', 'reviewed_at', 'reviewed_by_id',
    'closed_at', 'closed_by_id', 'reversed_at', 'reversed_by_id',
    'goodwill_impairment_amount', 'consolidation_warning',
    'goodwill_test_recoverable_amount', 'goodwill_test_account_id',
    'goodwill_test_currency_id',
    'nci_period_profit_amount', 'nci_period_oci_amount',
    'nci_period_comprehensive_income_amount',
    'nci_period_flow_available',
)

_CURRENT_ENGINE_PROVENANCE = 2


class EhConsolRun(models.Model):
    _name = 'eh.consol.run'
    _description = "Consolidation run"
    _order = 'period_to desc, id desc'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
        'eh.workflow.guard', 'eh.consol.company.scope.mixin',
    ]

    # State advances only through the run's own actions (compute / review /
    # close / reset / reopen), which run under sudo. A direct non-superuser
    # write to 'state' is refused by eh.workflow.guard, closing the
    # RPC-write-past-the-action bypass.
    _eh_guarded_fields = ('state',) + _ENGINE_OWNED_FIELDS

    @api.model
    def default_get(self, fields_list):
        """Drop manager-only inputs supplied through context / ir.default."""
        defaults = super().default_get(fields_list)
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager'
        ):
            for field_name in _MANAGER_ONLY_INPUT_FIELDS.intersection(
                fields_list
            ):
                defaults.pop(field_name, None)
        return defaults

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    entity_id = fields.Many2one(
        'eh.consol.entity', required=True,
        ondelete='restrict', index=True, tracking=True,
    )
    presentation_currency_id = fields.Many2one(
        related='entity_id.presentation_currency_id',
        store=True, readonly=True,
    )

    period_from = fields.Date(
        required=True, tracking=True,
        help="Inclusive start of the consolidation period.",
    )
    period_to = fields.Date(
        required=True, tracking=True,
        help=(
            "Inclusive end of the consolidation period. Closing "
            "rates for IAS 21 translation are sourced as at this "
            "date."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('reviewed', "Reviewed"),
            ('closed', "Closed"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.consol.run.line', 'run_id', copy=False,
    )
    line_count = fields.Integer(compute='_compute_counts', store=False)
    elimination_ids = fields.One2many(
        'eh.consol.elimination', 'run_id', copy=True,
    )
    elimination_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    unrealised_profit_ids = fields.One2many(
        'eh.consol.unrealised.profit', 'run_id', copy=True,
        help=(
            "Unrealised profit sitting in ending inventory from intra-group "
            "sales. Each record adds a balanced elimination (Dr COGS / "
            "retained earnings, Cr inventory) at compute time so the "
            "consolidated set removes the intra-group margin (IFRS 10 / "
            "IAS 27)."
        ),
    )
    equity_rollforward_ids = fields.One2many(
        'eh.consol.equity.rollforward', 'run_id', copy=True,
        string="Equity-Method Rollforwards",
        help=(
            "Period-specific IAS 28 dividends, OCI and foreign-associate "
            "translation evidence. Prior reviewed carrying adjustments roll "
            "forward automatically."
        ),
    )
    disposal_event_ids = fields.One2many(
        'eh.consol.disposal.event', 'run_id', copy=False, readonly=True,
        help=(
            "Immutable IAS 21.48 disposal events. They survive reset and "
            "recompute; generated recycle lines are rebuilt from these rows."
        ),
    )

    cta_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='presentation_currency_id',
        help=(
            "Currency translation adjustment for this run. The "
            "balancing OCI entry that arises when the closing-rate "
            "translation of the balance sheet does not match the "
            "average-rate translation of the P&L."
        ),
    )
    nci_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='presentation_currency_id',
        help=(
            "Sum of non-controlling interest carved out across "
            "subsidiaries with ownership < 100%."
        ),
    )

    # NCI carrying equity and NCI period comprehensive income are different
    # accounting dimensions. ``nci_amount`` above is a closing stock. These
    # inputs hold the post-consolidation attribution schedule prepared by a
    # manager after all elimination effects; Review snapshots them into the
    # readonly output fields below. A downstream primary statement may consume
    # only that reviewed output, never the closing-stock KPI or live inputs.
    nci_period_profit_input = fields.Monetary(
        string="NCI Period Profit (review basis)",
        currency_field='presentation_currency_id', copy=False,
        help=(
            "Profit or loss for this exact run period attributable to NCI, "
            "credit-positive, after consolidation eliminations. Review "
            "snapshots this manager-prepared basis as signed output."
        ),
    )
    nci_period_oci_input = fields.Monetary(
        string="NCI Period OCI (review basis)",
        currency_field='presentation_currency_id', copy=False,
        help=(
            "Other comprehensive income for this exact run period "
            "attributable to NCI, credit-positive, after consolidation "
            "adjustments. Zero must still be explicitly confirmed."
        ),
    )
    nci_period_flow_confirmed = fields.Boolean(
        string="NCI Period Attribution Confirmed", copy=False,
        help=(
            "Confirms that the profit and OCI inputs are period flows from "
            "the post-consolidation NCI attribution schedule, not closing "
            "NCI equity. Required before Review whenever the group has NCI."
        ),
    )
    nci_period_flow_basis = fields.Text(
        string="NCI Period Attribution Basis", copy=False,
        help=(
            "Required audit reference for the post-consolidation schedule "
            "supporting the NCI profit and OCI period flows."
        ),
    )
    nci_period_profit_amount = fields.Monetary(
        string="Reviewed NCI Period Profit", readonly=True, copy=False,
        currency_field='presentation_currency_id',
        help="Server-owned profit-flow snapshot created by Review.",
    )
    nci_period_oci_amount = fields.Monetary(
        string="Reviewed NCI Period OCI", readonly=True, copy=False,
        currency_field='presentation_currency_id',
        help="Server-owned OCI-flow snapshot created by Review.",
    )
    nci_period_comprehensive_income_amount = fields.Monetary(
        string="Reviewed NCI Period Comprehensive Income", readonly=True,
        copy=False, currency_field='presentation_currency_id',
        help=(
            "Server-owned sum of reviewed NCI period profit and OCI. This is "
            "the flow source for IAS 1 comprehensive-income attribution."
        ),
    )
    nci_period_flow_available = fields.Boolean(
        string="Reviewed NCI Period Flow Available", readonly=True,
        copy=False, index=True,
        help=(
            "True only after Review validates and snapshots the exact-period "
            "NCI profit/OCI attribution basis."
        ),
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help=(
            "The posted consolidation journal entry booked into the entity's "
            "dedicated consolidation ledger company, when one is configured. "
            "Once posted the move is immutable via Odoo's posted state; "
            "reopening or resetting the run reverses but never deletes it. Empty "
            "for a memo-only run (no consolidation company set)."
        ),
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help="Posted reversal of the latest consolidation move, if reopened.",
    )
    move_history_ids = fields.Many2many(
        'account.move', 'eh_consol_run_move_history_rel',
        'run_id', 'move_id', readonly=True, copy=False,
        string="Consolidation move history",
        help=(
            "Append-only history of original and reversal consolidation "
            "entries. Reset and reopen never unlink accounting evidence."
        ),
    )
    posting_predecessor_run_id = fields.Many2one(
        'eh.consol.run', readonly=True, copy=False, index=True,
        ondelete='restrict',
        string="Prior posted consolidation snapshot",
        help=(
            "Server-owned predecessor whose immutable snapshot is deducted "
            "from this run's posting. The resulting move is a balanced "
            "period-on-period delta, so the cumulative dedicated ledger "
            "equals this run rather than stacking complete snapshots."
        ),
    )
    engine_provenance_version = fields.Integer(
        readonly=True, copy=False, default=0, index=True,
        help=(
            "Server-owned version of the consolidation engine that most "
            "recently rebuilt this run's complete output graph. Zero means "
            "the graph predates durable provenance and cannot post or close."
        ),
    )
    source_move_version = fields.Integer(
        readonly=True, copy=False, default=0,
        help=(
            "Server-owned sum of the parent/member move-version counters "
            "captured under company locks at compute time. Any later source "
            "posting, account or rate change makes the run stale."
        ),
    )
    posting_currency_rounding = fields.Float(
        readonly=True, copy=False, default=0.0,
        help=(
            "Server-owned currency precision used to build the active "
            "consolidation move. Correction paths use this immutable basis "
            "rather than mutable live currency configuration."
        ),
    )
    legacy_evidence_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "A pre-control accounting link failed provenance reconstruction. "
            "The legacy graph is preserved but can never post, reverse or "
            "close as trusted evidence; rebuild it in a new run."
        ),
    )
    legacy_evidence_quarantine_reason = fields.Text(
        readonly=True, copy=False,
    )
    move_state = fields.Selection(
        related='move_id.state', string="Move status", readonly=True,
    )
    consolidation_company_id = fields.Many2one(
        related='entity_id.consolidation_company_id', readonly=True,
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    reviewed_at = fields.Datetime(readonly=True, tracking=True)
    reviewed_by_id = fields.Many2one('res.users', readonly=True)
    closed_at = fields.Datetime(readonly=True, tracking=True)
    closed_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)

    goodwill_recoverable_amount = fields.Monetary(
        currency_field='presentation_currency_id', copy=False,
        help=(
            "IAS 36 recoverable amount of the cash-generating unit carrying "
            "the recognised goodwill, in the presentation currency. Entered by "
            "a manager before testing goodwill for impairment. When the "
            "recognised goodwill exceeds this amount the difference is booked "
            "as an impairment charge that reduces goodwill."
        ),
    )
    goodwill_recoverable_confirmed = fields.Boolean(
        string="Recoverable Amount Determined",
        copy=False,
        help=(
            "Explicit manager confirmation that the IAS 36 recoverable "
            "amount has been determined for this run. Zero is a valid "
            "measurement only when deliberately confirmed; an untouched "
            "zero can never write off goodwill."
        ),
    )
    goodwill_impairment_amount = fields.Monetary(
        readonly=True, copy=False,
        currency_field='presentation_currency_id',
        help=(
            "The IAS 36 goodwill impairment charge booked on this run, if any. "
            "Set by Test Goodwill Impairment when the recognised goodwill "
            "exceeds the recoverable amount."
        ),
    )
    goodwill_impairment_account_id = fields.Many2one(
        'account.account', copy=False,
        ondelete='restrict',
        string="Goodwill Impairment Expense Account",
        help=(
            "Expense account on the parent / consolidation chart "
            "the goodwill impairment charge is debited to. When left empty the "
            "impairment test falls back to a name heuristic (an expense "
            "account whose name contains 'impairment'); if that also resolves "
            "nothing the test is refused rather than posting an accountless "
            "charge."
        ),
    )
    goodwill_test_recoverable_amount = fields.Monetary(
        readonly=True, copy=False,
        currency_field='goodwill_test_currency_id',
        help=(
            "Server-owned snapshot of the recoverable amount used by the "
            "latest IAS 36 test."
        ),
    )
    goodwill_test_account_id = fields.Many2one(
        'account.account', readonly=True, copy=False,
        ondelete='restrict',
        help=(
            "Server-owned snapshot of the expense account selected by the "
            "latest IAS 36 test. Empty only when no charge was required."
        ),
    )
    goodwill_test_currency_id = fields.Many2one(
        'res.currency', readonly=True, copy=False,
        ondelete='restrict',
        help=(
            "Durable denomination of the recoverable amount used by the "
            "latest IAS 36 test."
        ),
    )

    override_policy_checks = fields.Boolean(
        string="Override Policy / Reporting-Date Checks",
        copy=False, tracking=True,
        help=(
            "IFRS 10.B87 / B92-B93 guard override. A compute is refused "
            "while any member reports more than three months off the group "
            "reporting date or is not confirmed policy-aligned. Setting "
            "this flag (with a mandatory reason) lets the compute proceed "
            "anyway; the override and its reason are recorded in the "
            "chatter audit trail."
        ),
    )
    override_policy_reason = fields.Text(
        string="Override Reason",
        copy=False,
        help=(
            "Mandatory justification when the policy / reporting-date "
            "checks are overridden, e.g. the conforming adjustments made "
            "for the offset period (IFRS 10.B93)."
        ),
    )

    notes = fields.Html()

    consolidation_warning = fields.Text(
        readonly=True, copy=False,
        help=(
            "Diagnostics raised at compute time: subsidiary investments not "
            "yet eliminated against equity (when the automatic IFRS 3 "
            "elimination is off or the member's acquisition fields are "
            "incomplete) and mismatched intragroup reciprocals. Surfaced so "
            "a run is never mistaken for a complete IFRS 10 consolidation "
            "while these manual steps are outstanding."
        ),
    )

    _unique_posting_predecessor = models.Constraint(
        'unique(posting_predecessor_run_id)',
        "A posted consolidation snapshot can have only one active successor.",
    )

    @api.depends('line_ids', 'elimination_ids')
    def _compute_counts(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)
            rec.elimination_count = len(rec.elimination_ids)

    @api.depends('line_ids.kind', 'line_ids.amount')
    def _compute_totals(self):
        for rec in self:
            cta = sum(
                rec.line_ids.filtered(lambda l: l.kind == 'cta')
                .mapped('amount'),
            )
            nci = sum(
                rec.line_ids.filtered(lambda l: l.kind == 'nci')
                .mapped('amount'),
            )
            rec.cta_amount = cta
            rec.nci_amount = nci

    @api.model_create_multi
    def create(self, vals_list):
        engine = (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        )
        forged = set().union(*(
            set(vals) & set(_ENGINE_OWNED_FIELDS)
            for vals in vals_list
        ))
        if forged and not engine:
            raise AccessError(_(
                "Consolidation run output/evidence fields are server-owned "
                "and cannot be supplied at creation: %s",
                ', '.join(sorted(forged)),
            ))
        is_manager = self.env.user.has_group(
            'eh_account_base.group_eh_manager'
        )
        manager_inputs = set().union(*(
            set(vals) & _MANAGER_ONLY_INPUT_FIELDS for vals in vals_list
        ))
        if manager_inputs and not is_manager:
            raise AccessError(_(
                "Only a consolidation manager may set policy-override or IAS "
                "36 measurement inputs: %s",
                ', '.join(sorted(manager_inputs)),
            ))
        for vals in vals_list:
            if vals.get('name') not in (None, False, '/'):
                raise AccessError(_(
                    "A consolidation run name is assigned by the server "
                    "sequence and cannot be supplied by the caller."
                ))
            self._eh_validate_finite_values(vals)
            self._eh_validate_period_values(vals)
            self._eh_validate_impairment_account(vals)
        entity_ids = {vals.get('entity_id') for vals in vals_list}
        for vals in vals_list:
            entity = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
            if entity:
                entity._eh_assert_company_closure('read')
                entity._eh_validate_reference_values({}, current=entity)
        self._eh_lock_for_transition(extra_entity_ids=entity_ids)
        for vals in vals_list:
            entity = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
            if entity:
                entity._eh_assert_company_closure('read')
                entity._eh_validate_reference_values({}, current=entity)
        for vals in vals_list:
            if vals.get('name') in (None, False, '/'):
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.consol.run',
                ) or '/'
                vals['name'] = seq
        create_self = self
        if not is_manager:
            protected_defaults = {
                'default_%s' % field_name
                for field_name in _MANAGER_ONLY_INPUT_FIELDS
            }
            create_self = self.with_context({
                key: value for key, value in self.env.context.items()
                if key not in protected_defaults
            })
        runs = super(EhConsolRun, create_self).create(vals_list)
        runs._eh_assert_company_closure()
        return runs

    def _eh_assert_company_closure(self):
        if not self.env.su:
            self._eh_check_access('write')
            self.mapped('entity_id')._eh_assert_company_closure('read')
        return self

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    @api.model
    def _eh_validate_period_values(self, vals=None, current=None):
        vals = vals or {}
        period_from = fields.Date.to_date(vals.get(
            'period_from', current.period_from if current else False,
        ))
        period_to = fields.Date.to_date(vals.get(
            'period_to', current.period_to if current else False,
        ))
        if period_from and period_to and period_from > period_to:
            raise UserError(_(
                "The consolidation period start must not be later than its "
                "end date."
            ))
        return True

    @api.model
    def _eh_validate_impairment_account(self, vals=None, current=None):
        vals = vals or {}
        account = self.env['account.account'].browse(vals.get(
            'goodwill_impairment_account_id',
            current.goodwill_impairment_account_id.id if current else False,
        ))
        if account and account.account_type not in _EXPENSE_TYPES:
            raise UserError(_(
                "The goodwill impairment account must be an expense account."
            ))
        return True

    def _eh_validate_reference_closure(self):
        """Validate every configured posting/source reference pre-elevation."""
        if self.env.su:
            return self
        MoveLine = self.env['account.move.line']
        Account = self.env['account.account']
        Journal = self.env['account.journal']
        Rate = self.env['res.currency.rate']
        MoveLine._eh_check_access('read')
        Account._eh_check_access('read')
        Journal._eh_check_access('read')
        Rate._eh_check_access('read')
        for run in self:
            entity = run.entity_id
            # These validators enforce both company closure and semantic
            # posting roles.  Keep them in the pre-elevation corridor and run
            # them again after the deterministic locks in
            # ``_eh_prepare_transition``.
            entity._eh_validate_reference_values({}, current=entity)
            entity.member_ids._eh_validate_references({})
            scope = set(entity._eh_scope_companies().ids)
            # Exercise the caller's record rules once on each source model.
            # Never compare a visible count with a sudo count: that both scans
            # the full ledger twice and turns a transition error into a hidden-
            # row existence oracle. Company closure is proved independently;
            # these probes prove model/rule access without disclosing whether
            # or how many source rows exist.
            source_domain = [
                ('move_id.company_id', 'in', tuple(sorted(scope))),
            ]
            MoveLine.search(source_domain, limit=1, order='id')
            account_field = (
                'company_ids' if 'company_ids' in Account._fields
                else 'company_id')
            account_domain = [(account_field, 'in', tuple(sorted(scope)))]
            Account.search(account_domain, limit=1, order='id')
            target = entity.consolidation_company_id
            journal = entity.consolidation_journal_id
            if journal:
                journal._eh_check_access('read')
                if (
                    not target
                    or journal.company_id != target
                    or journal.type != 'general'
                ):
                    raise UserError(_(
                        "The consolidation journal must be a general journal "
                        "in the dedicated consolidation ledger company."
                    ))
            if (
                run.goodwill_impairment_account_id
                and run.goodwill_impairment_account_id.account_type
                not in _EXPENSE_TYPES
            ):
                raise UserError(_(
                    "The goodwill impairment account must be an expense "
                    "account."
                ))
            records = [run, entity] + list(entity.member_ids)
            referenced_accounts = Account
            for record in records:
                for field_name, field in record._fields.items():
                    if getattr(field, 'comodel_name', None) != 'account.account':
                        continue
                    account = record[field_name]
                    if not account:
                        continue
                    referenced_accounts |= account
                    if not (self._eh_account_company_ids(account) & scope):
                        raise UserError(_(
                            "A consolidation account reference belongs outside "
                            "the authorised company closure."
                        ))
            line_accounts = (
                run.line_ids.mapped('account_id')
                | run.line_ids.mapped('posting_account_id')
            )
            if referenced_accounts:
                referenced_accounts._eh_check_access('read')
            if line_accounts:
                line_accounts._eh_check_access('read')
            for line in run.line_ids:
                if line.company_id and line.company_id.id not in scope:
                    raise UserError(_(
                        "A consolidation line has a source company outside the "
                        "authorised consolidation closure."
                    ))
                if line.account_id:
                    if not (self._eh_account_company_ids(line.account_id) & scope):
                        raise UserError(_(
                            "A consolidation line account belongs outside the "
                            "authorised consolidation closure."
                        ))
                if line.posting_account_id:
                    target_company = entity.consolidation_company_id
                    allowed_target_ids = (
                        set(target_company.ids)
                        if target_company else scope
                    )
                    if (
                        not (
                            self._eh_account_company_ids(
                                line.posting_account_id,
                            ) & allowed_target_ids
                        )
                        or line.posting_account_id.account_type
                        != line.account_type
                    ):
                        raise UserError(_(
                            "A consolidation line posting snapshot failed its "
                            "company or account-role validation."
                        ))
        return self

    def _eh_validate_correction_reference_closure(self):
        """Prove actor visibility for reversal without live-role semantics."""
        if self.env.su:
            return self
        self.env['account.move']._eh_check_access('read')
        self.env['account.account']._eh_check_access('read')
        for run in self:
            evidence_moves = (
                run.move_id | run.reversal_move_id | run.move_history_ids
            )
            if evidence_moves:
                evidence_moves._eh_check_access('read')
            for line in run.line_ids:
                if line.account_id:
                    line.account_id._eh_check_access('read')
                if line.posting_account_id:
                    line.posting_account_id._eh_check_access('read')
        return self

    def _eh_validate_unrealised_profit_basis(self):
        """Validate every optional inventory-margin input before compute."""
        for run in self:
            for unrealised in run.unrealised_profit_ids:
                unrealised._eh_validate_basis(run, current=unrealised)
        return self

    def _eh_assert_account_role_graph(self):
        """Revalidate every configured accounting role under action locks."""
        for run in self:
            entity = run.entity_id
            entity._eh_validate_reference_values({}, current=entity)
            entity.member_ids._eh_validate_references({})
            run._eh_validate_impairment_account({}, current=run)
        return self

    def _eh_source_companies(self):
        companies = self.env['res.company']
        for run in self:
            companies |= run.entity_id._eh_operating_scope_companies()
        return companies

    def _eh_current_source_move_version(self):
        companies = self._eh_source_companies()
        companies.invalidate_recordset(['eh_move_version'])
        return sum(companies.mapped('eh_move_version'))

    def _eh_assert_source_move_version_fresh(self):
        for run in self:
            current = run._eh_current_source_move_version()
            if run.source_move_version != current:
                raise UserError(_(
                    "Run %(run)s is stale because source ledger, account or "
                    "rate data changed after compute. Reset and recompute it "
                    "before review, accounting mutation, posting or close.",
                    run=run.name,
                ))
        return self

    def _eh_restamp_owned_outputs(self):
        """Repair stored related/aggregate outputs under the run lock."""
        run_ids = tuple(sorted(self.ids))
        if not run_ids:
            return self
        self.env['eh.consol.run.line'].flush_model([
            'run_id', 'kind', 'amount',
        ])
        self.env['eh.consol.entity'].flush_model([
            'presentation_currency_id',
        ])
        self.env.cr.execute(
            """
            UPDATE eh_consol_run AS run
               SET presentation_currency_id = entity.presentation_currency_id,
                   cta_amount = COALESCE((
                       SELECT SUM(line.amount)
                         FROM eh_consol_run_line AS line
                        WHERE line.run_id = run.id
                          AND line.kind = 'cta'
                   ), 0.0),
                   nci_amount = COALESCE((
                       SELECT SUM(line.amount)
                         FROM eh_consol_run_line AS line
                        WHERE line.run_id = run.id
                          AND line.kind = 'nci'
                   ), 0.0),
                   goodwill_impairment_amount = COALESCE((
                       SELECT SUM(CASE WHEN line.amount > 0.0
                                       THEN line.amount ELSE 0.0 END)
                         FROM eh_consol_run_line AS line
                        WHERE line.run_id = run.id
                          AND line.kind = 'impairment'
                   ), 0.0)
              FROM eh_consol_entity AS entity
             WHERE run.entity_id = entity.id
               AND run.id IN %s
            """,
            (run_ids,),
        )
        self.invalidate_recordset([
            'presentation_currency_id', 'cta_amount', 'nci_amount',
            'goodwill_impairment_amount',
        ])
        return self

    def _eh_stamp_posting_accounts(self, lines=None):
        """Snapshot the exact posting target for every locked engine line.

        Account codes are company-dependent in Odoo 19 and both source and
        target codes remain editable.  Resolving again at post/close time can
        therefore re-route or strand an otherwise immutable run.  Resolve once
        while the source graph is locked, then retain the target account ID as
        the durable posting/provenance anchor.
        """
        for run in self:
            target_company = run.entity_id.consolidation_company_id
            cache = {}
            run_lines = (
                run.line_ids
                if lines is None else (run.line_ids & lines)
            )
            for line in run_lines.sorted('id'):
                account = line.account_id
                if not account:
                    if line.kind == 'disclosure':
                        posting_account = self.env['account.account']
                        code = name = account_type = False
                    else:
                        raise UserError(_(
                            "Run %s carries a non-disclosure line without an "
                            "account.", run.name,
                        ))
                else:
                    account_companies = (
                        account.company_ids
                        if 'company_ids' in account._fields
                        else account.company_id
                    )
                    allowed_owners = (
                        account_companies
                        & run.entity_id._eh_scope_companies()
                    )
                    line_company = line.company_id
                    member_company = line.member_id.company_id
                    parent_company = run.entity_id.parent_company_id
                    source_company = (
                        line_company
                        if line_company in account_companies
                        else member_company
                        if member_company in account_companies
                        else target_company
                        if target_company in account_companies
                        else parent_company
                        if parent_company in account_companies
                        else allowed_owners.sorted('id')[:1]
                    )
                    if not source_company:
                        raise UserError(_(
                            "A consolidation line account has no owner in the "
                            "authorised consolidation company closure."
                        ))
                    source_account = account.with_company(source_company)
                    code = source_account.code or False
                    name = source_account.name or False
                    account_type = account.account_type or False
                    posting_account = (
                        run._eh_resolve_consol_account(
                            account, target_company, cache,
                            source_company=source_company,
                        )
                        if target_company else account
                    )
                    if posting_account.account_type != account_type:
                        raise UserError(_(
                            "Posting account %(target)s has role %(target_role)s, "
                            "but source snapshot %(source)s has role "
                            "%(source_role)s.",
                            target=posting_account.display_name,
                            target_role=posting_account.account_type,
                            source=account.display_name,
                            source_role=account_type,
                        ))
                self.env.cr.execute(
                    """
                    UPDATE eh_consol_run_line
                       SET account_code = %s,
                           account_name = %s,
                           account_type = %s,
                           posting_account_id = %s
                     WHERE id = %s
                    """,
                    (
                        code, name, account_type,
                        posting_account.id or None, line.id,
                    ),
                )
            run_lines.invalidate_recordset([
                'account_code', 'account_name', 'account_type',
                'posting_account_id',
            ])
        return self

    @api.model
    def _eh_validate_finite_values(self, vals=None, current=None):
        vals = vals or {}
        for field_name in (
            'goodwill_recoverable_amount', 'goodwill_impairment_amount',
            'cta_amount', 'nci_amount',
            'nci_period_profit_input', 'nci_period_oci_input',
            'nci_period_profit_amount', 'nci_period_oci_amount',
            'nci_period_comprehensive_income_amount',
        ):
            value = vals.get(
                field_name,
                current[field_name] if current else 0.0,
            )
            number = float(value or 0.0)
            if not math.isfinite(number):
                raise UserError(_(
                    "Consolidation field %s must be a finite number.",
                    field_name,
                ))
            if field_name == 'goodwill_recoverable_amount' and number < 0.0:
                raise UserError(_(
                    "Goodwill recoverable amount cannot be negative."
                ))
        return True

    @api.constrains('goodwill_recoverable_amount')
    def _check_finite_run_inputs(self):
        for run in self:
            self._eh_validate_finite_values({}, current=run)

    @api.constrains('period_from', 'period_to')
    def _check_period_order(self):
        for run in self:
            self._eh_validate_period_values({}, current=run)

    @api.model
    def _eh_assert_finite_number(self, value, label, positive=False):
        number = float(value or 0.0)
        if not math.isfinite(number) or (positive and number <= 0.0):
            raise UserError(_(
                "%(label)s must be a %(qualifier)sfinite number.",
                label=label,
                qualifier=_('positive ') if positive else '',
            ))
        return number

    def _eh_validate_line_vals_finite(self, vals_list):
        for vals in vals_list or ():
            self._eh_assert_finite_number(
                vals.get('amount', 0.0),
                _("Consolidation output line amount"),
            )
        return vals_list

    def _eh_assert_finite_output_graph(self, require_live_roles=True):
        """Validate all accounting inputs and the deterministic output graph."""
        for run in self:
            run._eh_validate_finite_values({}, current=run)
            currency = run.presentation_currency_id
            if not currency or currency != run.entity_id.presentation_currency_id:
                raise UserError(_(
                    "The run presentation currency does not match its entity."
                ))
            rounding = run._eh_assert_finite_number(
                currency.rounding, _("Presentation-currency rounding"),
                positive=True,
            )
            if require_live_roles:
                for member in run.entity_id.member_ids:
                    member._eh_validate_finite_values({}, current=member)
                    if member.investment_currency_id != currency:
                        raise UserError(_(
                            "A consolidation member carries a stale investment-"
                            "currency anchor."
                        ))
            for line in run.line_ids:
                line._eh_validate_finite_amount({}, current=line)
                if line.presentation_currency_id != currency:
                    raise UserError(_(
                        "A consolidation output line carries a stale currency "
                        "anchor."
                    ))
                if line.account_id:
                    if not (
                        line.account_code
                        and line.account_name
                        and line.account_type
                    ):
                        raise UserError(_(
                            "A consolidation output line is missing its "
                            "immutable account snapshot."
                        ))
                    posting = line.posting_account_id
                    if not posting:
                        raise UserError(_(
                            "A consolidation output line is missing its "
                            "immutable posting-account snapshot."
                        ))
                    target = run.entity_id.consolidation_company_id
                    allowed_company_ids = (
                        set(target.ids)
                        if target
                        else set(run.entity_id._eh_scope_companies().ids)
                    )
                    if require_live_roles and (
                        not (
                            run._eh_account_company_ids(posting)
                            & allowed_company_ids
                        )
                        or posting.account_type != line.account_type
                    ):
                        raise UserError(_(
                            "A consolidation line's posting-account snapshot "
                            "belongs outside its posting company or no longer "
                            "matches the immutable source account role."
                        ))
                elif line.kind != 'disclosure' or line.posting_account_id:
                    raise UserError(_(
                        "Only a disclosure line may omit its account and "
                        "posting-account snapshots."
                    ))
            cta = sum(run.line_ids.filtered(
                lambda line: line.kind == 'cta').mapped('amount'))
            nci = sum(run.line_ids.filtered(
                lambda line: line.kind == 'nci').mapped('amount'))
            impairment_lines = run.line_ids.filtered(
                lambda line: line.kind == 'impairment')
            impairment_net = sum(impairment_lines.mapped('amount'))
            impairment = sum(
                amount for amount in impairment_lines.mapped('amount')
                if amount > 0.0
            )
            for value, label in (
                (cta, _("CTA output")),
                (nci, _("NCI output")),
                (impairment, _("Goodwill impairment output")),
                (impairment_net, _("Goodwill impairment balance")),
            ):
                run._eh_assert_finite_number(value, label)
            if (
                not float_is_zero(
                    currency.round(cta) - currency.round(run.cta_amount),
                    precision_rounding=rounding,
                )
                or not float_is_zero(
                    currency.round(nci) - currency.round(run.nci_amount),
                    precision_rounding=rounding,
                )
                or not float_is_zero(
                    currency.round(impairment)
                    - currency.round(run.goodwill_impairment_amount),
                    precision_rounding=rounding,
                )
                or not float_is_zero(
                    impairment_net, precision_rounding=rounding,
                )
            ):
                raise UserError(_(
                    "Consolidation stored totals do not match the locked output "
                    "line graph."
                ))
        return self

    def _eh_has_nci_scope(self):
        """Whether this run consolidates at least one minority-held sub."""
        self.ensure_one()
        return any(
            member.method == 'full' and member.ownership_pct < 100.0
            for member in self.entity_id.member_ids
        )

    def _eh_validate_nci_period_flow_basis(self):
        """Validate manager basis before it becomes reviewed output.

        NCI attribution after consolidation can differ from a mechanical
        minority share of subsidiary P&L because upstream/downstream
        eliminations and OCI allocation affect it. The engine therefore does
        not invent a figure from ``nci_amount`` (a closing equity stock) or
        from incomplete run-line heuristics. A manager supplies the exact
        post-consolidation schedule, confirms that both flow dimensions were
        considered, and cites its evidence. Review turns that basis into an
        immutable, server-owned output.
        """
        for run in self:
            run._eh_validate_finite_values({}, current=run)
            currency = run.presentation_currency_id
            rounding = currency.rounding or 0.01
            has_nci = run._eh_has_nci_scope()
            if not has_nci:
                if (
                    not float_is_zero(
                        run.nci_period_profit_input,
                        precision_rounding=rounding,
                    )
                    or not float_is_zero(
                        run.nci_period_oci_input,
                        precision_rounding=rounding,
                    )
                ):
                    raise UserError(_(
                        "Run %(run)s has no minority-held fully consolidated "
                        "member, so its NCI period profit and OCI must be zero.",
                        run=run.name,
                    ))
                continue
            if not run.nci_period_flow_confirmed:
                raise UserError(_(
                    "Review requires the exact-period NCI attribution "
                    "schedule. Confirm both NCI period profit and OCI; do "
                    "not use the closing NCI equity amount."
                ))
            if not (run.nci_period_flow_basis or '').strip():
                raise UserError(_(
                    "Review requires an audit reference for the "
                    "post-consolidation NCI profit/OCI attribution schedule."
                ))
        return self

    def _eh_snapshot_nci_period_flow(self):
        """Create immutable exact-period NCI flow outputs during Review."""
        missing = self.filtered(lambda run: (
            run._eh_has_nci_scope()
            and (
                not run.nci_period_flow_confirmed
                or not (run.nci_period_flow_basis or '').strip()
            )
        ))
        if missing:
            missing.sudo().with_context(**{
                CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
            }).write({
                'nci_period_profit_amount': 0.0,
                'nci_period_oci_amount': 0.0,
                'nci_period_comprehensive_income_amount': 0.0,
                'nci_period_flow_available': False,
            })
            for run in missing:
                run.message_post(body=_(
                    "Run reviewed without an exact-period NCI profit/OCI "
                    "attribution schedule. Consolidated balances remain "
                    "reviewed, but this run cannot source NCI attribution "
                    "in a primary statement."
                ))
        ready = self - missing
        ready._eh_validate_nci_period_flow_basis()
        engine_runs = ready.sudo().with_context(**{
            CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
        })
        for run in engine_runs:
            currency = run.presentation_currency_id
            profit = currency.round(
                run.nci_period_profit_input if run._eh_has_nci_scope()
                else 0.0
            )
            oci = currency.round(
                run.nci_period_oci_input if run._eh_has_nci_scope()
                else 0.0
            )
            run.write({
                'nci_period_profit_amount': profit,
                'nci_period_oci_amount': oci,
                'nci_period_comprehensive_income_amount': currency.round(
                    profit + oci
                ),
                'nci_period_flow_available': True,
            })
        return self

    def _eh_assert_nci_period_flow_output(self):
        """Prove reviewed NCI flow still equals its signed basis."""
        for run in self:
            if not run.nci_period_flow_available:
                rounding = run.presentation_currency_id.rounding or 0.01
                if any(
                    not float_is_zero(value, precision_rounding=rounding)
                    for value in (
                        run.nci_period_profit_amount,
                        run.nci_period_oci_amount,
                        run.nci_period_comprehensive_income_amount,
                    )
                ):
                    raise UserError(_(
                        "Run %s carries NCI period-flow output without a "
                        "reviewed availability stamp.", run.name,
                    ))
                continue
            run._eh_validate_nci_period_flow_basis()
            currency = run.presentation_currency_id
            rounding = currency.rounding or 0.01
            has_nci = run._eh_has_nci_scope()
            expected_profit = currency.round(
                run.nci_period_profit_input if has_nci else 0.0
            )
            expected_oci = currency.round(
                run.nci_period_oci_input if has_nci else 0.0
            )
            expected_total = currency.round(expected_profit + expected_oci)
            for actual, expected in (
                (run.nci_period_profit_amount, expected_profit),
                (run.nci_period_oci_amount, expected_oci),
                (run.nci_period_comprehensive_income_amount, expected_total),
            ):
                if not float_is_zero(
                    currency.round(actual) - expected,
                    precision_rounding=rounding,
                ):
                    raise UserError(_(
                        "Run %s NCI period-flow output no longer matches its "
                        "reviewed attribution basis.", run.name,
                    ))
        return self

    def _eh_assert_not_legacy_quarantined(self):
        quarantined = self.filtered('legacy_evidence_quarantined')
        if quarantined:
            raise UserError(_(
                "Run %(runs)s contains pre-control consolidation evidence "
                "whose provenance cannot be proved. Its rows and accounting "
                "links are retained for audit, but it cannot be recomputed, "
                "posted, reversed or closed as trusted evidence. Create a new "
                "run.",
                runs=', '.join(quarantined.mapped('display_name')),
            ))
        return self

    def _eh_assert_not_cited_by_confirmed_statements(self):
        """Keep signed primary-statement source evidence reproducible.

        Statements use a soft dependency, so the consolidation module cannot
        declare database foreign keys to them. Once either primary statement
        is confirmed, however, deleting this run's locked line graph through
        Reset/Reopen would destroy cited evidence. Both statement confirmation
        and these correction actions take the run lock, making this registry
        probe race-safe without a reverse hard dependency.
        """
        cited = []
        for model_name in ('eh.soci', 'eh.soce'):
            if model_name not in self.env.registry:
                continue
            Model = self.env[model_name]
            if 'consol_run_res_id' not in Model._fields:
                continue
            records = Model.sudo().search([
                ('consol_run_res_id', 'in', self.ids),
                ('consol_nci_available', '=', True),
                ('state', '=', 'confirmed'),
            ], limit=5)
            cited.extend(records.mapped('display_name'))
        if cited:
            raise UserError(_(
                "This consolidation run is cited by confirmed primary "
                "statement(s): %(statements)s. Reopen those statements "
                "before resetting or reopening their signed source run.",
                statements=', '.join(cited),
            ))
        return self

    def _eh_assert_current_engine_provenance(
        self, require_goodwill_test=True, require_live_roles=True,
    ):
        self._eh_assert_not_legacy_quarantined()
        stale = self.filtered(
            lambda run: run.engine_provenance_version
            != _CURRENT_ENGINE_PROVENANCE
        )
        if stale:
            raise UserError(_(
                "Run %(runs)s was not rebuilt by the current consolidation "
                "engine and cannot advance or change accounting evidence. "
                "Compute a clean draft run first.",
                runs=', '.join(stale.mapped('display_name')),
            ))
        self._eh_assert_finite_output_graph(
            require_live_roles=require_live_roles,
        )
        reviewed = self.filtered(
            lambda run: run.state in ('reviewed', 'closed')
        )
        if reviewed:
            reviewed._eh_assert_nci_period_flow_output()
        if require_live_roles:
            self._eh_assert_account_role_graph()
        if require_goodwill_test:
            self._eh_assert_goodwill_test_fresh()
        return self

    def _eh_assert_correction_provenance(self):
        """Validate immutable evidence without mutable live-role gates.

        Reopen/reset are the only escape hatch for a sealed entry.  A later
        account role/configuration change must block new evidence, but must
        never strand reversal of the already-posted entry.  The correction
        corridor therefore proves the retained IDs, immutable snapshots and
        output arithmetic while deliberately ignoring current account roles
        and the live IAS 36 configuration.
        """
        return self._eh_assert_current_engine_provenance(
            require_goodwill_test=False,
            require_live_roles=False,
        )

    def _eh_lock_for_transition(self, extra_entity_ids=()):
        """Take entity, fresh closure-company, then run locks in id order."""
        expected_entity_by_run = {
            run.id: run.entity_id.id for run in self
        }
        entities = self.mapped('entity_id') | self.env['eh.consol.entity'].browse(
            [entity_id for entity_id in extra_entity_ids if entity_id])
        entity_ids = tuple(sorted(entities.ids))
        if entity_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_consol_entity WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (entity_ids,))
            entities.invalidate_recordset()
        members = entities.mapped('member_ids')
        members.invalidate_recordset([
            'entity_id', 'company_id', 'cta_position_id',
        ])
        companies = self.env['res.company']
        for entity in entities:
            companies |= entity._eh_scope_companies()
        company_ids = tuple(sorted(companies.ids))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (company_ids,),
            )
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id, entity_id FROM eh_consol_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A consolidation run changed concurrently. Retry the "
                    "operation."
                ))
            if any(
                expected_entity_by_run.get(run_id) != entity_id
                for run_id, entity_id in locked_rows
            ):
                # A concurrent rehome completed after the caller resolved the
                # parent but before it obtained the old entity lock.  Never
                # continue with an unlocked replacement entity: fail closed
                # and let the caller retry in the canonical entity-first order.
                raise UserError(_(
                    "A consolidation run changed entity concurrently. Retry "
                    "the operation."
                ))
            self.invalidate_recordset()
        lines = self.mapped('line_ids')
        lines.invalidate_recordset(['run_id', 'cta_position_id'])
        position_ids = tuple(sorted(set(
            members.mapped('cta_position_id').ids
            + lines.mapped('cta_position_id').ids
        )))
        if position_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_fx_cta_position WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (position_ids,),
            )
        return self

    def _eh_prepare_transition(self, validate_references=True,
                               validate_unrealised=False,
                               correction_references=False):
        """Validate as the caller, serialize, revalidate, then elevate.

        The second validation is intentional: a sibling transaction may have
        changed an entity member or posting reference while this transaction
        waited for the deterministic company/run locks.
        """
        caller = self
        caller._eh_assert_company_closure()
        if correction_references:
            caller._eh_validate_correction_reference_closure()
        elif validate_references:
            caller._eh_validate_reference_closure()
        if validate_unrealised:
            caller._eh_validate_unrealised_profit_basis()
        caller._eh_lock_for_transition()
        caller._eh_restamp_owned_outputs()
        caller._eh_assert_company_closure()
        if correction_references:
            caller._eh_validate_correction_reference_closure()
        elif validate_references:
            caller._eh_validate_reference_closure()
        if validate_unrealised:
            caller._eh_validate_unrealised_profit_basis()
        return caller._eh_workflow_action().with_context(**{
            CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
        })

    # ---- integrity: freeze input once the figures are settled ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is settled
        (computed / reviewed / closed); they are the basis of the consolidated
        figures and any posted consolidation move.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps) carries no frozen field and
        passes. A write touching a frozen figure while any record is settled is
        always blocked for everyone (a posted figure is restated by reversal,
        never by direct write). 'state' itself is owned by eh.workflow.guard:
        it may change only through the run's own actions (which run under sudo),
        so a direct RPC state write is refused there, not here.
        """
        if 'name' in vals:
            raise AccessError(_(
                "A consolidation run's sequence identity is immutable."
            ))
        target = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
        if target and not self.env.su:
            target._eh_assert_company_closure('read')
        # The sanctioned engine paths (the IAS 36 goodwill impairment test,
        # which is allowed on a computed / reviewed run and writes the readonly
        # goodwill_impairment_amount) flag themselves with the engine context
        # key and are exempt from the figure freeze. env.su alone is not a
        # reliable engine signal, so only the flagged path is exempted.
        engine = (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        )
        owned = set(vals) & set(_ENGINE_OWNED_FIELDS)
        if owned and not engine:
            raise AccessError(_(
                "Consolidation output/evidence fields are server-owned and "
                "cannot be changed directly: %s",
                ', '.join(sorted(owned)),
            ))
        manager_inputs = set(vals) & _MANAGER_ONLY_INPUT_FIELDS
        if manager_inputs and not engine and not self.env.user.has_group(
            'eh_account_base.group_eh_manager'
        ):
            raise AccessError(_(
                "Only a consolidation manager may change policy-override or "
                "IAS 36 measurement inputs: %s",
                ', '.join(sorted(manager_inputs)),
            ))
        for run in self:
            self._eh_validate_finite_values(vals, current=run)
            self._eh_validate_period_values(vals, current=run)
            self._eh_validate_impairment_account(vals, current=run)
        protected = (
            set(_FROZEN_AFTER_CONFIRM) |
            set(_FROZEN_AFTER_POSTED_MOVE) |
            set(_NCI_PERIOD_FLOW_INPUT_FIELDS) |
            set(self._eh_guarded_fields) |
            {'entity_id'}
        )
        if set(vals) & protected and not engine:
            self._eh_assert_company_closure()
            self._eh_validate_reference_closure()
            if target:
                target._eh_validate_reference_values({}, current=target)
            self._eh_lock_for_transition(
                extra_entity_ids=(vals.get('entity_id'),))
            self._eh_assert_company_closure()
            self._eh_validate_reference_closure()
            target = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
            if target:
                target._eh_assert_company_closure('read')
                target._eh_validate_reference_values({}, current=target)
        frozen = [f for f in _FROZEN_AFTER_CONFIRM if f in vals]
        confirmed = self.filtered(lambda r: (
            r.state in _POSTED_STATES
            or r.elimination_ids.filtered(lambda elimination: (
                elimination.state != 'draft'
                or elimination.posted_at
                or elimination.posted_by_id
            ))
        ))
        if frozen and confirmed and not engine:
            raise UserError(_(
                "Figures on a settled consolidation run are frozen "
                "(%(fields)s). Reset it to draft (consolidation manager only) "
                "to change it.",
                fields=', '.join(frozen)))
        historical = self.filtered(
            lambda run: run.move_id or run.reversal_move_id
            or run.move_history_ids
            or run.elimination_ids.filtered(lambda elimination: (
                elimination.state != 'draft'
                or elimination.posted_at
                or elimination.posted_by_id
            ))
        )
        if frozen and historical and not engine:
            raise UserError(_(
                "Identity and period fields on a consolidation run with GL "
                "history are immutable (%(fields)s). Create a new run rather "
                "than reclassifying historical evidence.",
                fields=', '.join(frozen),
            ))
        nci_flow_inputs = set(vals) & _NCI_PERIOD_FLOW_INPUT_FIELDS
        signed_nci_flow = self.filtered(
            lambda run: run.state in ('reviewed', 'closed')
        )
        if nci_flow_inputs and signed_nci_flow and not engine:
            raise UserError(_(
                "NCI period profit/OCI basis is frozen after Review. Reset "
                "the run to draft and recompute before changing it."
            ))
        # The goodwill impairment-test fields freeze once the consolidation
        # move is posted; before that they are the manager's live inputs to
        # the IAS 36 test. The impairment engine writes goodwill_impairment_
        # amount only pre-posting under its own context key, so it is exempt.
        post_frozen = [f for f in _FROZEN_AFTER_POSTED_MOVE if f in vals]
        posted_move = self.filtered(
            lambda r: r.move_id and r.move_id.state == 'posted')
        if post_frozen and posted_move and not engine:
            raise UserError(_(
                "Goodwill impairment figures on a consolidation run whose "
                "move is posted are frozen (%(fields)s); they back the posted "
                "GL entry. Reverse the consolidation move to change them.",
                fields=', '.join(post_frozen)))
        # 'state' is guarded by eh.workflow.guard: a non-superuser cannot write
        # it directly at all, and the action methods (compute / review / close /
        # reset / reopen) run under sudo. The manager gate for un-freezing a
        # settled run (reset / reopen) is enforced at the top of those actions,
        # so there is no state-crossing arm to police here.
        result = super().write(vals)
        if (
            not engine
            and set(vals) & {
                'goodwill_recoverable_amount',
                'goodwill_impairment_account_id',
                'goodwill_recoverable_confirmed',
            }
        ):
            invalidate_vals = {
                'goodwill_test_recoverable_amount': 0.0,
                'goodwill_test_account_id': False,
                'goodwill_test_currency_id': False,
            }
            if (
                'goodwill_recoverable_amount' in vals
                and 'goodwill_recoverable_confirmed' not in vals
            ):
                invalidate_vals['goodwill_recoverable_confirmed'] = False
            self.sudo().with_context(**{
                CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
            }).write(invalidate_vals)
        if not engine and nci_flow_inputs:
            invalidate_vals = {
                'nci_period_profit_amount': 0.0,
                'nci_period_oci_amount': 0.0,
                'nci_period_comprehensive_income_amount': 0.0,
                'nci_period_flow_available': False,
            }
            changed_basis = nci_flow_inputs - {'nci_period_flow_confirmed'}
            if changed_basis and 'nci_period_flow_confirmed' not in vals:
                invalidate_vals['nci_period_flow_confirmed'] = False
            self.sudo().with_context(**{
                CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
            }).write(invalidate_vals)
        return result

    def unlink(self):
        """A settled run cannot be deleted: it carries consolidated figures
        and (once a consolidation move is posted) a GL entry that would be
        orphaned. Reset it to draft first (manager-gated) to delete it.
        """
        self._eh_assert_company_closure()
        self._eh_validate_reference_closure()
        self._eh_lock_for_transition()
        self._eh_assert_company_closure()
        self._eh_validate_reference_closure()
        settled = self.filtered(
            lambda r: r.state in _POSTED_STATES or r.move_id
            or r.reversal_move_id or r.move_history_ids or r.line_ids
            or r.disposal_event_ids
            or r.legacy_evidence_quarantined
            or r.elimination_ids.filtered(lambda elimination: (
                elimination.state != 'draft'
                or elimination.posted_at
                or elimination.posted_by_id
            )))
        if settled:
            raise UserError(_(
                "A computed, reviewed or closed consolidation run cannot be "
                "deleted; it carries settled consolidation figures and may "
                "back a posted GL move. Reset it to draft first."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition(validate_unrealised=True)
            runs._eh_compute_runs_locked()
        return True

    def _eh_compute_runs_locked(self):
        for run in self:
            run._eh_assert_not_legacy_quarantined()
            if run.state not in ('draft',):
                raise UserError(_(
                    "Compute is only available in draft state.",
                ))
            run._eh_check_policy_guards()
            run._eh_validate_period_values({}, current=run)
            run._eh_validate_impairment_account({}, current=run)
            run._eh_assert_account_role_graph()
            for member in run.entity_id.member_ids:
                member._eh_validate_finite_values({}, current=member)
            run._eh_validate_finite_values({}, current=run)
            run.elimination_ids._eh_restamp_line_outputs()
            run.line_ids.sudo().unlink()
            auto_warnings = run._build_lines()
            run._eh_stamp_posting_accounts()
            run._eh_restamp_owned_outputs()
            run._eh_assert_finite_output_graph()
            warnings = run._eh_compute_warnings() + (auto_warnings or [])
            run.sudo().write({
                'state': 'computed',
                'engine_provenance_version': _CURRENT_ENGINE_PROVENANCE,
                'source_move_version': run._eh_current_source_move_version(),
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
                'consolidation_warning': '\n'.join(warnings) or False,
                'goodwill_test_recoverable_amount': 0.0,
                'goodwill_test_account_id': False,
                'goodwill_test_currency_id': False,
                'goodwill_recoverable_confirmed': False,
                'nci_period_flow_confirmed': False,
                'nci_period_profit_amount': 0.0,
                'nci_period_oci_amount': 0.0,
                'nci_period_comprehensive_income_amount': 0.0,
                'nci_period_flow_available': False,
            })
            if warnings:
                warning_html = Markup("<br/>- ").join(
                    escape(warning) for warning in warnings
                )
                run.message_post(body=(
                    escape(_(
                        "Consolidation computed with outstanding manual steps:"
                    ))
                    + Markup("<br/>- ")
                    + warning_html
                ))
        return self

    def _eh_check_policy_guards(self):
        """IFRS 10.B87 / B92-B93 gate, checked before every compute.

        Consolidated financial statements must be prepared using uniform
        accounting policies (B87) and member reporting dates may differ from
        the group's by at most three months (B92-B93). A member breaching
        either rule blocks the compute with a UserError naming the member
        and the rule. The run-level override (override_policy_checks plus a
        mandatory reason) lets the compute proceed; the override, the
        reason, and the affected members are logged to the chatter so the
        exception is audit-visible, never silent.
        """
        self.ensure_one()
        breaches = []
        for member in self.entity_id.member_ids:
            name = member.company_id.display_name
            if member.reporting_date_offset_months \
                    > _MAX_REPORTING_OFFSET_MONTHS:
                breaches.append(_(
                    "%(member)s reports %(offset)d months off the group "
                    "reporting date (IFRS 10.B93 allows at most %(max)d)",
                    member=name,
                    offset=member.reporting_date_offset_months,
                    max=_MAX_REPORTING_OFFSET_MONTHS,
                ))
            if not member.policy_aligned:
                breaches.append(_(
                    "%(member)s is not confirmed policy-aligned "
                    "(IFRS 10.B87 requires uniform group accounting "
                    "policies)",
                    member=name,
                ))
        if not breaches:
            return
        if not self.override_policy_checks:
            raise UserError(_(
                "Run %(run)s cannot be computed; IFRS 10 consolidation "
                "guards failed:\n- %(breaches)s\n\nAlign the members, or "
                "set the run's policy-check override with a documented "
                "reason to proceed anyway.",
                run=self.name,
                breaches='\n- '.join(breaches),
            ))
        if not (self.override_policy_reason or '').strip():
            raise UserError(_(
                "Run %s overrides the IFRS 10 policy / reporting-date "
                "checks, so a written override reason is required.",
                self.name,
            ))
        breach_html = Markup("<br/>- ").join(
            escape(breach) for breach in breaches
        )
        self.message_post(body=(
            escape(_("IFRS 10 policy / reporting-date checks overridden by "))
            + escape(self.env.user.display_name)
            + Markup(".<br/>")
            + escape(_("Reason: "))
            + escape(self.override_policy_reason)
            + Markup("<br/>")
            + escape(_("Outstanding breaches:"))
            + Markup("<br/>- ")
            + breach_html
        ))

    def action_review(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can sign off a run as "
                "reviewed."
            ))
        # Keep the whole recordset transition atomic.  Callers are allowed to
        # catch a late validation/write failure; without this outer savepoint
        # an earlier run in the batch would retain a forged partial sign-off.
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition()
            for run in runs:
                run._eh_assert_current_engine_provenance()
                run._eh_assert_source_move_version_fresh()
                if run.state != 'computed':
                    raise UserError(_(
                        "Review is only available in computed state.",
                    ))
                run._eh_snapshot_nci_period_flow()
                run.sudo().write({
                    'state': 'reviewed',
                    'reviewed_at': fields.Datetime.now(),
                    'reviewed_by_id': self.env.user.id,
                })
        return True

    def action_close(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can close a reviewed run."
            ))
        # As for review, a batch close is one accounting transition.  A late
        # missing/invalid GL anchor must roll back every earlier close stamp.
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition()
            for run in runs:
                run._eh_assert_current_engine_provenance()
                run._eh_assert_source_move_version_fresh()
                if run.state not in ('reviewed',):
                    raise UserError(_(
                        "Close is only available in reviewed state.",
                    ))
                # When the entity carries a dedicated consolidation ledger
                # company, closing requires the immutable consolidation move
                # to be posted first.  Memo-only entities keep the legacy
                # close path.
                if run.entity_id.consolidation_company_id:
                    run._eh_assert_active_move_provenance()
                run.sudo().write({
                    'state': 'closed',
                    'closed_at': fields.Datetime.now(),
                    'closed_by_id': self.env.user.id,
                })
        return True

    def action_reset_to_draft(self):
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can reset a run.",
            ))
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition(
                validate_references=False,
                correction_references=True,
            )
            runs._eh_reset_runs_locked()
        return True

    def _eh_reset_runs_locked(self):
        for run in self:
            run._eh_assert_not_legacy_quarantined()
            run._eh_assert_not_cited_by_confirmed_statements()
            # A closed run is signed and cited in audit. It must not be
            # reset (and so recomputed) in place. Reopening it is a distinct,
            # explicit action that leaves an audit trail; require it first.
            if run.state == 'closed':
                raise UserError(_(
                    "Run %s is closed and cannot be reset. A manager must "
                    "reopen it first (Reopen), which is recorded, before it "
                    "can be reset and recomputed.", run.name,
                ))
            if (
                run.state != 'draft'
                or run.move_id
                or run.reversal_move_id
                or run.move_history_ids
            ):
                run._eh_assert_correction_provenance()
            # A reset drops all computed lines, so a consolidation move posted
            # from those lines must be undone first, otherwise the posted move
            # would outlive the figures it represents. Reverse and unlink it
            # (manager-gated here, logged) before dropping the lines.
            run._eh_reverse_consolidation_move(
                require_goodwill_test=False,
            )
            run.line_ids.sudo().with_context(
                **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY}).unlink()
            run._eh_restamp_owned_outputs()
            run.sudo().write({
                'state': 'draft',
                'engine_provenance_version': 0,
                'source_move_version': 0,
                'posting_predecessor_run_id': False,
                'computed_at': False,
                'computed_by_id': False,
                'reviewed_at': False,
                'reviewed_by_id': False,
                'closed_at': False,
                'closed_by_id': False,
                'goodwill_recoverable_amount': 0.0,
                'goodwill_recoverable_confirmed': False,
                'goodwill_impairment_amount': 0.0,
                'goodwill_impairment_account_id': False,
                'goodwill_test_recoverable_amount': 0.0,
                'goodwill_test_account_id': False,
                'goodwill_test_currency_id': False,
                'nci_period_flow_confirmed': False,
                'nci_period_profit_amount': 0.0,
                'nci_period_oci_amount': 0.0,
                'nci_period_comprehensive_income_amount': 0.0,
                'nci_period_flow_available': False,
                'consolidation_warning': False,
            })
        return self

    def action_reopen(self):
        """Manager-gated reopen of a closed run.

        A closed consolidation is locked and reproducible: its lines are
        frozen and it cannot be recomputed or reset. Reopening is the single
        explicit, audited path back to an editable state. It moves the run to
        reviewed (not draft) so the existing lines are preserved until the
        manager deliberately resets and recomputes.
        """
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only a consolidation manager can reopen a closed run.",
            ))
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition(
                validate_references=False,
                correction_references=True,
            )
            runs._eh_reopen_runs_locked()
        return True

    def _eh_reopen_runs_locked(self):
        for run in self:
            run._eh_assert_not_cited_by_confirmed_statements()
            run._eh_assert_correction_provenance()
            if run.state != 'closed':
                raise UserError(_(
                    "Reopen is only available for a closed run.",
                ))
            # Reopening a closed run that carried a posted consolidation move
            # must undo that move: the signed consolidation is being unlocked
            # for correction, so its posted entry can no longer stand. Reverse
            # and unlink it (logged) as part of the reopen.
            run._eh_reverse_consolidation_move()
            run.sudo().write({
                'state': 'reviewed',
                'closed_at': False,
                'closed_by_id': False,
            })
            run.message_post(body=_(
                "Run reopened for correction by %s.",
                self.env.user.display_name,
            ))
        return self

    # ---- IAS 36 goodwill impairment ----

    def _eh_goodwill_accounts(self):
        """Return the account.account records that carry recognised goodwill
        for this run: the goodwill account configured on each full member whose
        IFRS 3 acquisition elimination booked goodwill. Empty when no member
        carries a goodwill account.
        """
        self.ensure_one()
        accounts = self.env['account.account'].browse()
        for member in self.entity_id.member_ids:
            if member.goodwill_account_id:
                accounts |= member.goodwill_account_id
        return accounts

    def _eh_recognised_goodwill(self):
        """Return the net recognised goodwill on this run: the signed sum of
        every run line booked to a goodwill account, in the presentation
        currency. Goodwill is an asset, so a positive figure is a debit
        balance (recognised goodwill); a bargain-purchase credit nets it down.
        """
        self.ensure_one()
        goodwill_accounts = self._eh_goodwill_accounts()
        if not goodwill_accounts:
            return 0.0
        total = sum(
            self.line_ids.filtered(
                lambda l: l.account_id in goodwill_accounts
            ).mapped('amount')
        )
        return self.presentation_currency_id.round(total)

    def _eh_unimpaired_goodwill(self):
        """Return recognised goodwill before any IAS 36 charge on this run."""
        self.ensure_one()
        accounts = self._eh_goodwill_accounts()
        total = sum(self.line_ids.filtered(
            lambda line: (
                line.account_id in accounts and line.kind != 'impairment'
            )
        ).mapped('amount'))
        return self.presentation_currency_id.round(total)

    def _eh_stamp_goodwill_test(self, account=False):
        self.ensure_one()
        self.with_context(**{
            CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
        }).write({
            'goodwill_test_recoverable_amount': (
                self.goodwill_recoverable_amount
            ),
            'goodwill_test_account_id': account.id if account else False,
            'goodwill_test_currency_id': self.presentation_currency_id.id,
        })
        return self

    def _eh_assert_goodwill_test_fresh(self):
        """Prove the IAS 36 lines still match the current tested basis."""
        for run in self:
            currency = run.presentation_currency_id
            rounding = currency.rounding or 0.01
            recognised = run._eh_assert_finite_number(
                run._eh_unimpaired_goodwill(), _("Unimpaired goodwill"),
            )
            impairment_lines = run.line_ids.filtered(
                lambda line: line.kind == 'impairment')
            if recognised <= 0.0 or float_is_zero(
                recognised, precision_rounding=rounding,
            ):
                if impairment_lines or not float_is_zero(
                    run.goodwill_impairment_amount,
                    precision_rounding=rounding,
                ):
                    raise UserError(_(
                        "Run %s carries impairment evidence without positive "
                        "recognised goodwill.", run.name,
                    ))
                continue
            if (
                (
                    float_is_zero(
                        run.goodwill_recoverable_amount,
                        precision_rounding=rounding,
                    )
                    and not run.goodwill_recoverable_confirmed
                )
                or
                run.goodwill_test_currency_id != currency
                or run.goodwill_test_account_id
                != run.goodwill_impairment_account_id
                or not float_is_zero(
                    currency.round(run.goodwill_test_recoverable_amount)
                    - currency.round(run.goodwill_recoverable_amount),
                    precision_rounding=rounding,
                )
            ):
                raise UserError(_(
                    "Run %s has recognised goodwill but its latest IAS 36 "
                    "test does not match the current recoverable amount, "
                    "expense account and currency. Re-run the goodwill test.",
                    run.name,
                ))
            expected = currency.round(max(
                recognised - run.goodwill_recoverable_amount, 0.0,
            ))
            if float_is_zero(expected, precision_rounding=rounding):
                if impairment_lines or not float_is_zero(
                    run.goodwill_impairment_amount,
                    precision_rounding=rounding,
                ):
                    raise UserError(_(
                        "Run %s carries an impairment charge although its "
                        "current IAS 36 basis requires none.", run.name,
                    ))
                continue
            account = run.goodwill_test_account_id
            positive = impairment_lines.filtered(lambda line: line.amount > 0.0)
            negative = impairment_lines.filtered(lambda line: line.amount < 0.0)
            if (
                not account
                or account.account_type not in _EXPENSE_TYPES
                or len(impairment_lines) != 2
                or len(positive) != 1
                or len(negative) != 1
                or positive.account_id != account
                or negative.account_id not in run._eh_goodwill_accounts()
                or not float_is_zero(
                    currency.round(positive.amount) - expected,
                    precision_rounding=rounding,
                )
                or not float_is_zero(
                    currency.round(negative.amount) + expected,
                    precision_rounding=rounding,
                )
                or not float_is_zero(
                    currency.round(run.goodwill_impairment_amount) - expected,
                    precision_rounding=rounding,
                )
            ):
                raise UserError(_(
                    "Run %s no longer carries the exact impairment pair "
                    "produced by its current IAS 36 test.", run.name,
                ))
        return self

    def _eh_invalidate_review_after_engine_mutation(self):
        reviewed = self.filtered(lambda run: run.state == 'reviewed')
        if reviewed:
            reviewed.sudo().with_context(**{
                CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
            }).write({
                'state': 'computed',
                'reviewed_at': False,
                'reviewed_by_id': False,
                'nci_period_flow_confirmed': False,
                'nci_period_profit_amount': 0.0,
                'nci_period_oci_amount': 0.0,
                'nci_period_comprehensive_income_amount': 0.0,
                'nci_period_flow_available': False,
            })
        return self

    def _eh_impairment_account(self):
        """Return the account the goodwill impairment charge is debited to.

        Prefers the run's goodwill_impairment_account_id; only when unset falls
        back to a name heuristic (an expense account on the parent chart whose
        name contains 'impairment'). Returns empty when neither resolves, so
        the caller refuses the test rather than posting an accountless charge.
        """
        self.ensure_one()
        if self.goodwill_impairment_account_id:
            return self.goodwill_impairment_account_id
        Account = self.env['account.account'].sudo()
        return Account.search([
            ('account_type', 'in', list(_EXPENSE_TYPES)),
            ('name', 'ilike', 'impairment'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=1)

    def action_impair_goodwill(self):
        """Manager-gated IAS 36 goodwill impairment test on a computed /
        reviewed run.

        Tests the recognised goodwill (the signed sum of run lines on the
        members' goodwill accounts) against goodwill_recoverable_amount. When
        the recognised goodwill exceeds the recoverable amount the excess is
        booked as a balanced two-leg impairment run line:

          Dr impairment expense account   amount = +impairment
          Cr goodwill account             amount = -impairment

        The pair nets to zero by construction, so the run stays balanced and
        the CTA plug is untouched. When a consolidation ledger company is
        configured the new lines feed the posted move exactly like every other
        run line (post the move after running the test). Re-running the test
        first reverses any prior impairment on the run so the charge is never
        double-booked. When the goodwill is not impaired (recoverable amount at
        or above recognised goodwill) any prior impairment is reversed and no
        new charge is booked.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can test goodwill for "
                "impairment.",
            ))
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition()
            runs._eh_impair_goodwill_locked()
        return True

    def _eh_impair_goodwill_locked(self):
        for run in self:
            run._eh_assert_current_engine_provenance(
                require_goodwill_test=False,
            )
            run._eh_assert_source_move_version_fresh()
            if run.state not in ('computed', 'reviewed'):
                raise UserError(_(
                    "Goodwill impairment can only be tested on a computed or "
                    "reviewed run.",
                ))
            if run.move_id and run.move_id.state == 'posted':
                raise UserError(_(
                    "Run %s already has a posted consolidation move; reopen it "
                    "before re-testing goodwill impairment so the charge is "
                    "reflected in the move.", run.name,
                ))
            goodwill_accounts = run._eh_goodwill_accounts()
            if not goodwill_accounts:
                raise UserError(_(
                    "Run %s carries no goodwill account (no full member has a "
                    "Goodwill Account configured), so there is no recognised "
                    "goodwill to test for impairment.", run.name,
                ))
            currency = run.presentation_currency_id
            rounding = currency.rounding or 0.01
            if (
                float_is_zero(
                    run.goodwill_recoverable_amount,
                    precision_rounding=rounding,
                )
                and not run.goodwill_recoverable_confirmed
            ):
                raise UserError(_(
                    "Run %(run)s has no manager-confirmed IAS 36 recoverable "
                    "amount. Determine the amount and tick Recoverable Amount "
                    "Determined before testing; an untouched zero must never "
                    "write off all goodwill.",
                    run=run.name,
                ))
            # Drop any prior impairment lines so the test is idempotent and the
            # recognised-goodwill base is measured before the charge.
            prior = run.line_ids.filtered(lambda l: l.kind == 'impairment')
            if prior:
                prior.sudo().with_context(
                    **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY}).unlink()
            recognised = run._eh_assert_finite_number(
                run._eh_recognised_goodwill(), _("Recognised goodwill"),
            )
            recoverable = currency.round(run.goodwill_recoverable_amount or 0.0)
            raw_impairment = run._eh_assert_finite_number(
                recognised - recoverable, _("Goodwill impairment basis"),
            )
            impairment = currency.round(raw_impairment)
            if (
                float_is_zero(impairment, precision_rounding=rounding)
                or impairment < 0.0
            ):
                # Not impaired: recoverable amount is at or above recognised
                # goodwill. Leave the run with no impairment charge. This is a
                # sanctioned engine write of a frozen (readonly) figure, so it
                # carries the engine context flag past the write freeze.
                run.with_context(
                    **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY}
                ).goodwill_impairment_amount = 0.0
                run._eh_stamp_goodwill_test(
                    run.goodwill_impairment_account_id,
                )
                run._eh_invalidate_review_after_engine_mutation()
                run.message_post(body=_(
                    "Goodwill impairment test: recognised goodwill %(gw)s is "
                    "within the recoverable amount %(rec)s. No impairment "
                    "booked.",
                    gw=recognised, rec=recoverable,
                ))
                run._eh_restamp_owned_outputs()
                run._eh_assert_finite_output_graph()
                run._eh_assert_goodwill_test_fresh()
                continue
            impairment_account = run._eh_impairment_account()
            if not impairment_account:
                raise UserError(_(
                    "Run %(run)s has a goodwill impairment of %(amount)s to "
                    "book, but no impairment expense account could be "
                    "resolved. Set a Goodwill Impairment Expense Account on "
                    "the run (or create an expense account whose name contains "
                    "'impairment' on the parent chart).",
                    run=run.name,
                    amount=currency.round(impairment),
                ))
            if not run.goodwill_impairment_account_id:
                # The legacy heuristic remains a convenience, but the chosen
                # account becomes an explicit immutable test input before any
                # evidence is created.  Freshness never depends on a later
                # name/sequence search.
                run.with_context(**{
                    CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY,
                }).write({
                    'goodwill_impairment_account_id': impairment_account.id,
                })
            # Credit the goodwill account carrying the largest recognised
            # goodwill balance (asset, debit-positive), so the impairment
            # reduces the goodwill that is actually recognised.
            goodwill_target = run._eh_impairment_goodwill_target(
                goodwill_accounts)
            Line = self.env['eh.consol.run.line'].sudo().with_context(
                **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY})
            base = {
                'run_id': run.id,
                'kind': 'impairment',
            }
            created = Line.create([
                dict(base,
                     account_id=impairment_account.id,
                     amount=impairment,
                     notes=_("IAS 36 goodwill impairment charge")),
                dict(base,
                     account_id=goodwill_target.id,
                     amount=-impairment,
                     notes=_("IAS 36 goodwill impairment write-down")),
            ])
            run._eh_stamp_posting_accounts(created)
            run.with_context(
                **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY}
            ).goodwill_impairment_amount = impairment
            run._eh_stamp_goodwill_test(impairment_account)
            run._eh_invalidate_review_after_engine_mutation()
            run.message_post(body=_(
                "Goodwill impairment of %(amount)s booked: recognised goodwill "
                "%(gw)s exceeds the recoverable amount %(rec)s (IAS 36).",
                amount=currency.round(impairment),
                gw=recognised, rec=recoverable,
            ))
            run._eh_restamp_owned_outputs()
            run._eh_assert_finite_output_graph()
            run._eh_assert_goodwill_test_fresh()
        return self

    def _eh_impairment_goodwill_target(self, goodwill_accounts):
        """Return the single goodwill account to credit for the impairment
        write-down: the one carrying the largest recognised (debit) goodwill
        balance on this run, so the write-down lands on real goodwill. Falls
        back to the first configured goodwill account when none carries a
        balance (should not happen when recognised goodwill is positive).
        """
        self.ensure_one()
        best = None
        best_bal = None
        for account in goodwill_accounts:
            bal = sum(
                self.line_ids.filtered(
                    lambda l: l.account_id == account
                ).mapped('amount')
            )
            if best_bal is None or bal > best_bal:
                best, best_bal = account, bal
        return best or goodwill_accounts[:1]

    # ---- IAS 21.48 member-disposal CTA recycling ----

    def _eh_member_cta_balance(self, member):
        """Return the member's REMAINING accumulated CTA on this run: the
        signed sum of its kind='cta' lines plus any prior kind='cta_recycle'
        legs booked on the same CTA account(s) (the recycle debit reduces
        the credit reserve, so repeated partial disposals draw the balance
        down and can never recycle more than was accumulated). Credit
        negative: a negative balance is an accumulated translation gain.
        """
        self.ensure_one()
        cta_lines = self.line_ids.filtered(
            lambda l: l.kind == 'cta' and l.member_id == member)
        cta_accounts = cta_lines.mapped('account_id')
        recycle_reserve_legs = self.line_ids.filtered(
            lambda l: l.kind == 'cta_recycle' and l.member_id == member
            and l.account_id in cta_accounts)
        return self.presentation_currency_id.round(
            sum(cta_lines.mapped('amount'))
            + sum(recycle_reserve_legs.mapped('amount')))

    def _eh_disposal_event_line_vals(self, event):
        """Reconstruct the two immutable run lines recorded by one event."""
        self.ensure_one()
        if event.run_id != self or event.member_id.entity_id != self.entity_id:
            raise UserError(_(
                "A persisted CTA disposal event is outside its run/member graph."
            ))
        base = {
            'run_id': self.id,
            'kind': 'cta_recycle',
            'company_id': event.member_id.company_id.id,
            'member_id': event.member_id.id,
            'cta_position_id': event.cta_position_id.id or False,
            'disposal_event_id': event.id,
            'notes': event.notes,
        }
        return [
            dict(
                base, account_id=event.reserve_account_id.id,
                amount=-event.reclass_amount,
            ),
            dict(
                base, account_id=event.counterpart_account_id.id,
                amount=event.reclass_amount,
            ),
        ]

    def _eh_rebuild_disposal_event_lines(self):
        """Rebuild IAS 21.48 lines after a reset/recompute from durable events.

        An old event must still fit inside the newly computed parent CTA. If a
        source correction changes the reserve's sign or makes the persisted
        disposal larger than the reserve, recompute is refused: rewriting the
        historical event would destroy evidence and replaying it would overdraw
        CTA.
        """
        self.ensure_one()
        if not self.disposal_event_ids:
            return self.env['eh.consol.run.line']
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        remaining = {
            member.id: currency.round(sum(
                self.line_ids.filtered(
                    lambda line, m=member: (
                        line.kind == 'cta' and line.member_id == m
                    )
                ).mapped('amount')
            ))
            for member in self.disposal_event_ids.mapped('member_id')
        }
        vals_list = []
        for event in self.disposal_event_ids.sorted(
            key=lambda row: (row.event_at, row.id),
        ):
            available = remaining.get(event.member_id.id, 0.0)
            reclass = event.reclass_amount
            if (
                float_is_zero(available, precision_rounding=rounding)
                or available * reclass < 0.0
                or abs(reclass) - abs(available) > rounding / 2.0
            ):
                raise UserError(_(
                    "Run %(run)s cannot replay disposal event %(event)s: its "
                    "recorded CTA treatment %(amount)s no longer fits the "
                    "recomputed member reserve %(reserve)s. Preserve the event "
                    "and create a correcting run instead.",
                    run=self.name,
                    event=event.id,
                    amount=currency.round(reclass),
                    reserve=currency.round(available),
                ))
            vals_list.extend(self._eh_disposal_event_line_vals(event))
            remaining[event.member_id.id] = currency.round(
                available - reclass,
            )
        Line = self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY},
        )
        created = Line.create(vals_list)
        return created

    def _eh_recycle_member_cta(self, member, pct, loses_control=False):
        """Apply IAS 21.48/48C to a member disposal on this run.

        Loss of control reclassifies the entire remaining parent-attributable
        CTA to profit or loss. A partial disposal of a subsidiary that retains
        control does not touch profit or loss: its proportionate CTA slice is
        reattributed to NCI. Partial disposals of non-subsidiary interests keep
        proportionate P&L recycling.

        Two balanced kind='cta_recycle' legs are booked:

          accumulated GAIN (balance credit-negative):
            Dr CTA reserve account   amount = -reclass  (positive)
            Cr FX gain account       amount = +reclass  (negative)

          accumulated LOSS (balance debit-positive): the mirrored pair
          through the FX loss account.

        The pair nets to zero by construction, so the run stays balanced.
        For retained-control subsidiary disposals, the counterpart is the
        member/entity NCI equity account. Otherwise, the P&L account comes from
        the member's CTA position link (gain/loss reclass accounts) when set,
        else from the entity's CTA recycling accounts. Refused when the run's
        consolidation move is already posted (reopen first, like the impairment
        test) so the posted GL never diverges from the run lines.
        """
        self.ensure_one()
        self._eh_assert_current_engine_provenance()
        self._eh_assert_source_move_version_fresh()
        if self.state not in ('computed', 'reviewed'):
            raise UserError(_(
                "CTA can only be recycled on a computed or reviewed run.",
            ))
        if self.move_id and self.move_id.state == 'posted':
            raise UserError(_(
                "Run %s already has a posted consolidation move; reopen it "
                "before recycling CTA so the reclassification is reflected "
                "in the move.", self.name,
            ))
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        balance = self._eh_member_cta_balance(member)
        if float_is_zero(balance, precision_rounding=rounding):
            raise UserError(_(
                "Member %(member)s carries no remaining accumulated CTA on "
                "run %(run)s; there is nothing to recycle.",
                member=member.company_id.display_name,
                run=self.name,
            ))
        retained_control = (
            member.method == 'full'
            and pct < 100.0
            and not loses_control
        )
        effective_pct = (
            100.0
            if member.method == 'full' and (pct >= 100.0 or loses_control)
            else pct
        )
        raw_reclass = self._eh_assert_finite_number(
            balance * effective_pct / 100.0, _("CTA disposal basis"),
        )
        reclass = currency.round(raw_reclass)
        if float_is_zero(reclass, precision_rounding=rounding):
            raise UserError(_(
                "The disposal share of member %s's accumulated CTA rounds "
                "to zero; nothing to recycle.",
                member.company_id.display_name,
            ))
        # The reserve leg reverses the plug: a credit-negative (gain)
        # balance is debited out of the reserve; the P&L leg books the
        # mirrored gain (credit) or loss (debit).
        position = member.cta_position_id
        if retained_control:
            counterpart_account = (
                member.nci_account_id or self._nci_account()
            )
            missing_label = _("NCI Equity Account")
            treatment = _("reattributed to non-controlling interest")
        elif balance < 0.0:
            counterpart_account = (
                position.gain_account_id
                or self.entity_id.cta_gain_account_id
            )
            missing_label = _("CTA Recycling Gain Account")
            treatment = _("recycled to profit or loss")
        else:
            counterpart_account = (
                position.loss_account_id
                or self.entity_id.cta_loss_account_id
            )
            missing_label = _("CTA Recycling Loss Account")
            treatment = _("recycled to profit or loss")
        if not counterpart_account:
            raise UserError(_(
                "No %(label)s is configured (set it on the entity, or on "
                "the member where applicable), so the CTA of member "
                "%(member)s cannot receive the required disposal treatment.",
                label=missing_label,
                member=member.company_id.display_name,
            ))
        cta_lines = self.line_ids.filtered(
            lambda l: l.kind == 'cta' and l.member_id == member)
        reserve_account = cta_lines.mapped('account_id')[:1]
        if not reserve_account:
            raise UserError(_(
                "Member %s carries no CTA reserve line on this run.",
                member.company_id.display_name,
            ))
        note = _(
            "IAS 21.48/48C CTA disposal treatment for %(member)s "
            "(%(pct).2f%% disposed; %(treatment)s)",
            member=member.company_id.display_name,
            pct=pct,
            treatment=treatment,
        )
        Event = self.env['eh.consol.disposal.event'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY},
        )
        event = Event.create({
            'run_id': self.id,
            'member_id': member.id,
            'event_at': fields.Datetime.now(),
            'event_by_id': self.env.user.id,
            'disposal_pct': pct,
            'effective_pct': effective_pct,
            'loses_control': bool(loses_control or pct >= 100.0),
            'treatment': 'nci' if retained_control else 'profit_or_loss',
            'reserve_account_id': reserve_account.id,
            'counterpart_account_id': counterpart_account.id,
            'cta_position_id': position.id or False,
            'reclass_amount': reclass,
            'notes': note,
        })
        Line = self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY},
        )
        created = Line.create(self._eh_disposal_event_line_vals(event))
        self._eh_stamp_posting_accounts(created)
        self._eh_restamp_owned_outputs()
        self._eh_assert_finite_output_graph()
        self._eh_invalidate_review_after_engine_mutation()
        self.message_post(body=_(
            "CTA of %(amount)s %(treatment)s on disposal of %(member)s "
            "(%(pct).2f%% disposed, %(effective).2f%% reserve treatment, "
            "IAS 21.48/48C) by %(user)s. Remaining member CTA: %(left)s.",
            amount=currency.round(abs(reclass)),
            treatment=treatment,
            member=member.company_id.display_name,
            pct=pct,
            effective=effective_pct,
            user=self.env.user.display_name,
            left=self._eh_member_cta_balance(member),
        ))
        return True

    # ---- consolidation move (IFRS 10 auditability) ----

    def _eh_assert_no_active_posting_successor(self):
        """A snapshot cannot be reversed while a later delta depends on it."""
        successor = self.env['eh.consol.run'].sudo().search([
            ('posting_predecessor_run_id', 'in', self.ids),
            ('move_id', '!=', False),
            ('move_id.state', '=', 'posted'),
        ], limit=1)
        if successor:
            raise UserError(_(
                "Run %(run)s is the posting basis of later active run "
                "%(successor)s. Reverse/reset the ledger chain newest-first "
                "before correcting this snapshot.",
                run=self.display_name,
                successor=successor.display_name,
            ))
        return self

    def _eh_posting_predecessor(self):
        """Return the last proven active snapshot, rejecting overlap/stacking.

        Every posting transition holds the entity row lock, so the complete
        active chain is stable while this method validates it.  A historical
        database containing multiple old full-snapshot moves with no chain is
        refused rather than silently treating a stacked ledger as a valid
        opening position.
        """
        self.ensure_one()
        active = self.env['eh.consol.run'].sudo().search([
            ('entity_id', '=', self.entity_id.id),
            ('id', '!=', self.id),
            ('move_id', '!=', False),
            ('move_id.state', '=', 'posted'),
        ], order='period_to, period_from, id')
        previous = self.env['eh.consol.run']
        for candidate in active:
            if previous and candidate.period_from <= previous.period_to:
                raise UserError(_(
                    "Active consolidation runs %(first)s and %(second)s "
                    "overlap. Reverse the later move and rebuild a "
                    "non-overlapping chronological ledger chain.",
                    first=previous.display_name,
                    second=candidate.display_name,
                ))
            if candidate.posting_predecessor_run_id != previous:
                raise UserError(_(
                    "The active consolidation ledger contains unchained "
                    "complete snapshots at run %(run)s. Reverse the legacy "
                    "stack newest-first before posting another period.",
                    run=candidate.display_name,
                ))
            candidate._eh_assert_active_move_provenance(correction=True)
            previous = candidate
        if not previous:
            return previous
        if self.period_to < previous.period_from:
            raise UserError(_(
                "Run %(run)s is earlier than the latest posted snapshot "
                "%(prior)s. Post consolidation snapshots chronologically.",
                run=self.display_name,
                prior=previous.display_name,
            ))
        if self.period_from <= previous.period_to:
            raise UserError(_(
                "Run %(run)s overlaps active posted run %(prior)s. Posted "
                "consolidation periods must be non-overlapping.",
                run=self.display_name,
                prior=previous.display_name,
            ))
        return previous

    def action_post_move(self):
        """Post a single balanced, immutable consolidation move (manager-gated).

        Available only when the entity carries a dedicated consolidation ledger
        company and the run is computed or reviewed. Aggregates every run line
        by account into one signed net per account, maps each account into the
        consolidation company's chart by code, and books one journal line per
        account (Dr for a positive net, Cr for a negative net). The run already
        nets to zero (the CTA plug guarantees it), so the move balances by
        construction. The move is stored on the run and, once posted, is
        immutable via Odoo's posted state.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can post the consolidation move.",
            ))
        with self.env.cr.savepoint():
            runs = self._eh_prepare_transition()
            runs._eh_post_consolidation_moves_locked()
        return True

    def _eh_post_consolidation_moves_locked(self):
        """Build, post and anchor every move inside the caller's savepoint."""
        for run in self.sorted(lambda item: (
            item.period_to, item.period_from, item.id,
        )):
            run._eh_assert_current_engine_provenance()
            run._eh_assert_source_move_version_fresh()
            company = run.entity_id.consolidation_company_id
            if not company:
                raise UserError(_(
                    "Run %s has no consolidation ledger company configured on "
                    "its entity; there is nowhere to post a consolidation "
                    "move.", run.name,
                ))
            if run.state not in ('computed', 'reviewed'):
                raise UserError(_(
                    "The consolidation move can only be posted from a computed "
                    "or reviewed run.",
                ))
            if run.move_id:
                raise UserError(_(
                    "Run %s already has an active consolidation move (%s).",
                    run.name, run.move_id.name,
                ))
            run._eh_assert_posting_period_open(company)
            predecessor = run._eh_posting_predecessor()
            run.sudo().write({
                'posting_predecessor_run_id': predecessor.id or False,
            })
            move = run._eh_build_consolidation_move(company)
            move.action_post()
            posting_rounding = run._eh_assert_finite_number(
                company.currency_id.rounding,
                _("Consolidation posting-currency rounding"),
                positive=True,
            )
            run.sudo().write({
                'move_id': move.id,
                'reversal_move_id': False,
                'move_history_ids': [(4, move.id)],
                'posting_currency_rounding': posting_rounding,
            })
            run.message_post(body=_(
                "Consolidation move %s posted to %s by %s.",
                move.name, company.display_name, self.env.user.display_name,
            ))
        return self

    def _eh_assert_posting_period_open(self, company):
        """Fail before building a GL move when the snapshot date is locked.

        Odoo may otherwise shift a miscellaneous entry to the first open date.
        A consolidation snapshot is period evidence and must stay dated at
        ``period_to``; silently moving it outside that period is forbidden.
        """
        self.ensure_one()
        locks = []
        for field_name, label in (
            ('hard_lock_date', _("hard lock date")),
            ('fiscalyear_lock_date', _("fiscal-year lock date")),
        ):
            if field_name not in company._fields:
                continue
            lock_date = fields.Date.to_date(company[field_name])
            if lock_date and self.period_to <= lock_date:
                locks.append((label, lock_date))
        if locks:
            label, lock_date = max(locks, key=lambda item: item[1])
            raise UserError(_(
                "Run %(run)s ends on %(date)s, which is closed in "
                "consolidation ledger %(company)s by its %(lock)s "
                "(%(lock_date)s). Reopen the period or post a later, "
                "non-overlapping consolidation run; the engine will not "
                "shift period evidence to another date.",
                run=self.display_name,
                date=self.period_to,
                company=company.display_name,
                lock=label,
                lock_date=lock_date,
            ))
        return True

    def _eh_consolidation_journal(self, company):
        """Return the general journal to book the consolidation move to.

        Prefers the entity's configured consolidation journal; otherwise the
        first general journal in the consolidation company. Raises when none
        exists.
        """
        self.ensure_one()
        journal = self.entity_id.consolidation_journal_id
        if journal and journal.company_id == company:
            return journal
        journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id),
            ('type', '=', 'general'),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No general journal exists in the consolidation company %s. "
                "Create one (or set a Consolidation Journal on the entity) "
                "before posting the consolidation move.",
                company.display_name,
            ))
        return journal

    def _eh_resolve_consol_account(self, account, company, cache,
                                   source_company=None):
        """Map a run-line account into the consolidation company's chart by
        code, returning the resolved account.account in that company.

        When the consolidation company already owns the account (shared chart)
        it is returned as-is. Otherwise an account with the same code in the
        consolidation company is looked up. Raises a clear UserError naming the
        account when no counterpart exists, so a missing mapping fails loudly
        rather than silently dropping a leg.
        """
        self.ensure_one()
        if not account:
            raise UserError(_(
                "Run %s carries a consolidation line with no account (for "
                "example a CTA or NCI line with no configured account). "
                "Configure the account before posting the consolidation "
                "move.", self.name,
            ))
        source_company = source_company or (
            account.company_ids[:1]
            if 'company_ids' in account._fields else account.company_id
        ) or company
        cache_key = (account.id, source_company.id)
        if cache_key in cache:
            return cache[cache_key]
        company_field = _acc_company_field(self.env)
        if company_field == 'company_ids':
            owns = company in account.company_ids
        else:
            owns = account.company_id == company
        if owns:
            cache[cache_key] = account
            return account
        # Resolve by the source account's code. account.account.code is a
        # company-dependent computed field on Odoo 19, so read it in the
        # SOURCE account's own company context (env.company may be a third
        # company under which a parent-only account's code reads empty), then
        # look it up in the TARGET company's context to match the code as
        # stored there.
        code = account.with_company(source_company).code
        Account = self.env['account.account'].sudo().with_company(company)
        resolved = Account.search([
            ('code', '=', code),
            (company_field, 'in', company.ids),
        ], limit=1)
        if not resolved:
            raise UserError(_(
                "Account %(code)s %(name)s is not present in the "
                "consolidation company %(company)s chart of accounts. Add it "
                "(by code), or point the consolidation company at the parent "
                "chart, before posting the consolidation move.",
                code=code or '?',
                name=account.name or '',
                company=company.display_name,
            ))
        cache[cache_key] = resolved
        return resolved

    def _eh_build_consolidation_move(self, company):
        """Build (not post) the balanced consolidation account.move for one run
        in the consolidation ledger company.

        One journal line per account, from the signed net of the run lines on
        that account. The signed net follows Odoo's convention (debit positive,
        credit negative), so a positive net books a debit and a negative net a
        credit. Rounding is in the company currency. The run already nets to
        zero, so the sum of the legs is zero by construction; a residual (only
        possible from per-account rounding) is absorbed into the largest leg so
        the move always balances.
        """
        self.ensure_one()
        journal = self._eh_consolidation_journal(company)
        line_vals = self._eh_consolidation_move_line_vals(company)
        move = self.env['account.move'].sudo()._eh_create_sealed({
            'company_id': company.id,
            'move_type': 'entry',
            'date': self.period_to,
            'journal_id': journal.id,
            'ref': "Consolidation %s" % self.name,
            'eh_sealed': True,
            'line_ids': [(0, 0, v) for v in line_vals],
        })
        return move

    def _eh_balanced_move_line_vals(self, by_account, rounding):
        """Convert signed account amounts into one deterministic move shape."""
        line_vals = []
        for acc_id, amount in by_account.items():
            rounded = float_round(
                self._eh_assert_finite_number(
                    amount, _("Consolidation move account amount"),
                ),
                precision_rounding=rounding,
            )
            if float_is_zero(rounded, precision_rounding=rounding):
                continue
            line_vals.append({
                'account_id': acc_id,
                'debit': rounded if rounded > 0.0 else 0.0,
                'credit': -rounded if rounded < 0.0 else 0.0,
            })
        if not line_vals:
            raise UserError(_(
                "Run %s produced no non-zero consolidation movement to post.",
                self.name,
            ))
        # Absorb any residual (from per-account rounding) into the largest leg
        # so the move balances exactly. The run nets to zero pre-rounding, so
        # this residual is at most a rounding unit per account.
        net = float_round(
            sum(v['debit'] - v['credit'] for v in line_vals),
            precision_rounding=rounding,
        )
        if not float_is_zero(net, precision_rounding=rounding):
            biggest = max(
                line_vals,
                key=lambda v: abs(v['debit'] - v['credit']))
            adjusted = float_round(
                (biggest['debit'] - biggest['credit']) - net,
                precision_rounding=rounding,
            )
            biggest['debit'] = adjusted if adjusted > 0.0 else 0.0
            biggest['credit'] = -adjusted if adjusted < 0.0 else 0.0
        return line_vals

    @api.model
    def _eh_signed_move_amounts(self, line_vals):
        return {
            vals['account_id']: vals['debit'] - vals['credit']
            for vals in line_vals
        }

    def _eh_consolidation_snapshot_line_vals(
        self, company, correction=False,
    ):
        """Return the balanced full snapshot represented by this run."""
        self.ensure_one()
        by_account = defaultdict(float)
        for line in self.line_ids:
            if line.kind == 'disclosure':
                continue
            resolved = line.posting_account_id
            if not resolved or (
                not correction
                and not (
                    self._eh_account_company_ids(resolved) & set(company.ids)
                )
            ):
                raise UserError(_(
                    "Run %(run)s carries a missing or wrong-company immutable "
                    "posting-account snapshot.",
                    run=self.name,
                ))
            by_account[resolved.id] += line.amount
        rounding = (
            self.posting_currency_rounding
            if correction else company.currency_id.rounding
        )
        rounding = self._eh_assert_finite_number(
            rounding,
            _("Consolidation posting-currency rounding"),
            positive=True,
        )
        return self._eh_balanced_move_line_vals(by_account, rounding)

    def _eh_consolidation_move_line_vals(self, company, correction=False):
        """Return a balanced delta from the prior to the current snapshot."""
        self.ensure_one()
        current = self._eh_signed_move_amounts(
            self._eh_consolidation_snapshot_line_vals(
                company, correction=correction,
            )
        )
        predecessor = self.posting_predecessor_run_id
        if predecessor:
            if (
                predecessor.entity_id != self.entity_id
                or predecessor.period_to >= self.period_from
            ):
                raise UserError(_(
                    "Run %s carries an invalid posting predecessor.",
                    self.name,
                ))
            prior = self._eh_signed_move_amounts(
                predecessor._eh_consolidation_snapshot_line_vals(
                    company, correction=True,
                )
            )
        else:
            prior = {}
        delta = {
            account_id: current.get(account_id, 0.0)
            - prior.get(account_id, 0.0)
            for account_id in set(current) | set(prior)
        }
        rounding = (
            self.posting_currency_rounding
            if correction else company.currency_id.rounding
        )
        rounding = self._eh_assert_finite_number(
            rounding,
            _("Consolidation posting-currency rounding"),
            positive=True,
        )
        return self._eh_balanced_move_line_vals(delta, rounding)

    def _eh_assert_active_move_provenance(self, correction=False):
        """Prove the active pointer is the current, unoffset engine output."""
        self.ensure_one()
        if correction:
            self._eh_assert_correction_provenance()
        else:
            self._eh_assert_current_engine_provenance()
        move = self.move_id
        company = self.entity_id.consolidation_company_id
        predecessor = self.posting_predecessor_run_id
        if predecessor:
            if (
                predecessor.entity_id != self.entity_id
                or predecessor.period_to >= self.period_from
            ):
                raise UserError(_(
                    "The consolidation move has an invalid posting "
                    "predecessor."
                ))
            predecessor._eh_assert_active_move_provenance(correction=True)
        configured_journal = self.entity_id.consolidation_journal_id
        history = self.move_history_ids
        if (
            not company
            or not move
            or move.state != 'posted'
            or move.company_id != company
            or move.move_type != 'entry'
            or move.ref != "Consolidation %s" % self.name
            or (
                not correction
                and
                configured_journal
                and move.journal_id != configured_journal
            )
            or (not correction and move.journal_id.type != 'general')
            or move.date != self.period_to
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
            or move.reversed_entry_id
            or move not in history
        ):
            raise UserError(_(
                "The active consolidation move failed its current sealed "
                "company/journal/history provenance check."
            ))
        if history.filtered(
            lambda candidate: (
                candidate.state == 'posted'
                and candidate.reversed_entry_id == move
            )
        ):
            raise UserError(_(
                "The active consolidation move has already been offset by a "
                "posted reversal and cannot support posting or close."
            ))

        expected = self._eh_consolidation_move_line_vals(
            company, correction=correction,
        )
        rounding = (
            self.posting_currency_rounding
            if correction else company.currency_id.rounding
        )
        rounding = self._eh_assert_finite_number(
            rounding,
            _("Consolidation posting-currency rounding"),
            positive=True,
        )
        if len(move.line_ids) != len(expected):
            raise UserError(_(
                "The active consolidation move no longer has the exact engine "
                "line shape for this run."
            ))
        actual_by_account = defaultdict(float)
        for line in move.line_ids:
            if not line.account_id:
                raise UserError(_(
                    "The active consolidation move contains a line without an "
                    "account."
                ))
            actual_by_account[line.account_id.id] += (
                (line.debit or 0.0) - (line.credit or 0.0)
            )
        expected_by_account = {
            vals['account_id']: vals['debit'] - vals['credit']
            for vals in expected
        }
        if set(actual_by_account) != set(expected_by_account):
            raise UserError(_(
                "The active consolidation move account set does not match the "
                "current engine output."
            ))
        for account_id, expected_amount in expected_by_account.items():
            if not float_is_zero(
                float_round(
                    actual_by_account[account_id],
                    precision_rounding=rounding,
                )
                - float_round(
                    expected_amount,
                    precision_rounding=rounding,
                ),
                precision_rounding=rounding,
            ):
                raise UserError(_(
                    "The active consolidation move amounts do not match the "
                    "current engine output."
                ))
        return move

    def _eh_assert_inactive_reversal_provenance(self):
        """Validate the retained latest reversal after active pointer clearing."""
        self.ensure_one()
        reversal = self.reversal_move_id
        if not reversal:
            return reversal
        company = self.entity_id.consolidation_company_id
        original = reversal.reversed_entry_id
        if (
            not company
            or reversal.state != 'posted'
            or reversal.company_id != company
            or reversal.move_type != 'entry'
            or not reversal.eh_sealed
            or reversal.eh_legacy_unverified_seal
            or not original
            or original.company_id != company
            or original not in self.move_history_ids
            or reversal not in self.move_history_ids
        ):
            raise UserError(_(
                "The retained consolidation reversal failed its sealed "
                "original/history provenance check."
            ))
        return reversal

    def _eh_reverse_consolidation_move(self, require_goodwill_test=True):
        with self.env.cr.savepoint():
            return self._eh_reverse_consolidation_move_locked(
                require_goodwill_test=require_goodwill_test,
            )

    def _eh_reverse_consolidation_move_locked(self,
                                              require_goodwill_test=True):
        """Reverse a posted consolidation move and retain both entries.

        Called from the manager-gated reopen / reset paths. A posted move is
        immutable, so it is reversed with a balanced counter-entry. Neither
        entry is ever drafted or unlinked. No-op when the run carries no move.
        """
        self.ensure_one()
        self._eh_assert_correction_provenance()
        self._eh_assert_no_active_posting_successor()
        move = self.move_id
        if not move:
            self._eh_assert_inactive_reversal_provenance()
            return
        company = self.entity_id.consolidation_company_id
        self._eh_assert_active_move_provenance(correction=True)
        if self.reversal_move_id:
            raise UserError(_(
                "An active consolidation move cannot also carry a latest "
                "reversal pointer."
            ))
        if move.state == 'posted':
            reversal = move._eh_reverse_with_verified_capability(
                default_values_list=[{
                    'date': self.period_to,
                    'ref': _("Reversal of consolidation %s", self.name),
                }],
                # Consolidation entries commonly contain P&L accounts that
                # are deliberately non-reconcilable.  ``cancel=True`` asks
                # account.move to reconcile every reversed leg and conflicts
                # with strict reconciliation guards installed by the payment
                # suite.  A posted counter-entry is the required evidence;
                # reconciliation is neither meaningful nor required here.
                cancel=False,
            )
            # The Base helper atomically suppresses core's late auto-reconcile
            # and restores the immutable provenance link before returning.
            reversal._eh_post_verified_reversal()
            self._eh_seal_reversal(reversal)
            self.sudo().write({
                'move_id': False,
                'reversal_move_id': reversal.id,
                'posting_predecessor_run_id': False,
                'move_history_ids': [(4, move.id), (4, reversal.id)],
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
            })
            self.message_post(body=_(
                "Consolidation move %s reversed (%s) on run reopen/reset by %s.",
                move.name,
                reversal.name if reversal else '',
                self.env.user.display_name,
            ))
        else:
            raise UserError(_(
                "The linked consolidation move is not posted. It is retained "
                "for audit and cannot be deleted by reset/reopen."
            ))

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No consolidation move has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Consolidation move"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
        }

    # ---- compute ----

    def _build_lines(self):
        """Pull each member's trial balance, translate, sum, eliminate.

        Output is one eh.consol.run.line per (consolidated_account,
        kind, source_member). Kind values:

          subsidiary_balance: the translated balance for a subsidiary.
          parent_balance: the parent company's own balance.
          elimination: an elimination journal line.
          cta: the IAS 21 currency translation adjustment.
          nci: non-controlling interest carve-out.

        The kinds aggregate at view time so users see a consolidated
        account total broken down by source.
        """
        self.ensure_one()
        # Auto-intragroup diagnostics collected during this build and returned
        # to action_compute (a recordset cannot hold a transient attribute
        # across calls). Empty unless the entity opted into auto elimination.
        auto_warnings = []
        # Run lines are engine generated and read only to users (see the
        # ir.model.access rules). Create them as sudo so a non manager who
        # triggers a compute is not blocked by the read only access.
        Line = self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY})
        vals_list = []
        # Parent balances first.
        parent_company = self.entity_id.parent_company_id
        parent_scope = self.entity_id._eh_descendant_companies(parent_company)
        parent_lines = self._fetch_balances(
            company=parent_scope,
            currency_to=self.presentation_currency_id,
            translate=(
                parent_company.currency_id
                != self.presentation_currency_id
            ),
            source_currency=parent_company.currency_id,
            retained_earnings_account=(
                self.entity_id.parent_retained_earnings_account_id
            ),
        )
        for acc_id, amount in parent_lines.items():
            vals_list.append({
                'run_id': self.id,
                'account_id': acc_id,
                'kind': 'parent_balance',
                'company_id': parent_company.id,
                'amount': amount,
            })
        # Member balances next.
        for member in self.entity_id.member_ids:
            if member.method == 'equity':
                # Equity-method members are not rolled up line by line; the
                # parent carries a single investment line. Under the IAS
                # 28.1A fair value option no pick-up is booked at all (the
                # fair-value engine remeasures the investment); the run
                # carries a zero-amount memo disclosure line recording the
                # election. Otherwise the pick-up configuration is
                # MANDATORY: a missing investment or share-of-profit
                # account refuses the compute rather than silently
                # dropping the associate from the consolidated result.
                if member.fv_option:
                    vals_list.append({
                        'run_id': self.id,
                        'kind': 'disclosure',
                        'company_id': member.company_id.id,
                        'member_id': member.id,
                        'amount': 0.0,
                        'notes': _(
                            "IAS 28.1A fair value option elected for "
                            "%s: no equity pick-up; the investment is "
                            "measured at fair value through profit or "
                            "loss by the fair-value engine.",
                            member.company_id.display_name,
                        ),
                    })
                    continue
                self._eh_require_equity_config(member)
                pickup_vals = self._build_equity_pickup_vals(member)
                if pickup_vals:
                    vals_list.extend(pickup_vals)
                continue
            translated = self._fetch_balances(
                company=self.entity_id._eh_member_source_companies(member),
                currency_to=self.presentation_currency_id,
                translate=True,
                source_currency=member.functional_currency_id,
                retained_earnings_account=(
                    member.retained_earnings_account_id
                ),
            )
            # Proportional method (IFRS 11-style proportionate
            # consolidation): the ownership share is applied to EVERY
            # balance, and it is applied AFTER translation. Translation
            # first, scaling second keeps the IAS 21 closing / average rate
            # arithmetic identical to the full method (scaling a translated
            # amount equals translating a scaled amount for a linear rate,
            # but scaling before translation would break the CTA
            # attribution audit trail, so the order is fixed here and
            # relied on by the tests).
            scale = (
                member.ownership_pct / 100.0
                if member.method == 'proportional' else 1.0
            )
            account_map = member._eh_resolved_account_map()
            for acc_id, amount in translated.items():
                mapped_account = account_map.get(acc_id)
                vals_list.append({
                    'run_id': self.id,
                    'account_id': mapped_account.id if mapped_account else acc_id,
                    'kind': 'subsidiary_balance',
                    'company_id': member.company_id.id,
                    'member_id': member.id,
                    'amount': self.presentation_currency_id.round(
                        amount * scale),
                })
            # IFRS 3 acquisition elimination: when the investment is fully
            # configured, auto-generate the elimination legs that remove the
            # parent's investment against acquisition-date equity (and book
            # acquisition NCI + goodwill). Those legs already book NCI, so the
            # separate _build_nci_vals carve-out is suppressed for this member
            # to avoid double-counting the minority interest.
            elim_vals = self._build_acquisition_elimination_vals(member)
            if elim_vals:
                vals_list.extend(elim_vals)
                # The elimination books NCI on the ACQUISITION-DATE equity only.
                # Post-acquisition profit/loss must also be attributed to the
                # minority, so book a further NCI line for the movement in the
                # subsidiary's reporting equity+P&L base since acquisition. This
                # is zero when there has been no post-acquisition movement, so a
                # sub that has not moved since acquisition behaves as before.
                post_acq_vals = self._build_post_acq_nci_vals(member, translated)
                if post_acq_vals:
                    vals_list.extend(post_acq_vals)
            elif member.method == 'full' and member.ownership_pct < 100.0:
                # Compute and persist NCI for this member when ownership
                # is below 100% on the full method and no auto-elimination
                # booked the acquisition NCI.
                nci_vals = self._build_nci_vals(member, translated)
                if nci_vals:
                    vals_list.extend(nci_vals)
        # Apply eliminations as run lines tagged kind='elimination'.
        for elim in self.elimination_ids.filtered(
            lambda e: e.state == 'posted',
        ):
            for line in elim.line_ids:
                vals_list.append({
                    'run_id': self.id,
                    'account_id': line.account_id.id,
                    'kind': 'elimination',
                    'amount': line.amount,
                    'elimination_id': elim.id,
                })
        # IFRS 10.B86 automatic intragroup elimination: when opted in on the
        # entity, generate balanced elimination pairs for reciprocal
        # receivables/payables and sales/purchases between member companies.
        # Off by default, so a run on an entity without the flag behaves
        # exactly as before.
        if self.entity_id.auto_eliminate_intragroup:
            auto_vals, auto_warnings = self._build_auto_intragroup_vals()
            if auto_vals:
                vals_list.extend(auto_vals)
        # IFRS 10 / IAS 27 intra-group unrealised-profit elimination: for each
        # configured record, remove the margin sitting in the buyer's ending
        # inventory. Only fires when such records exist, so a run without any
        # behaves exactly as before.
        for up in self.unrealised_profit_ids:
            up_vals = self._build_unrealised_profit_vals(up)
            if up_vals:
                vals_list.extend(up_vals)
        if vals_list:
            self._eh_validate_line_vals_finite(vals_list)
            Line.create(vals_list)
        # CTA must come last: it reads the lines just written to
        # balance the run. The sum is computed from self.line_ids,
        # so the prior batch must have flushed.
        #
        # The plug is SPLIT BY MEMBER: each member's translated lines (plus
        # its balanced NCI / elimination pairs, which net to zero) leave a
        # per-member residual, booked as that member's own kind='cta' line
        # tagged with the member (and its CTA position link, when set), so
        # the accumulated translation reserve is attributable per foreign
        # operation and recyclable on that member's disposal (IAS 21.48).
        # Any residue not attributable to a member (manual eliminations,
        # unrealised-profit records, auto intragroup pairs; all balanced,
        # so normally zero) lands on an untagged residual CTA line. The sum
        # of all CTA lines equals the old single-line plug exactly, so the
        # run still nets to zero and cta_amount is unchanged.
        cta_total = self._compute_cta()
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        cta_vals = []
        booked = 0.0
        for member in self.entity_id.member_ids:
            member_total = sum(
                self.line_ids.filtered(
                    lambda l: l.member_id == member
                    and l.kind in _CTA_BASE_KINDS
                ).mapped('amount'),
            )
            self._eh_assert_finite_number(
                member_total, _("Member CTA basis"),
            )
            member_plug = -currency.round(member_total)
            if float_is_zero(member_plug, precision_rounding=rounding):
                continue
            nci_cta = 0.0
            if member.method == 'full' and member.ownership_pct < 100.0:
                nci_cta = currency.round(
                    member_plug * (1.0 - member.ownership_pct / 100.0),
                )
            parent_cta = currency.round(member_plug - nci_cta)
            if not float_is_zero(parent_cta, precision_rounding=rounding):
                cta_vals.append({
                    'run_id': self.id,
                    'kind': 'cta',
                    'company_id': member.company_id.id,
                    'member_id': member.id,
                    'cta_position_id': member.cta_position_id.id or False,
                    'amount': parent_cta,
                    'notes': _("Parent-attributable IAS 21 translation reserve"),
                })
            if not float_is_zero(nci_cta, precision_rounding=rounding):
                nci_account = member.nci_account_id or self._nci_account()
                self._eh_require_nci_accounts(member, nci_account)
                cta_vals.append({
                    'run_id': self.id,
                    'kind': 'nci',
                    'company_id': member.company_id.id,
                    'member_id': member.id,
                    'account_id': nci_account.id,
                    'amount': nci_cta,
                    'notes': _(
                        "NCI share of IAS 21 exchange differences (IAS 21.41)"
                    ),
                })
            booked += parent_cta + nci_cta
        residual = currency.round(self._eh_assert_finite_number(
            cta_total - booked, _("Residual CTA basis"),
        ))
        if not float_is_zero(residual, precision_rounding=rounding):
            cta_vals.append({
                'run_id': self.id,
                'kind': 'cta',
                'amount': residual,
            })
        if cta_vals:
            needs_cta_account = any(not vals.get('account_id') for vals in cta_vals)
            cta_account = self._cta_account() if needs_cta_account else False
            if needs_cta_account and not cta_account:
                # A non-zero CTA with no account would post a run line with
                # account_id False (silently dropping the translation reserve
                # from the consolidated set). Refuse and tell the user exactly
                # what to configure rather than emitting an accountless line.
                raise UserError(_(
                    "Run %(run)s needs to book a currency translation "
                    "adjustment (CTA) of %(amount)s, but no CTA account could "
                    "be resolved. Set a CTA / Translation Reserve Account on "
                    "the consolidation entity %(entity)s (or create an equity "
                    "account whose name contains 'translation' or 'CTA' on the "
                    "parent chart).",
                    run=self.name,
                    amount=currency.round(cta_total),
                    entity=self.entity_id.display_name,
                ))
            for vals in cta_vals:
                if not vals.get('account_id'):
                    vals['account_id'] = cta_account.id
            self._eh_validate_line_vals_finite(cta_vals)
            Line.create(cta_vals)
        # Generated recycle lines are disposable projections of immutable
        # disposal events. Rebuild them last (balanced pairs, therefore no CTA
        # effect) so reset/recompute never erases IAS 21.48 evidence.
        self._eh_rebuild_disposal_event_lines()
        return auto_warnings

    def _eh_assert_translation_rate_available(
        self, source_currency, target_currency, company, on_date,
    ):
        """Refuse Odoo's implicit 1.0/earliest-future FX fallback.

        Odoo deliberately returns 1.0 when a non-company currency has no
        applicable rate row.  That is a useful generic default, but it is not
        acceptable evidence for IAS 21.  Every non-base currency participating
        in a conversion must have a global or company-root rate dated no later
        than the conversion date.  The company currency itself is the unit
        base and legitimately needs no row.
        """
        self.ensure_one()
        if not source_currency or not target_currency \
                or source_currency == target_currency:
            return True
        rate_company = getattr(company, 'root_id', company) or company
        base_currency = rate_company.currency_id
        required = (source_currency | target_currency) - base_currency
        on_date = fields.Date.to_date(on_date)
        Rate = self.env['res.currency.rate'].sudo()
        missing = required.filtered(lambda currency: not Rate.search_count([
            ('currency_id', '=', currency.id),
            ('company_id', 'in', (False, rate_company.id)),
            ('name', '<=', on_date),
        ], limit=1))
        if missing:
            raise UserError(_(
                "Run %(run)s cannot translate %(source)s to %(target)s for "
                "%(company)s at %(date)s: no applicable global or company "
                "rate exists on or before that date for %(missing)s. "
                "Identity and earliest-future fallbacks are forbidden for "
                "consolidation.",
                run=self.name,
                source=source_currency.name,
                target=target_currency.name,
                company=company.display_name,
                date=on_date,
                missing=', '.join(missing.mapped('name')),
            ))
        return True

    def _eh_source_amount_basis(self, companies, source_currency):
        """Return (amount field, extra domain) for the functional basis.

        Odoo's ``balance`` is denominated in company currency. When a member's
        IAS 21 functional-currency override differs, treating that balance as
        the override currency is a unit error. Make the override authoritative:
        every non-zero source line must carry that currency as transaction
        currency and extraction uses signed ``amount_currency``. A mixed basis
        is refused rather than silently translated.
        """
        self.ensure_one()
        booking_currencies = companies.mapped('currency_id')
        if not source_currency or booking_currencies == source_currency:
            return 'balance', []
        invalid = self.env['account.move.line'].sudo().search([
            ('move_id.company_id', 'in', companies.ids),
            ('move_id.state', '=', 'posted'),
            ('move_id.date', '<=', self.period_to),
            ('balance', '!=', 0.0),
            ('currency_id', '!=', source_currency.id),
        ], limit=1, order='id')
        if invalid:
            raise UserError(_(
                "Run %(run)s cannot use functional currency %(currency)s for "
                "company scope %(companies)s: posted source lines are not "
                "consistently denominated in that currency. Either correct the "
                "ledger's amount-currency basis or remove the override.",
                run=self.name,
                currency=source_currency.display_name,
                companies=', '.join(companies.mapped('display_name')),
            ))
        return 'amount_currency', [('currency_id', '=', source_currency.id)]

    def _fetch_balances(
        self, company, currency_to, translate=False,
        source_currency=None, retained_earnings_account=None,
        include_opening=True,
    ):
        """Return {account_id: amount} of balances for the company up
        to period_to.

        Income / expense accounts use period sum (date in
        [period_from, period_to]); assets and liabilities use the cumulative
        sum to period_to. Equity transactions use their transaction-date spot
        rates. Net P&L before period_from is translated by source fiscal year
        and accumulated into opening retained earnings, so an unclosed prior
        result cannot be mislabeled as CTA.

        When translate=True, amounts are converted from
        source_currency to currency_to. Income / expense use the
        period average rate (time weighted across the period per IAS 21),
        assets and liabilities use the closing rate (rate at period_to), and
        equity uses transaction-date historical rates. All unit rates are
        obtained unrounded; only translated account totals are rounded in the
        presentation currency.
        """
        self.ensure_one()
        companies = company.exists()
        if not companies:
            return {}
        rate_company = companies.sorted('id')[:1]
        amount_field, currency_domain = self._eh_source_amount_basis(
            companies, source_currency or rate_company.currency_id,
        )
        AML = self.env['account.move.line'].sudo()
        balances_pl = list(grouped_sum(
            AML,
            [
                ('move_id.company_id', 'in', companies.ids),
                ('move_id.state', '=', 'posted'),
                ('account_id.account_type', 'in',
                 list(_INCOME_TYPES + _EXPENSE_TYPES)),
                ('move_id.date', '>=', self.period_from),
                ('move_id.date', '<=', self.period_to),
            ] + currency_domain,
            'account_id', amount_field,
        ))
        balances_bs = list(grouped_sum(
            AML,
            [
                ('move_id.company_id', 'in', companies.ids),
                ('move_id.state', '=', 'posted'),
                ('account_id.account_type', 'not in',
                 list(_INCOME_TYPES + _EXPENSE_TYPES + _EQUITY_TYPES)),
                ('move_id.date', '<=', self.period_to),
            ] + currency_domain,
            'account_id', amount_field,
        ))
        AML.flush_model([
            'move_id', 'account_id', 'date', 'balance', 'amount_currency',
            'currency_id',
        ])
        self.env['account.move'].flush_model(['company_id', 'state'])
        self.env['account.account'].flush_model(['account_type'])
        equity_query = f"""
            SELECT line.account_id,
                   line.date,
                   COALESCE(SUM(line.{amount_field}), 0.0)
              FROM account_move_line AS line
              JOIN account_move AS move ON move.id = line.move_id
              JOIN account_account AS account ON account.id = line.account_id
             WHERE move.company_id = ANY(%s)
               AND move.state = 'posted'
               AND account.account_type = ANY(%s)
               AND line.date <= %s
               AND (%s IS NULL OR line.currency_id = %s)
             GROUP BY line.account_id, line.date
             ORDER BY line.account_id, line.date
            """
        self.env.cr.execute(
            equity_query,
            (
                companies.ids,
                list(_EQUITY_TYPES),
                self.period_to,
                source_currency.id if amount_field == 'amount_currency' else None,
                source_currency.id if amount_field == 'amount_currency' else None,
            ),
        )
        balances_equity = self.env.cr.fetchall()
        for _account_id, amount in balances_pl + balances_bs:
            self._eh_assert_finite_number(
                amount, _("Posted source balance"),
            )
        for _account, _date, amount in balances_equity:
            self._eh_assert_finite_number(
                amount, _("Posted source equity balance"),
            )
        out = {}
        # rates
        if translate and source_currency and currency_to and source_currency != currency_to:
            # Balance-sheet items translate at the closing rate (period_to).
            # P&L items translate at the period average rate. Sourcing both
            # at period_to (the previous behaviour) made the two rates
            # identical, which collapsed the CTA to zero for every run with
            # a foreign-currency subsidiary, materially misstating equity.
            self._eh_assert_translation_rate_available(
                source_currency, currency_to, rate_company, self.period_to,
            )
            closing_rate_per = source_currency._convert(
                1.0, currency_to, rate_company, self.period_to,
                round=False,
            )
            avg_rate_per = self._period_average_rate(
                source_currency, currency_to, rate_company,
            )
        else:
            avg_rate_per = 1.0
            closing_rate_per = 1.0
        self._eh_assert_finite_number(
            avg_rate_per, _("Average translation rate"), positive=True,
        )
        self._eh_assert_finite_number(
            closing_rate_per, _("Closing translation rate"), positive=True,
        )
        for acc_id, amount in balances_pl:
            out[acc_id] = self._eh_assert_finite_number(
                currency_to.round(
                    out.get(acc_id, 0.0) + (amount or 0.0) * avg_rate_per),
                _("Translated profit-and-loss balance"),
            )
        for acc_id, amount in balances_bs:
            out[acc_id] = self._eh_assert_finite_number(
                currency_to.round(
                    out.get(acc_id, 0.0)
                    + (amount or 0.0) * closing_rate_per),
                _("Translated balance-sheet balance"),
            )
        equity_totals = defaultdict(float)
        spot_rates = {}
        for account_id, on_date, amount in balances_equity:
            on_date = fields.Date.to_date(on_date)
            if translate and source_currency != currency_to:
                if on_date not in spot_rates:
                    self._eh_assert_translation_rate_available(
                        source_currency, currency_to, rate_company, on_date,
                    )
                    spot_rates[on_date] = self._eh_assert_finite_number(
                        source_currency._convert(
                            1.0, currency_to, rate_company, on_date,
                            round=False,
                        ),
                        _("Historical equity translation rate"),
                        positive=True,
                    )
                rate = spot_rates[on_date]
            else:
                rate = 1.0
            equity_totals[account_id] += (amount or 0.0) * rate
        for account_id, amount in equity_totals.items():
            out[account_id] = self._eh_assert_finite_number(
                currency_to.round(out.get(account_id, 0.0) + amount),
                _("Translated historical equity balance"),
            )
        prior_result = (
            self._preperiod_retained_earnings(
                company=company,
                currency_to=currency_to,
                translate=translate,
                source_currency=source_currency,
                amount_field=amount_field,
                currency_filter=(
                    source_currency if amount_field == 'amount_currency'
                    else False
                ),
            )
            if include_opening else 0.0
        )
        rounding = currency_to.rounding or 0.01
        if not float_is_zero(prior_result, precision_rounding=rounding):
            retained_account = self._retained_earnings_account(
                companies, configured=retained_earnings_account,
            )
            out[retained_account.id] = self._eh_assert_finite_number(
                currency_to.round(
                    out.get(retained_account.id, 0.0) + prior_result),
                _("Opening retained earnings"),
            )
        return out

    def _retained_earnings_account(self, company, configured=None):
        """Resolve one source-company retained-earnings account, fail closed.

        The synthetic opening result must land on a genuine
        ``equity_unaffected`` account. Picking arbitrarily among multiple
        candidates would make consolidated account mapping non-deterministic.
        """
        if configured:
            return configured
        accounts = self.env['account.account'].sudo().search([
            ('account_type', '=', 'equity_unaffected'),
            (_acc_company_field(self.env), 'in', company.ids),
        ], limit=2, order='id')
        if len(accounts) != 1:
            raise UserError(_(
                "Company %(company)s has pre-period profit or loss that must "
                "be accumulated into opening retained earnings, but its chart "
                "does not resolve exactly one Current Year Earnings / "
                "Unallocated Earnings account (account type "
                "equity_unaffected). Found %(count)d; configure the explicit "
                "opening retained-earnings account on the entity/member.",
                company=', '.join(company.mapped('display_name')),
                count=len(accounts),
            ))
        return accounts

    def _preperiod_retained_earnings(
        self, company, currency_to, translate=False, source_currency=None,
        amount_field='balance', currency_filter=False,
    ):
        """Translate unclosed pre-period P&L into opening retained earnings.

        Odoo does not require year-end closing journals. Consequently, P&L
        before ``period_from`` remains on income/expense accounts while the
        corresponding asset/liability legs remain inside cumulative balances.
        Excluding that P&L makes the trial balance fail and a balancing engine
        falsely calls the missing result CTA. Aggregate each source fiscal
        year's net result and translate it at that year's time-weighted average
        rate, then carry the cumulative translated amount as opening retained
        earnings. A fiscal year whose P&L was explicitly closed nets to zero
        and contributes nothing.
        """
        self.ensure_one()
        if not self.period_from:
            return 0.0
        MoveLine = self.env['account.move.line'].sudo()
        companies = company.exists()
        rate_company = companies.sorted('id')[:1]
        MoveLine.flush_model([
            'move_id', 'account_id', 'date', 'balance', 'amount_currency',
            'currency_id',
        ])
        self.env['account.move'].flush_model(['company_id', 'state'])
        self.env['account.account'].flush_model(['account_type'])
        preperiod_query = f"""
            SELECT line.date,
                   COALESCE(SUM(line.{amount_field}), 0.0)
              FROM account_move_line AS line
              JOIN account_move AS move ON move.id = line.move_id
              JOIN account_account AS account ON account.id = line.account_id
             WHERE move.company_id = ANY(%s)
               AND move.state = 'posted'
               AND account.account_type = ANY(%s)
               AND line.date < %s
               AND (%s IS NULL OR line.currency_id = %s)
             GROUP BY line.date
             ORDER BY line.date
            """
        self.env.cr.execute(
            preperiod_query,
            (
                companies.ids,
                list(_INCOME_TYPES + _EXPENSE_TYPES),
                self.period_from,
                currency_filter.id if currency_filter else None,
                currency_filter.id if currency_filter else None,
            ),
        )
        rows = self.env.cr.fetchall()
        dated = sorted(
            (fields.Date.to_date(on_date), amount or 0.0)
            for on_date, amount in rows if on_date
        )
        raw_total = self._eh_assert_finite_number(
            sum(amount for _on_date, amount in dated),
            _("Pre-period source result"),
        )
        source_rounding = (
            source_currency.rounding
            if source_currency else rate_company.currency_id.rounding
        ) or 0.01
        if not dated or float_is_zero(
            raw_total, precision_rounding=source_rounding,
        ):
            return 0.0
        if not translate or not source_currency \
                or source_currency == currency_to:
            return currency_to.round(raw_total)

        translated = 0.0
        idx = 0
        cutoff = self.period_from - timedelta(days=1)
        while idx < len(dated):
            first_date = dated[idx][0]
            fiscal = rate_company.compute_fiscalyear_dates(first_date)
            fiscal_from = fields.Date.to_date(fiscal['date_from'])
            fiscal_to = min(
                fields.Date.to_date(fiscal['date_to']), cutoff,
            )
            fiscal_result = 0.0
            while idx < len(dated) and dated[idx][0] <= fiscal_to:
                fiscal_result += dated[idx][1]
                idx += 1
            fiscal_result = self._eh_assert_finite_number(
                fiscal_result, _("Source fiscal-year result"),
            )
            if float_is_zero(
                fiscal_result, precision_rounding=source_rounding,
            ):
                continue
            rate = self._time_weighted_rate(
                source_currency, currency_to, rate_company,
                fiscal_from, fiscal_to,
            )
            translated = self._eh_assert_finite_number(
                translated + fiscal_result * rate,
                _("Translated opening retained earnings"),
            )
        return currency_to.round(translated)

    def _period_average_rate(self, source_currency, currency_to, company):
        """Return the time weighted average conversion rate for the run
        period: the average number of units of currency_to per one unit of
        source_currency across [period_from, period_to], per IAS 21.

        Each daily spot rate is weighted by the number of days it is in
        effect inside the period, so a rate that changes mid period
        contributes in proportion to its duration. Falls back to the
        period midpoint rate when the rate table holds no change inside
        the period, and to the closing rate for a degenerate (single day
        or inverted) period.
        """
        return self._time_weighted_rate(
            source_currency, currency_to, company,
            self.period_from, self.period_to,
        )

    def _time_weighted_rate(
        self, source_currency, currency_to, company, date_from, date_to,
    ):
        """Unrounded time-weighted unit conversion rate for a date window."""
        self.ensure_one()
        if not date_from or not date_to or date_to <= date_from:
            conversion_date = date_to or date_from or self.period_to
            self._eh_assert_translation_rate_available(
                source_currency, currency_to, company, conversion_date,
            )
            return self._eh_assert_finite_number(
                source_currency._convert(
                    1.0, currency_to, company,
                    conversion_date,
                    round=False,
                ),
                _("Translation rate"), positive=True,
            )
        Rate = self.env['res.currency.rate'].sudo()
        rate_company = getattr(company, 'root_id', company) or company
        change_dates = Rate.search([
            ('currency_id', 'in', (source_currency | currency_to).ids),
            '|', ('company_id', '=', False),
            ('company_id', '=', rate_company.id),
            ('name', '>', date_from),
            ('name', '<=', date_to),
        ]).mapped('name')
        boundaries = sorted(
            {date_from} | {d for d in change_dates if date_from < d <= date_to}
        )
        weighted_sum = 0.0
        total_days = 0
        for idx, seg_start in enumerate(boundaries):
            seg_end = (
                boundaries[idx + 1] if idx + 1 < len(boundaries)
                else date_to + timedelta(days=1)
            )
            days = (seg_end - seg_start).days
            if days <= 0:
                continue
            self._eh_assert_translation_rate_available(
                source_currency, currency_to, company, seg_start,
            )
            rate = source_currency._convert(
                1.0, currency_to, company, seg_start,
                round=False,
            )
            rate = self._eh_assert_finite_number(
                rate, _("Translation rate"), positive=True,
            )
            weighted_sum = self._eh_assert_finite_number(
                weighted_sum + rate * days,
                _("Weighted translation rate"),
            )
            total_days += days
        if not total_days:
            midpoint = date_from + timedelta(
                days=(date_to - date_from).days // 2,
            )
            self._eh_assert_translation_rate_available(
                source_currency, currency_to, company, midpoint,
            )
            return self._eh_assert_finite_number(
                source_currency._convert(
                    1.0, currency_to, company, midpoint,
                    round=False,
                ),
                _("Translation rate"), positive=True,
            )
        return self._eh_assert_finite_number(
            weighted_sum / total_days,
            _("Average translation rate"), positive=True,
        )

    def _build_nci_vals(self, member, translated_balances):
        """Return the NCI carve-out run-line vals for a member (a balanced
        two-leg list), or None.

        NCI = subsidiary_net_assets x (1 - ownership_pct / 100), where the
        net-asset base is the subsidiary's equity PLUS its current-period
        result. Within a period the profit or loss has not yet closed to
        the equity accounts, so it lives on the P&L accounts; leaving it
        out understated NCI for every profitable or loss-making subsidiary
        and left the whole period result attributed to the parent. Both
        equity and P&L balances are already translated in
        translated_balances (equity at closing rate, P&L at average rate).

        The carve-out is a reclass WITHIN equity, so it books TWO legs:

          Cr NCI equity account            amount = nci_amount (credit-negative)
          Dr parent equity / retained-earn amount = -nci_amount (debit-positive)

        The pair nets to zero by construction, so the minority share moves out
        of parent equity into the NCI account without leaving an unmatched leg
        for the CTA plug to absorb (booking only the NCI credit contaminated the
        reported IAS 21 translation adjustment). The debit leg is tagged
        'elimination' (a consolidation reclass), not 'nci', so the reported NCI
        total stays the genuine minority interest. Returns vals rather than
        creating, so the caller can batch the insert.
        """
        self.ensure_one()
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated_balances.keys())
        if not acc_ids:
            return None
        accounts = Account.browse(acc_ids)
        equity_accs = accounts.filtered(
            lambda a: a.account_type in ('equity', 'equity_unaffected')
        )
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        equity_total = sum(
            translated_balances.get(a.id, 0.0) for a in equity_accs
        )
        # P&L balances carry the unclosed period result in Odoo's sign
        # convention (income credit-negative, expense debit-positive), so
        # adding them extends the equity base by the period result in the
        # same convention.
        pl_total = sum(
            translated_balances.get(a.id, 0.0) for a in pl_accs
        )
        nci_base = equity_total + pl_total
        nci_share = 1.0 - member.ownership_pct / 100.0
        currency = self.presentation_currency_id
        if member.nci_basis == 'fair_value':
            # IFRS 3.19(a) fair-value basis, carve path (no acquisition
            # elimination booked for this member): NCI carrying amount =
            # acquisition-date fair value of the minority PLUS the minority
            # share of the post-acquisition equity movement.
            #
            #   base   = translated equity + P&L, Odoo sign (credit-negative)
            #   -base  = equity-positive reporting net assets
            #   A      = acquisition-date equity (presentation currency)
            #   move   = (-base) - A          (post-acquisition movement)
            #   NCI    = FV + (1-o) * move    (equity-positive)
            #
            # stored credit-negative like every equity carve leg.
            A = self._eh_acquisition_equity_pres(member)
            post_acq_movement = (-nci_base) - A
            raw_nci = -(
                (member.nci_fair_value or 0.0)
                + nci_share * post_acq_movement
            )
        else:
            raw_nci = nci_base * nci_share
        nci_amount = currency.round(self._eh_assert_finite_number(
            raw_nci, _("NCI measurement basis"),
        ))
        # Round-aware zero test: after an FX multiply nci_amount can carry
        # a tiny residual (e.g. 1e-12) that exact == 0.0 never catches,
        # which would emit a spurious near-zero NCI line.
        rounding = currency.rounding or 0.01
        if float_is_zero(nci_amount, precision_rounding=rounding):
            return None
        nci_account = member.nci_account_id or self._nci_account()
        self._eh_require_nci_accounts(member, nci_account)
        reclass_account = self._nci_reclass_account(member)
        self._eh_require_reclass_account(member, reclass_account)
        base = {
            'run_id': self.id,
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base,
                 account_id=nci_account.id,
                 kind='nci',
                 amount=nci_amount),
            dict(base,
                 account_id=reclass_account.id,
                 kind='elimination',
                 amount=-nci_amount),
        ]

    def _eh_require_nci_accounts(self, member, nci_account):
        """Refuse the compute when a member needs an NCI carve-out but no NCI
        equity account can be resolved. Booking the carve-out with account_id
        False would silently drop the minority interest from the consolidated
        set, so surface exactly what to configure instead.
        """
        self.ensure_one()
        if nci_account:
            return
        raise UserError(_(
            "Run %(run)s needs to carve out non-controlling interest for "
            "subsidiary %(sub)s, but no NCI account could be resolved. Set an "
            "NCI Account on the member, or a Default NCI Account on the "
            "consolidation entity %(entity)s (or create an equity account "
            "whose name contains 'non-controlling' or 'minority' on the "
            "parent chart).",
            run=self.name,
            sub=member.company_id.display_name,
            entity=self.entity_id.display_name,
        ))

    def _eh_require_reclass_account(self, member, reclass_account):
        """Refuse the compute when the NCI carve-out cannot resolve the parent
        equity / retained-earnings account it debits. Without it the reclass
        would post a leg with account_id False and unbalance the equity pair,
        leaking into the CTA plug.
        """
        self.ensure_one()
        if reclass_account:
            return
        raise UserError(_(
            "Run %(run)s needs to reclassify the minority share of subsidiary "
            "%(sub)s out of consolidated retained earnings, but no parent "
            "retained-earnings account could be resolved for the offsetting "
            "leg. Configure Parent Retained Earnings on entity %(entity)s, "
            "or ensure its parent chart has exactly one Current Year Earnings "
            "account.",
            run=self.name,
            sub=member.company_id.display_name,
            entity=self.entity_id.display_name,
        ))

    def _reporting_equity_base(self, translated_balances):
        """Return the subsidiary's translated net-asset base: equity accounts
        (closing rate) plus P&L accounts (average rate), in Odoo's sign
        convention. This is the same base _build_nci_vals carves NCI from,
        factored out so the acquisition and post-acquisition NCI paths agree.
        """
        self.ensure_one()
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated_balances.keys())
        if not acc_ids:
            return 0.0
        accounts = Account.browse(acc_ids)
        equity_accs = accounts.filtered(
            lambda a: a.account_type in ('equity', 'equity_unaffected')
        )
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        equity_total = sum(
            translated_balances.get(a.id, 0.0) for a in equity_accs
        )
        pl_total = sum(
            translated_balances.get(a.id, 0.0) for a in pl_accs
        )
        return self._eh_assert_finite_number(
            equity_total + pl_total,
            _("Translated reporting equity base"),
        )

    def _build_post_acq_nci_vals(self, member, translated_balances):
        """Return the POST-ACQUISITION NCI run-line vals for an
        investment-configured full member, or None when there is no movement.

        The IFRS 3 acquisition elimination books NCI on the ACQUISITION-DATE
        equity A only (nci_leg = -(1-o)*A). It does not attribute any of the
        subsidiary's post-acquisition profit or loss to the minority, so
        without this line the whole post-acquisition result would sit with the
        parent. Here we book the minority's share of the movement in the
        subsidiary's reporting net-asset base since acquisition:

          base = translated reporting equity + P&L (Odoo sign convention)
          post_acq_movement_equity = -base - A     (equity-positive)
          nci = (1 - o) * post_acq_movement_equity

        In Odoo's sign convention a credit-balance equity/net-profit carries a
        negative amount, so -base is the equity-positive net-asset figure and
        subtracting A (the equity-positive acquisition equity) leaves the
        post-acquisition movement. The NCI line is stored in the same
        credit-negative sign the acquisition nci_leg uses, so total NCI for the
        member = acquisition NCI + this line. It is zero (skipped) when the
        reporting base equals the acquisition equity, i.e. no post-acquisition
        movement.

        Like the base carve-out this is a reclass WITHIN equity and books TWO
        legs (Cr NCI equity, Dr parent equity / retained earnings) that net to
        zero by construction, so the minority's share of the post-acquisition
        result moves out of parent equity into NCI without leaving an unmatched
        leg for the CTA plug to absorb. The debit leg is tagged 'elimination',
        not 'nci', so the reported NCI total stays the genuine minority interest.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        A = self._eh_acquisition_equity_pres(member)
        base = self._reporting_equity_base(translated_balances)
        # -base is the equity-positive reporting net assets; the post-acq
        # movement is that less the acquisition-date equity A.
        post_acq_movement = (-base) - A
        nci_share = 1.0 - member.ownership_pct / 100.0
        # Store in the credit-negative convention the acquisition nci_leg uses:
        # a post-acq profit (positive movement) credits NCI (negative amount).
        nci_amount = currency.round(self._eh_assert_finite_number(
            -post_acq_movement * nci_share,
            _("Post-acquisition NCI basis"),
        ))
        rounding = currency.rounding or 0.01
        if float_is_zero(nci_amount, precision_rounding=rounding):
            return None
        nci_account = member.nci_account_id or self._nci_account()
        self._eh_require_nci_accounts(member, nci_account)
        reclass_account = self._nci_reclass_account(member)
        self._eh_require_reclass_account(member, reclass_account)
        base_vals = {
            'run_id': self.id,
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        return [
            dict(base_vals,
                 account_id=nci_account.id,
                 kind='nci',
                 amount=nci_amount),
            dict(base_vals,
                 account_id=reclass_account.id,
                 kind='elimination',
                 amount=-nci_amount),
        ]

    def _investment_configured(self, member):
        """True when a member carries the config the IFRS 3 auto-elimination
        needs: full method, an investment account, and a positive investment
        amount. Members without this behave exactly as before (no elimination
        auto-fires), preserving existing runs.
        """
        return bool(
            member.method == 'full'
            and member.investment_account_id
            and member.investment_amount
            and member.investment_amount > 0.0
        )

    def _eh_acquisition_equity_pres(self, member):
        """Return the member's acquisition-date equity in the PRESENTATION
        currency.

        acquisition_equity is stated in the presentation currency by
        default. When the member carries a historical_rate (> 0) it is
        stated in the subsidiary's functional currency instead and is
        translated at that acquisition-date rate (IAS 21.23(b): the
        pre-acquisition equity removed by the elimination is a non-monetary
        historical figure, translated at the historical rate, never
        retranslated at closing)."""
        self.ensure_one()
        A = member.acquisition_equity or 0.0
        rate = member.historical_rate or 0.0
        if rate > 0.0:
            if not member.acquisition_date:
                raise UserError(_(
                    "Member %s has a historical acquisition rate without an "
                    "acquisition date. Add the IAS 21 date anchor before "
                    "computing consolidation.",
                    member.company_id.display_name,
                ))
            if member.acquisition_date > self.period_to:
                raise UserError(_(
                    "Member %(member)s has acquisition date %(date)s after "
                    "run end %(end)s; acquisition-date equity cannot enter "
                    "this consolidation period.",
                    member=member.company_id.display_name,
                    date=member.acquisition_date,
                    end=self.period_to,
                ))
            A = self._eh_assert_finite_number(
                A * rate, _("Historical acquisition equity"),
            )
        return self.presentation_currency_id.round(
            self._eh_assert_finite_number(
                A, _("Acquisition equity"),
            )
        )

    def _build_acquisition_elimination_vals(self, member):
        """Return the IFRS 3 acquisition-elimination run-line vals for a
        member, or None when its investment is not configured (or the
        entity has switched the auto-elimination off).

        Removes the parent's investment against documented, account-level
        subsidiary acquisition-equity components and books acquisition-date
        NCI plus either goodwill or a bargain-purchase gain:

          Let A = acquisition-date equity in the presentation currency
                  (_eh_acquisition_equity_pres: acquisition_equity, times
                  the historical rate when one is set),
              I = member.investment_amount,
              o = member.ownership_pct / 100,
              N = acquisition-date NCI:
                    proportionate basis: N = (1-o) * A     (IFRS 3.19(b))
                    fair-value basis:    N = nci_fair_value (IFRS 3.19(a))

          Dr each mapped equity component by its documented amount
          Cr investment_account       by I           amount = -I
          Cr nci_account              by N           amount = -N
          Dr goodwill_account         by positive (I + N - A), or
          Cr bargain-purchase income  by negative (I + N - A)

        The component total and the three acquisition residuals sum to zero:
          A - I - N + (I + N - A) = 0.

        On the proportionate basis the goodwill/bargain residual reduces to the
        familiar partial-goodwill figure I - o*A; on the fair-value basis it
        is the full-goodwill figure I + FV(NCI) - A. A negative residual is a
        bargain purchase credited to the entity's explicit income account
        under IFRS 3.34, never to the goodwill asset. The goodwill and bargain
        legs are separately tagged, and a non-zero acquisition-date minority
        leg is tagged 'nci' so the nci_amount KPI includes it.

        All legs are rounded to the presentation currency. Returns a list of
        vals so the caller can batch the insert. Returns None only when the
        member has not opted into the structured automatic elimination.
        """
        self.ensure_one()
        if not self.entity_id.auto_eliminate_investment:
            # Opted out: keep the diagnostic-warning behaviour and leave the
            # elimination to a manual entry.
            return None
        if not self._investment_configured(member):
            return None
        currency = self.presentation_currency_id
        o = member.ownership_pct / 100.0
        A = self._eh_acquisition_equity_pres(member)
        I = member.investment_amount or 0.0
        components = member.acquisition_component_ids.sorted(
            key=lambda component: (component.sequence, component.id),
        )
        if not components:
            raise UserError(_(
                "Member %(member)s has an automatic IFRS 3 investment "
                "elimination but no acquisition-equity components. Add the "
                "share-capital/reserve components and reconcile them to "
                "Acquisition Equity; a collapsed cross-account leg is not "
                "auditable.",
                member=member.company_id.display_name,
            ))
        rate = member.historical_rate or 1.0
        component_amounts = [
            currency.round(self._eh_assert_finite_number(
                component.amount * rate,
                _("Acquisition equity component"),
            ))
            for component in components
        ]
        component_total = currency.round(sum(component_amounts))
        if not currency.is_zero(component_total - A):
            raise UserError(_(
                "Member %(member)s acquisition components total %(components)s "
                "but Acquisition Equity is %(equity)s in presentation currency. "
                "Reconcile the component basis before computing.",
                member=member.company_id.display_name,
                components=component_total,
                equity=A,
            ))
        investment_leg = currency.round(self._eh_assert_finite_number(
            -I, _("Investment elimination leg"),
        ))
        if member.nci_basis == 'fair_value':
            raw_nci_leg = -(member.nci_fair_value or 0.0)
        else:
            raw_nci_leg = -(1.0 - o) * A
        nci_leg = currency.round(self._eh_assert_finite_number(
            raw_nci_leg, _("Acquisition NCI leg"),
        ))
        # Goodwill/bargain gain absorbs the rounding residual so every component
        # plus investment and NCI sums to exactly zero.
        goodwill_leg = self._eh_assert_finite_number(
            -(component_total + investment_leg + nci_leg),
            _("Goodwill elimination leg"),
        )
        rounding = currency.rounding or 0.01
        account_map = member._eh_resolved_account_map()
        base = {
            'run_id': self.id,
            'kind': 'elimination',
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        vals = []
        for component, amount in zip(components, component_amounts):
            mapped_target = (
                account_map.get(component.source_account_id.id)
                or component.source_account_id
            )
            if (
                component.target_account_id
                and component.target_account_id != mapped_target
            ):
                raise UserError(_(
                    "Member %(member)s acquisition component %(source)s "
                    "targets %(component_target)s, but the same subsidiary "
                    "balance rolls up to %(balance_target)s. Align the "
                    "component target with the member account map so the "
                    "source equity is removed account by account.",
                    member=member.company_id.display_name,
                    source=component.source_account_id.display_name,
                    component_target=component.target_account_id.display_name,
                    balance_target=mapped_target.display_name,
                ))
            target = mapped_target
            vals.append(dict(
                base,
                account_id=target.id,
                amount=amount,
                notes=_(
                    "IFRS 3 acquisition equity component: %s",
                    component.source_account_id.display_name,
                ),
            ))
        vals.append(dict(
            base,
            account_id=member.investment_account_id.id,
            amount=investment_leg,
        ))
        if not float_is_zero(nci_leg, precision_rounding=rounding):
            nci_account = member.nci_account_id or self._nci_account()
            self._eh_require_nci_accounts(member, nci_account)
            vals.append(dict(
                base,
                account_id=nci_account.id,
                kind='nci',
                amount=nci_leg,
            ))
        if goodwill_leg > 0.0 and not float_is_zero(
            goodwill_leg, precision_rounding=rounding,
        ):
            if not member.goodwill_account_id:
                raise UserError(_(
                    "Member %s recognises positive goodwill but has no Goodwill "
                    "Account configured.",
                    member.company_id.display_name,
                ))
            vals.append(dict(
                base,
                account_id=member.goodwill_account_id.id,
                kind='goodwill',
                amount=goodwill_leg,
            ))
        elif goodwill_leg < 0.0 and not float_is_zero(
            goodwill_leg, precision_rounding=rounding,
        ):
            gain_account = self.entity_id.bargain_purchase_gain_account_id
            if not gain_account:
                raise UserError(_(
                    "Member %(member)s produces a bargain-purchase gain of "
                    "%(amount)s. Configure the entity's Bargain Purchase Gain "
                    "Account; IFRS 3.34 forbids crediting the goodwill asset.",
                    member=member.company_id.display_name,
                    amount=currency.round(abs(goodwill_leg)),
                ))
            vals.append(dict(
                base,
                account_id=gain_account.id,
                kind='bargain_purchase',
                amount=goodwill_leg,
                notes=_("IFRS 3.34 bargain-purchase gain"),
            ))
        return vals

    def _equity_pickup_configured(self, member):
        """True when an equity-method member can be picked up automatically:
        it must carry both an investment account and a share-of-profit
        account.
        """
        return bool(
            member.method == 'equity'
            and member.investment_account_id
            and member.share_of_profit_account_id
        )

    def _eh_require_equity_config(self, member):
        """Refuse the compute when an equity-method member (without the IAS
        28.1A fair value option) is missing its pick-up configuration.

        Skipping the member silently would drop the associate from the
        consolidated result entirely, which is exactly the kind of
        honour-system gap IAS 28 does not allow: equity accounting is
        mandatory for an associate unless the fair value option applies.
        """
        self.ensure_one()
        if self._equity_pickup_configured(member):
            return
        missing = []
        if not member.investment_account_id:
            missing.append(_("an Investment Account"))
        if not member.share_of_profit_account_id:
            missing.append(_("a Share of Profit Account"))
        raise UserError(_(
            "Run %(run)s cannot be computed: equity-method member "
            "%(member)s is missing %(missing)s. IAS 28 equity accounting "
            "is mandatory for an associate, so configure the member (or "
            "elect the IAS 28.1A fair value option on it) before "
            "computing.",
            run=self.name,
            member=member.company_id.display_name,
            missing=_(" and ").join(missing),
        ))

    def _build_equity_pickup_vals(self, member):
        """Return the IAS 28 equity pick-up run-line vals for an equity-method
        member, or None when it is not configured for auto pick-up.

        Computes the member's translated period profit from its P&L accounts,
        takes the parent's ownership share, and books two balanced legs that
        increase the investment carrying value and consolidated income:

          share = (ownership_pct / 100) * profit

          Dr investment_account       by share   amount = +share
          Cr share_of_profit_account  by share   amount = -share

        Profit here is the positive (income - expense) result. In Odoo's sign
        convention a translated P&L balance carries income credit-negative and
        expense debit-positive, so the net period result is the negation of
        the summed P&L balances. The investment is debited (increased) by the
        parent's share of a profit, credited by its share of a loss; the two
        legs sum to zero by construction. Returns None (skips) when profit
        rounds to zero.
        """
        self.ensure_one()
        if not self._equity_pickup_configured(member):
            return None
        translated = self._fetch_balances(
            company=self.entity_id._eh_member_source_companies(member),
            currency_to=self.presentation_currency_id,
            translate=True,
            source_currency=member.functional_currency_id,
            include_opening=False,
        )
        Account = self.env['account.account'].sudo()
        acc_ids = list(translated.keys())
        accounts = Account.browse(acc_ids)
        pl_accs = accounts.filtered(
            lambda a: a.account_type in (_INCOME_TYPES + _EXPENSE_TYPES)
        )
        pl_total = sum(translated.get(a.id, 0.0) for a in pl_accs)
        # pl_total is credit-negative for a net profit; negate to get the
        # profit as a positive number.
        profit = -pl_total
        currency = self.presentation_currency_id
        share = currency.round(self._eh_assert_finite_number(
            (member.ownership_pct / 100.0) * profit,
            _("Equity-method share of profit"),
        ))
        rounding = currency.rounding or 0.01
        rollforward = self.equity_rollforward_ids.filtered(
            lambda row: row.member_id == member,
        )
        if len(rollforward) > 1:
            raise UserError(_(
                "Run %s has duplicate equity-method rollforwards for member %s.",
                self.name,
                member.company_id.display_name,
            ))
        if rollforward:
            rollforward._eh_validate_values({}, current=rollforward)
        if (
            member.functional_currency_id != self.presentation_currency_id
            and not rollforward
        ):
            raise UserError(_(
                "Foreign equity-method member %(member)s needs a period "
                "rollforward that confirms its IAS 21 carrying-translation "
                "adjustment (including a documented zero).",
                member=member.company_id.display_name,
            ))

        # Bring forward the latest signed cumulative carrying adjustment. The
        # parent's source ledger still carries the investment cost; prior
        # consolidation-only IAS 28 adjustments therefore need an opening pair
        # against parent retained earnings in every standalone run.
        prior_run = self.sudo().search([
            ('entity_id', '=', self.entity_id.id),
            ('period_to', '<', self.period_from),
            ('state', 'in', ('reviewed', 'closed')),
            ('engine_provenance_version', '=', _CURRENT_ENGINE_PROVENANCE),
        ], order='period_to desc, id desc', limit=1)
        prior_opening_by_account = defaultdict(float)
        if prior_run:
            prior_lines = prior_run.line_ids.filtered(lambda line: (
                line.member_id == member and line.kind == 'equity_pickup'
            ))
            investment_lines = prior_lines.filtered(
                lambda line: line.account_id == member.investment_account_id,
            )
            opening_re = self._nci_reclass_account(member)
            for investment_line in investment_lines:
                counterparts = prior_lines.filtered(lambda line: (
                    line.id != investment_line.id
                    and line.notes == investment_line.notes
                    and currency.is_zero(
                        line.amount + investment_line.amount,
                    )
                ))
                if len(counterparts) != 1:
                    raise UserError(_(
                        "Prior run %(prior)s does not preserve a unique "
                        "counterpart for IAS 28 investment adjustment %(note)s "
                        "of member %(member)s.",
                        prior=prior_run.display_name,
                        note=investment_line.notes or '/',
                        member=member.company_id.display_name,
                    ))
                counterpart = counterparts.account_id
                if counterpart.account_type in (
                    _INCOME_TYPES + _EXPENSE_TYPES
                ):
                    counterpart = opening_re
                if not counterpart:
                    raise UserError(_(
                        "Member %s needs an explicit parent retained-earnings "
                        "account to carry prior IAS 28 profit/distribution "
                        "adjustments into the new period.",
                        member.company_id.display_name,
                    ))
                prior_opening_by_account[counterpart.id] += (
                    investment_line.amount
                )
        base = {
            'run_id': self.id,
            'kind': 'equity_pickup',
            'company_id': member.company_id.id,
            'member_id': member.id,
        }
        vals = []

        def _append_pair(investment_amount, counterpart, note):
            investment_amount = currency.round(investment_amount)
            if float_is_zero(
                investment_amount, precision_rounding=rounding,
            ):
                return
            if not counterpart:
                raise UserError(_(
                    "Member %(member)s has a non-zero %(basis)s but no "
                    "counterpart account is configured.",
                    member=member.company_id.display_name,
                    basis=note,
                ))
            vals.extend([
                dict(
                    base,
                    account_id=member.investment_account_id.id,
                    amount=investment_amount,
                    notes=note,
                ),
                dict(
                    base,
                    account_id=counterpart.id,
                    amount=-investment_amount,
                    notes=note,
                ),
            ])

        for counterpart_id, prior_adjustment in sorted(
            prior_opening_by_account.items(),
        ):
            counterpart = Account.browse(counterpart_id)
            _append_pair(
                currency.round(prior_adjustment),
                counterpart,
                _(
                    "IAS 28 cumulative carrying adjustment brought forward: %s",
                    counterpart.display_name,
                ),
            )
        _append_pair(
            share,
            member.share_of_profit_account_id,
            _("IAS 28 current-period share of profit or loss"),
        )
        if rollforward:
            _append_pair(
                -rollforward.dividends_received,
                rollforward.dividend_income_account_id,
                _("IAS 28 distribution reducing the investment carrying amount"),
            )
            _append_pair(
                rollforward.oci_share,
                rollforward.oci_account_id,
                _("IAS 28 share of associate OCI"),
            )
            _append_pair(
                rollforward.foreign_exchange_adjustment,
                self._cta_account(),
                _("IAS 21 foreign-associate carrying translation adjustment"),
            )
        return vals or None

    def _build_unrealised_profit_vals(self, unrealised):
        """Return the intra-group unrealised-profit elimination run-line vals
        for one record, or None when the margin rounds to zero.

        Removes profit left in the buyer's ending inventory from an intra-group
        sale (IFRS 10 / IAS 27): the seller recognised a margin that, from the
        group's point of view, is not yet realised because the stock has not
        left the group. Consolidated inventory is written back to group cost
        and consolidated profit is reduced by the same amount:

          Let m = unrealised.unrealised_amount (positive margin).

          Dr cogs / retained-earnings account   amount = +m
          Cr inventory account                  amount = -m

        The two signed amounts sum to zero by construction, so the run stays
        balanced. Returns a list of vals so the caller can batch the insert.
        A non-zero row with incomplete accounts is refused. For an upstream
        sale, an additional balanced parent/NCI attribution pair reallocates
        the minority share of the eliminated seller profit.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        amount = currency.round(self._eh_assert_finite_number(
            unrealised.unrealised_amount or 0.0,
            _("Unrealised-profit amount"),
        ))
        rounding = currency.rounding or 0.01
        if float_is_zero(amount, precision_rounding=rounding):
            return None
        if not (
            unrealised.cogs_or_re_account_id
            and unrealised.inventory_account_id
        ):
            raise UserError(_(
                "Unrealised-profit basis %(basis)s is non-zero but its COGS / "
                "retained-earnings and inventory accounts are incomplete.",
                basis=unrealised.display_name,
            ))
        seller = (
            unrealised.seller_member_id
            if unrealised.sale_direction == 'upstream'
            else self.env['eh.consol.member']
        )
        base = {
            'run_id': self.id,
            'kind': 'elimination',
            'company_id': seller.company_id.id if seller else False,
            'member_id': seller.id if seller else False,
        }
        vals = [
            dict(base, account_id=unrealised.cogs_or_re_account_id.id,
                 amount=amount),
            dict(base, account_id=unrealised.inventory_account_id.id,
                 amount=-amount),
        ]
        if seller and seller.ownership_pct < 100.0:
            nci_share = 1.0 - seller.ownership_pct / 100.0
            nci_amount = currency.round(amount * nci_share)
            if not float_is_zero(nci_amount, precision_rounding=rounding):
                nci_account = seller.nci_account_id or self._nci_account()
                self._eh_require_nci_accounts(seller, nci_account)
                reclass_account = self._nci_reclass_account(seller)
                vals.extend([
                    dict(
                        base,
                        account_id=nci_account.id,
                        kind='nci',
                        amount=nci_amount,
                        notes=_(
                            "NCI share of upstream unrealised-profit reversal"
                        ),
                    ),
                    dict(
                        base,
                        account_id=reclass_account.id,
                        amount=-nci_amount,
                        notes=_(
                            "Parent/NCI attribution of upstream unrealised profit"
                        ),
                    ),
                ])
        return vals

    # ---- automatic intragroup elimination (IFRS 10.B86) ----

    def _eh_group_companies(self):
        """Return the res.company records that participate in intragroup
        elimination: the parent plus every full / proportional member.

        Equity-method members are not rolled up line by line, so their
        intragroup balances are not double-counted in the consolidated set and
        must not be auto-eliminated here.
        """
        self.ensure_one()
        companies = self.entity_id._eh_descendant_companies(
            self.entity_id.parent_company_id,
        )
        for member in self.entity_id.member_ids:
            if member.method in ('full', 'proportional') and member.company_id:
                companies |= self.entity_id._eh_member_source_companies(member)
        return companies

    def _eh_member_for_source_company(self, company):
        """Resolve the single member root owning an exact company/branch."""
        self.ensure_one()
        matches = self.entity_id.member_ids.filtered(
            lambda member: company in self.entity_id._eh_member_source_companies(
                member,
            )
        )
        if len(matches) > 1:
            raise UserError(_(
                "Company %s belongs to overlapping consolidation member scopes.",
                company.display_name,
            ))
        return matches

    def _eh_intragroup_translate_rate(self, company, is_pl,
                                      source_currency=None):
        """Return the per-unit conversion rate from a company's currency to the
        presentation currency, consistent with _fetch_balances: closing rate
        (period_to) for balance-sheet items, period average rate for P&L.
        Returns 1.0 when the company currency already is the presentation
        currency.
        """
        self.ensure_one()
        source = source_currency or company.currency_id
        target = self.presentation_currency_id
        if not source or not target or source == target:
            return 1.0
        # A member's functional-currency override is authoritative for its
        # whole branch closure.  Older Odoo series do not make company-root
        # rates visible automatically when `_convert` receives a branch, so
        # resolve the owning member root explicitly and use that same company
        # for both rate validation and conversion.
        member = self._eh_member_for_source_company(company)
        rate_company = member.company_id if member else company
        if is_pl:
            return self._period_average_rate(source, target, rate_company)
        self._eh_assert_translation_rate_available(
            source, target, rate_company, self.period_to,
        )
        return self._eh_assert_finite_number(
            source._convert(
                1.0, target, rate_company, self.period_to, round=False,
            ),
            _("Intragroup closing translation rate"), positive=True,
        )

    def _eh_actual_commercial_root(self, partner):
        """Resolve commercial root from parent/is_company, never stored mirror."""
        seen = set()
        current = partner
        while current and not current.is_company and current.parent_id:
            if current.id in seen:
                raise UserError(_(
                    "Partner hierarchy contains a cycle and cannot support "
                    "automatic intragroup elimination."
                ))
            seen.add(current.id)
            current = current.parent_id
        if current and current.id in seen:
            raise UserError(_(
                "Partner hierarchy contains a cycle and cannot support "
                "automatic intragroup elimination."
            ))
        return current

    def _eh_commercial_contact_ids(self, partner):
        """Return exact contact closure rooted at authoritative commercial root.

        Stored ``commercial_partner_id`` was RPC-writable before Base 1.7.8.
        Traverse parent/is_company directly so legacy drift can neither inject
        nor hide ledger lines.  Nested companies start their own commercial
        root and are deliberately excluded from this contact closure.
        """
        root = self._eh_actual_commercial_root(partner)
        if not root:
            return ()
        self.env.cr.execute(
            """
            WITH RECURSIVE commercial_contact(id, path) AS (
                SELECT partner.id, ARRAY[partner.id]
                  FROM res_partner AS partner
                 WHERE partner.id = %s
                UNION ALL
                SELECT child.id, contact.path || child.id
                  FROM commercial_contact AS contact
                  JOIN res_partner AS child ON child.parent_id = contact.id
                 WHERE NOT child.is_company
                   AND NOT child.id = ANY(contact.path)
            )
            SELECT id FROM commercial_contact ORDER BY id
            """,
            (root.id,),
        )
        return tuple(row[0] for row in self.env.cr.fetchall())

    def _eh_intragroup_balance(self, company, counterparty, account_types,
                               is_pl):
        """Return the signed, presentation-currency balance on `company`'s
        posted AML for the given account types, restricted to lines whose
        partner's commercial partner is `counterparty`'s company partner.

        The sign follows Odoo's convention (debit positive). P&L uses the
        period window and the average rate; balance-sheet uses the cumulative
        balance to period_to and the closing rate, matching _fetch_balances so
        the eliminated amounts are on the same basis as the rolled-up balances.
        Returns {account_id: translated_amount}.
        """
        self.ensure_one()
        member = self._eh_member_for_source_company(company)
        source_currency = (
            member.functional_currency_id if member else company.currency_id
        )
        counter_partner_ids = self._eh_commercial_contact_ids(
            counterparty.partner_id,
        )
        if not counter_partner_ids:
            return {}
        AML = self.env['account.move.line'].sudo()
        amount_field, currency_domain = self._eh_source_amount_basis(
            company, source_currency,
        )
        domain = [
            ('move_id.company_id', '=', company.id),
            ('move_id.state', '=', 'posted'),
            ('account_id.account_type', 'in', list(account_types)),
            ('partner_id', 'in', counter_partner_ids),
        ]
        if is_pl:
            domain += [
                ('move_id.date', '>=', self.period_from),
                ('move_id.date', '<=', self.period_to),
            ]
        else:
            domain += [('move_id.date', '<=', self.period_to)]
        domain += currency_domain
        rows = list(grouped_sum(AML, domain, 'account_id', amount_field))
        rate = self._eh_intragroup_translate_rate(
            company, is_pl, source_currency=source_currency,
        )
        result = {}
        for acc_id, amount in rows:
            self._eh_assert_finite_number(
                amount, _("Posted intragroup source balance"),
            )
            if amount:
                result[acc_id] = self._eh_assert_finite_number(
                    self.presentation_currency_id.round(
                        (amount or 0.0) * rate),
                    _("Translated intragroup balance"),
                )
        return result

    def _eh_manual_elim_accounts(self):
        """Return the set of account ids already covered by a posted manual
        elimination on this run. Used to skip auto elimination for a pair whose
        accounts a reviewer has already eliminated by hand, so the two paths do
        not double-eliminate.
        """
        self.ensure_one()
        return set(
            self.elimination_ids.filtered(lambda e: e.state == 'posted')
            .line_ids.mapped('account_id').ids
        )

    def _build_auto_intragroup_vals(self):
        """Build automatic IFRS 10.B86 intragroup elimination run-line vals.

        Returns (vals_list, warnings). For each ordered pair of group companies
        (A, B):

          receivables/payables: A's receivable to B is eliminated against
          A's own reciprocal (removing the intragroup AR), paired with the
          removal of B's payable to A (removing the intragroup AP). Each is a
          balanced two-leg pair summing to zero, so the run stays balanced and
          the CTA is untouched.

          sales/purchases: A's sales income earned on the counterparty B is
          eliminated against B's purchases / cost of sales on A, again as a
          balanced pair.

        When the reciprocal balances do not agree (A's AR to B does not equal
        B's AP to A, or A's sales to B do not equal B's purchases from A) a
        diagnostic is collected rather than the difference being silently
        plugged; the matched (receivable / sales-side) amount is still
        eliminated. Accounts already covered by a posted manual elimination are
        skipped to avoid double elimination.
        """
        self.ensure_one()
        currency = self.presentation_currency_id
        rounding = currency.rounding or 0.01
        companies = self._eh_group_companies()
        manual_accounts = self._eh_manual_elim_accounts()
        vals_list = []
        warnings = []
        base = {'run_id': self.id, 'kind': 'elimination'}

        Account = self.env['account.account'].sudo()

        def _emit_pair(acc_a, matched_a, acc_b, note):
            """Remove the exact matched slice from two real source accounts."""
            leg_a = currency.round(self._eh_assert_finite_number(
                -matched_a, _("Automatic intragroup elimination amount"),
            ))
            if float_is_zero(leg_a, precision_rounding=rounding):
                return
            vals_list.extend([
                dict(base, account_id=acc_a, amount=leg_a, notes=note),
                dict(base, account_id=acc_b, amount=-leg_a, notes=note),
            ])

        def _match_accounts(left, right, label, company_a, company_b):
            """Greedily reconcile account-level signed balances.

            Each allocation is capped at both actual account residuals. No side
            is fabricated to eliminate the other side's unmatched total. Any
            remaining amount is emitted as an account-coded diagnostic.
            """
            left_remaining = {
                account_id: amount for account_id, amount in left.items()
                if account_id not in manual_accounts and not float_is_zero(
                    amount, precision_rounding=rounding,
                )
            }
            right_remaining = {
                account_id: amount for account_id, amount in right.items()
                if account_id not in manual_accounts and not float_is_zero(
                    amount, precision_rounding=rounding,
                )
            }
            member_a = self._eh_member_for_source_company(company_a)
            member_b = self._eh_member_for_source_company(company_b)
            map_a = member_a._eh_resolved_account_map() if member_a else {}
            map_b = member_b._eh_resolved_account_map() if member_b else {}
            for left_id in sorted(left_remaining):
                for right_id in sorted(right_remaining):
                    left_amount = left_remaining[left_id]
                    right_amount = right_remaining[right_id]
                    if (
                        float_is_zero(left_amount, precision_rounding=rounding)
                        or float_is_zero(
                            right_amount, precision_rounding=rounding,
                        )
                        or left_amount * right_amount >= 0.0
                    ):
                        continue
                    matched_abs = min(abs(left_amount), abs(right_amount))
                    matched_left = math.copysign(matched_abs, left_amount)
                    note = _(
                        "Auto IC %(label)s: %(a)s account %(left)s vs %(b)s "
                        "account %(right)s",
                        label=label,
                        a=company_a.name,
                        b=company_b.name,
                        left=Account.browse(left_id).display_name,
                        right=Account.browse(right_id).display_name,
                    )
                    left_target = map_a.get(left_id)
                    right_target = map_b.get(right_id)
                    _emit_pair(
                        left_target.id if left_target else left_id,
                        matched_left,
                        right_target.id if right_target else right_id,
                        note,
                    )
                    left_remaining[left_id] = currency.round(
                        left_amount - matched_left,
                    )
                    right_remaining[right_id] = currency.round(
                        right_amount + matched_left,
                    )
            for side, residuals, company, counterparty in (
                (_("source"), left_remaining, company_a, company_b),
                (_("reciprocal"), right_remaining, company_b, company_a),
            ):
                for account_id, residual in sorted(residuals.items()):
                    if float_is_zero(residual, precision_rounding=rounding):
                        continue
                    warnings.append(_(
                        "Unmatched intragroup %(label)s %(side)s residual: "
                        "%(company)s / %(account)s against %(counterparty)s = "
                        "%(amount).2f %(currency)s. Add/correct the reciprocal "
                        "or post an evidenced manual elimination.",
                        label=label,
                        side=side,
                        company=company.display_name,
                        account=Account.browse(account_id).display_name,
                        counterparty=counterparty.display_name,
                        amount=residual,
                        currency=currency.name,
                    ))

        for company_a in companies:
            for company_b in companies:
                if company_a == company_b:
                    continue
                # --- reciprocal receivables vs payables ---
                ar = self._eh_intragroup_balance(
                    company_a, company_b,
                    ('asset_receivable',), is_pl=False)
                ap = self._eh_intragroup_balance(
                    company_b, company_a,
                    ('liability_payable',), is_pl=False)
                if ar or ap:
                    _match_accounts(
                        ar, ap, _("AR/AP"), company_a, company_b,
                    )
                # --- sales income vs purchases / cost of sales ---
                sales = self._eh_intragroup_balance(
                    company_a, company_b, _INCOME_TYPES, is_pl=True)
                purch = self._eh_intragroup_balance(
                    company_b, company_a, _EXPENSE_TYPES, is_pl=True)
                if sales or purch:
                    _match_accounts(
                        sales, purch, _("sales/COGS"), company_a, company_b,
                    )
        return vals_list, warnings

    def _eh_compute_warnings(self):
        """Return diagnostics for consolidation steps not yet automated.

        Manual investment eliminations that remain outside the automatic IFRS
        3 basis must never be shipped silently:

        * A full-method subsidiary's investment in the parent's books has
          to be eliminated against the subsidiary's equity, or consolidated
          equity is overstated (the parent's investment and the sub's share
          capital both appear). We flag any full member whose configured
          investment account is not covered by a posted elimination, and
          note full members with no investment account configured at all.
        """
        self.ensure_one()
        warns = []
        members = self.entity_id.member_ids
        # Equity-method members without a pick-up configuration now BLOCK the
        # compute (see _eh_require_equity_config) unless they elect the IAS
        # 28.1A fair value option, so no warning branch remains for them; a
        # fair-value-option member is documented by its disclosure line.
        posted_elim_accounts = self.elimination_ids.filtered(
            lambda e: e.state == 'posted'
        ).line_ids.mapped('account_id')
        unresolved = []
        unconfigured = []
        for m in members.filtered(lambda m: m.method == 'full'):
            # A member whose investment is fully configured for the IFRS 3
            # auto-elimination is resolved: the run books the elimination
            # itself, so it is neither unconfigured nor un-eliminated. When
            # the entity has switched the auto-elimination off, the same
            # member falls back to the diagnostic warnings below.
            auto_eliminated = self.line_ids.filtered(lambda line: (
                line.member_id == m
                and line.kind == 'elimination'
                and line.account_id == m.investment_account_id
            ))
            if auto_eliminated:
                continue
            if not m.investment_account_id:
                unconfigured.append(m.company_id.display_name)
            elif m.investment_account_id not in posted_elim_accounts:
                unresolved.append(m.company_id.display_name)
        if unresolved:
            warns.append(_(
                "The parent's investment is not eliminated against the "
                "equity of these subsidiaries, so consolidated equity is "
                "overstated until you post an investment-elimination entry "
                "(configure the member's acquisition fields and enable "
                "Auto-Eliminate Investment on the entity to have the run "
                "book it automatically): %s.",
                ', '.join(unresolved)))
        if unconfigured:
            warns.append(_(
                "These full-consolidation subsidiaries have no investment "
                "account configured, so the investment elimination cannot "
                "be verified; confirm the investment-in-subsidiary is "
                "eliminated: %s.",
                ', '.join(unconfigured)))
        return warns

    def _compute_cta(self):
        """Compute the CTA balancing entry.

        After translating each member's TB at the configured rates,
        the consolidated balance sheet may not balance because
        income / expense use average rate while assets / liabilities
        use closing rate. The difference is the CTA that goes to
        OCI under IAS 21. We compute it as the negation of the sum
        of every translated balance line on the run so the books
        balance after the entry posts.
        """
        self.ensure_one()
        total = sum(
            self.line_ids.filtered(
                lambda l: l.kind in _CTA_BASE_KINDS,
            ).mapped('amount'),
        )
        # CTA balances the books -> negate. Round in the presentation
        # currency so a 0-dp (JPY) or 3-dp (KWD/BHD) plug carries the right
        # precision and sum(run lines) still nets to zero.
        total = self._eh_assert_finite_number(
            total, _("CTA balancing basis"),
        )
        return -self.presentation_currency_id.round(total)

    def _cta_account(self):
        """Return the CTA / translation-reserve account for this run.

        Prefers the entity's explicit cta_account_id; only when unset falls
        back to the name heuristic (an equity account on the parent chart
        whose name contains 'translation' or 'CTA'). Returns empty when
        neither resolves; the caller then refuses the compute rather than
        posting a CTA line with no account.
        """
        self.ensure_one()
        if self.entity_id.cta_account_id:
            return self.entity_id.cta_account_id
        Account = self.env['account.account'].sudo()
        accounts = Account.search([
            ('account_type', '=', 'equity'),
            '|',
            ('name', 'ilike', 'translation'),
            ('name', 'ilike', 'CTA'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=2, order='id')
        return accounts if len(accounts) == 1 else Account

    def _nci_account(self):
        """Return the default NCI equity account for this run.

        Prefers the entity's explicit nci_account_id; only when unset falls
        back to the name heuristic (an equity account on the parent chart
        whose name contains 'non-controlling' or 'minority'). Returns empty
        when neither resolves; callers that would book an NCI line then refuse
        rather than posting a line with no account.
        """
        self.ensure_one()
        if self.entity_id.nci_account_id:
            return self.entity_id.nci_account_id
        Account = self.env['account.account'].sudo()
        accounts = Account.search([
            ('account_type', '=', 'equity'),
            '|',
            ('name', 'ilike', 'non-controlling'),
            ('name', 'ilike', 'minority'),
            (_acc_company_field(self.env), 'in',
             self.entity_id.parent_company_id.ids),
        ], limit=2, order='id')
        return accounts if len(accounts) == 1 else Account

    def _nci_reclass_account(self, member):
        """Return the consolidated parent-equity / retained-earnings account
        the NCI carve-out debits (reclassifying the minority share OUT of parent
        equity INTO the NCI account), or empty when none can be resolved.

        The NCI carve-out is a two-legged reclass within equity. It must use the
        entity's explicit parent retained-earnings target, or an exactly unique
        ``equity_unaffected`` account on the parent/branch chart. It never picks
        an arbitrary equity row and never debits the subsidiary acquisition-
        elimination account for a post-acquisition attribution.
        """
        self.ensure_one()
        if self.entity_id.parent_retained_earnings_account_id:
            return self.entity_id.parent_retained_earnings_account_id
        return self._retained_earnings_account(
            self.entity_id._eh_descendant_companies(
                self.entity_id.parent_company_id,
            ),
        )

    # ---- helpers ----

    def action_view_lines(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Run lines"),
            'res_model': 'eh.consol.run.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }

    def action_view_trial_balance(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Consolidated trial balance"),
            'res_model': 'eh.consol.trial.balance',
            'view_mode': 'list,pivot',
            'domain': [('run_id', '=', self.id)],
            'context': {
                'default_run_id': self.id,
                'search_default_group_account': 0,
            },
        }

    def action_view_eliminations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Eliminations"),
            'res_model': 'eh.consol.elimination',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('run_id', '=', self.id)],
            'context': {'default_run_id': self.id},
        }
