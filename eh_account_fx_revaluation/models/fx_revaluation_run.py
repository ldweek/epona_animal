# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation run record.

A run translates every open foreign currency journal item on flagged
accounts at the closing rate of revaluation_date and books the
unrealised gain or loss to a single audited journal entry.

State machine:

  draft -> computed -> posted -> reversed
                              \\-> cancelled
                    -> cancelled

Lines (eh.fx.revaluation.line) are produced in the compute step and
made immutable on post. The generated move and the optional reversal
move are linked back to the run.
"""

import logging
import math
from collections import defaultdict
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .fx_integrity import (
    FX_GAIN_ACCOUNT_TYPES,
    FX_LOSS_ACCOUNT_TYPES,
    FX_MOVE_LINK_CAPABILITY,
    assert_company_allowed,
    lock_report_input_versions,
    record_company_domain,
    record_matches_company,
    require_account_type,
    require_record_company,
    resolve_reference,
)

_logger = logging.getLogger(__name__)

# Measurement / input fields that become the audit record behind a posted
# journal entry. Once the run has posted (or reversed after auto-reverse),
# these are frozen at the ORM layer. 'state' is deliberately NOT listed: the
# action methods write only state + audit stamps, so a pure state-transition
# write is never blocked, while a write touching any of these while posted is
# always blocked. action_compute runs only in draft/computed, so freezing on
# 'posted'/'reversed' never interferes with recompute.
_FROZEN_AFTER_POST = (
    'line_ids', 'revaluation_date', 'journal_id', 'gain_account_id',
    'loss_account_id', 'auto_reverse', 'reversal_date', 'aggregate_by_partner',
    'company_id', 'description', 'notes',
)
_FROZEN_AFTER_COMPUTE = (
    'revaluation_date', 'journal_id', 'gain_account_id',
    'loss_account_id', 'auto_reverse', 'reversal_date',
    'aggregate_by_partner', 'company_id',
)
_RUN_EVIDENCE_FIELDS = (
    'state', 'currency_id', 'source_move_version',
    'move_id', 'reversal_move_id',
    'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
    'reversed_at', 'reversed_by_id', 'cancelled_at', 'cancelled_by_id',
    'total_gain', 'total_loss', 'net_adjustment', 'line_count',
)


class EhFxRevaluationRun(models.Model):
    _name = 'eh.fx.revaluation.run'
    _description = "FX Revaluation Run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'revaluation_date desc, id desc'

    # A run's state is a posting state machine: each transition posts / reverses
    # a journal entry. Block any direct write to state that does not originate
    # from one of the action_* methods (which flag the write).
    _eh_guarded_fields = _RUN_EVIDENCE_FIELDS
    _eh_create_guarded_fields = _RUN_EVIDENCE_FIELDS

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    description = fields.Char(
        help="Optional label, e.g. 'March 2026 month end'.",
    )
    state = fields.Selection([
        ('draft', "Draft"),
        ('computed', "Computed"),
        ('posted', "Posted"),
        ('reversed', "Reversed"),
        ('cancelled', "Cancelled"),
    ], default='draft', required=True, tracking=True)

    revaluation_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Closing date for which rates are fetched and balances "
             "translated.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal", required=True,
        domain="[('type', '=', 'general')]", check_company=True,
    )
    gain_account_id = fields.Many2one(
        'account.account', string="Unrealised FX Gain Account",
        required=True, check_company=True,
        domain="[('account_type', 'in', ('income', 'income_other'))]",
    )
    loss_account_id = fields.Many2one(
        'account.account', string="Unrealised FX Loss Account",
        required=True, check_company=True,
        domain="[('account_type', 'in', ('expense', 'expense_other'))]",
    )
    auto_reverse = fields.Boolean(
        default=True,
        help="If set, posting the run also creates and posts a reversal "
             "entry on Reversal Date.",
    )
    reversal_date = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self) + timedelta(days=1),
        tracking=True,
        help=(
            "Accounting date for automatic or later manual reversal. It must "
            "be after revaluation date and is frozen once run is computed."
        ),
    )
    aggregate_by_partner = fields.Boolean(
        default=True,
        help="If set, lines are aggregated per (account, partner, "
             "currency). Otherwise lines are aggregated per (account, "
             "currency) only.",
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True, store=True,
    )
    source_move_version = fields.Integer(
        readonly=True, copy=False, default=0,
        help=(
            "Server-owned company ledger/rate version captured when Compute "
            "rebuilt the run. Posting is refused if source accounting or "
            "currency-rate data changed afterward."
        ),
    )

    line_ids = fields.One2many(
        'eh.fx.revaluation.line', 'run_id', copy=False,
    )
    move_id = fields.Many2one(
        'account.move', string="Revaluation Entry", readonly=True,
        copy=False, ondelete='restrict',
    )
    reversal_move_id = fields.Many2one(
        'account.move', string="Reversal Entry", readonly=True,
        copy=False, ondelete='restrict',
    )

    # ---- audit ----
    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)
    cancelled_at = fields.Datetime(readonly=True, tracking=True)
    cancelled_by_id = fields.Many2one('res.users', readonly=True)

    # ---- totals ----
    total_gain = fields.Monetary(compute='_compute_totals', store=True)
    total_loss = fields.Monetary(compute='_compute_totals', store=True)
    net_adjustment = fields.Monetary(compute='_compute_totals', store=True)
    line_count = fields.Integer(compute='_compute_totals', store=True)
    realized_gain_loss = fields.Monetary(
        compute='_compute_realization_split',
        string="Settled Revaluation-Date Adjustment",
        help=(
            "Net adjustment of lines whose source journal items have "
            "all since been fully reconciled. This labels the settled-source "
            "share of the adjustment measured on the revaluation date; it "
            "does not book or infer a later realization entry. Not stored: "
            "it tracks live reconciliation state."
        ),
    )
    unrealized_gain_loss = fields.Monetary(
        compute='_compute_realization_split',
        string="Open Revaluation-Date Adjustment",
        help=(
            "Net adjustment of lines whose source journal items are "
            "still open (unreconciled). These follow the configured "
            "auto-reversal date and remeasure at the next close until "
            "settled."
        ),
    )

    notes = fields.Text()

    def init(self):
        """Retain terminal audit history while permitting one correction."""
        self.env.cr.execute("""
            DO $$
            DECLARE constraint_name text;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'eh_fx_revaluation_run'::regclass
                       AND contype = 'u'
                       AND pg_get_constraintdef(oid)
                           LIKE 'UNIQUE (revaluation_date, company_id)%'
                LOOP
                    EXECUTE format(
                        'ALTER TABLE eh_fx_revaluation_run '
                        'DROP CONSTRAINT %I', constraint_name
                    );
                END LOOP;
            END $$
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                eh_fx_revaluation_run_live_date_uniq
            ON eh_fx_revaluation_run (company_id, revaluation_date)
            WHERE state IN ('draft', 'computed', 'posted')
        """)

    # ---- compute ----

    @api.depends('line_ids.adjustment')
    def _compute_totals(self):
        for run in self:
            gains = sum(l.adjustment for l in run.line_ids if l.adjustment > 0)
            losses = sum(-l.adjustment for l in run.line_ids if l.adjustment < 0)
            run.total_gain = gains
            run.total_loss = losses
            run.net_adjustment = gains - losses
            run.line_count = len(run.line_ids)

    @api.depends('line_ids.adjustment', 'line_ids.is_realized')
    def _compute_realization_split(self):
        """Split the run's net adjustment into realized vs unrealized.

        Derivation: every revaluation line stores the exact source
        journal items it aggregated (source_move_line_ids). A line is
        realized once ALL of its source items are fully reconciled,
        i.e. the exposure has been settled and the FX movement this run
        measured has crystallised through settlement. Everything else
        (still-open items) is unrealized and lives under the
        auto-reverse pattern. Both totals are signed nets, so
        realized_gain_loss + unrealized_gain_loss == net_adjustment.
        """
        for run in self:
            realized = 0.0
            unrealized = 0.0
            for line in run.line_ids:
                if line.is_realized:
                    realized += line.adjustment
                else:
                    unrealized += line.adjustment
            run.realized_gain_loss = realized
            run.unrealized_gain_loss = unrealized

    # ---- onchange (live form feedback) ----

    @api.onchange('company_id')
    def _onchange_company_id_default_accounts(self):
        """Pre-fill a general journal when company changes on a fresh draft.

        onchange always fires on a single NewId record, so we treat
        self as one and avoid the search-in-loop shape. We only write
        fields that are still empty so a deliberate choice is never
        overwritten.
        """
        self.ensure_one()
        if not self.company_id or self.journal_id:
            return
        assert_company_allowed(
            self.env, self.company_id.id, _("FX revaluation run"),
        )
        Journal = self.env['account.journal']
        journal = Journal.search([
            ('type', '=', 'general'),
            *record_company_domain(Journal, self.company_id),
        ], limit=1)
        if journal:
            self.journal_id = journal

    @api.onchange('revaluation_date')
    def _onchange_revaluation_date_reversal(self):
        for run in self:
            if run.revaluation_date:
                run.reversal_date = run.revaluation_date + timedelta(days=1)

    @api.constrains('revaluation_date', 'reversal_date')
    def _check_reversal_date(self):
        for run in self:
            if (
                run.revaluation_date and run.reversal_date
                and run.reversal_date <= run.revaluation_date
            ):
                raise ValidationError(_(
                    "Reversal date must be after revaluation date."
                ))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            revaluation_date = fields.Date.to_date(
                vals.get('revaluation_date')
                or fields.Date.context_today(self)
            )
            vals.setdefault(
                'reversal_date', revaluation_date + timedelta(days=1),
            )
            assert_company_allowed(
                self.env,
                vals.get('company_id') or self.env.company.id,
                _("FX revaluation run"),
            )
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.fx.revaluation.run') or '/'
                vals['name'] = seq
        runs = super().create(vals_list)
        runs._eh_validate_references()
        return runs

    # ---- write / unlink guards ----

    def write(self, vals):
        """Freeze measurement / input fields once the run has posted.

        A pure state-transition write (the action methods write only
        {'state': ...} plus posted_at/posted_by and the move links) carries
        no frozen field and passes. A write touching a frozen field while
        any record is posted or reversed is always blocked. action_compute
        runs only in draft/computed, so its line_ids rebuild is never
        blocked here.
        """
        if vals and not self.env.su:
            self._eh_check_access('write')
        self._eh_validate_references(vals)
        self._eh_lock_for_transition()
        self._eh_validate_references(vals)
        frozen = [f for f in _FROZEN_AFTER_POST if f in vals]
        confirmed = self.filtered(lambda r: r.state in ('posted', 'reversed'))
        if frozen and confirmed:
            raise UserError(_(
                "Fields on a posted FX revaluation run are frozen "
                "(%(fields)s). Reverse it first (EH Accounting Manager "
                "only) to change it.",
                fields=', '.join(frozen)))
        computed_frozen = [f for f in _FROZEN_AFTER_COMPUTE if f in vals]
        computed = self.filtered(lambda r: r.state == 'computed')
        if computed_frozen and computed:
            raise UserError(_(
                "A computed FX revaluation run's measurement basis is "
                "frozen (%(fields)s). Cancel and reset the run before "
                "changing it, then compute again so rates and exposures "
                "match the posting basis.",
                fields=', '.join(computed_frozen),
            ))
        # A posted / reversed run's state is a control point: resetting it to
        # draft would lift the freeze above. A raw ORM state write without the
        # sanctioned-transition context flag is manager-gated so a plain user
        # cannot un-freeze a GL-backed run.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_run_state_change')):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed FX revaluation run."))

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        posted = self.filtered(
            lambda r: r.state in ('posted', 'reversed')
            or r.move_id or r.reversal_move_id)
        if posted:
            raise UserError(_(
                "A posted FX revaluation run cannot be deleted; reverse "
                "it first."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        """Scan open foreign currency balances and produce revaluation lines."""
        with self.env.cr.savepoint():
            runs = self._eh_prepare_actions()
            runs._eh_assert_rate_direction_review_complete()
            if runs.filtered(
                    lambda run: run.state not in ('draft', 'computed')):
                raise UserError(_(
                    "Compute is only available in draft or computed state.",
                ))
            for run in runs.sorted('id'):
                run.line_ids.unlink()
                run._build_lines()
                run.write({
                    'state': 'computed',
                    'source_move_version': (
                        run._eh_current_source_move_version()
                    ),
                    'computed_at': fields.Datetime.now(),
                    'computed_by_id': self.env.user.id,
                })

    def action_post(self):
        with self.env.cr.savepoint():
            runs = self._eh_prepare_actions(manager=True)
            runs._eh_assert_rate_direction_review_complete()
            if runs.filtered(lambda run: run.state != 'computed'):
                raise UserError(_(
                    "Run must be in computed state before posting.",
                ))
            if runs.filtered(lambda run: not run.line_ids):
                raise UserError(_(
                    "No revaluation lines to post.",
                ))
            runs._eh_assert_source_move_version_fresh()
            for run in runs.sorted('id'):
                run._eh_assert_posting_date_open(run.revaluation_date)
                if run.auto_reverse:
                    run._eh_assert_posting_date_open(run.reversal_date)
                move = run._build_move()
                run.write({
                    'state': 'posted',
                    'posted_at': fields.Datetime.now(),
                    'posted_by_id': self.env.user.id,
                    'move_id': move.id,
                })
                if run.auto_reverse:
                    rev_move = run._build_reversal_move()
                    run.with_context(eh_run_state_change=True).write({
                        'state': 'reversed',
                        'reversed_at': fields.Datetime.now(),
                        'reversed_by_id': self.env.user.id,
                        'reversal_move_id': rev_move.id,
                        'source_move_version': 0,
                    })

    def action_reverse(self):
        with self.env.cr.savepoint():
            runs = self._eh_prepare_actions(manager=True)
            if runs.filtered(lambda run: run.state != 'posted'):
                raise UserError(_(
                    "Only posted runs can be reversed.",
                ))
            if runs.filtered('reversal_move_id'):
                raise UserError(_(
                    "Run already has a reversal move.",
                ))
            for run in runs.sorted('id'):
                rev_move = run._build_reversal_move()
                run.with_context(eh_run_state_change=True).write({
                    'state': 'reversed',
                    'reversed_at': fields.Datetime.now(),
                    'reversed_by_id': self.env.user.id,
                    'reversal_move_id': rev_move.id,
                    'source_move_version': 0,
                })
        return True

    def action_cancel(self):
        with self.env.cr.savepoint():
            runs = self._eh_prepare_actions(manager=True)
            if runs.filtered(
                lambda run: run.state in ('posted', 'reversed')
            ):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run.",
                ))
            runs.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
                'source_move_version': 0,
            })
        return True

    def action_set_to_draft(self):
        with self.env.cr.savepoint():
            runs = self._eh_prepare_actions(manager=True)
            if runs.filtered(lambda run: run.state != 'cancelled'):
                raise UserError(_(
                    "Only cancelled runs can return to draft.",
                ))
            requested_keys = [
                (run.company_id.id, run.revaluation_date) for run in runs
            ]
            if len(set(requested_keys)) != len(requested_keys):
                raise UserError(_(
                    "Only one cancelled FX run per company and date can be "
                    "returned to draft at a time."
                ))
            for run in runs:
                conflict = self.search([
                    ('id', 'not in', runs.ids),
                    ('company_id', '=', run.company_id.id),
                    ('revaluation_date', '=', run.revaluation_date),
                    ('state', 'in', ('draft', 'computed', 'posted')),
                ], limit=1)
                if conflict:
                    raise UserError(_(
                        "%(company)s already has active FX run %(run)s for "
                        "%(date)s. Cancel, reverse, or use that run before "
                        "resetting this history record.",
                        company=run.company_id.display_name,
                        run=conflict.display_name,
                        date=run.revaluation_date,
                    ))
            runs.mapped('line_ids').unlink()
            runs.write({
                'state': 'draft',
                'cancelled_at': False,
                'cancelled_by_id': False,
                'source_move_version': 0,
            })
        return True

    def action_fetch_missing_rates(self):
        """Pull rates from the configured provider for revaluation_date.

        The button is most useful right before action_compute so the
        ledger has a source-proven fixing dated no later than the
        revaluation date. The configured provider serves the company's
        currency as base; ECB cross-derives via EUR for non-EUR companies.
        Only foreign currencies with at least one open monetary line in
        scope are requested, which keeps the cron-friendly path light on
        cold caches.
        """
        with self.env.cr.savepoint():
            self._eh_fetch_missing_rates_atomic()
        return True

    def _eh_fetch_missing_rates_atomic(self):
        self._eh_check_access('write')
        self._eh_validate_references()
        for run in self:
            config = self.env['eh.fx.rate.config'].search([
                ('company_id', '=', run.company_id.id),
            ], limit=1)
            if not config or not config.enabled:
                raise UserError(_(
                    "Company %s has no enabled FX rate config. Open "
                    "Configuration > Heritage > FX Rate Config to "
                    "select a provider.",
                ) % run.company_id.display_name)
            # Only ask for currencies that actually appear in open
            # lines on revaluable accounts; this avoids hammering the
            # provider for currencies the ledger never used.
            accounts = run._eligible_accounts()
            wanted_ids = run._open_currency_ids(accounts)
            currencies = self.env['res.currency'].browse(list(wanted_ids))
            codes = [c.name for c in currencies if c and c != run.company_id.currency_id]
            if not codes:
                raise UserError(_(
                    "Run %s has no foreign-currency exposure to fetch "
                    "rates for.",
                ) % (run.name or run.id))
            # Run operators deliberately have read-only access to provider
            # configuration: they may pull the configured source, but may not
            # see its API key or alter provider/override settings.  Elevate
            # only after the caller, run company, config visibility, exposure
            # currencies and requested date have all been resolved under the
            # caller's access rules.  ``fetch_rates`` is @api.private, so this
            # is a narrow server-owned execution seam rather than a generic
            # configuration-write bypass.
            written = config.sudo().fetch_rates(
                currency_codes=codes, on_date=run.revaluation_date,
            )
            run.message_post(body=_(
                "Fetched and wrote %(count)s rate(s) from %(provider)s. "
                "%(summary)s",
                count=len(written),
                provider=config.provider,
                summary=config.last_fetch_summary,
            ))
        return True

    # ---- helpers ----

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        for run in self:
            company_id = vals.get('company_id', run.company_id.id)
            assert_company_allowed(
                self.env, company_id, _("FX revaluation run"))
            company = resolve_reference(
                self.env, 'res.company', company_id,
                _("FX revaluation company"), access='read')
            journal_id = vals.get('journal_id', run.journal_id.id)
            journal = resolve_reference(
                self.env, 'account.journal', journal_id,
                _("FX revaluation journal"), access='read')
            require_record_company(
                journal, company, _("FX revaluation journal"))
            if journal and journal.type != 'general':
                raise ValidationError(_(
                    "FX revaluation runs require a general journal."
                ))
            for field_name, label, allowed_types in (
                ('gain_account_id', _("FX gain account"),
                 FX_GAIN_ACCOUNT_TYPES),
                ('loss_account_id', _("FX loss account"),
                 FX_LOSS_ACCOUNT_TYPES),
            ):
                account_id = vals.get(field_name, run[field_name].id)
                account = resolve_reference(
                    self.env, 'account.account', account_id,
                    label, access='read')
                require_record_company(account, company, label)
                require_account_type(account, allowed_types, label)
            for line in run.line_ids:
                if line.company_id != company:
                    raise ValidationError(_(
                        "FX revaluation line company must match its run."))
                if line.account_id:
                    if not self.env.su:
                        line.account_id._eh_check_access('read')
                    if not record_matches_company(line.account_id, company):
                        raise ValidationError(_(
                            "FX revaluation line accounts must be available "
                            "to the run company."))
        return self

    def _eh_prepare_action(self, manager=False):
        self.ensure_one()
        return self._eh_prepare_actions(manager=manager)

    def _eh_prepare_actions(self, manager=False):
        """Authorise and globally lock a complete transition recordset."""
        if not self:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        if manager:
            self._check_manager()
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        return self._eh_workflow_action()

    def _eh_lock_for_transition(self):
        """Lock source epochs, then runs, in global id order.

        Version-row locks serialize the source snapshot/check with move and
        rate writers without blocking the business-critical company row. The
        run lock then closes double-submit and inverse-recordset deadlocks. A
        concurrent company rehome fails closed instead of continuing with an
        unlocked replacement source ledger.
        """
        run_ids = tuple(sorted(self.ids))
        if not run_ids:
            return self
        self.flush_recordset()
        expected_company_by_run = {
            run.id: run.company_id.id for run in self
        }
        company_ids = tuple(sorted(set(expected_company_by_run.values())))
        lock_report_input_versions(self.env, company_ids)
        self.env.cr.execute(
            "SELECT id, company_id FROM eh_fx_revaluation_run WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (run_ids,),
        )
        locked_rows = self.env.cr.fetchall()
        if {row[0] for row in locked_rows} != set(run_ids):
            raise UserError(_(
                "An FX revaluation run changed concurrently. Retry the "
                "operation."
            ))
        if any(
            expected_company_by_run.get(run_id) != company_id
            for run_id, company_id in locked_rows
        ):
            raise UserError(_(
                "An FX revaluation run changed company concurrently. Retry "
                "the operation."
            ))
        self.invalidate_recordset()
        return self

    def _eh_lock_for_post(self):
        """Compatibility alias for existing lock regressions."""
        return self._eh_lock_for_transition()

    def _eh_current_source_move_version(self):
        self.ensure_one()
        company = self.company_id
        company.invalidate_recordset(['eh_move_version'])
        return company.eh_move_version

    def _eh_assert_source_move_version_fresh(self):
        for run in self:
            if (
                run.source_move_version
                != run._eh_current_source_move_version()
            ):
                raise UserError(_(
                    "Run %(run)s is stale because source ledger or currency-"
                    "rate data changed after Compute. Compute it again before "
                    "posting.",
                    run=run.name,
                ))
        return self

    def _eh_assert_rate_direction_review_complete(self):
        """Block ledger use while legacy provider rows remain unreviewed."""
        Config = self.env['eh.fx.rate.config']
        Company = self.env['res.company']
        for run in self:
            companies = run.company_id
            if 'root_id' in Company._fields:
                companies |= run.company_id.root_id
            hold = Config.sudo().search([
                ('company_id', 'in', companies.ids),
                ('rate_direction_review_required', '=', True),
            ], limit=1)
            if hold:
                raise UserError(_(
                    "FX revaluation is blocked for %(company)s. Finance "
                    "must review historical currency-rate rows and "
                    "acknowledge the legacy rate-direction safety hold.",
                    company=run.company_id.display_name,
                ))
        return self

    def _eh_assert_posting_date_open(self, posting_date):
        """Preflight fiscal/hard locks before any FX move is created."""
        self.ensure_one()
        company = self.company_id
        # A lock can be advanced by another worker after this run (and its
        # company) entered the ORM cache.  Re-read both the physical locks and
        # their user/exception-aware projections before deciding whether the
        # entry may be created.  Hard locks may live on a parent company in
        # Odoo's branch hierarchy, hence invalidating the complete ancestry.
        lock_fields = [
            name for name in (
                'fiscalyear_lock_date',
                'hard_lock_date',
                'user_fiscalyear_lock_date',
                'user_hard_lock_date',
            ) if name in company._fields
        ]
        lock_companies = (
            company.parent_ids
            if 'parent_ids' in company._fields
            else company
        )
        lock_companies.invalidate_recordset(lock_fields)
        company.invalidate_recordset(lock_fields)
        method = company._get_user_fiscal_lock_date
        try:
            # Odoo 18/19: journal-aware and includes hard-lock exceptions.
            lock_date = method(self.journal_id)
        except TypeError:
            # Odoo 16/17 signature has no journal argument.
            lock_date = method()
        if lock_date and posting_date <= lock_date:
            raise UserError(_(
                "FX entry date %(date)s is on or before accounting lock date "
                "%(lock)s for %(company)s. Choose an open revaluation or "
                "reversal date before posting.",
                date=posting_date,
                lock=lock_date,
                company=company.display_name,
            ))
        return True

    def _eligible_accounts(self):
        self.ensure_one()
        Account = self.env['account.account']
        return Account.search([
            ('eh_fx_revalue', '=', True),
            *record_company_domain(Account, self.company_id),
        ])

    def _open_line_candidate_domain(self, accounts):
        """Return SQL-prunable point-in-time exposure candidates.

        Currently open lines are always candidates. A fully reconciled line
        is relevant only when at least one reconciliation occurred after the
        close, because only then can it have been open at the close. This
        removes the permanently settled historical ledger before Python
        residual reconstruction while preserving later settlements.
        """
        self.ensure_one()
        return [
            ('account_id', 'in', accounts.ids),
            ('move_id.state', '=', 'posted'),
            ('date', '<=', self.revaluation_date),
            ('currency_id', '!=', self.company_id.currency_id.id),
            ('currency_id', '!=', False),
            ('company_id', '=', self.company_id.id),
            '|', '|',
            ('reconciled', '=', False),
            ('matched_debit_ids.max_date', '>', self.revaluation_date),
            ('matched_credit_ids.max_date', '>', self.revaluation_date),
        ]

    def _open_currency_ids(self, accounts):
        """Get exposure currencies with one grouped SQL query."""
        if not accounts:
            return set()
        groups = self.env['account.move.line'].read_group(
            self._open_line_candidate_domain(accounts),
            ['currency_id'], ['currency_id'], lazy=False,
        )
        return {
            row['currency_id'][0] for row in groups if row.get('currency_id')
        }

    def _open_lines_query(self, accounts):
        """Fetch open foreign currency journal items as raw dicts.

        Selects posted account.move.line records on the given accounts
        whose currency_id != company.currency_id and that have a non
        zero residual in foreign currency.
        """
        self.ensure_one()
        if not accounts:
            return []
        AccountMoveLine = self.env['account.move.line']
        lines = AccountMoveLine.search(
            self._open_line_candidate_domain(accounts),
        )
        lines.invalidate_recordset()
        out = []
        for line in lines:
            is_revaluation_leg = bool(
                line.move_id.eh_fx_revaluation_run_id
                or line.move_id.eh_fx_revaluation_reversal_run_id
            )
            # Point-in-time residual: reconstruct the balance that was
            # still open AS OF the revaluation date, ignoring any partial
            # reconciliation dated after it. Reading the live
            # amount_residual would understate exposure for a line that
            # was open on the revaluation date but settled afterwards.
            if is_revaluation_leg:
                # Prior sealed revaluation legs change only functional-
                # currency carrying amount. Keep their zero foreign amount
                # in the group so a later non-reversing run posts only the
                # incremental movement (and unwinds it after settlement).
                balance_company = line.balance
                balance_foreign = 0.0
            else:
                balance_company, balance_foreign = self._residual_at_date(
                    line, self.revaluation_date,
                )
            if (
                line.currency_id.is_zero(balance_foreign)
                and self.company_id.currency_id.is_zero(balance_company)
            ):
                continue
            out.append({
                'line_id': line.id,
                'account_id': line.account_id.id,
                'partner_id': line.partner_id.id,
                'currency_id': line.currency_id.id,
                'balance_company': balance_company,
                'balance_foreign': balance_foreign,
            })
        return out

    def _residual_at_date(self, line, date):
        """Residual of `line` (company currency, foreign currency) as it
        stood on `date`, reversing only partials reconciled on or before
        that date.

        Mirrors Odoo's residual computation but filters partials by
        max_date so a settlement dated after the revaluation date does
        not retroactively close a line that was open at period end.
        """
        residual = line.balance
        residual_currency = line.amount_currency
        # line is the debit move of these partials.
        for partial in line.matched_credit_ids:
            if partial.max_date and partial.max_date <= date:
                residual -= partial.amount
                residual_currency -= partial.debit_amount_currency
        # line is the credit move of these partials.
        for partial in line.matched_debit_ids:
            if partial.max_date and partial.max_date <= date:
                residual += partial.amount
                residual_currency += partial.credit_amount_currency
        return residual, residual_currency

    def _closing_rate(self, currency):
        """Return units of company currency per 1 unit of foreign currency
        at the revaluation date.

        Odoo's general conversion helper intentionally falls back first to a
        currency's earliest *later* row and then to 1.0 when no row exists.
        Those conveniences are unsafe for an IAS 21 closing measurement.  A
        foreign currency therefore requires an actual company/global fixing
        dated on or before the close.  Technical rates are selected directly
        in the same company-before-global order and divided without invoking
        either fallback.  The functional currency remains a valid unit base
        when it has no explicit rate row of its own.
        """
        self.ensure_one()
        company_ccy = self.company_id.currency_id
        if currency == company_ccy:
            return 1.0
        foreign_technical_rate = self._eh_technical_rate_as_of(
            currency, required=True,
        )
        company_technical_rate = self._eh_technical_rate_as_of(
            company_ccy, required=False,
        )
        rate = company_technical_rate / foreign_technical_rate
        try:
            valid_rate = math.isfinite(float(rate)) and float(rate) > 0.0
        except (TypeError, ValueError, OverflowError):
            valid_rate = False
        if not valid_rate:
            raise UserError(_(
                "No finite positive closing rate is available for currency "
                "%(name)s on %(date)s.",
                name=currency.name,
                date=self.revaluation_date,
            ))
        return rate

    def _eh_technical_rate_as_of(self, currency, required):
        """Return a deterministic technical rate without Odoo fallbacks."""
        self.ensure_one()
        Rate = self.env['res.currency.rate'].sudo()
        Company = self.env['res.company']
        rate_company = (
            self.company_id.root_id
            if 'root_id' in Company._fields
            else self.company_id
        )
        base_domain = [
            ('currency_id', '=', currency.id),
            ('name', '<=', self.revaluation_date),
        ]
        row = Rate.search([
            *base_domain,
            ('company_id', '=', rate_company.id),
        ], order='name desc, id desc', limit=1)
        if not row:
            row = Rate.search([
                *base_domain,
                ('company_id', '=', False),
            ], order='name desc, id desc', limit=1)
        if not row:
            if not required and currency == self.company_id.currency_id:
                return 1.0
            raise UserError(_(
                "No closing-rate fixing for %(currency)s is dated on or "
                "before %(date)s. A later rate or Odoo's 1.0 fallback "
                "cannot be used for an IAS 21 close.",
                currency=currency.name,
                date=self.revaluation_date,
            ))
        try:
            rate = float(row.rate)
            valid = math.isfinite(rate) and rate > 0.0
        except (TypeError, ValueError, OverflowError):
            valid = False
            rate = 0.0
        if not valid:
            raise UserError(_(
                "Closing-rate fixing %(rate)s for %(currency)s on %(date)s "
                "is not finite and positive.",
                rate=row.display_name,
                currency=currency.name,
                date=row.name,
            ))
        return rate

    def _build_lines(self):
        self.ensure_one()
        accounts = self._eligible_accounts()
        raw = self._open_lines_query(accounts)
        groups = defaultdict(lambda: {
            'balance_company': 0.0,
            'balance_foreign': 0.0,
            'line_ids': [],
        })
        for row in raw:
            partner_key = row['partner_id'] if self.aggregate_by_partner else False
            key = (row['account_id'], partner_key, row['currency_id'])
            g = groups[key]
            g['balance_company'] += row['balance_company']
            g['balance_foreign'] += row['balance_foreign']
            g['line_ids'].append(row['line_id'])

        Line = self.env['eh.fx.revaluation.line']
        Currency = self.env['res.currency']
        Account = self.env['account.account']
        company_ccy = self.company_id.currency_id

        rate_cache = {}

        line_values = []
        for (account_id, partner_id, currency_id), data in groups.items():
            if currency_id not in rate_cache:
                rate_cache[currency_id] = self._closing_rate(
                    Currency.browse(currency_id),
                )
            closing_rate = rate_cache[currency_id]
            new_balance_company = company_ccy.round(
                data['balance_foreign'] * closing_rate,
            )
            adjustment = company_ccy.round(
                new_balance_company - data['balance_company'],
            )
            account = Account.browse(account_id)
            nature = self._classify_nature(account, adjustment)
            line_values.append({
                'run_id': self.id,
                'account_id': account_id,
                'partner_id': partner_id or False,
                'foreign_currency_id': currency_id,
                'balance_company': company_ccy.round(data['balance_company']),
                'balance_foreign': data['balance_foreign'],
                'closing_rate': closing_rate,
                'new_balance_company': new_balance_company,
                'adjustment': adjustment,
                'nature': nature,
                'source_line_count': len(data['line_ids']),
                'source_move_line_ids': [(6, 0, data['line_ids'])],
            })
        if line_values:
            Line.create(line_values)

    @staticmethod
    def _classify_nature(account, adjustment):
        """Classify the adjustment as gain or loss using the SIGNED
        balance convention.

        balance_company is Odoo's signed balance: positive for asset
        / debit-side, negative for liability / credit-side. Adjustment
        is (new - old) of that signed balance.

        Asset (positive base balance): positive adjustment grows the
        asset = gain, negative shrinks it = loss.
        Liability (negative base balance): negative adjustment makes
        the balance more negative (debt grows) = loss; positive
        adjustment moves toward zero (debt shrinks) = gain.
        """
        if adjustment == 0:
            return 'flat'
        is_asset = account.account_type in (
            'asset_cash', 'asset_receivable', 'asset_current',
            'asset_non_current', 'asset_fixed', 'asset_prepayments',
        )
        if is_asset:
            return 'gain' if adjustment > 0 else 'loss'
        # Liability: more negative (adjustment < 0) = liability grows = loss.
        return 'loss' if adjustment < 0 else 'gain'

    def _build_move(self):
        self.ensure_one()
        company_ccy = self.company_id.currency_id
        move_lines = []
        gain_total = 0.0
        loss_total = 0.0
        for line in self.line_ids:
            if line.adjustment == 0 or line.nature == 'flat':
                continue
            # The leg sign follows the economic nature of the adjustment,
            # not the raw sign of (new minus old). _classify_nature has
            # already encoded the asset / liability inversion:
            #   gain on asset (asset up):        DR account, CR FX gain
            #   loss on asset (asset down):      CR account, DR FX loss
            #   gain on liability (liab down):   DR account, CR FX gain
            #   loss on liability (liab up):     CR account, DR FX loss
            # In every case: gain implies debit on the account leg, loss
            # implies credit. The foreign currency residual is kept on the
            # original journal items; this leg is a pure functional
            # currency translation adjustment: keep its exposure currency
            # for fixed-currency-account validity and later-run grouping,
            # while amount_currency remains zero because foreign units do
            # not change.
            amount = company_ccy.round(abs(line.adjustment))
            if line.nature == 'gain':
                debit, credit = amount, 0.0
                gain_total += amount
            else:  # 'loss'
                debit, credit = 0.0, amount
                loss_total += amount
            move_lines.append((0, 0, {
                'name': _("FX revaluation %s", line.account_id.code or ''),
                'account_id': line.account_id.id,
                'partner_id': line.partner_id.id if line.partner_id else False,
                'currency_id': line.foreign_currency_id.id,
                'amount_currency': 0.0,
                'debit': debit,
                'credit': credit,
            }))

        if gain_total:
            move_lines.append((0, 0, {
                'name': _("Unrealised FX gain"),
                'account_id': self.gain_account_id.id,
                'debit': 0.0,
                'credit': company_ccy.round(gain_total),
            }))
        if loss_total:
            move_lines.append((0, 0, {
                'name': _("Unrealised FX loss"),
                'account_id': self.loss_account_id.id,
                'debit': company_ccy.round(loss_total),
                'credit': 0.0,
            }))

        if not move_lines:
            raise UserError(_(
                "Revaluation produced no adjustments to post.",
            ))

        move = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': self.revaluation_date,
            'journal_id': self.journal_id.id,
            'ref': _("FX Revaluation %s", self.name),
            'eh_fx_revaluation_run_id': self.id,
            'line_ids': move_lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move

    def _build_reversal_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_(
                "Cannot reverse: original revaluation move is missing.",
            ))
        if (
            self.move_id.eh_fx_revaluation_run_id != self
            or self.move_id.company_id != self.company_id
            or self.move_id.state != 'posted'
            or not self.move_id.eh_sealed
        ):
            raise UserError(_(
                "Cannot reverse: original FX revaluation provenance is "
                "missing or inconsistent."))
        reversal_date = self.reversal_date
        self._eh_assert_posting_date_open(reversal_date)
        defaults = {
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("FX Revaluation Reversal %s", self.name),
        }
        rev_move = self.move_id.sudo(False)._eh_reverse_with_verified_capability(
            [defaults], cancel=False,
        )
        rev_move._eh_post_verified_reversal()
        self._eh_seal_reversal(rev_move)
        rev_move.sudo().with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_write_sealed_metadata(
            {'eh_fx_revaluation_reversal_run_id': self.id},
            {'eh_fx_revaluation_reversal_run_id'},
        )
        return rev_move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_fx_revaluation_run_id = fields.Many2one(
        'eh.fx.revaluation.run', string="FX Revaluation Run",
        readonly=True, index=True, ondelete='restrict', copy=False,
    )
    eh_fx_revaluation_reversal_run_id = fields.Many2one(
        'eh.fx.revaluation.run', string="FX Revaluation Reversal Run",
        readonly=True, index=True, ondelete='restrict', copy=False,
    )
    eh_fx_hedge_movement_id = fields.Many2one(
        'eh.fx.hedge.movement', string="FX Hedge Movement",
        readonly=True, index=True, ondelete='restrict', copy=False,
    )
    eh_fx_hedge_reclassification_movement_id = fields.Many2one(
        'eh.fx.hedge.movement', string="FX Hedge Reclassification",
        readonly=True, index=True, ondelete='restrict', copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = (
            'eh_fx_revaluation_run_id',
            'eh_fx_revaluation_reversal_run_id',
            'eh_fx_hedge_movement_id',
            'eh_fx_hedge_reclassification_movement_id',
        )
        if any(any(vals.get(field) for field in protected)
               for vals in vals_list) and not (
            self.env.context.get('eh_fx_move_link_capability')
            is FX_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "FX journal provenance is engine-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if {
            'eh_fx_revaluation_run_id',
            'eh_fx_revaluation_reversal_run_id',
            'eh_fx_hedge_movement_id',
            'eh_fx_hedge_reclassification_movement_id',
        }.intersection(vals) and not (
            self.env.context.get('eh_fx_move_link_capability')
            is FX_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "FX journal provenance is immutable."))
        return super().write(vals)
