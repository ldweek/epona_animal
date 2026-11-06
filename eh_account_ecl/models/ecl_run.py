# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.run: a period-end expected credit loss measurement for one company.

Populate: age the company's open receivables by days past their due date at
the reporting date and total the gross carrying amount into each matrix
bucket, split per portfolio segment when segments exist. Compute: under the
general approach, run the stage engine (DPD backstops, qualitative flags,
low-credit-risk exemption, cure probation, POCI pinning) and total the
probability-weighted expected credit loss to the closing allowance; the
simplified approach measures lifetime ECL straight off the matrix. Post:
recognise the movement from the opening allowance in one balanced entry,
debiting impairment loss and crediting the loss-allowance contra-asset on an
increase, and reversing on a decrease (IFRS 9.5.5.8), then build the
per-stage IFRS 7.35H reconciliation. Write-offs consume the recognised
allowance (IFRS 9.5.4.4) and feed back into the reconciliation.
"""

import logging

from psycopg2.extras import execute_values

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

ECL_RUN_ENGINE_CTX = 'eh_ecl_run_engine'


class EhEclRun(models.Model):
    _name = 'eh.ecl.run'
    _description = "Expected credit loss run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
                'eh.workflow.guard']
    _order = 'reporting_date desc, id desc'
    _rec_name = 'name'

    # State moves only through this model's own actions (which run as su). A
    # direct non-superuser write to state is refused by eh.workflow.guard, so a
    # plain user cannot RPC ``write({'state': 'posted'})`` past action_post and
    # its balanced ledger movement.
    _eh_guarded_fields = (
        'state', 'move_id', 'reversal_move_id',
        'zero_movement_posted',
        'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('posted', "Posted"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    measurement_approach = fields.Selection(
        [('simplified', "Simplified (provision matrix)"),
         ('general', "General (3-stage)")],
        default='simplified', required=True, tracking=True,
        help="A run measures with exactly one approach. Simplified measures "
             "lifetime ECL from the provision matrix (IFRS 9.5.5.15). "
             "General measures ECL per bucket from EAD, LGD and the stage's "
             "probability of default, with the stage engine re-staging "
             "every compute (IFRS 9.5.5.1-5.5.11).")
    backstop_dpd_stage2 = fields.Integer(
        string="Stage 2 Backstop (DPD)", default=30, tracking=True,
        help="Days past due beyond which a significant increase in credit "
             "risk is presumed and the exposure moves to Stage 2 "
             "(rebuttable presumption, IFRS 9.5.5.11).")
    backstop_dpd_stage3 = fields.Integer(
        string="Stage 3 Backstop (DPD)", default=90, tracking=True,
        help="Days past due beyond which default is presumed and the "
             "exposure moves to Stage 3 (IFRS 9.B5.5.37).")
    probation_runs = fields.Integer(
        string="Cure Probation (runs)", default=2, tracking=True,
        help="Consecutive runs an exposure must sit below its staged risk "
             "level before the stage engine reverts (cures) it, so a "
             "one-period improvement does not flip lifetime ECL back to "
             "12-month.")
    stage3_pd_policy = fields.Selection(
        [
            ('keyed_lifetime', "Use keyed lifetime PD"),
            ('defaulted_100', "Treat Stage 3 as defaulted (100% PD)"),
        ],
        default='keyed_lifetime', required=True, tracking=True,
        help="Controls the general-model PD for non-POCI Stage 3 exposures. "
             "Use keyed lifetime PD when credit impairment does not mean a "
             "contractual default under the entity's policy. Choose 100% PD "
             "when Stage 3 is the entity's default population; LGD and "
             "forward-looking recovery scenarios still determine the loss.",
    )

    bucket_ids = fields.One2many('eh.ecl.bucket', 'run_id', copy=True)
    scenario_ids = fields.One2many(
        'eh.ecl.scenario', 'run_id', copy=True,
        help="Forward-looking scenarios; weights must sum to 1. Without "
             "scenarios the run measures on a single implicit base "
             "scenario.")
    transfer_ids = fields.One2many(
        'eh.ecl.transfer', 'run_id', copy=False, readonly=True)
    recon_ids = fields.One2many(
        'eh.ecl.recon', 'run_id', copy=False, readonly=True)
    writeoff_ids = fields.One2many(
        'eh.ecl.writeoff', 'run_id', copy=False, readonly=True)

    opening_allowance = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Loss allowance carried at the start of the period; the run "
             "posts only the movement to the closing allowance. Defaults by "
             "roll-forward from the prior posted run's closing allowance; "
             "manual override is allowed and flagged if it disagrees with the "
             "ledger.")
    rolled_opening_allowance = fields.Monetary(
        compute='_compute_roll_forward', currency_field='currency_id',
        help="Closing allowance of the immediately prior posted run for this "
             "company; the roll-forward source for the opening allowance.")
    ledger_allowance = fields.Monetary(
        compute='_compute_roll_forward', currency_field='currency_id',
        help="Loss-allowance account ledger balance carried before this "
             "reporting date; the opening allowance should tie to it.")
    opening_ties_out = fields.Boolean(
        compute='_compute_roll_forward',
        help="True when the entered opening allowance agrees with the "
             "loss-allowance ledger balance carried into this period.")
    closing_allowance = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_gross = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Closing less opening allowance; positive = charge to P&L.")

    impairment_expense_account_id = fields.Many2one(
        'account.account', string="Impairment Loss Account", tracking=True,
        domain="[('account_type', 'in', ['expense', 'income_other'])]")
    loss_allowance_account_id = fields.Many2one(
        'account.account', string="Loss Allowance Account", tracking=True,
        domain="[('account_type', '=', 'asset_current')]",
        help="Contra-asset that carries the accumulated loss allowance.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_date = fields.Date(
        tracking=True, copy=False,
        help="Explicit accounting date for reversing a posted ECL movement. "
             "It must be on or after the original movement and in an open "
             "accounting period.",
    )
    zero_movement_posted = fields.Boolean(
        readonly=True, copy=False, tracking=True,
        help=(
            "Server-owned posting evidence for a run whose rounded closing "
            "allowance exactly equals its opening allowance. No artificial "
            "zero-value journal is created; this marker lets the immutable "
            "period snapshot participate in the ECL roll-forward chain."
        ),
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    has_exposure_providers = fields.Boolean(
        compute='_compute_has_exposure_providers',
        help="True when at least one loss-allowance exposure provider is "
             "installed (for example presented-cheque suspense holdings), so "
             "the populate-including-exposures path has something extra to "
             "ingest beyond the open receivables.")

    notes = fields.Text()

    _check_backstops = models.Constraint(
        'CHECK (backstop_dpd_stage2 > 0 AND '
        'backstop_dpd_stage3 > backstop_dpd_stage2)',
        'DPD backstops must be positive and the Stage 3 backstop must '
        'exceed the Stage 2 backstop.')
    _check_probation = models.Constraint(
        'CHECK (probation_runs >= 1)',
        'Cure probation must be at least one run.')

    def init(self):
        """Keep terminal history while allowing one live same-date correction."""
        self.env.cr.execute("""
            DO $$
            DECLARE constraint_name text;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'eh_ecl_run'::regclass
                       AND contype = 'u'
                       AND pg_get_constraintdef(oid)
                           LIKE 'UNIQUE (company_id, reporting_date)%'
                LOOP
                    EXECUTE format(
                        'ALTER TABLE eh_ecl_run DROP CONSTRAINT %I',
                        constraint_name
                    );
                END LOOP;
            END $$
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS eh_ecl_run_live_date_uniq
            ON eh_ecl_run (company_id, reporting_date)
            WHERE state IN ('draft', 'computed', 'posted')
        """)

    # Measurement / input fields frozen once the run is posted or reversed. A
    # posted run has recognised a balanced ledger movement (IFRS 9.5.5.8); its
    # inputs must not silently drift from what was posted. The state-transition
    # writes performed by action_compute / action_post / action_reverse /
    # action_cancel touch only state + audit stamps + move links, none of which
    # appear here, so those flows are never blocked. State itself is never
    # frozen. Reverse the run (manager-gated) to reopen it for editing.
    _FROZEN_AFTER_COMPUTE = (
        'reporting_date', 'measurement_approach', 'company_id',
        'opening_allowance', 'impairment_expense_account_id',
        'loss_allowance_account_id', 'journal_id', 'bucket_ids',
        'scenario_ids', 'backstop_dpd_stage2', 'backstop_dpd_stage3',
        'probation_runs', 'stage3_pd_policy',
    )

    @api.depends('bucket_ids.ecl_effective', 'bucket_ids.gross_carrying',
                 'opening_allowance')
    def _compute_totals(self):
        for run in self:
            run.closing_allowance = sum(
                run.bucket_ids.mapped('ecl_effective'))
            run.total_gross = sum(run.bucket_ids.mapped('gross_carrying'))
            run.movement = run.closing_allowance - run.opening_allowance

    @api.depends('company_id', 'reporting_date', 'loss_allowance_account_id',
                 'opening_allowance')
    def _compute_roll_forward(self):
        for run in self:
            rolled = run._prior_closing_allowance()
            ledger = run._ledger_allowance_balance()
            run.rolled_opening_allowance = rolled
            run.ledger_allowance = ledger
            currency = run.currency_id or run.company_id.currency_id
            if currency and run.loss_allowance_account_id:
                run.opening_ties_out = currency.is_zero(
                    run.opening_allowance - ledger)
            else:
                # No ledger reference available yet; nothing to disagree with.
                run.opening_ties_out = True

    def _compute_has_exposure_providers(self):
        # A provider is any installed model that implements the documented
        # exposure hook (excluding account.move.line, already swept). The set
        # is registry-wide, so it is identical for every run in the batch.
        has_provider = bool(self._eh_ecl_exposure_providers())
        for run in self:
            run.has_exposure_providers = has_provider

    def _prior_run(self):
        """Immediately prior still-posted run, or an empty recordset.

        A reversed run no longer carries an allowance in the ledger. Treating
        its former closing measurement as opening recreates an allowance that
        was explicitly unwound and breaks the ledger tie-out.
        """
        self.ensure_one()
        if not self.company_id:
            return self.browse()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
        ]
        if self.reporting_date:
            domain.append(('reporting_date', '<', self.reporting_date))
        if self.id:
            domain.append(('id', '!=', self.id))
        prior = self.search(
            domain, order='reporting_date desc, id desc', limit=1,
        )
        if prior:
            prior._eh_assert_posted_provenance()
        return prior

    def _writeoff_posted_total(self, on_or_before=None):
        """Allowance already consumed by this run's posted write-offs."""
        self.ensure_one()
        cutoff = fields.Date.to_date(on_or_before) if on_or_before else False
        return sum(self.writeoff_ids.filtered(
            lambda writeoff: (
                writeoff.state == 'posted'
                and (not cutoff or writeoff.date <= cutoff)
            )
        ).mapped('amount'))

    def _prior_closing_allowance(self):
        """Prior posted run's closing allowance net of its write-offs.

        Write-offs posted against the prior run consumed part of its
        recognised allowance (Dr allowance / Cr receivable), so the balance
        actually carried into this period is closing less write-offs; this
        keeps the roll-forward tied to the loss-allowance ledger.
        """
        self.ensure_one()
        prior = self._prior_run()
        if not prior:
            return 0.0
        return prior.closing_allowance - prior._writeoff_posted_total(
            on_or_before=self.reporting_date,
        )

    def _ledger_allowance_balance(self):
        """Loss-allowance ledger balance carried before this reporting date.

        The contra-asset carries a credit (negative signed) balance, so the
        loss allowance it represents is the negated signed balance.
        """
        self.ensure_one()
        account = self.loss_allowance_account_id
        if not account or not self.company_id:
            return 0.0
        domain = [
            ('account_id', '=', account.id),
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
        ]
        if self.reporting_date:
            domain.append(('date', '<', self.reporting_date))
        if self.move_id:
            domain.append(('move_id', '!=', self.move_id.id))
        Line = self.env['account.move.line']
        if not self.env.su:
            Line._eh_check_access('read')
            visible = Line.search(domain)
            privileged = Line.sudo().search(domain)
            if set(visible.ids) != set(privileged.ids):
                raise AccessError(_(
                    "The ECL roll-forward ledger basis contains rows the "
                    "caller is not authorised to read."
                ))
            lines = privileged
        else:
            lines = Line.sudo().search(domain)
        signed = sum(lines.mapped('balance'))
        return -signed

    @api.model
    def default_get(self, requested_fields=None, **kwargs):
        """Accept framework keyword variants without shadowing ``fields``."""
        keyword_names = [
            kwargs.pop(name)
            for name in ('fields', 'fields_list')
            if name in kwargs
        ]
        if kwargs:
            raise TypeError(
                "default_get received unexpected keyword(s): %s"
                % ', '.join(sorted(kwargs))
            )
        supplied = [value for value in [requested_fields, *keyword_names]
                    if value is not None]
        if len(supplied) > 1:
            raise TypeError("default_get received more than one field list")
        field_names = supplied[0] if supplied else []
        field_names = field_names or []
        vals = super().default_get(field_names)
        if 'opening_allowance' in field_names \
                and not vals.get('opening_allowance'):
            company = self.env['res.company'].browse(
                vals.get('company_id')) or self.env.company
            from odoo import fields as field_api
            date = field_api.Date.to_date(vals.get('reporting_date')) \
                or field_api.Date.context_today(self)
            rolled = self._roll_forward_default(company, date)
            if rolled:
                vals['opening_allowance'] = rolled
        return vals

    @api.model
    def _roll_forward_default(self, company, reporting_date):
        """Prior posted run's carried allowance for a company/date, or 0.0."""
        if not company:
            return 0.0
        domain = [
            ('company_id', '=', company.id),
            ('state', '=', 'posted'),
        ]
        if reporting_date:
            domain.append(('reporting_date', '<', reporting_date))
        prior = self.search(domain, order='reporting_date desc, id desc',
                            limit=1)
        if not prior:
            return 0.0
        prior._eh_assert_posted_provenance()
        return prior.closing_allowance - prior._writeoff_posted_total(
            on_or_before=reporting_date,
        )

    def _eh_assert_posted_provenance(self):
        """Prove a posted run is backed by a move or a genuine zero marker."""
        for run in self:
            currency = run.currency_id or run.company_id.currency_id
            zero_movement = bool(
                currency and currency.is_zero(currency.round(run.movement))
            )
            if run.state != 'posted' or run.reversal_move_id:
                raise UserError(_(
                    "Run %s is not an active posted ECL roll-forward source.",
                    run.display_name,
                ))
            if run.zero_movement_posted:
                if run.move_id or not zero_movement:
                    raise UserError(_(
                        "Run %s carries invalid zero-movement posting "
                        "provenance.", run.display_name,
                    ))
                continue
            move = run.move_id
            if (
                zero_movement
                or not move
                or move.state != 'posted'
                or move.company_id != run.company_id
                or move.journal_id != run.journal_id
                or move.move_type != 'entry'
                or move.date != run.reporting_date
                or not move.eh_sealed
                or move.eh_legacy_unverified_seal
                or move.reversed_entry_id
            ):
                raise UserError(_(
                    "Run %s failed its active sealed ECL posting provenance "
                    "check.", run.display_name,
                ))
        return self

    def _eh_active_posted_successor(self):
        """Return the earliest active later snapshot in this company."""
        self.ensure_one()
        if not self.company_id or not self.reporting_date:
            return self.browse()
        successor = self.sudo().search([
            ('company_id', '=', self.company_id.id),
            ('reporting_date', '>', self.reporting_date),
            ('state', '=', 'posted'),
            ('id', '!=', self.id),
        ], order='reporting_date, id', limit=1)
        if successor:
            successor._eh_assert_posted_provenance()
        return successor

    def _eh_assert_no_active_posted_successor(self, operation):
        """Protect a later posted snapshot from changes to its opening basis."""
        for run in self:
            successor = run._eh_active_posted_successor()
            if successor:
                raise UserError(_(
                    "Run %(run)s cannot %(operation)s while later active run "
                    "%(successor)s depends on its allowance. Reverse the ECL "
                    "chain newest-first.",
                    run=run.display_name,
                    operation=operation,
                    successor=successor.display_name,
                ))
        return self

    @api.model_create_multi
    def create(self, vals_list):
        company_ids = {
            vals.get('company_id') or self.env.company.id
            for vals in vals_list
        }
        for company_id in company_ids:
            self._eh_assert_company_allowed(company_id)
        # Company is the common first lock for both configuration changes and
        # transitions.  It also stabilises the opening roll-forward read.
        self._eh_lock_for_transition(extra_company_ids=company_ids)
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.ecl.run') or '/'
            if 'opening_allowance' not in vals:
                company = self.env['res.company'].browse(
                    vals.get('company_id')) or self.env.company
                date = fields.Date.to_date(vals.get('reporting_date')) \
                    or fields.Date.context_today(self)
                rolled = self._roll_forward_default(company, date)
                if rolled:
                    vals['opening_allowance'] = rolled
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
                "An ECL run may only use a company enabled in the caller's "
                "current company context."
            ))

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        if not self.env.su:
            self._eh_check_access('write')
        for run in self:
            company_id = vals.get('company_id', run.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            journal = self.env['account.journal'].browse(
                vals.get('journal_id', run.journal_id.id))
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id != company:
                    raise UserError(_("The ECL journal must belong to the run company."))
            for field_name in (
                'impairment_expense_account_id', 'loss_allowance_account_id',
            ):
                account = self.env['account.account'].browse(
                    vals.get(field_name, run[field_name].id))
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if company.id not in self._eh_account_company_ids(account):
                    raise UserError(_(
                        "ECL posting accounts must belong to the run company."
                    ))
                if (
                    field_name == 'loss_allowance_account_id'
                    and account.account_type == 'asset_receivable'
                    and run.state not in ('posted', 'reversed')
                ):
                    raise UserError(_(
                        "The ECL loss allowance must use a dedicated "
                        "non-receivable contra-asset account. A receivable "
                        "account would be swept back into the exposure base."
                    ))
            foreign_segments = run.bucket_ids.mapped('segment_id').filtered(
                lambda segment: segment.company_id != company)
            if foreign_segments:
                raise UserError(_(
                    "Every ECL bucket segment must belong to the run company."
                ))
        return self

    def _eh_lock_for_transition(self, extra_company_ids=(),
                                lock_company=True):
        company_ids = tuple(sorted(
            set(self.mapped('company_id').ids) |
            {int(company_id) for company_id in extra_company_ids if company_id}
        ))
        if lock_company and company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (company_ids,),
            )
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_ecl_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return self

    def _eh_prepare_transition(self, engine=False, source_access=False,
                               include_providers=False, lock_company=True):
        caller = self
        caller._eh_validate_references()
        if source_access:
            caller._eh_validate_source_access(
                include_providers=include_providers)
        caller._eh_lock_for_transition(lock_company=lock_company)
        caller._eh_validate_references()
        if source_access:
            caller._eh_validate_source_access(
                include_providers=include_providers)
        records = caller._eh_workflow_action()
        if engine:
            records = records.with_context(**{ECL_RUN_ENGINE_CTX: True})
        return records

    def write(self, vals):
        engine = self.env.su and self.env.context.get(ECL_RUN_ENGINE_CTX)
        protected = set(self._FROZEN_AFTER_COMPUTE) | set(
            self._eh_guarded_fields)
        if set(vals) & protected and not engine:
            self._eh_validate_references(vals)
            self._eh_lock_for_transition(
                extra_company_ids=(vals.get('company_id'),))
            self._eh_validate_references(vals)
        elif set(vals) & {
            'company_id', 'journal_id', 'impairment_expense_account_id',
            'loss_allowance_account_id',
        }:
            self._eh_validate_references(vals)
        frozen = [f for f in self._FROZEN_AFTER_COMPUTE if f in vals]
        posted = self.filtered(
            lambda r: r.state in ('computed', 'posted', 'reversed'))
        # A pure state-transition write (action_compute / action_post /
        # action_reverse / action_cancel write only state + audit stamps +
        # move links) carries no frozen field and passes. A write touching a
        # measurement / input field while any record is posted or reversed is
        # always blocked, so a raw ORM write cannot drift a posted run's
        # figures from the recognised ledger movement.
        if frozen and posted and not engine:
            raise UserError(_(
                "Inputs on a posted ECL run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it "
                "(IFRS 9.5.5.8).",
                fields=', '.join(frozen)))
        # State itself is guarded by eh.workflow.guard: a non-superuser write
        # to state is refused, so it can only move through this model's own
        # actions (which run as su). No separate manager-gate on the raw state
        # write is needed - the guard blocks every direct write.
        return super().write(vals)

    def unlink(self):
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        posted = self.filtered(
            lambda r: r.state in ('computed', 'posted', 'reversed')
            or r.move_id or r.reversal_move_id)
        if posted:
            raise UserError(_(
                "A posted ECL run cannot be deleted; reverse it first "
                "(EH Accounting Manager only)."))
        return super().unlink()

    # ---- transitions ----

    def eh_deferred_tax_temp_diffs(self, reporting_date):
        """IAS 12 producer hook for the eh_account_deferred_tax seam.

        The IFRS 9 loss allowance is a DEDUCTIBLE temporary difference: the
        receivable's accounting carrying amount is below its tax base by the
        allowance (tax typically relieves the loss only on write-off).
        Modelled as an asset with carrying 0 and tax base = the closing
        allowance, so the run books a DTA of allowance x rate. One difference
        per company: the carried allowance of the latest active posted run at
        or before the reporting date, net of posted write-offs effective by
        that date (summing every historical run would double-count).
        """
        report_date = fields.Date.to_date(
            reporting_date or fields.Date.context_today(self),
        )
        latest = {}
        for run in self.filtered(
                lambda r: r.state == 'posted'
                and r.reporting_date
                and r.reporting_date <= report_date):
            cur = latest.get(run.company_id.id)
            if cur is None or run.reporting_date > cur.reporting_date:
                latest[run.company_id.id] = run
        out = []
        for run in latest.values():
            run._eh_assert_posted_provenance()
            allowance = (
                (run.closing_allowance or 0.0)
                - run._writeoff_posted_total(on_or_before=report_date)
            )
            currency = run.currency_id or run.company_id.currency_id
            if currency.is_zero(allowance):
                continue
            out.append({
                'name': _("ECL loss allowance: %s", run.display_name),
                'source_key': 'company:%s:ecl_loss_allowance' % (
                    run.company_id.id,
                ),
                'category': 'receivable_ecl',
                'nature': 'asset',
                'carrying_amount': 0.0,
                'tax_base': allowance,
                'through_oci': False,
            })
        return out

    def action_populate_from_receivables(self):
        """Age open receivables at the reporting date into the buckets.

        Fills the gross carrying amount per ageing band (and the EAD under
        the general approach, where the open residual is the exposure at
        default of a drawn receivable). When portfolio segments exist for
        the company, the segment-less manual bands act as the template and
        one populated band per (segment, template band) is created, so
        each segment carries its own loss-rate profile (IFRS 9.B5.5.35).

        Idempotent: repopulating deletes and recreates only the buckets it
        created itself (origin 'auto'); hand-defined buckets are never
        restructured, only their measured amounts refreshed.
        """
        self, receivable_lines, provider_exposures = (
            self._eh_prepare_population(include_providers=False)
        )
        return self._eh_populate_exposures(
            include_providers=False,
            receivable_lines=receivable_lines,
            provider_exposures=provider_exposures,
        )

    def action_populate_from_exposures(self):
        """Populate from open receivables AND external exposure providers.

        Same ageing sweep as populate-from-receivables, then also ingests
        the exposures reported by every installed loss-allowance provider
        (see _eh_ecl_exposure_providers) and ages them into the same matrix
        bands. Its reason for being: some credit exposures leave the open
        receivables population yet remain at risk, so an aged-receivables
        sweep alone understates the loss allowance. The archetype is a
        presented incoming post-dated cheque (eh_account_pdc): its invoice
        receivable is reconciled at deposit, so the exposure sits on the
        bank suspense account, invisible to the receivables sweep, until
        the cheque clears or bounces (IFRS 9.5.5.1 measures ECL on all
        exposures in scope, not only those still booked as receivables).

        Idempotent for the same reason the receivables sweep is: every
        gross carrying amount is reset to zero and re-totalled from source,
        so re-running never double-counts and never clobbers a manual band.
        """
        self, receivable_lines, provider_exposures = (
            self._eh_prepare_population(include_providers=True)
        )
        return self._eh_populate_exposures(
            include_providers=True,
            receivable_lines=receivable_lines,
            provider_exposures=provider_exposures,
        )

    def _eh_receivable_domain(self):
        """Point-in-time receivable candidates for this legal entity."""
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('date', '<=', self.reporting_date),
        ]
        # Defence in depth for legacy runs whose allowance was configured on
        # a receivable account before that configuration became invalid.
        if self.loss_allowance_account_id:
            domain.append((
                'account_id', '!=', self.loss_allowance_account_id.id,
            ))
        return domain

    def _eh_assert_population_model_access(self, include_providers=False):
        """Check model ACLs before taking the run-specific population lock."""
        if self.env.su:
            return self
        self.env['account.move.line']._eh_check_access('read')
        self.env['account.partial.reconcile']._eh_check_access('read')
        self.env['res.partner']._eh_check_access('read')
        if include_providers:
            for model_name in self._eh_ecl_exposure_providers():
                model = self.env.get(model_name)
                if model is not None:
                    model._eh_check_access('read')
        return self

    def _eh_authorized_receivable_lines(self):
        """Return one complete, actor-authorized receivable basis snapshot."""
        self.ensure_one()
        Line = self.env['account.move.line']
        domain = self._eh_receivable_domain()
        privileged = Line.sudo().search(domain)
        if not self.env.su:
            visible = Line.search(domain)
            if set(visible.ids) != set(privileged.ids):
                raise AccessError(_(
                    "The ECL receivables basis contains rows the caller is "
                    "not authorised to read."
                ))
        return privileged

    def _eh_prepare_population(self, include_providers=False):
        """Lock only this run, then materialise each source basis once."""
        self.ensure_one()
        caller = self
        caller._eh_validate_references()
        caller._eh_assert_population_model_access(include_providers)
        # A company-row lock would block unrelated journal postings until the
        # outer transaction commits. Population needs only per-run exclusion.
        caller._eh_lock_for_transition(lock_company=False)
        caller._eh_validate_references()
        receivable_lines = caller._eh_authorized_receivable_lines()
        provider_exposures = (
            caller._gather_provider_exposures()
            if include_providers else []
        )
        records = caller._eh_workflow_action().with_context(**{
            ECL_RUN_ENGINE_CTX: True,
            'eh_ecl_population_prelocked': True,
        })
        return records, receivable_lines, provider_exposures

    def _eh_populate_exposures(self, include_providers=False,
                               receivable_lines=None,
                               provider_exposures=None):
        """Populate ECL buckets without shadowing BaseModel._populate()."""
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Populate is only available in draft or computed state."))
        self.bucket_ids.filtered(lambda b: b.origin == 'auto').unlink()
        if not self.bucket_ids:
            raise UserError(_(
                "Define the provision-matrix buckets before populating."))
        self._eh_validate_bucket_bands()
        segments = self.env['eh.ecl.segment'].search([
            ('company_id', '=', self.company_id.id)])
        templates = self.bucket_ids.filtered(lambda b: not b.segment_id)
        covered = {(b.segment_id.id, b.days_from, b.days_to)
                   for b in self.bucket_ids if b.segment_id}
        auto_vals = []
        for segment in segments:
            for template in templates:
                if (segment.id, template.days_from,
                        template.days_to) in covered:
                    continue
                auto_vals.append({
                    'run_id': self.id,
                    'name': '%s / %s' % (template.name, segment.name),
                    'segment_id': segment.id,
                    'origin': 'auto',
                    'days_from': template.days_from,
                    'days_to': template.days_to,
                    'loss_rate': template.loss_rate,
                    'discount_rate': template.discount_rate,
                    'periods_to_recovery': template.periods_to_recovery,
                    'maturity_date': template.maturity_date,
                    'stage': template.stage,
                    'pd_12m': template.pd_12m,
                    'pd_lifetime': template.pd_lifetime,
                    'lgd': template.lgd,
                })
        if auto_vals:
            self.env['eh.ecl.bucket'].create(auto_vals)
        self._eh_validate_bucket_bands()
        general = self.measurement_approach == 'general'
        totals = {b.id: 0.0 for b in self.bucket_ids}
        # Sweep every receivable dated on or before the reporting date,
        # reconciled or not. A receivable open at period end that settles
        # AFTER the reporting date was still an exposure to measure at the
        # reporting date: the live ``reconciled`` flag would drop it and the
        # live ``amount_residual`` would shrink a partially-settled one, both
        # understating the loss allowance for a run computed after close
        # (IFRS 9.5.5.17). Measure each line at its point-in-time residual as
        # at the reporting date instead, reversing only reconciliations
        # completed on or before it (mirrors the sibling FX module's
        # eh.fx.revaluation.run._residual_at_date).
        lines = receivable_lines or self.env['account.move.line']
        residuals = self._residuals_at_date(lines, self.reporting_date)
        for line in lines:
            residual = residuals.get(line.id, line.balance)
            if self.currency_id.is_zero(residual) or residual <= 0:
                continue
            due = line.date_maturity or line.date
            days_overdue = (self.reporting_date - due).days
            if days_overdue < 0:
                days_overdue = 0
            bucket = self._bucket_for_exposure(
                days_overdue, line.partner_id.commercial_partner_id, segments)
            totals[bucket.id] += residual
        if include_providers:
            for exposure in provider_exposures or []:
                residual = exposure.get('amount_residual') or 0.0
                if self.currency_id.is_zero(residual) or residual <= 0:
                    continue
                days_overdue = max(exposure.get('days_outstanding') or 0, 0)
                partner = self.env['res.partner'].browse(
                    exposure.get('partner_id')) \
                    if exposure.get('partner_id') else self.env['res.partner']
                partner = partner.commercial_partner_id
                bucket = self._bucket_for_exposure(
                    days_overdue, partner, segments)
                totals[bucket.id] += residual
        self._eh_write_population_totals(totals, general=general)
        return True

    def _eh_validate_bucket_bands(self):
        """Require one contiguous, non-overlapping 0-to-infinity matrix."""
        self.ensure_one()
        groups = {}
        for bucket in self.bucket_ids:
            groups.setdefault(bucket.segment_id.id or False, []).append(bucket)
        for segment_id, buckets in groups.items():
            ordered = sorted(buckets, key=lambda b: (b.days_from, b.id))
            expected_from = 0
            for index, bucket in enumerate(ordered):
                label = bucket.segment_id.display_name or _("Unsegmented")
                if bucket.days_to and bucket.days_to < bucket.days_from:
                    raise UserError(_(
                        "ECL matrix band %(band)s in %(segment)s ends before "
                        "it starts.",
                        band=bucket.display_name,
                        segment=label,
                    ))
                if bucket.days_from != expected_from:
                    relation = (
                        _("overlaps a prior band")
                        if bucket.days_from < expected_from
                        else _("leaves an uncovered gap")
                    )
                    raise UserError(_(
                        "ECL matrix band %(band)s in %(segment)s %(relation)s "
                        "at %(day)d days past due. Bands must be contiguous "
                        "from day 0.",
                        band=bucket.display_name,
                        segment=label,
                        relation=relation,
                        day=expected_from,
                    ))
                is_last = index == len(ordered) - 1
                if not bucket.days_to:
                    if not is_last:
                        raise UserError(_(
                            "Open-ended ECL matrix band %(band)s in "
                            "%(segment)s must be the final band.",
                            band=bucket.display_name,
                            segment=label,
                        ))
                    expected_from = False
                else:
                    expected_from = bucket.days_to + 1
            if expected_from is not False:
                segment = self.env['eh.ecl.segment'].browse(segment_id)
                raise UserError(_(
                    "ECL matrix %(segment)s has no open-ended final band; "
                    "exposures older than day %(day)d would be omitted.",
                    segment=segment.display_name or _("Unsegmented"),
                    day=expected_from - 1,
                ))
        return True

    def _residuals_at_date(self, lines, reporting_date):
        """Compute every point-in-time residual with two grouped queries."""
        ids = tuple(lines.ids)
        residuals = {line.id: line.balance for line in lines}
        if not ids:
            return residuals
        self.env.cr.execute(
            "SELECT debit_move_id, COALESCE(SUM(amount), 0) "
            "FROM account_partial_reconcile "
            "WHERE debit_move_id IN %s AND max_date <= %s "
            "GROUP BY debit_move_id",
            (ids, reporting_date),
        )
        for line_id, settled in self.env.cr.fetchall():
            residuals[line_id] -= settled
        self.env.cr.execute(
            "SELECT credit_move_id, COALESCE(SUM(amount), 0) "
            "FROM account_partial_reconcile "
            "WHERE credit_move_id IN %s AND max_date <= %s "
            "GROUP BY credit_move_id",
            (ids, reporting_date),
        )
        for line_id, settled in self.env.cr.fetchall():
            residuals[line_id] += settled
        return residuals

    def _eh_write_population_totals(self, totals, general=False):
        """Write all population results in one SQL statement and recompute."""
        self.ensure_one()
        buckets = self.bucket_ids
        field_names = ['gross_carrying']
        rows = [(bucket.id, totals[bucket.id]) for bucket in buckets]
        if general:
            field_names.append('exposure_at_default')
            rows = [(bucket_id, amount, amount) for bucket_id, amount in rows]
        buckets.flush_recordset(field_names)
        if general:
            execute_values(
                self.env.cr,
                "UPDATE eh_ecl_bucket AS bucket "
                "SET gross_carrying = source.gross, "
                "    exposure_at_default = source.ead "
                "FROM (VALUES %s) AS source(id, gross, ead) "
                "WHERE bucket.id = source.id",
                rows,
            )
        else:
            execute_values(
                self.env.cr,
                "UPDATE eh_ecl_bucket AS bucket "
                "SET gross_carrying = source.gross "
                "FROM (VALUES %s) AS source(id, gross) "
                "WHERE bucket.id = source.id",
                rows,
            )
        buckets.invalidate_recordset(field_names, flush=False)
        buckets.modified(field_names)
        return True

    def _bucket_for_exposure(self, days_overdue, partner, segments):
        """The matrix bucket an aged exposure lands in, or an empty record.

        Placement rule shared by the receivables sweep and the provider
        sweep: the exposure's partner picks the portfolio segment (first
        matching segment wins), then the aged band within that segment. If
        no segment band covers the age, fall back to the unsegmented band so
        a segmented exposure is never dropped (IFRS 9.B5.5.35).
        """
        self.ensure_one()
        matching_segments = segments.filtered(
            lambda candidate: candidate._matches_partner(partner)
        )
        if len(matching_segments) > 1:
            raise UserError(_(
                "Exposure partner %(partner)s matches multiple ECL "
                "segments (%(segments)s). Make the segment rules mutually "
                "exclusive before populating.",
                partner=partner.display_name or _("Unknown"),
                segments=', '.join(matching_segments.mapped('display_name')),
            ))
        segment = matching_segments[:1]
        matches = self.bucket_ids.filtered(
            lambda bucket: bucket.segment_id == segment
            and bucket._matches(days_overdue)
        )
        if not matches and segment:
            matches = self.bucket_ids.filtered(
                lambda bucket: not bucket.segment_id
                and bucket._matches(days_overdue)
            )
        if len(matches) != 1:
            raise UserError(_(
                "Exposure for %(partner)s at %(days)d days past due matches "
                "%(count)d ECL matrix bands. Exactly one band is required.",
                partner=partner.display_name or _("Unknown"),
                days=days_overdue,
                count=len(matches),
            ))
        return matches

    def _residual_at_date(self, line, reporting_date):
        """Company-currency residual of ``line`` as it stood on
        ``reporting_date``.

        Starts from the signed company-currency balance and reverses only the
        partial reconciliations whose settlement completed on or before the
        reporting date, so a receivable open at period end is always measured
        at its period-end carrying amount regardless of later cash. A
        settlement dated after the reporting date is ignored, so it can never
        retroactively remove or shrink an exposure that was outstanding at the
        reporting date (mirrors eh.fx.revaluation.run._residual_at_date).
        """
        residual = line.balance
        # line is the debit move of these partials (a credit was matched
        # against it): a pre-date settlement reduces the open debit residual.
        for partial in line.matched_credit_ids:
            if partial.max_date and partial.max_date <= reporting_date:
                residual -= partial.amount
        # line is the credit move of these partials.
        for partial in line.matched_debit_ids:
            if partial.max_date and partial.max_date <= reporting_date:
                residual += partial.amount
        return residual

    @api.model
    def _eh_ecl_exposure_providers(self):
        """Model names to poll for extra credit exposures, discovered soft.

        A loss-allowance provider is any installed model that implements the
        documented eh_ecl_exposure_lines(reporting_date) hook, returning open
        credit exposures that have left the standard receivables population.
        Discovery is a registry scan (no hard dependency in either
        direction, so eh_account_ecl and a provider such as eh_account_pdc
        stay independent SKUs): a model qualifies when it defines the hook as
        its own attribute, and account.move.line is excluded because its
        receivables are already swept directly.

        Override or extend this method to register a provider explicitly
        instead of by scan; return a list of model names.
        """
        providers = []
        for name, model in self.env.registry.models.items():
            if name == 'account.move.line':
                continue
            if hasattr(model, 'eh_ecl_exposure_lines'):
                providers.append(name)
        return providers

    def _eh_validate_source_access(self, include_providers=False):
        """Require full actor visibility before the population engine sudoes."""
        if self.env.su:
            return self
        Line = self.env['account.move.line']
        Line._eh_check_access('read')
        self.env['account.partial.reconcile']._eh_check_access('read')
        self.env['res.partner']._eh_check_access('read')
        for run in self:
            domain = [
                ('company_id', '=', run.company_id.id),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('date', '<=', run.reporting_date),
            ]
            visible = Line.search(domain)
            privileged = Line.sudo().search(domain)
            if set(visible.ids) != set(privileged.ids):
                raise AccessError(_(
                    "The ECL receivables basis contains rows the caller is "
                    "not authorised to read."
                ))
            if not include_providers:
                continue
            for model_name in run._eh_ecl_exposure_providers():
                model = self.env.get(model_name)
                if model is None:
                    continue
                model._eh_check_access('read')
                provider_domain = (
                    [('company_id', '=', run.company_id.id)]
                    if 'company_id' in model._fields else [])
                provider_visible = model.search(provider_domain)
                provider_all = model.sudo().search(provider_domain)
                if set(provider_visible.ids) != set(provider_all.ids):
                    raise AccessError(_(
                        "An ECL exposure provider contains rows the caller is "
                        "not authorised to read."
                    ))
        return self

    def _gather_provider_exposures(self):
        """Poll every provider for this company's open exposures.

        Returns a flat list of exposure dicts (see eh_ecl_exposure_lines):
        each carries at least amount_residual (company currency),
        days_outstanding and partner_id, which is all the ageing placement
        needs. Provider visibility is compared with the privileged source set
        before the hook runs. Any provider error fails the population closed:
        omitting an in-scope exposure would understate the allowance.
        """
        self.ensure_one()
        exposures = []
        for model_name in self._eh_ecl_exposure_providers():
            model = self.env.get(model_name)
            if model is None:
                continue
            domain = []
            if 'company_id' in model._fields:
                domain.append(('company_id', '=', self.company_id.id))
            try:
                records = model.sudo().search(domain)
                if not self.env.su:
                    visible = model.search(domain)
                    if set(visible.ids) != set(records.ids):
                        raise AccessError(_(
                            "ECL provider %(provider)s contains rows the "
                            "caller is not authorised to read.",
                            provider=model_name,
                        ))
                if not records:
                    continue
                lines = records.eh_ecl_exposure_lines(
                    reporting_date=self.reporting_date)
            except AccessError:
                raise
            except Exception as provider_error:
                _logger.exception(
                    "ECL exposure provider %s failed on run %s",
                    model_name, self.display_name,
                )
                raise UserError(_(
                    "ECL exposure provider %(provider)s failed. Population "
                    "was rolled back so the allowance cannot omit unknown "
                    "credit exposures.",
                    provider=model_name,
                )) from provider_error
            for line in lines or []:
                exposures.append(line)
        return exposures

    def action_stage_from_ageing(self):
        """Derive each bucket's IFRS 9 stage from its ageing band.

        The run's configured Stage 2 and Stage 3 DPD backstops drive the
        classification. This is disclosure/classification only; it does not
        change simplified-approach ECL math (all buckets use lifetime ECL).
        """
        self = self._eh_prepare_transition(engine=True)
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Staging is only available in draft or computed state."))
        for bucket in self.bucket_ids:
            if bucket.days_from <= self.backstop_dpd_stage2:
                bucket.stage = '1'
            elif bucket.days_from <= self.backstop_dpd_stage3:
                bucket.stage = '2'
            else:
                bucket.stage = '3'
        return True

    def action_stage_from_risk(self):
        """Set each bucket's stage from its general-model risk flags.

        Stage 3 when credit-impaired, Stage 2 on a significant increase in
        credit risk, otherwise Stage 1 (IFRS 9.5.5.3-5.5.5). Used by the
        general 3-stage approach; the simplified approach classifies stage
        from ageing instead.
        """
        self = self._eh_prepare_transition(engine=True)
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Staging is only available in draft or computed state."))
        for bucket in self.bucket_ids:
            bucket.stage = bucket.stage_auto
        return True

    def action_apply_stage_engine(self):
        """Re-stage every bucket from the prior run's stages and the run's
        risk signals, logging each movement to the transfer audit trail."""
        self = self._eh_prepare_transition(engine=True)
        for run in self:
            if run.measurement_approach != 'general':
                raise UserError(_(
                    "The stage engine applies to the general 3-stage "
                    "approach; the simplified approach always measures "
                    "lifetime ECL."))
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Staging is only available in draft or computed state."))
            run._apply_stage_engine()
        return True

    def _apply_stage_engine(self):
        """One deterministic staging pass (IFRS 9.5.5.3-5.5.11).

        For each non-POCI bucket the candidate stage comes from the DPD
        backstops and qualitative flags. An upgrade (higher stage) applies
        immediately; a downgrade must survive the cure probation: the
        exposure only reverts after probation_runs consecutive runs below
        its staged level, counted through cure_streak carried from the
        prior run's bucket. POCI buckets never re-stage (lifetime ECL from
        initial recognition, IFRS 9.5.5.13) and are logged as pinned. The
        pass is idempotent: it rebuilds this run's transfer log from the
        prior run's snapshot, so recomputing a draft run never duplicates
        or loses trail entries.
        """
        self.ensure_one()
        engine_ctx = {'eh_ecl_stage_engine': True}
        self.transfer_ids.sudo().with_context(**engine_ctx).unlink()
        prior_run = self._prior_run()
        logs = []
        for bucket in self.bucket_ids:
            prior_bucket = bucket._match_prior_bucket(prior_run)
            if prior_bucket:
                prior_stage = prior_bucket.stage or '1'
                prior_streak = prior_bucket.cure_streak
                prior_amount = prior_bucket.ecl_effective
            else:
                # New exposure (or first ever run): its opening stage is
                # the stamped snapshot if this run was already staged once,
                # else the stage it was keyed with.
                prior_stage = bucket.stage_prior or bucket.stage or '1'
                prior_streak = 0
                prior_amount = 0.0
            base_log = {
                'run_id': self.id,
                'bucket_id': bucket.id,
                'bucket_name': bucket.name,
                'segment_id': bucket.segment_id.id,
                'amount': prior_amount,
            }
            if bucket.poci:
                pinned = bucket.stage or '1'
                bucket.write({'stage_prior': prior_stage, 'cure_streak': 0})
                logs.append(dict(
                    base_log, from_stage=pinned,
                    to_stage=pinned, reason='poci', amount=0.0))
                continue
            candidate, reason = bucket._candidate_stage()
            new_stage = prior_stage
            streak = 0
            if int(candidate) > int(prior_stage):
                new_stage = candidate
            elif int(candidate) < int(prior_stage):
                streak = prior_streak + 1
                if streak >= self.probation_runs:
                    new_stage = candidate
                    streak = 0
                    reason = 'cure'
            bucket.write({
                'stage': new_stage,
                'stage_prior': prior_stage,
                'cure_streak': streak,
            })
            if new_stage != prior_stage:
                logs.append(dict(
                    base_log, from_stage=prior_stage, to_stage=new_stage,
                    reason=reason))
        if logs:
            self.env['eh.ecl.transfer'].sudo().with_context(
                **engine_ctx).create(logs)
        return True

    def _validate_scenarios(self):
        self.ensure_one()
        if not self.scenario_ids:
            return
        total = sum(self.scenario_ids.mapped('weight'))
        if abs(total - 1.0) > 0.0001:
            raise UserError(_(
                "The scenario weights on run %(run)s sum to %(total)s; "
                "they must sum to 1 (IFRS 9.5.5.17(a)).",
                run=self.display_name, total=round(total, 4)))

    def action_compute(self):
        self = self._eh_prepare_transition(engine=True)
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state."))
            if not run.bucket_ids:
                raise UserError(_(
                    "Define at least one provision-matrix bucket."))
            if run.measurement_approach == 'general':
                run._validate_scenarios()
                run._apply_stage_engine()
            run.sudo().write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
        return True

    def action_post(self):
        self = self._eh_prepare_transition()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can post an ECL run."))
            # Serialise concurrent posts (a double click or a retried RPC on a
            # multi-worker deployment) BEFORE reading state, so two transactions
            # cannot both observe 'computed', both build+post an impairment move
            # and both stamp 'posted' - which would double the recognised loss
            # allowance (IFRS 9.5.5.8) and orphan the first move. The loser
            # blocks on the row lock, re-reads the committed 'posted'/'reversed'
            # state and stops at the guard below.
            if run.state != 'computed':
                raise UserError(_("Run must be computed before posting."))
            run._eh_assert_no_active_posted_successor(
                _("post a backdated snapshot"),
            )
            run._validate_accounts()
            move = run._build_move()
            run.sudo().write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id or False,
                'zero_movement_posted': not bool(move),
            })
            run._eh_assert_posted_provenance()
            run._rebuild_recon()
        return True

    def action_reverse(self):
        self = self._eh_prepare_transition()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse an ECL run."))
            # Same double-submit guard as action_post: lock and re-read before
            # checking state / reversal so two concurrent reversals cannot both
            # build a reversal move for the one posted run.
            if run.state != 'posted':
                raise UserError(_("Only a posted run can reverse."))
            if not run.reversal_date:
                raise UserError(_(
                    "Enter an explicit reversal accounting date first."
                ))
            original_date = run.move_id.date or run.reporting_date
            if run.reversal_date < original_date:
                raise UserError(_(
                    "The reversal date cannot precede the original ECL "
                    "movement date."
                ))
            run._eh_assert_posting_date_open(run.reversal_date)
            run._eh_assert_posted_provenance()
            run._eh_assert_no_active_posted_successor(_("be reversed"))
            posted_writeoffs = run.writeoff_ids.filtered(
                lambda writeoff: writeoff.state == 'posted'
            )
            if posted_writeoffs:
                raise UserError(_(
                    "Run %(run)s cannot be reversed while %(count)d posted "
                    "write-off(s) consume its allowance. Reverse/correct the "
                    "dependent write-offs through an auditable correction "
                    "workflow first.",
                    run=run.display_name,
                    count=len(posted_writeoffs),
                ))
            if run.zero_movement_posted:
                run.sudo().write({'state': 'reversed'})
                continue
            if (
                run.move_id.company_id != run.company_id
                or run.move_id.state != 'posted'
                or run.move_id.move_type != 'entry'
                or not run.move_id.eh_sealed
            ):
                raise UserError(_(
                    "The linked ECL move failed its sealed provenance check "
                    "and will not be reversed."
                ))
            reversal = run.move_id._eh_reverse_with_verified_capability([{
                'date': run.reversal_date,
                'journal_id': run.journal_id.id,
                'ref': _("ECL reversal %s", run.name),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            reversal.invalidate_recordset(['state', 'date', 'eh_sealed'])
            if (
                reversal.state != 'posted'
                or reversal.date != run.reversal_date
                or not reversal.eh_sealed
            ):
                raise UserError(_(
                    "The ECL reversal did not post on the exact requested "
                    "date; the correction was rolled back."
                ))
            run._eh_seal_reversal(reversal)
            run.sudo().write({
                'state': 'reversed',
                'reversal_move_id': reversal.id,
            })
        return True

    def action_cancel(self):
        self = self._eh_prepare_transition()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can cancel an ECL run."))
            if run.state in ('posted', 'reversed'):
                raise UserError(_("Cannot cancel a posted or reversed run."))
            run.sudo().write({'state': 'cancelled'})

    def action_set_to_draft(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_("Only cancelled runs can return to draft."))
            run.sudo().write({'state': 'draft'})

    def action_revise(self):
        """Return a computed run to draft for an explicit input revision."""
        self = self._eh_prepare_transition()
        for run in self:
            if run.state != 'computed':
                raise UserError(_(
                    "Only a computed ECL run can be reopened for revision."
                ))
            run.sudo().write({
                'state': 'draft',
                'computed_at': False,
                'computed_by_id': False,
            })
        return True

    def action_roll_forward_opening(self):
        """Reset the opening allowance to the roll-forward source.

        Prefers the prior posted run's closing allowance; falls back to the
        loss-allowance ledger balance carried into the period when there is
        no prior run.
        """
        self = self._eh_prepare_transition(engine=True)
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Roll-forward is only available in draft or computed "
                    "state."))
            rolled = run._prior_closing_allowance()
            if not rolled:
                rolled = run._ledger_allowance_balance()
            run.opening_allowance = rolled
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No movement entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_open_write_off(self):
        """Open a new allowance write-off pre-linked to this run."""
        self.ensure_one()
        if self.state != 'posted':
            raise UserError(_(
                "Write-offs consume a recognised allowance; post the run "
                "first."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.ecl.writeoff',
            'view_mode': 'form', 'views': [(False, 'form')],
            'context': {'default_run_id': self.id},
        }

    # ---- reconciliation (IFRS 7.35H) ----

    _RECON_STAGES = ('1', '2', '3', 'poci')

    def _rebuild_recon(self):
        """Rebuild the per-stage opening-to-closing allowance roll.

        Identity per stage: closing = opening + transfers in - transfers
        out + remeasurement - write-offs. Opening is the prior posted
        run's closing for the stage; the transfer columns come from the
        stage-engine log at prior-run allowance amounts; write-offs are
        this run's posted allowance write-offs; remeasurement is the
        residual measurement change, so the identity ties by construction
        and the closing column equals the measured allowance less what
        write-offs consumed.
        """
        for run in self:
            if run.state not in ('posted', 'reversed'):
                continue
            rebuild_ctx = {'eh_ecl_recon_rebuild': True}
            run.recon_ids.sudo().with_context(**rebuild_ctx).unlink()
            stages = run._RECON_STAGES
            opening = dict.fromkeys(stages, 0.0)
            prior = run._prior_run()
            if prior:
                # Derive the predecessor snapshot from its immutable buckets,
                # net only of write-offs effective by this reporting date.
                # A reconciliation rebuilt after a future-dated write-off may
                # contain later activity and is therefore not an as-of source.
                for bucket in prior.bucket_ids:
                    opening[bucket._recon_stage()] += bucket.ecl_effective
                for writeoff in prior.writeoff_ids:
                    if (
                        writeoff.state == 'posted'
                        and writeoff.date <= run.reporting_date
                    ):
                        opening[writeoff.stage] -= writeoff.amount
            measured = dict.fromkeys(stages, 0.0)
            for bucket in run.bucket_ids:
                measured[bucket._recon_stage()] += bucket.ecl_effective
            transfers_in = dict.fromkeys(stages, 0.0)
            transfers_out = dict.fromkeys(stages, 0.0)
            for transfer in run.transfer_ids:
                if transfer.from_stage == transfer.to_stage:
                    continue
                transfers_out[transfer.from_stage] += transfer.amount
                transfers_in[transfer.to_stage] += transfer.amount
            writeoffs = dict.fromkeys(stages, 0.0)
            for writeoff in run.writeoff_ids:
                if writeoff.state == 'posted':
                    writeoffs[writeoff.stage] += writeoff.amount
            vals = []
            for stage in stages:
                remeasurement = (measured[stage] - opening[stage]
                                 - transfers_in[stage] + transfers_out[stage])
                vals.append({
                    'run_id': run.id,
                    'stage': stage,
                    'opening': opening[stage],
                    'transfers_in': transfers_in[stage],
                    'transfers_out': transfers_out[stage],
                    'remeasurement': remeasurement,
                    'writeoffs': writeoffs[stage],
                    'closing': measured[stage] - writeoffs[stage],
                })
            self.env['eh.ecl.recon'].sudo().with_context(
                **rebuild_ctx).create(vals)
        return True

    # ---- helpers ----

    def _eh_assert_posting_date_open(self, posting_date):
        """Fail before creating an ECL move in a locked accounting period."""
        self.ensure_one()
        company = self.company_id
        lock_fields = [
            name for name in (
                'fiscalyear_lock_date', 'hard_lock_date',
                'user_fiscalyear_lock_date', 'user_hard_lock_date',
            ) if name in company._fields
        ]
        lock_companies = (
            company.parent_ids
            if 'parent_ids' in company._fields
            else company
        )
        lock_companies.invalidate_recordset(lock_fields)
        company.invalidate_recordset(lock_fields)
        resolver = getattr(company, '_get_user_fiscal_lock_date', None)
        lock_date = False
        if resolver:
            try:
                lock_date = resolver(self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    lock_date = resolver()
                except TypeError:
                    raise journal_signature_error
        else:
            configured = [
                company[field_name]
                for field_name in ('fiscalyear_lock_date', 'hard_lock_date')
                if field_name in company._fields and company[field_name]
            ]
            lock_date = max(configured) if configured else False
        if lock_date and posting_date <= lock_date:
            raise UserError(_(
                "ECL entry date %(date)s is on or before accounting lock "
                "date %(lock)s for %(company)s. Choose an open posting or "
                "reversal date.",
                date=posting_date,
                lock=lock_date,
                company=company.display_name,
            ))
        return True

    def _eh_lock_for_post(self):
        """Take a row lock on this run and drop cached state so a serialised
        concurrent post/reverse re-reads the committed state rather than a
        stale pre-transition snapshot.

        Closes the double-submit race in which two transactions both read
        state=='computed', both build+post an impairment move and both stamp
        'posted' - producing two posted entries for one run (doubling the
        IFRS 9.5.5.8 loss allowance) and orphaning the first move. Mirrors
        eh_account_fx_revaluation's eh.fx.revaluation.run._eh_lock_for_post.
        """
        return self._eh_lock_for_transition()

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.impairment_expense_account_id:
            missing.append(_("impairment loss account"))
        if not self.loss_allowance_account_id:
            missing.append(_("loss allowance account"))
        if missing:
            raise UserError(_(
                "Configure the %s on run %s before posting.",
                ', '.join(missing), self.display_name))

    def _build_move(self):
        self.ensure_one()
        currency = self.currency_id
        movement = currency.round(self.movement)
        if currency.is_zero(movement):
            return self.env['account.move']
        if movement > 0:
            # Increase the allowance: expense up, contra-asset up (credit).
            lines = [
                (0, 0, {
                    'name': _("Impairment loss %s", self.name),
                    'account_id': self.impairment_expense_account_id.id,
                    'debit': movement, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Loss allowance %s", self.name),
                    'account_id': self.loss_allowance_account_id.id,
                    'debit': 0.0, 'credit': movement}),
            ]
        else:
            amount = -movement
            lines = [
                (0, 0, {
                    'name': _("Loss allowance release %s", self.name),
                    'account_id': self.loss_allowance_account_id.id,
                    'debit': amount, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Impairment reversal %s", self.name),
                    'account_id': self.impairment_expense_account_id.id,
                    'debit': 0.0, 'credit': amount}),
            ]
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.reporting_date,
            'journal_id': self.journal_id.id,
            'ref': _("ECL %s", self.name),
            'line_ids': lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move
