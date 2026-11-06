# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.deferred.tax.run: a period-end deferred tax computation for one company.

Compute: resolve each line's rate (manual override, then the jurisdiction
enacted-rate table at the reporting date, then the run's statutory rate as
the fallback), roll opening balances and opening rates forward from the
prior posted run, then total the closing deferred tax asset and liability,
the movement from the opening position, and its split into rate-change
remeasurement (IAS 12.60(b)) versus origination. Compute also rebuilds the
auto rows of the IAS 12.81(c) effective-tax-rate reconciliation, keeping
manual rows.

Post: recognise only the period movement in one balanced journal. Movements
route to profit or loss, except lines flagged as OCI-related, whose movement
routes to the OCI reserve (IAS 12.61A). Under the gross offsetting policy
(the default and the historical behaviour) one DTA leg and one DTL leg are
posted; under the net-by-jurisdiction policy (IAS 12.74) the run posts one
net leg per jurisdiction per side while the lines keep the gross detail for
disclosure. The entry balances by construction: the deferred tax expense /
income leg is the balancing plug.

Reverse: post the symmetric inverse dated the day after the period end and
flip the run to Reversed, preserving both the entry and its reversal.
"""

import hashlib
import json
import logging
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Input / measurement fields on the run that become the basis of a posted
# movement. Once the run is posted or reversed they are frozen at the ORM
# write layer. 'state' is deliberately NOT listed: the action methods write
# only state + the audit stamps (posted_at/posted_by_id, ...), so a pure
# state-transition write carries no frozen field and always passes.
_FROZEN_AFTER_CONFIRM = (
    'line_ids', 'period_end', 'statutory_rate', 'company_id',
    'dta_account_id', 'dtl_account_id', 'deferred_tax_expense_account_id',
    'oci_account_id', 'journal_id',
    'accounting_profit', 'permanent_diff_tax', 'current_tax_expense',
    'offsetting_policy', 'recon_line_ids',
    'offsetting_policy_change_reason', 'reconciliation_materiality',
    'projected_taxable_profit', 'opening_unrecognised_dta',
    'recoverability_memo',
)

DEFERRED_TAX_ENGINE_CTX = 'eh_deferred_tax_engine'

_ENGINE_OWNED_FIELDS = (
    'move_id', 'reversal_move_id',
    'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
    'reversed_at', 'reversed_by_id',
    'computation_fingerprint',
)

_LIVE_PERIOD_STATES = ('draft', 'computed', 'posted')


class EhDeferredTaxRun(models.Model):
    _name = 'eh.deferred.tax.run'
    _description = "Deferred tax run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'

    # State is a posting state machine: it may only change through this
    # model's own action_* methods (which build/reverse the GL movement and
    # run the manager checks), never a direct RPC/ORM write. eh.workflow.guard
    # blocks any unflagged write to these fields for a non-superuser.
    _eh_guarded_fields = ('state',) + _ENGINE_OWNED_FIELDS

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
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )
    period_end = fields.Date(
        required=True, tracking=True,
        default=fields.Date.context_today,
        help="Reporting date at which the deferred tax position is measured. "
             "The posting date of the movement entry.",
    )
    statutory_rate = fields.Float(
        digits=(6, 3), required=True, default=25.0, tracking=True,
        help="Enacted / substantively enacted tax rate for the period, as a "
             "percentage. Fallback: a line resolves its rate from its "
             "manual override, then its jurisdiction's enacted-rate table "
             "at the reporting date, and only then from this rate. Also "
             "drives the expected tax in the reconciliation and the "
             "recoverability ceiling.",
    )
    offsetting_policy = fields.Selection(
        [
            ('gross', "Gross (no offsetting)"),
            ('net_by_jurisdiction', "Net by jurisdiction (IAS 12.74)"),
        ],
        default='gross', required=True, tracking=True,
        help="IAS 12.74 permits offsetting a deferred tax asset against a "
             "deferred tax liability only when the entity has a legally "
             "enforceable right to set off current tax assets against "
             "current tax liabilities AND both relate to income taxes "
             "levied by the same taxation authority on the same taxable "
             "entity (or on entities intending to settle net). Choosing "
             "the net policy asserts those conditions hold within each "
             "jurisdiction of this company: posting then books one net leg "
             "per jurisdiction per side, while the lines keep the gross "
             "detail for disclosure. Gross is the default and the "
             "historical behaviour. Keep the policy consistent across "
             "periods: the net movement is based on the line openings, so "
             "switching policy mid-stream requires a reclassification of "
             "the prior gross balances.",
    )
    prior_offsetting_policy = fields.Selection(
        selection=lambda self: self._fields['offsetting_policy'].selection,
        compute='_compute_prior_offsetting_policy',
        string="Prior Posted Policy",
        help="Offsetting policy used by the immediately preceding posted "
             "run for this company.",
    )
    offsetting_policy_changed = fields.Boolean(
        compute='_compute_prior_offsetting_policy',
        help="The current policy differs from the immediately preceding "
             "posted run, so posting includes a DTA/DTL reclassification.",
    )
    offsetting_policy_change_reason = fields.Char(
        tracking=True,
        help="Required when the offsetting policy changes between posted "
             "periods. Explain the change in IAS 12.74 facts or policy; the "
             "movement entry will reclassify the opening DTA/DTL balances "
             "before landing on the current presentation.",
    )

    line_ids = fields.One2many(
        'eh.deferred.tax.line', 'run_id', copy=True,
    )
    recon_line_ids = fields.One2many(
        'eh.deferred.tax.recon.line', 'run_id', copy=False,
        string="Reconciliation Rows",
    )

    # ---- accounts ----
    dta_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Asset Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['asset_non_current', 'asset_current'])]",
    )
    dtl_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Liability Account",
        tracking=True,
        domain="[('account_type', 'in', "
               "['liability_non_current', 'liability_current'])]",
    )
    deferred_tax_expense_account_id = fields.Many2one(
        'account.account', string="Deferred Tax Expense Account",
        tracking=True,
        domain="[('account_type', 'in', ['expense', 'income_other'])]",
    )
    oci_account_id = fields.Many2one(
        'account.account', string="OCI / Equity Account", tracking=True,
        domain="[('account_type', '=', 'equity')]",
        help="Reserve that carries the deferred tax on OCI-related items. "
             "Required only when a line is flagged as recognised in OCI.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
    )

    # ---- computed totals ----
    closing_dta = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    unrecognised_dta = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Total deferred tax asset not recognised because deductible "
             "differences exceed projected recoverable profit, or lines are "
             "flagged not recoverable (IAS 12.81(e) disclosure).")
    closing_dtl = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    net_deferred_tax = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Closing deferred tax liability less asset; positive = net "
             "liability.")
    opening_tie_out = fields.Boolean(
        compute='_compute_opening_tie_out',
        help="Set when at least one line's opening does not tie to the prior "
             "posted run's closing position. A keying error in the opening "
             "silently mis-states the period movement; investigate before "
             "posting.")
    pl_movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Movement recognised in profit or loss; positive = expense.")
    oci_movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Movement recognised in OCI; positive = charge to OCI.")
    rate_change_pl = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Rate-change remeasurement of opening balances routed to "
             "profit or loss (IAS 12.60(b)): sum of the rate-change effect "
             "of the non-OCI lines, positive = charge. Feeds the "
             "reconciliation's rate-change row.")
    rate_change_oci = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Rate-change remeasurement of opening balances routed to OCI "
             "(IAS 12.61A/63): sum of the rate-change effect of the "
             "OCI-flagged lines, positive = charge to OCI.")

    # ---- IAS 12.74 offsetting presentation ----
    net_dta_presented = fields.Monetary(
        compute='_compute_net_presentation', store=True,
        currency_field='currency_id',
        help="Deferred tax asset presented on the balance sheet under the "
             "run's offsetting policy: the gross closing DTA under the "
             "gross policy, or the sum of the per-jurisdiction net asset "
             "positions under the net policy (IAS 12.74).")
    net_dtl_presented = fields.Monetary(
        compute='_compute_net_presentation', store=True,
        currency_field='currency_id',
        help="Deferred tax liability presented on the balance sheet under "
             "the run's offsetting policy.")
    offsetting_note = fields.Text(
        compute='_compute_offsetting_note',
        help="Per-jurisdiction gross and presented positions for the "
             "balance sheet note.")
    disclosure_note_html = fields.Html(
        compute='_compute_disclosure_note_html', sanitize=True,
        help="Structured IAS 12.81(e)/(g) disclosure by temporary-difference "
             "type and expiry band, including gross and tax-effected values.",
    )

    # ---- IAS 12.24/34 run-level recoverability memo ----
    projected_taxable_profit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Projected future taxable profit supporting recovery of the "
             "recognised deferred tax assets (IAS 12.24/34). At the "
             "statutory rate it gives the recognition ceiling; any closing "
             "DTA above the ceiling is disclosed as unrecognised and feeds "
             "the reconciliation's unrecognised row. Zero means no "
             "run-level constraint (line-level caps still apply).")
    dta_ceiling = fields.Monetary(
        compute='_compute_dta_ceiling', store=True,
        currency_field='currency_id',
        help="Projected taxable profit at the statutory rate: the run-"
             "level ceiling on the recognised deferred tax asset.")
    run_level_unrecognised_dta = fields.Monetary(
        compute='_compute_run_level_unrecognised_dta', store=True,
        currency_field='currency_id',
        help="Line-level eligible DTA not recognised after allocating the "
             "run-level ceiling, disclosed per IAS 12.81(e) on top of the "
             "individual-line unrecognised amounts.")
    opening_unrecognised_dta = fields.Monetary(
        currency_field='currency_id',
        help="Unrecognised deferred tax asset disclosed at the prior "
             "reporting date (line-level plus run-level). Rolled forward "
             "from the prior posted run when left at zero. The "
             "reconciliation's unrecognised row shows the period movement "
             "against this figure.")
    recoverability_memo = fields.Text(
        help="Evidence for the projected taxable profit: forecasts, "
             "reversal of existing taxable differences, tax-planning "
             "opportunities (IAS 12.28-31).")

    # ---- IAS 12.81(c) effective-tax-rate reconciliation ----
    accounting_profit = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Accounting profit before tax for the period. Drives the "
             "expected tax at the statutory rate in the reconciliation.")
    permanent_diff_tax = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Tax effect of permanent differences (non-deductible expenses, "
             "exempt income), entered as a signed amount added to expected "
             "tax in the reconciliation.")
    current_tax_expense = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Current tax expense for the period (from the tax return / BAS), "
             "entered so the reconciliation ties to the total tax charge.")
    expected_tax = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Accounting profit at the statutory rate.")
    total_tax_expense = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Current tax expense plus the deferred movement to profit or "
             "loss.")
    effective_rate = fields.Float(
        compute='_compute_reconciliation', store=True, digits=(6, 3),
        help="Total tax expense over accounting profit, as a percentage.")
    reconciliation_residual = fields.Monetary(
        compute='_compute_reconciliation', store=True,
        currency_field='currency_id',
        help="Total tax expense less (expected tax plus permanent-difference "
             "tax). A non-zero residual flags an unexplained reconciling "
             "item to investigate.")
    unexplained_reconciliation_residual = fields.Monetary(
        compute='_compute_unexplained_reconciliation', store=True,
        currency_field='currency_id',
        help="The residual row left after expected tax, automatic IAS 12 "
             "adjustments and user-entered reconciling items.",
    )
    reconciliation_materiality = fields.Monetary(
        currency_field='currency_id', default=0.0, tracking=True,
        help="Maximum unexplained reconciliation residual allowed at "
             "posting. Zero applies company-currency rounding only.",
    )
    reconciliation_residual_material = fields.Boolean(
        compute='_compute_unexplained_reconciliation', store=True,
        help="The unexplained residual exceeds the documented posting "
             "materiality and blocks posting.",
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_date = fields.Date(
        copy=False,
        help=(
            "Required accounting date for the controlled counter-entry. "
            "Posting fails closed if Odoo would shift this date because of "
            "a lock."
        ),
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    computation_fingerprint = fields.Char(
        readonly=True, copy=False,
        help="Digest of the reviewed run inputs, temporary differences, "
             "manual reconciliation rows and applicable enacted rates at "
             "the last Compute. Posting fails closed if it changed.",
    )
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    _check_rate = models.Constraint(
        'CHECK (statutory_rate >= 0 AND statutory_rate <= 100)',  # noqa: gcclog-hardcode statutory percentage bounds
        'Statutory rate must be between 0 and 100.',
    )

    def init(self):
        """Keep immutable reversed history while allowing one live correction."""
        self.env.cr.execute("""
            DO $$
            DECLARE constraint_name text;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                      FROM pg_constraint
                     WHERE conrelid = 'eh_deferred_tax_run'::regclass
                       AND contype = 'u'
                       AND pg_get_constraintdef(oid)
                           LIKE 'UNIQUE (company_id, period_end)%'
                LOOP
                    EXECUTE format(
                        'ALTER TABLE eh_deferred_tax_run '
                        'DROP CONSTRAINT %I', constraint_name
                    );
                END LOOP;
            END $$
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                eh_deferred_tax_run_live_period_uniq
            ON eh_deferred_tax_run (company_id, period_end)
            WHERE state IN ('draft', 'computed', 'posted')
        """)

    # ---- compute ----

    @api.depends(
        'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.movement_dta', 'line_ids.movement_dtl',
        'line_ids.through_oci', 'line_ids.unrecognised_dta',
        'line_ids.rate_change_effect',
    )
    def _compute_totals(self):
        for run in self:
            run.closing_dta = sum(run.line_ids.mapped('closing_dta'))
            run.unrecognised_dta = sum(
                run.line_ids.mapped('unrecognised_dta'))
            run.closing_dtl = sum(run.line_ids.mapped('closing_dtl'))
            run.net_deferred_tax = run.closing_dtl - run.closing_dta
            pl = run.line_ids.filtered(lambda l: not l.through_oci)
            oci = run.line_ids.filtered(lambda l: l.through_oci)
            run.pl_movement = (
                sum(pl.mapped('movement_dtl')) - sum(pl.mapped('movement_dta')))
            run.oci_movement = (
                sum(oci.mapped('movement_dtl'))
                - sum(oci.mapped('movement_dta')))
            run.rate_change_pl = sum(pl.mapped('rate_change_effect'))
            run.rate_change_oci = sum(oci.mapped('rate_change_effect'))

    def _jurisdiction_groups(self):
        """Lines grouped by jurisdiction, deterministic order.

        Returns a list of (jurisdiction, lines) with the empty jurisdiction
        (legacy rows never recomputed) collected under a False key, ordered
        by jurisdiction name for stable posting and disclosure output.
        """
        self.ensure_one()
        groups = {}
        for line in self.line_ids:
            groups.setdefault(line.jurisdiction_id, []).append(line)
        ordered = sorted(
            groups.items(),
            key=lambda item: (item[0].name or '', item[0].id or 0))
        Line = self.env['eh.deferred.tax.line']
        return [
            (jur, Line.browse([l.id for l in lines]))
            for jur, lines in ordered
        ]

    @api.depends(
        'offsetting_policy', 'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.jurisdiction_id',
    )
    def _compute_net_presentation(self):
        """Balance sheet presentation under the offsetting policy.

        Gross: the presented figures are the gross closing totals. Net by
        jurisdiction (IAS 12.74): within each jurisdiction DTA and DTL are
        offset and only the net asset or net liability is presented; the
        presented totals are the sums of those per-jurisdiction nets. The
        net position (DTL less DTA) is identical under both policies.
        """
        for run in self:
            if run.offsetting_policy != 'net_by_jurisdiction':
                run.net_dta_presented = sum(
                    run.line_ids.mapped('closing_dta'))
                run.net_dtl_presented = sum(
                    run.line_ids.mapped('closing_dtl'))
                continue
            net_dta = net_dtl = 0.0
            for _jur, lines in run._jurisdiction_groups():
                net = (sum(lines.mapped('closing_dtl'))
                       - sum(lines.mapped('closing_dta')))
                if net >= 0.0:
                    net_dtl += net
                else:
                    net_dta -= net
            run.net_dta_presented = net_dta
            run.net_dtl_presented = net_dtl

    @api.depends(
        'offsetting_policy', 'net_dta_presented', 'net_dtl_presented',
        'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.jurisdiction_id',
    )
    def _compute_offsetting_note(self):
        for run in self:
            rows = []
            for jur, lines in run._jurisdiction_groups():
                gross_dta = sum(lines.mapped('closing_dta'))
                gross_dtl = sum(lines.mapped('closing_dtl'))
                if run.offsetting_policy == 'net_by_jurisdiction':
                    net = gross_dtl - gross_dta
                    shown_dta = -net if net < 0.0 else 0.0
                    shown_dtl = net if net >= 0.0 else 0.0
                else:
                    shown_dta, shown_dtl = gross_dta, gross_dtl
                rows.append(
                    "%s: gross DTA %.2f, gross DTL %.2f, "
                    "presented DTA %.2f, presented DTL %.2f" % (
                        jur.name if jur else _("(no jurisdiction)"),
                        gross_dta, gross_dtl, shown_dta, shown_dtl))
            if run.offsetting_policy == 'net_by_jurisdiction':
                rows.append(_(
                    "Offset per IAS 12.74: legally enforceable right of "
                    "set-off and same taxation authority assumed within "
                    "each jurisdiction."))
            run.offsetting_note = "\n".join(rows)

    @api.depends('projected_taxable_profit', 'statutory_rate')
    def _compute_dta_ceiling(self):
        for run in self:
            projected = run.projected_taxable_profit
            if projected > 0.0:
                raw = projected * run.statutory_rate / 100.0
                run.dta_ceiling = run.currency_id.round(raw)
            else:
                run.dta_ceiling = 0.0

    @api.depends('line_ids.run_level_unrecognised_dta')
    def _compute_run_level_unrecognised_dta(self):
        for run in self:
            run.run_level_unrecognised_dta = sum(
                run.line_ids.mapped('run_level_unrecognised_dta'))

    @api.depends('line_ids.opening_tie_out')
    def _compute_opening_tie_out(self):
        for run in self:
            run.opening_tie_out = any(run.line_ids.mapped('opening_tie_out'))

    @api.depends(
        'accounting_profit', 'statutory_rate', 'permanent_diff_tax',
        'current_tax_expense', 'pl_movement',
    )
    def _compute_reconciliation(self):
        for run in self:
            run.expected_tax = run.accounting_profit * run.statutory_rate / 100.0
            run.total_tax_expense = run.current_tax_expense + run.pl_movement
            run.effective_rate = (
                run.total_tax_expense / run.accounting_profit * 100.0
                if run.accounting_profit else 0.0)
            run.reconciliation_residual = (
                run.total_tax_expense
                - (run.expected_tax + run.permanent_diff_tax))

    def _prior_posted_run(self):
        """Return the immediately preceding posted run for this company."""
        self.ensure_one()
        if not self.company_id or not self.period_end:
            return self.env['eh.deferred.tax.run']
        return self.env['eh.deferred.tax.run'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('period_end', '<', self.period_end),
        ], order='period_end desc, id desc', limit=1)

    @api.depends('company_id', 'period_end', 'offsetting_policy')
    def _compute_prior_offsetting_policy(self):
        for run in self:
            prior = run._prior_posted_run()
            run.prior_offsetting_policy = prior.offsetting_policy or False
            run.offsetting_policy_changed = bool(
                prior and prior.offsetting_policy != run.offsetting_policy
            )

    @api.depends(
        'recon_line_ids.amount', 'recon_line_ids.is_auto',
        'recon_line_ids.is_residual', 'reconciliation_materiality',
        'currency_id', 'accounting_profit', 'current_tax_expense',
        'permanent_diff_tax',
    )
    def _compute_unexplained_reconciliation(self):
        for run in self:
            residual = sum(run.recon_line_ids.filtered(
                'is_residual').mapped('amount'))
            run.unexplained_reconciliation_residual = residual
            rounding = run.currency_id.rounding if run.currency_id else 0.01
            threshold = max(run.reconciliation_materiality or 0.0, rounding)
            reconciliation_in_scope = bool(
                run.accounting_profit
                or run.current_tax_expense
                or run.permanent_diff_tax
                or run.recon_line_ids.filtered(lambda row: not row.is_auto)
            )
            run.reconciliation_residual_material = (
                reconciliation_in_scope and abs(residual) > threshold
            )

    def _ias12_disclosure_rows(self):
        """Structured IAS 12.81(e)/(g) disclosure for this run.

        Rows are grouped by temporary-difference type and carryforward expiry
        band.  Both gross deductible/taxable amounts and their tax effects are
        retained so the note is not limited to a tax-effected memo.
        """
        self.ensure_one()
        Line = self.env['eh.deferred.tax.line']
        categories = dict(
            Line._fields['category']._description_selection(self.env)
        )
        band_labels = {
            'expired': _("Expired at reporting date"),
            'within_1y': _("Expires within 1 year"),
            'one_to_five': _("Expires in 1-5 years"),
            'after_five': _("Expires after 5 years"),
            'no_expiry': _("No expiry / not applicable"),
        }
        band_order = {
            key: index for index, key in enumerate(
                ('expired', 'within_1y', 'one_to_five',
                 'after_five', 'no_expiry')
            )
        }
        category_order = {
            key: index for index, (key, _label) in enumerate(
                Line._fields['category']._description_selection(self.env)
            )
        }
        grouped = defaultdict(lambda: {
            'line_count': 0,
            'gross_deductible': 0.0,
            'gross_taxable': 0.0,
            'recognised_dta': 0.0,
            'recognised_dtl': 0.0,
            'unrecognised_dta': 0.0,
            'unrecognised_gross': 0.0,
            'pl_movement': 0.0,
            'oci_movement': 0.0,
        })
        for line in self.line_ids:
            if not line.expiry_date:
                band = 'no_expiry'
            elif line.expiry_date <= self.period_end:
                band = 'expired'
            else:
                days = (line.expiry_date - self.period_end).days
                if days <= 366:  # noqa: gcclog-hardcode one-year disclosure band
                    band = 'within_1y'
                elif days <= 366 * 5:  # noqa: gcclog-hardcode five-year disclosure band
                    band = 'one_to_five'
                else:
                    band = 'after_five'
            values = grouped[(line.category, band)]
            values['line_count'] += 1
            values['gross_deductible'] += line.deductible_diff
            values['gross_taxable'] += line.taxable_diff
            values['recognised_dta'] += line.closing_dta
            values['recognised_dtl'] += line.closing_dtl
            unrecognised = (
                line.unrecognised_dta + line.run_level_unrecognised_dta
            )
            values['unrecognised_dta'] += unrecognised
            if line.tax_rate:
                values['unrecognised_gross'] += min(
                    line.deductible_diff,
                    unrecognised * 100.0 / line.tax_rate,
                )
            movement = line.movement_dtl - line.movement_dta
            route = 'oci_movement' if line.through_oci else 'pl_movement'
            values[route] += movement
        rows = []
        for (category, band), values in sorted(
            grouped.items(),
            key=lambda item: (
                category_order.get(item[0][0], 999),
                band_order[item[0][1]],
            ),
        ):
            rows.append({
                'category': category,
                'category_label': categories.get(category, category),
                'expiry_band': band,
                'expiry_label': band_labels[band],
                **values,
            })
        return rows

    @api.depends(
        'period_end', 'currency_id', 'line_ids.category',
        'line_ids.expiry_date', 'line_ids.deductible_diff',
        'line_ids.taxable_diff', 'line_ids.tax_rate',
        'line_ids.closing_dta', 'line_ids.closing_dtl',
        'line_ids.unrecognised_dta',
        'line_ids.run_level_unrecognised_dta',
        'line_ids.movement_dta', 'line_ids.movement_dtl',
        'line_ids.through_oci',
    )
    def _compute_disclosure_note_html(self):
        headers = [
            _("Type"), _("Expiry band"), _("Gross deductible"),
            _("Gross taxable"), _("Recognised DTA"),
            _("Recognised DTL"), _("Unrecognised gross"),
            _("Unrecognised tax effect"), _("P&L movement"),
            _("OCI movement"),
        ]
        for run in self:
            head = Markup('').join(
                Markup('<th>{}</th>').format(escape(label))
                for label in headers
            )
            body_rows = []
            for row in run._ias12_disclosure_rows():
                cells = [row['category_label'], row['expiry_label']] + [
                    "{:,.2f}".format(row[key]) for key in (
                        'gross_deductible', 'gross_taxable',
                        'recognised_dta', 'recognised_dtl',
                        'unrecognised_gross', 'unrecognised_dta',
                        'pl_movement', 'oci_movement',
                    )
                ]
                body_rows.append(Markup('<tr>{}</tr>').format(
                    Markup('').join(
                        Markup('<td>{}</td>').format(escape(cell))
                        for cell in cells
                    )
                ))
            if not body_rows:
                body_rows.append(Markup(
                    '<tr><td colspan="10">{}</td></tr>'
                ).format(escape(_("No temporary differences."))))
            run.disclosure_note_html = Markup(
                '<table class="table table-sm table-striped">'
                '<thead><tr>{}</tr></thead><tbody>{}</tbody></table>'
            ).format(head, Markup('').join(body_rows))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        live_periods = []
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            period_end = fields.Date.to_date(
                vals.get('period_end') or fields.Date.context_today(self)
            )
            self._eh_assert_company_allowed(company_id)
            live_periods.append((company_id, period_end))
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(company_id)
                vals['name'] = (
                    self.env['ir.sequence'].with_company(company).with_context(
                        ir_sequence_date=period_end,
                    ).next_by_code('eh.deferred.tax.run') or '/'
                )
        self._eh_lock_live_periods(live_periods)
        for vals in vals_list:
            self._eh_assert_live_period_available(
                vals.get('company_id') or self.env.company.id,
                vals.get('period_end') or fields.Date.context_today(self),
                vals.get('state', 'draft'),
            )
        runs = super().create(vals_list)
        runs._eh_validate_references()
        return runs

    @api.model
    def _eh_assert_live_period_available(
        self, company_id, period_end, state, exclude_id=False,
    ):
        if state not in _LIVE_PERIOD_STATES:
            return True
        domain = [
            ('company_id', '=', int(company_id)),
            ('period_end', '=', fields.Date.to_date(period_end)),
            ('state', 'in', _LIVE_PERIOD_STATES),
        ]
        if exclude_id:
            domain.append(('id', '!=', int(exclude_id)))
        if self.sudo().search(domain, limit=1):
            raise ValidationError(_(
                "Only one live deferred tax run may exist per company and "
                "reporting date. Reverse or cancel it before creating its "
                "correction."
            ))
        return True

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A deferred-tax run may only use a company enabled in the "
                "caller's current company context."
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
                    raise UserError(_(
                        "The deferred-tax journal must belong to the run company."
                    ))
            for field_name in (
                'dta_account_id', 'dtl_account_id',
                'deferred_tax_expense_account_id', 'oci_account_id',
            ):
                account = self.env['account.account'].browse(
                    vals.get(field_name, run[field_name].id))
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if company.id not in self._eh_account_company_ids(account):
                    raise UserError(_(
                        "Deferred-tax posting accounts must belong to the run "
                        "company."
                    ))
            for line in run.line_ids:
                if (
                    line.jurisdiction_id
                    and line.jurisdiction_id.company_id != company
                ):
                    raise UserError(_(
                        "Every deferred-tax jurisdiction must belong to the run "
                        "company."
                    ))
        return self

    @api.model
    def _eh_lock_live_periods(self, periods):
        """Serialize only Deferred Tax live-period keys.

        A transaction-scoped advisory key avoids the former res_company row
        lock, so editing a disclosure line cannot serialize with unrelated
        account.move posting for the same company.
        """
        keys = sorted({
            (int(company_id), fields.Date.to_date(period_end))
            for company_id, period_end in periods
            if company_id and period_end
        })
        for company_id, period_end in keys:
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("eh.deferred.tax.run:%s:%s" % (
                    company_id, fields.Date.to_string(period_end),
                ),),
            )

    def _eh_lock_for_transition(self):
        """Lock only the Deferred Tax run rows being mutated."""
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_deferred_tax_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        return self

    # ---- integrity: freeze input once the movement is posted ----

    def write(self, vals):
        """Freeze the measurement / input fields once the run is posted or
        reversed; they are the basis of a posted GL movement.

        A pure state-transition write (the action methods write only
        {'state': ...} plus the audit stamps) carries no frozen field and
        passes. A write touching a frozen figure while any record is posted or
        reversed is always blocked. 'state' is never frozen, so recompute in
        draft / computed and the legitimate transitions keep working.
        """
        if set(vals) & {
            'company_id', 'journal_id', 'dta_account_id', 'dtl_account_id',
            'deferred_tax_expense_account_id', 'oci_account_id',
        }:
            self._eh_validate_references(vals)
        frozen = [f for f in _FROZEN_AFTER_CONFIRM if f in vals]
        if frozen or 'state' in vals or 'reversal_date' in vals:
            # Input edits and workflow actions use the same company/run lock
            # order.  A writer that started while post was in flight waits,
            # invalidates, and then observes the committed posted state.
            self._eh_validate_references(vals)
            if set(vals) & {'company_id', 'period_end'}:
                periods = []
                for run in self:
                    periods.extend([
                        (run.company_id.id, run.period_end),
                        (
                            vals.get('company_id', run.company_id.id),
                            vals.get('period_end', run.period_end),
                        ),
                    ])
                self._eh_lock_live_periods(periods)
            self._eh_lock_for_transition()
            self._eh_validate_references(vals)
        if (
            'reversal_date' in vals
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager')
        ):
            raise UserError(_(
                "Only an EH Accounting Manager can set the reversal date."
            ))
        if set(vals) & {'company_id', 'period_end', 'state'}:
            for run in self:
                self._eh_assert_live_period_available(
                    vals.get('company_id', run.company_id.id),
                    vals.get('period_end', run.period_end),
                    vals.get('state', run.state),
                    exclude_id=run.id,
                )
        if 'reversal_date' in vals and self.filtered(
                lambda run: run.state == 'reversed'):
            raise UserError(_(
                "A posted reversal accounting date is immutable."
            ))
        confirmed = self.filtered(
            lambda r: r.state in ('posted', 'reversed'))
        engine = self.env.su and self.env.context.get(DEFERRED_TAX_ENGINE_CTX)
        if frozen and confirmed and not engine:
            raise UserError(_(
                "Figures on a posted deferred tax run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it.",
                fields=', '.join(frozen)))
        # The state of a posted / reversed run is itself a control point:
        # resetting it to draft would silently lift the figure freeze above.
        # A raw ORM state write on such a run without the sanctioned-transition
        # context flag must be manager-gated, so a plain user cannot un-freeze
        # a GL-backed run. The action methods set the flag after their own
        # manager check and move handling.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_run_state_change')):
            crossing = confirmed.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reversed deferred tax run."))

    def unlink(self):
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        posted = self.filtered(
            lambda r: r.state in ('posted', 'reversed')
            or r.move_id or r.reversal_move_id)
        if posted:
            raise UserError(_(
                "A posted or reversed deferred tax run cannot be deleted; it "
                "carries a posted GL movement. Reverse it first."))
        return super().unlink()

    @api.constrains('oci_account_id', 'line_ids', 'state')
    def _check_oci_account(self):
        for run in self:
            if run.state == 'draft':
                continue
            if run.line_ids.filtered(lambda l: l.through_oci) \
                    and not run.oci_account_id:
                raise ValidationError(_(
                    "Run %s has a line recognised in OCI but no OCI / equity "
                    "account is configured.", run.display_name))

    # ---- transitions ----

    def action_compute(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self = caller._eh_workflow_action().with_context(
            **{DEFERRED_TAX_ENGINE_CTX: True})
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state."))
            run._resolve_line_rates()
            run._roll_forward_opening()
            run._rebuild_reconciliation()
            fingerprint = run._input_fingerprint()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
                'computation_fingerprint': fingerprint,
            })
        return True

    def _eh_deferred_tax_providers(self):
        """Models exposing eh_deferred_tax_temp_diffs(reporting_date), found by
        registry scan so producer modules (ECL, provisions, ...) stay
        independent SKUs with no hard dependency in either direction. Override
        to register a producer explicitly instead of by scan."""
        return [
            name for name, model in self.env.registry.models.items()
            if hasattr(model, 'eh_deferred_tax_temp_diffs')
        ]

    def _eh_validate_provider_access(self):
        """Require actor visibility of every provider row before sudo gather."""
        if self.env.su:
            return self
        for run in self:
            for model_name in run._eh_deferred_tax_providers():
                model = self.env.get(model_name)
                if model is None:
                    continue
                model._eh_check_access('read')
                domain = ([('company_id', '=', run.company_id.id)]
                          if 'company_id' in model._fields else [])
                visible = model.search(domain)
                privileged = model.sudo().search(domain)
                if set(visible.ids) != set(privileged.ids):
                    raise AccessError(_(
                        "A deferred-tax provider contains rows the caller is "
                        "not authorised to read."
                    ))
        return self

    def action_gather_from_engines(self):
        """Pull temporary differences from the suite's IFRS engines (IFRS 9
        ECL loss allowance, IAS 37 provisions, and any module implementing the
        hook) into this run, so the category hooks are driven by a real
        producer rather than hand-keyed. Idempotent: previously auto-gathered
        lines are replaced; hand-keyed lines are left intact. Provider reads
        and line creation are fail-closed and atomic: one failure preserves
        the complete previous gathered snapshot."""
        caller = self
        caller._eh_validate_references()
        caller._eh_validate_provider_access()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        caller._eh_validate_provider_access()
        self = caller._eh_workflow_action().with_context(
            **{DEFERRED_TAX_ENGINE_CTX: True})
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Gather is only available in draft or computed state."))
        Line = self.env['eh.deferred.tax.line']
        seq = 1000
        staged = []
        seen_identities = set()
        categories = {
            key for key, _label in
            Line._fields['category']._description_selection(self.env)
        }
        natures = {
            key for key, _label in
            Line._fields['nature']._description_selection(self.env)
        }
        for model_name in self._eh_deferred_tax_providers():
            model = self.env.get(model_name)
            if model is None:
                continue
            domain = ([('company_id', '=', self.company_id.id)]
                      if 'company_id' in model._fields else [])
            try:
                # A provider may execute SQL before failing.  The savepoint
                # restores cursor usability so the caller gets one controlled
                # error instead of an aborted transaction.
                with self.env.cr.savepoint():
                    records = model.sudo().search(domain)
                    diffs = list(
                        records.eh_deferred_tax_temp_diffs(self.period_end)
                        if records else []
                    )
                    for row_number, diff in enumerate(diffs, start=1):
                        if not isinstance(diff, dict):
                            raise ValueError(
                                "row %s is not a mapping" % row_number
                            )
                        category = diff.get('category', 'other')
                        nature = diff.get('nature', 'asset')
                        if category not in categories or nature not in natures:
                            raise ValueError(
                                "row %s has an unsupported category/nature" %
                                row_number
                            )
                        display_name = diff.get('name') or model_name
                        source_key = (
                            diff.get('source_key')
                            or 'name:%s' % display_name
                        )
                        identity = (model_name, source_key)
                        if identity in seen_identities:
                            raise ValueError(
                                "duplicate source key %s" % source_key
                            )
                        seen_identities.add(identity)
                        seq += 1
                        staged.append({
                            'run_id': self.id,
                            'sequence': seq,
                            'eh_auto_gathered': True,
                            'name': display_name,
                            'source_model': model_name,
                            'source_key': source_key,
                            'category': category,
                            'nature': nature,
                            'carrying_amount': diff.get(
                                'carrying_amount', 0.0,
                            ),
                            'tax_base': diff.get('tax_base', 0.0),
                            'through_oci': diff.get('through_oci', False),
                        })
            except Exception as exc:  # noqa: BLE001 - controlled boundary
                _logger.exception(
                    "Deferred-tax provider %s failed on run %s.",
                    model_name, self.display_name,
                )
                raise UserError(_(
                    "Deferred-tax gather failed for provider %(provider)s. "
                    "No gathered lines were replaced; correct the provider "
                    "data and try again.",
                    provider=model_name,
                )) from exc
        try:
            # Replacement is atomic.  A constraint/database error on one
            # staged row restores the previous gathered evidence and leaves
            # the cursor usable for the controlled UserError below.
            with self.env.cr.savepoint():
                self.line_ids.filtered('eh_auto_gathered').sudo().unlink()
                if staged:
                    Line.create(staged)
        except Exception as exc:  # noqa: BLE001 - controlled ORM boundary
            _logger.exception(
                "Deferred-tax gathered-line creation failed on run %s.",
                self.display_name,
            )
            raise UserError(_(
                "A provider returned invalid Deferred Tax line data. No "
                "gathered lines were replaced; correct the provider output "
                "and try again."
            )) from exc
        self.message_post(body=_(
            "Gathered %d temporary difference(s) from the IFRS engines.",
            len(staged)))
        return True

    def action_post(self):
        with self.env.cr.savepoint():
            return self._action_post_locked()

    def _action_post_locked(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self = caller._eh_workflow_action().with_context(
            **{DEFERRED_TAX_ENGINE_CTX: True})
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can post a deferred tax "
                    "run."))
            # Serialise concurrent posts (a double click or a browser retry)
            # BEFORE reading state, so two transactions cannot both observe
            # 'computed', both build+post a move and both stamp 'posted' -
            # which would leave two posted deferred-tax entries and orphan the
            # first move. The loser re-reads the committed 'posted'/'reversed'
            # state and stops at the guard below.
            if run.state != 'computed':
                raise UserError(_("Run must be computed before posting."))
            run._validate_accounts()
            run._validate_offsetting_policy_transition()
            fingerprint = run._input_fingerprint()
            if (
                not run.computation_fingerprint
                or fingerprint != run.computation_fingerprint
            ):
                raise UserError(_(
                    "Deferred Tax inputs or enacted rates changed after "
                    "Compute. Compute %(run)s again before posting.",
                    run=run.display_name,
                ))
            # Inputs remain editable while a run is Computed. Rebuild the
            # disclosure rows under the same lock so a late recoverability-
            # ceiling edit cannot post the new DTA while retaining a stale
            # unrecognised-DTA reconciliation.
            run._resolve_line_rates()
            run._roll_forward_opening()
            run._rebuild_reconciliation()
            if run.reconciliation_residual_material:
                raise UserError(_(
                    "The unexplained tax-reconciliation residual on %(run)s "
                    "is %(residual)s, above the documented materiality of "
                    "%(materiality)s. Add explanatory manual rows or revise "
                    "and document the materiality, then Compute again.",
                    run=run.display_name,
                    residual=run.unexplained_reconciliation_residual,
                    materiality=run.reconciliation_materiality,
                ))
            run._eh_assert_posting_date_open(run.period_end)
            move = run._build_move()
            if not move:
                raise UserError(_(
                    "The deferred tax movement is nil; there is nothing to "
                    "post for %s.", run.display_name))
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
        return True

    def action_reverse(self):
        with self.env.cr.savepoint():
            return self._action_reverse_locked()

    def _action_reverse_locked(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self = caller._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can reverse a deferred tax "
                    "run."))
            # Same double-submit guard as action_post: lock and re-read before
            # checking state / move_id so two concurrent reversals cannot both
            # build a reversal move for the one posted run.
            if run.state != 'posted':
                raise UserError(_("Only posted runs can be reversed."))
            if not run.move_id:
                raise UserError(_("Run has no posted move to reverse."))
            if not run.reversal_date:
                raise UserError(_(
                    "Enter an explicit reversal accounting date first."
                ))
            if run.reversal_date > fields.Date.context_today(run):
                raise UserError(_(
                    "A deferred-tax reversal cannot use a future date."
                ))
            if run.reversal_date < run.move_id.date:
                raise UserError(_(
                    "A deferred-tax reversal cannot precede the original "
                    "posted movement date."
                ))
            run._eh_assert_posting_date_open(run.reversal_date)
            if (
                run.move_id.company_id != run.company_id
                or run.move_id.state != 'posted'
                or run.move_id.move_type != 'entry'
                or not run.move_id.eh_sealed
            ):
                raise UserError(_(
                    "The linked deferred-tax move failed its sealed provenance "
                    "check and will not be reversed."
                ))
            reversal = run.move_id._eh_reverse_with_verified_capability([{
                'date': run.reversal_date,
                'journal_id': run.journal_id.id,
                'ref': _("Deferred tax reversal %s", run.name),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            reversal.invalidate_recordset(['state', 'date', 'eh_sealed'])
            if (
                reversal.state != 'posted'
                or reversal.date != run.reversal_date
                or not reversal.eh_sealed
            ):
                raise UserError(_(
                    "The deferred-tax reversal did not post on the exact "
                    "requested date; the correction was rolled back."
                ))
            run._eh_seal_reversal(reversal)
            run.with_context(eh_run_state_change=True).write({
                'state': 'reversed',
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
                'reversal_move_id': reversal.id,
            })
        return True

    def action_cancel(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self = caller._eh_workflow_action()
        for run in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can cancel a deferred tax "
                    "run."))
            if run.state in ('posted', 'reversed'):
                raise UserError(_(
                    "Cannot cancel a posted or reversed run."))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        self = caller._eh_workflow_action()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_(
                    "Only cancelled runs can return to draft."))
            run.state = 'draft'

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No movement entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- helpers ----

    def _resolved_rate_values(self):
        """Return each line's authoritative reporting-date rate in one read."""
        self.ensure_one()
        jurisdictions = self.line_ids.mapped('jurisdiction_id')
        rates_by_jurisdiction = {}
        if jurisdictions:
            rates = self.env['eh.tax.rate'].search([
                ('jurisdiction_id', 'in', jurisdictions.ids),
                ('effective_from', '<=', self.period_end),
                '|', ('enacted_date', '=', False),
                ('enacted_date', '<=', self.period_end),
            ], order='jurisdiction_id, effective_from desc, id desc')
            for rate in rates:
                rates_by_jurisdiction.setdefault(
                    rate.jurisdiction_id.id, rate.rate,
                )
        resolved = {}
        for line in self.line_ids:
            table_rate = rates_by_jurisdiction.get(line.jurisdiction_id.id)
            if line.manual_rate:
                resolved[line.id] = line.manual_rate
            elif table_rate is not None:
                resolved[line.id] = table_rate
            elif line.tax_rate:
                resolved[line.id] = line.tax_rate
            else:
                resolved[line.id] = self.statutory_rate
        return resolved

    def _prior_closing_snapshot(self):
        """One prior-run read and indexed line lookup for all current lines."""
        self.ensure_one()
        prior = self._prior_posted_run()
        by_source = {}
        by_name = {}
        legacy_ecl = []
        for line in prior.line_ids.sorted(lambda row: (row.sequence, row.id)):
            if line.source_model and line.source_key:
                by_source.setdefault(
                    (line.source_model, line.source_key), line,
                )
            by_name.setdefault(line.name, line)
            if (
                line.eh_auto_gathered
                and not line.source_key
                and line.category == 'receivable_ecl'
            ):
                legacy_ecl.append(line)
        return {
            'run': prior,
            'by_source': by_source,
            'by_name': by_name,
            'legacy_ecl': legacy_ecl,
        }

    def _prior_closing_for_line(self, line, snapshot=None):
        self.ensure_one()
        snapshot = snapshot or self._prior_closing_snapshot()
        empty = {'found': False, 'dta': 0.0, 'dtl': 0.0, 'rate': 0.0}
        if not snapshot['run'] or not line.name:
            return empty
        prior_line = self.env['eh.deferred.tax.line']
        if line.source_model and line.source_key:
            prior_line = snapshot['by_source'].get(
                (line.source_model, line.source_key), prior_line,
            )
            if (
                not prior_line
                and line.category == 'receivable_ecl'
                and len(snapshot['legacy_ecl']) == 1
            ):
                prior_line = snapshot['legacy_ecl'][0]
        else:
            prior_line = snapshot['by_name'].get(line.name, prior_line)
        if not prior_line:
            return empty
        return {
            'found': True,
            'dta': prior_line.closing_dta,
            'dtl': prior_line.closing_dtl,
            'rate': prior_line.tax_rate,
        }

    def _input_fingerprint(self):
        """Digest all reviewed inputs plus the rates effective at period end."""
        self.ensure_one()
        run_fields = (
            'company_id', 'period_end', 'statutory_rate',
            'offsetting_policy', 'offsetting_policy_change_reason',
            'dta_account_id', 'dtl_account_id',
            'deferred_tax_expense_account_id', 'oci_account_id', 'journal_id',
            'accounting_profit', 'permanent_diff_tax', 'current_tax_expense',
            'projected_taxable_profit', 'opening_unrecognised_dta',
            'recoverability_memo', 'reconciliation_materiality',
        )
        line_fields = (
            'sequence', 'name', 'category', 'nature', 'carrying_amount',
            'tax_base', 'jurisdiction_id', 'tax_rate', 'manual_rate',
            'manual_rate_reason', 'recoverable', 'recoverable_amount',
            'expiry_date', 'through_oci', 'opening_dta', 'opening_dtl',
            'opening_rate', 'eh_auto_gathered', 'source_model', 'source_key',
        )

        def value(record, field_name):
            field = record._fields[field_name]
            current = record[field_name]
            if field.type == 'many2one':
                return current.id or False
            if field.type in ('date', 'datetime'):
                return str(current or '')
            return current

        rates = self._resolved_rate_values()
        payload = {
            'run': {name: value(self, name) for name in run_fields},
            'prior_policy': self._prior_posted_run().offsetting_policy or False,
            'lines': [
                {
                    **{name: value(line, name) for name in line_fields},
                    'resolved_rate': rates.get(line.id),
                }
                for line in self.line_ids.sorted(lambda row: row.id)
            ],
            'manual_reconciliation': [
                {
                    'sequence': line.sequence,
                    'kind': line.kind,
                    'name': line.name,
                    'amount': line.amount,
                }
                for line in self.recon_line_ids.filtered(
                    lambda row: not row.is_auto
                ).sorted(lambda row: row.id)
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(',', ':'), default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _validate_offsetting_policy_transition(self):
        """Require an audit reason before auto-reclassifying opening legs."""
        for run in self:
            prior = run._prior_posted_run()
            if (
                prior
                and prior.offsetting_policy != run.offsetting_policy
                and not (run.offsetting_policy_change_reason or '').strip()
            ):
                raise UserError(_(
                    "Offsetting policy changed from %(prior)s to %(current)s. "
                    "Document the IAS 12.74 policy-change reason, recompute, "
                    "and then post the DTA/DTL opening reclassification.",
                    prior=prior._fields['offsetting_policy']
                        .convert_to_export(prior.offsetting_policy, prior),
                    current=run._fields['offsetting_policy']
                        .convert_to_export(run.offsetting_policy, run),
                ))
        return self

    def _resolve_line_rates(self):
        """Apply each line's resolved rate for this reporting date.

        A manual override or a jurisdiction table rate is re-applied on
        every compute (the table is authoritative; the override carries its
        reason). The statutory fallback only seeds a line whose rate is
        still empty, preserving the historical behaviour of keeping a rate
        keyed directly on the line. Lines without a jurisdiction (created
        before the rate table existed) are attached to the company default
        jurisdiction first, so offsetting and disclosure group correctly.
        """
        self.ensure_one()
        orphans = self.line_ids.filtered(lambda l: not l.jurisdiction_id)
        if orphans:
            default = self.env['eh.tax.jurisdiction']._get_company_default(
                self.company_id)
            orphans.write({'jurisdiction_id': default.id})
        resolved = self._resolved_rate_values()
        Line = self.env['eh.deferred.tax.line']
        by_rate = defaultdict(lambda: Line)
        for line in self.line_ids:
            target = resolved[line.id]
            if line.tax_rate != target:
                by_rate[target] |= line
        for target, lines in by_rate.items():
            lines.write({'tax_rate': target})

    def _roll_forward_opening(self):
        """Default each line's opening from the prior posted run's closing.

        Only lines whose opening is still nil (never keyed) are filled, so a
        manually entered opening is always preserved. Lines that already carry
        an opening keep it, and the per-line tie-out flag independently reports
        any disagreement with the prior closing for review. The opening RATE
        rolls forward with the balances so the rate-change remeasurement
        (IAS 12.60(b)) discloses against the rate actually applied last
        period; a still-empty opening rate on a manually keyed opening stays
        empty (no rate-change component, the historical behaviour). The
        run-level opening unrecognised-DTA figure rolls forward the same way
        for the reconciliation's unrecognised row.
        """
        self.ensure_one()
        currency = self.currency_id
        snapshot = self._prior_closing_snapshot()
        updates = []
        for line in self.line_ids:
            if not currency.is_zero(line.opening_dta or 0.0) \
                    or not currency.is_zero(line.opening_dtl or 0.0):
                continue
            prior = self._prior_closing_for_line(line, snapshot=snapshot)
            if not prior['found']:
                continue
            opening_dta = (
                prior['dta'] if not currency.is_zero(prior['dta'])
                else line.opening_dta
            )
            opening_dtl = (
                prior['dtl'] if not currency.is_zero(prior['dtl'])
                else line.opening_dtl
            )
            opening_rate = line.opening_rate or prior['rate']
            if (
                opening_dta != line.opening_dta
                or opening_dtl != line.opening_dtl
                or opening_rate != line.opening_rate
            ):
                updates.append({
                    'id': line.id,
                    'opening_dta': opening_dta,
                    'opening_dtl': opening_dtl,
                    'opening_rate': opening_rate,
                })
        if updates:
            fields_to_update = [
                'opening_dta', 'opening_dtl', 'opening_rate',
            ]
            self.env['eh.deferred.tax.line'].flush_model(fields_to_update)
            self.env.cr.execute("""
                UPDATE eh_deferred_tax_line AS line
                   SET opening_dta = values.opening_dta,
                       opening_dtl = values.opening_dtl,
                       opening_rate = values.opening_rate
                  FROM jsonb_to_recordset(%s::jsonb) AS values(
                       id integer,
                       opening_dta numeric,
                       opening_dtl numeric,
                       opening_rate double precision
                  )
                 WHERE line.id = values.id
            """, (json.dumps(updates),))
            updated = self.env['eh.deferred.tax.line'].browse(
                [values['id'] for values in updates]
            )
            updated.invalidate_recordset(fields_to_update, flush=False)
            updated.modified(fields_to_update)
        if currency.is_zero(self.opening_unrecognised_dta or 0.0):
            prior_run = snapshot['run']
            if prior_run:
                carried = (prior_run.unrecognised_dta
                           + prior_run.run_level_unrecognised_dta)
                if not currency.is_zero(carried):
                    self.opening_unrecognised_dta = carried

    def _rebuild_reconciliation(self):
        """Rebuild the auto rows of the IAS 12.81(c) reconciliation.

        Auto rows: expected tax, the permanent-difference header input,
        the rate-change remeasurement in profit or loss, the movement in
        unrecognised DTA (line-level plus run-level, against the opening
        figure), and a residual that balances the rows to the total tax
        expense. Manual rows (prior-year, credits, extra items) are
        preserved and enter the residual computation.
        """
        self.ensure_one()
        currency = self.currency_id
        self.recon_line_ids.filtered('is_auto').unlink()
        rows = [{
            'kind': 'expected',
            'name': _("Expected tax at statutory rate (%(rate)s%%)",
                      rate=self.statutory_rate),
            'amount': self.expected_tax,
            'sequence': 10,
            'is_auto': True,
        }]
        if not currency.is_zero(self.permanent_diff_tax):
            rows.append({
                'kind': 'permanent',
                'name': _("Permanent differences"),
                'amount': self.permanent_diff_tax,
                'sequence': 20,
                'is_auto': True,
            })
        if not currency.is_zero(self.rate_change_pl):
            rows.append({
                'kind': 'rate_change',
                'name': _("Remeasurement of opening balances at changed "
                          "rates"),
                'amount': self.rate_change_pl,
                'sequence': 30,
                'is_auto': True,
            })
        unrecognised_movement = (
            self.unrecognised_dta + self.run_level_unrecognised_dta
            - self.opening_unrecognised_dta)
        if not currency.is_zero(unrecognised_movement):
            rows.append({
                'kind': 'unrecognised',
                'name': _("Movement in unrecognised deferred tax assets"),
                'amount': unrecognised_movement,
                'sequence': 40,
                'is_auto': True,
            })
        explained = sum(r['amount'] for r in rows) \
            + sum(self.recon_line_ids.mapped('amount'))
        residual = self.total_tax_expense - explained
        if not currency.is_zero(residual):
            rows.append({
                'kind': 'other',
                'name': _("Unexplained reconciling residual"),
                'amount': residual,
                'sequence': 90,
                'is_auto': True,
                'is_residual': True,
            })
        self.write({'recon_line_ids': [(0, 0, vals) for vals in rows]})

    def _eh_assert_posting_date_open(self, posting_date):
        """Fail before move creation when fiscal or hard locks cover a date."""
        self.ensure_one()
        company = self.company_id
        lock_fields = [
            name for name in (
                'fiscalyear_lock_date', 'hard_lock_date',
                'user_fiscalyear_lock_date', 'user_hard_lock_date',
            ) if name in company._fields
        ]
        lock_companies = (
            company.parent_ids if 'parent_ids' in company._fields else company
        )
        lock_companies.invalidate_recordset(lock_fields)
        company.invalidate_recordset(lock_fields)
        method = company._get_user_fiscal_lock_date
        try:
            lock_date = method(self.journal_id)
        except TypeError:  # Odoo 16/17: no journal parameter
            lock_date = method()
        if lock_date and posting_date <= lock_date:
            raise UserError(_(
                "Deferred Tax entry date %(date)s is on or before accounting "
                "lock date %(lock)s for %(company)s. Use an open reporting "
                "or reversal date.",
                date=posting_date,
                lock=lock_date,
                company=company.display_name,
            ))
        return True

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.dta_account_id:
            missing.append(_("deferred tax asset account"))
        if not self.dtl_account_id:
            missing.append(_("deferred tax liability account"))
        if not self.deferred_tax_expense_account_id:
            missing.append(_("deferred tax expense account"))
        if self.line_ids.filtered(lambda l: l.through_oci) \
                and not self.oci_account_id:
            missing.append(_("OCI / equity account"))
        if missing:
            raise UserError(_(
                "Configure the %s on run %s before posting.",
                ', '.join(missing), self.display_name))

    def _build_move(self):
        """Post the period movement as one balanced entry.

        Deferred tax assets are debit-natured and liabilities credit-natured,
        so an increase in a DTA is a debit and an increase in a DTL a credit.
        The profit-or-loss / OCI counterpart is the balancing plug, which
        keeps the entry balanced to the cent regardless of rounding.

        Gross policy (default, historical behaviour): one aggregate DTA leg
        and one aggregate DTL leg. Net-by-jurisdiction policy (IAS 12.74):
        within each jurisdiction the closing and opening positions are
        netted (DTL less DTA) and the movement is posted as one net leg per
        jurisdiction per side, moving the balance between the DTA and DTL
        accounts when the net position flips sign. The per-jurisdiction net
        legs sum to the same total as the gross legs, so the OCI leg and
        the expense plug are identical under both policies and the entry
        still balances by construction.
        """
        self.ensure_one()
        currency = self.currency_id
        oci = self.line_ids.filtered(lambda l: l.through_oci)

        def presentation(policy, dta_field, dtl_field):
            positions = {}
            labels = {}
            for jurisdiction, lines in self._jurisdiction_groups():
                key = jurisdiction.id or 0
                labels[key] = (
                    jurisdiction.name
                    if jurisdiction else _("(no jurisdiction)")
                )
                gross_dta = sum(lines.mapped(dta_field))
                gross_dtl = sum(lines.mapped(dtl_field))
                if policy == 'net_by_jurisdiction':
                    net = gross_dtl - gross_dta
                    positions[key] = (max(-net, 0.0), max(net, 0.0))
                else:
                    positions[key] = (gross_dta, gross_dtl)
            return positions, labels

        prior = self._prior_posted_run()
        opening_policy = (
            prior.offsetting_policy if prior else self.offsetting_policy
        )
        closing, labels = presentation(
            self.offsetting_policy, 'closing_dta', 'closing_dtl',
        )
        opening, opening_labels = presentation(
            opening_policy, 'opening_dta', 'opening_dtl',
        )
        labels.update(opening_labels)

        # The opening accounts follow the policy actually posted in the prior
        # run, while the closing accounts follow this run's policy.  When the
        # policy changes, the difference is a balance-neutral DTA/DTL opening
        # reclassification rather than the fictitious net opening previously
        # reconstructed by applying the new policy to old gross balances.
        legs = []
        if self.offsetting_policy == 'net_by_jurisdiction':
            for key in sorted(set(closing) | set(opening)):
                closing_dta, closing_dtl = closing.get(key, (0.0, 0.0))
                opening_dta, opening_dtl = opening.get(key, (0.0, 0.0))
                dta_debit_j = currency.round(closing_dta - opening_dta)
                dtl_debit_j = currency.round(-(closing_dtl - opening_dtl))
                label = labels[key]
                legs.append((self.dta_account_id, dta_debit_j,
                             _("Deferred tax asset movement (%s)", label)))
                legs.append((self.dtl_account_id, dtl_debit_j,
                             _("Deferred tax liability movement (%s)",
                               label)))
        else:
            closing_dta = sum(value[0] for value in closing.values())
            closing_dtl = sum(value[1] for value in closing.values())
            opening_dta = sum(value[0] for value in opening.values())
            opening_dtl = sum(value[1] for value in opening.values())
            dta_debit = currency.round(closing_dta - opening_dta)
            dtl_debit = currency.round(-(closing_dtl - opening_dtl))
            legs.append((self.dta_account_id, dta_debit,
                         _("Deferred tax asset movement")))
            legs.append((self.dtl_account_id, dtl_debit,
                         _("Deferred tax liability movement")))

        oci_debit = currency.round(
            sum(oci.mapped('movement_dtl')) - sum(oci.mapped('movement_dta')))
        # Expense / income leg is the balancing plug for the P&L portion,
        # computed from the rounded balance-sheet legs so the entry always
        # balances to the cent.
        expense_debit = currency.round(
            -(sum(amount for _account, amount, _label in legs) + oci_debit))

        legs.append((self.oci_account_id, oci_debit,
                     _("Deferred tax in OCI")))
        legs.append((self.deferred_tax_expense_account_id, expense_debit,
                     _("Deferred tax expense / income")))
        lines = []
        for account, signed_debit, label in legs:
            if currency.is_zero(signed_debit) or not account:
                continue
            lines.append((0, 0, {
                'name': "%s %s" % (label, self.name),
                'account_id': account.id,
                'debit': signed_debit if signed_debit > 0 else 0.0,
                'credit': -signed_debit if signed_debit < 0 else 0.0,
            }))
        if not lines:
            return self.env['account.move']
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.period_end,
            'journal_id': self.journal_id.id,
            'ref': _("Deferred tax %s", self.name),
            'line_ids': lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move
