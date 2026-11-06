# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7.39 contractual-maturity analysis driven from the general ledger.

The register in eh.fin.risk is entered by hand. This model instead reads the
open (unreconciled, posted) move lines on a chosen set of financial-liability
accounts and buckets them into contractual-maturity bands relative to a
reporting date, so the liquidity maturity analysis ties back to the GL.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from .integrity import (
    cutoff_residual_query,
    cutoff_residuals,
    execute_query_dict,
)

# Parent figures frozen once the run is finalised. Writing any of these on a
# finalised run is refused so a signed-off maturity analysis cannot be
# silently re-keyed or re-populated.
_MATURITY_FROZEN_FIELDS = frozenset({
    'reporting_date', 'annual_interest_rate', 'account_ids',
    'instrument_ids', 'line_ids', 'company_id', 'band_scheme',
    'include_open_items', 'include_leases',
})

# Ordered bands so the child lines read on-demand -> longest. The list is
# the union of both band schemes; each scheme populates only its own five
# bands (see _SCHEME_BANDS), and the ordering here drives band_sequence so
# mixed-scheme data still lists shortest-first.
MATURITY_BANDS = [
    ('on_demand', "On demand"),
    ('d0_30', "0-30 days"),
    ('lt_3m', "Under 3 months"),
    ('d31_90', "31-90 days"),
    ('3m_1y', "3 months to 1 year"),
    ('d91_365', "91-365 days"),
    ('1y_5y', "1 to 5 years"),
    ('gt_5y', "Over 5 years"),
]

# The five bands each scheme buckets into. 'contractual' is the original
# IFRS 7.39 presentation and stays the default so existing runs repopulate
# byte-identically; 'days' is the day-count presentation (overdue and
# on-demand items collapse into the first band).
_SCHEME_BANDS = {
    'contractual': ['on_demand', 'lt_3m', '3m_1y', '1y_5y', 'gt_5y'],
    'days': ['d0_30', 'd31_90', 'd91_365', '1y_5y', 'gt_5y'],
}

# Classes an extracted band row can belong to (IFRS 7.39 requires the
# analysis by class of financial instrument, IFRS 7.6).
MATURITY_ITEM_CLASSES = [
    ('liability', "Financial liabilities (selected accounts)"),
    ('instrument', "Interest-bearing instruments"),
    ('receivable', "Trade receivables (open items)"),
    ('payable', "Trade payables (open items)"),
    ('lease', "Lease liabilities (IFRS 16 schedules)"),
    ('manual', "Manual"),
    ('legacy_unknown', "Legacy - class ambiguous"),
]


class EhFinMaturityRun(models.Model):
    _name = 'eh.fin.maturity.run'
    _description = "Contractual maturity analysis run (IFRS 7.39)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'reporting_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'total_undiscounted',
        'total_receivable_inflows', 'legacy_ambiguous_count',
    })

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised run is locked: its inputs, bands and instruments "
             "cannot be edited, re-populated or appended. Only a manager can "
             "finalise or reopen it.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Bands are measured relative to this date.")

    band_scheme = fields.Selection(
        [('contractual', "On demand / <3m / 3m-1y / 1-5y / >5y"),
         ('days', "0-30 / 31-90 / 91-365 days / 1-5y / >5y")],
        default='contractual', required=True, tracking=True,
        help="Bucket boundaries used when populating (IFRS 7 does not "
             "prescribe the bands, B11: the entity uses its judgement). "
             "The contractual scheme is the original presentation and the "
             "default; the day-count scheme buckets by days from the "
             "reporting date to the contractual due date, with overdue and "
             "on-demand items in the first band.")
    include_open_items = fields.Boolean(
        string="Extract open receivables / payables", tracking=True,
        help="When populating, also build undiscounted contractual bucket "
             "rows from the due dates of ALL open (posted, unreconciled) "
             "trade receivable and trade payable items of the company, as "
             "their own classes. Off by default so existing runs keep "
             "analysing only the selected accounts / instruments.")
    include_leases = fields.Boolean(
        string="Extract lease schedules", tracking=True,
        help="When populating, also build bucket rows from the remaining "
             "contractual lease payments in the IFRS 16 lease schedules "
             "(eh_account_assets_pro), as the lease liability class "
             "(IFRS 16.58). Requires the assets and leases module; off by "
             "default.")
    annual_interest_rate = fields.Float(
        digits=(16, 6), default=0.0, tracking=True,
        string="Legacy ledger interest estimate (% p.a.)",
        help="Retained for upgrade compatibility. A single run-level rate "
             "cannot prove each liability's contractual coupons or payment "
             "dates, so population refuses a non-zero value on the ledger-"
             "account path. Capture interest-bearing facilities as individual "
             "instruments with their own rate, frequency and maturity instead.")

    account_ids = fields.Many2many(
        'account.account', 'eh_fin_maturity_run_account_rel',
        'run_id', 'account_id', string="Financial liability accounts",
        check_company=True,
        help="The borrowing / financial-liability accounts whose open move "
             "lines are analysed by contractual maturity.")

    instrument_ids = fields.One2many(
        'eh.fin.maturity.instrument', 'run_id', copy=True,
        string="Interest-bearing instruments",
        help="Per-instrument contractual terms (principal, coupon rate and "
             "frequency, maturity). When any instrument is listed, its "
             "projected contractual coupons and principal repayment are "
             "bucketed by band, so a coupon-bearing bond bands its interim "
             "coupons into the earlier bands and its principal plus final "
             "coupon into the maturity band. Leave empty to analyse the "
             "selected ledger accounts instead.")

    line_ids = fields.One2many(
        'eh.fin.maturity.line', 'run_id', copy=False,
        string="Maturity bands")

    total_undiscounted = fields.Monetary(
        compute='_compute_total', store=True, currency_field='currency_id',
        string="Total contractual outflows",
        help="Sum of payable, liability, borrowing, lease and manual outflow "
             "rows. Receivable inflows are disclosed separately and never "
             "netted into this liquidity headline. Quarantined legacy rows "
             "are retained for review but excluded until resolved.")
    total_receivable_inflows = fields.Monetary(
        compute='_compute_total', store=True, currency_field='currency_id',
        string="Total receivable inflows",
        help="Sum of extracted trade-receivable contractual inflows, shown "
             "separately from contractual liability outflows.")
    legacy_ambiguous_count = fields.Integer(
        compute='_compute_legacy_ambiguous_count', store=True,
        help=(
            "Legacy maturity rows whose historical manual/extracted origin "
            "cannot be proven. They are excluded from totals and block "
            "population/finalisation until a manager resolves them."
        ),
    )
    notes = fields.Text()

    @api.depends(
        'line_ids.undiscounted_amount', 'line_ids.origin',
        'line_ids.item_class',
    )
    def _compute_total(self):
        for run in self:
            included = run.line_ids.filtered(
                lambda line: line.origin in ('manual', 'extracted')
            )
            run.total_receivable_inflows = sum(
                included.filtered(
                    lambda line: line.item_class == 'receivable'
                ).mapped('undiscounted_amount')
            )
            run.total_undiscounted = sum(
                included.filtered(
                    lambda line: line.item_class != 'receivable'
                ).mapped('undiscounted_amount')
            )

    @api.depends('line_ids.origin')
    def _compute_legacy_ambiguous_count(self):
        for run in self:
            run.legacy_ambiguous_count = len(run.line_ids.filtered(
                lambda line: line.origin == 'legacy_ambiguous'
            ))

    @api.model_create_multi
    def create(self, vals_list):
        # Creating a run already finalised would skip the manager-gated
        # action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                vals['name'] = self._eh_disclosure_next_sequence(
                    'eh.fin.maturity.run', company) or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the run inputs, bands and instruments once finalised (a
        # signed-off analysis is frozen for everyone; restate via a
        # manager-gated reopen). The state field itself is owned by the
        # inherited eh.workflow.guard, which refuses any non-superuser direct
        # write; the sanctioned finalise / reopen actions run under sudo.
        if _MATURITY_FROZEN_FIELDS.intersection(vals):
            for run in self:
                if run.state == 'finalised':
                    raise UserError(_(
                        "Maturity run %s is finalised and cannot be edited. "
                        "Ask a manager to reopen it first.", run.name))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "maturity run."))

    def unlink(self):
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised and cannot be deleted. "
                    "Ask a manager to reopen it first.", run.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the run: inputs, bands and instruments freeze. Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self.line_ids._compute_band_sequence()
        self._compute_total()
        self.line_ids.flush_recordset(['band_sequence'])
        self.flush_recordset([
            'total_undiscounted', 'total_receivable_inflows',
        ])
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is already finalised.", run.name))
            if run.legacy_ambiguous_count:
                raise UserError(_(
                    "Maturity run %(run)s contains %(count)d quarantined "
                    "legacy rows whose origin is ambiguous. Confirm them as "
                    "manual evidence or exclude them before finalising.",
                    run=run.name, count=run.legacy_ambiguous_count,
                ))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised run to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True

    def _resolve_legacy_ambiguous_lines(self, origin):
        """Record a manager's explicit remediation without deleting history."""
        self = self._eh_disclosure_prepare_action(engine=True)
        if origin not in ('manual', 'legacy_excluded'):
            raise UserError(_("Unsupported legacy maturity resolution."))
        for run in self:
            if run.state != 'draft':
                raise UserError(_(
                    "Reopen maturity run %s before resolving legacy rows.",
                    run.name,
                ))
            lines = run.line_ids.filtered(
                lambda line: line.origin == 'legacy_ambiguous'
            )
            if not lines:
                raise UserError(_(
                    "Maturity run %s has no quarantined legacy rows.",
                    run.name,
                ))
            lines.write({
                'origin': origin,
                **({'item_class': 'manual'} if origin == 'manual' else {}),
                'legacy_resolved_by_id': self.env.user.id,
                'legacy_resolved_at': fields.Datetime.now(),
            })
        return True

    def action_confirm_legacy_lines_manual(self):
        """Manager attests that quarantined legacy rows were manually keyed."""
        return self._resolve_legacy_ambiguous_lines('manual')

    def action_exclude_legacy_lines(self):
        """Retain ambiguous rows as evidence but exclude them from the note."""
        return self._resolve_legacy_ambiguous_lines('legacy_excluded')

    def _band_for(self, maturity_date, reporting_date):
        """Return the band key for a contractual maturity date relative to
        the reporting date, under the run's band scheme.

        Contractual scheme (default, original behaviour): a maturity on or
        before the reporting date (or with no maturity date) is on demand;
        then <3 months, 3 months to 1 year, 1 to 5 years, over 5 years.
        Day-count scheme: days from the reporting date to the due date,
        0-30 (including overdue and undated items), 31-90, 91-365, then 1
        to 5 years and over 5 years."""
        self.ensure_one()
        if self.band_scheme == 'days':
            if not maturity_date:
                return 'd0_30'
            days = (maturity_date - reporting_date).days
            if days <= 30:
                return 'd0_30'
            if days <= 90:
                return 'd31_90'
            if days <= 365:
                return 'd91_365'
            if maturity_date < reporting_date + relativedelta(years=5):
                return '1y_5y'
            return 'gt_5y'
        if not maturity_date or maturity_date <= reporting_date:
            return 'on_demand'
        if maturity_date < reporting_date + relativedelta(months=3):
            return 'lt_3m'
        if maturity_date < reporting_date + relativedelta(years=1):
            return '3m_1y'
        if maturity_date < reporting_date + relativedelta(years=5):
            return '1y_5y'
        return 'gt_5y'

    def action_populate(self):
        """Read open (unreconciled, posted) move lines on the selected
        accounts and rebuild the band lines.

        IFRS 7.39 / B11D requires the maturity analysis to show the
        UNDISCOUNTED contractual amounts of financial liabilities as positive
        outflows. A financial-liability line carries a credit balance, so its
        ledger balance (debit - credit) is negative;
        taking that raw balance would mislabel a payable outflow as a negative
        amount. The contractual principal outflow is therefore the credit-side
        magnitude (credit - debit), i.e. the sign-flipped balance, which is
        positive for a net liability. The ledger path can prove principal and
        due date only. Contractual coupons require an individual instrument
        with its own rate and frequency; a single estimated rate over mixed
        accounts is refused instead of fabricating cash flows.

        Only lines on or before the reporting date are analysed, so entries
        posted after the as-at date do not leak into the disclosure.

        Idempotent: extracted rows are wiped and rebuilt on every populate;
        manually keyed band rows (origin = manual) are preserved, the same
        pattern as the ECL populate."""
        self = self._eh_disclosure_prepare_action(engine=True)
        Line = self.env['eh.fin.maturity.line']
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "re-populated. Ask a manager to reopen it first.",
                    run.name))
            if run.legacy_ambiguous_count:
                raise UserError(_(
                    "Maturity run %(run)s contains quarantined legacy rows. "
                    "Resolve them as manual evidence or exclude them before "
                    "repopulating; the engine will not guess or delete their "
                    "history.", run=run.name,
                ))
            if run.instrument_ids and run.account_ids:
                raise UserError(_(
                    "Maturity run %s has both instruments and ledger accounts. "
                    "Choose one authoritative liability source; combining "
                    "them could count the same facility twice.", run.name,
                ))
            if run.account_ids and run.annual_interest_rate:
                raise UserError(_(
                    "Maturity run %s applies one legacy interest estimate to "
                    "ledger accounts. That cannot prove contractual coupon "
                    "dates or distinguish interest-bearing balances. Set the "
                    "estimate to zero for principal-only accounts, or capture "
                    "each facility as an instrument.", run.name,
                ))
            # Rebuild only evidence that this engine itself generated. Manual
            # rows and resolved-but-excluded legacy evidence are both retained.
            run.line_ids.filtered(
                lambda line: line.origin == 'extracted'
            ).unlink()
            # Per-instrument projection buckets each instrument's coupons and
            # principal repayment using its OWN rate, so a coupon bond places
            # interim coupons into the earlier bands (IFRS 7.39 / B11D).
            if run.instrument_ids:
                run._create_class_bands(
                    Line, 'instrument', run._instrument_flows())
            elif run.account_ids:
                run._create_class_bands(
                    Line, 'liability', run._ledger_account_flows())
            # Optional extraction sources, additive per class. Skip-zero so
            # a class with no open items adds no empty rows.
            if run.include_open_items:
                run._create_class_bands(
                    Line, 'receivable',
                    run._open_item_flows('asset_receivable'),
                    skip_zero=True)
                run._create_class_bands(
                    Line, 'payable',
                    run._open_item_flows('liability_payable'),
                    skip_zero=True)
            if run.include_leases:
                run._create_class_bands(
                    Line, 'lease', run._lease_flows(), skip_zero=True)
        return True

    def _create_class_bands(self, Line, item_class, flows, skip_zero=False):
        """Bucket (date, amount) contractual cash flows into the run's band
        scheme and create one extracted row per band for the class. The
        instrument / selected-account classes keep the original behaviour of
        one row per band including zero bands; the extraction classes skip
        zero bands (skip_zero)."""
        self.ensure_one()
        currency = self.currency_id
        buckets = {key: 0.0 for key in _SCHEME_BANDS[self.band_scheme]}
        for flow_date, amount in flows:
            band = self._band_for(flow_date, self.reporting_date)
            buckets[band] += amount
        for key in _SCHEME_BANDS[self.band_scheme]:
            amount = buckets[key]
            if currency:
                amount = currency.round(amount)
            if skip_zero and currency and currency.is_zero(amount):
                continue
            Line.create({
                'run_id': self.id,
                'band': key,
                'item_class': item_class,
                'origin': 'extracted',
                'undiscounted_amount': amount,
            })

    def _instrument_flows(self):
        """(date, amount) contractual cash flows of the listed instruments
        (IFRS 7.39 / B11D). Each instrument projects its own undiscounted
        contractual coupons at its own rate and frequency, plus its
        principal repayment at maturity, so a plain coupon bond bands its
        interim coupons into the earlier bands and its principal plus final
        coupon into the maturity band."""
        self.ensure_one()
        for instrument in self.instrument_ids:
            yield from instrument._contractual_cash_flows(
                self.reporting_date)

    def _ledger_account_flows(self):
        """(date, amount) undiscounted contractual outflows of the open
        (posted, unreconciled) move lines on the selected financial
        liability accounts: the credit-side principal magnitude. Interest-
        bearing facilities must use the instrument path, which carries the
        contractual rate and coupon frequency required by IFRS 7.B11D."""
        self.ensure_one()
        move_lines = self.env['account.move.line'].search([
            ('account_id', 'in', self.account_ids.ids),
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.reporting_date),
        ])
        residuals = cutoff_residuals(move_lines, self.reporting_date)
        for ml in move_lines:
            maturity = ml.date_maturity or ml.date
            # Contractual principal outflow for a liability is the
            # residual credit-side magnitude, reconstructed at the cutoff.
            # A later payment/reconciliation must not erase a liability that
            # was still open at the reporting date.
            principal = -residuals[ml.id][0]
            if ml.company_id.currency_id != self.currency_id:
                principal = ml.company_id.currency_id._convert(
                    principal, self.currency_id, self.company_id,
                    self.reporting_date,
                )
            if self.currency_id.is_zero(principal):
                continue
            yield maturity, principal

    def _open_item_flows(self, account_type):
        """(date, amount) open-item contractual cash flows for a receivable
        or payable class: every open (posted, unreconciled) item of the
        company on accounts of that type, at its residual amount and due
        date. Receivables report as positive inflows, payables as positive
        outflows (amount_residual is signed debit-positive, so the payable
        side is sign-flipped)."""
        self.ensure_one()
        MoveLine = self.env['account.move.line']
        company_ids = self._eh_disclosure_company_ids()
        if len(company_ids) > 1:
            move_lines = MoveLine.search([
                ('company_id', 'in', company_ids),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', '=', account_type),
                ('date', '<=', self.reporting_date),
            ])
            residuals = cutoff_residuals(move_lines, self.reporting_date)
            by_date = {}
            sign = 1.0 if account_type == 'asset_receivable' else -1.0
            for move_line in move_lines:
                amount = residuals[move_line.id][0] * sign
                source_currency = move_line.company_id.currency_id
                if source_currency != self.currency_id:
                    amount = source_currency._convert(
                        amount, self.currency_id, self.company_id,
                        self.reporting_date,
                    )
                flow_date = move_line.date_maturity or move_line.date
                by_date[flow_date] = by_date.get(flow_date, 0.0) + amount
            yield from sorted(by_date.items())
            return
        query, company_residual, _foreign_residual = cutoff_residual_query(
            MoveLine, [
                ('company_id', '=', company_ids[0]),
                ('parent_state', '=', 'posted'),
                ('account_id.account_type', '=', account_type),
                ('date', '<=', self.reporting_date),
            ], self.reporting_date,
        )
        aml_alias = query.table_alias
        flow_date = SQL(
            "COALESCE(%s, %s)",
            SQL.identifier(aml_alias, 'date_maturity'),
            SQL.identifier(aml_alias, 'date'),
        )
        query.add_where(SQL("%s <> 0", company_residual))
        query.groupby = flow_date
        rows = execute_query_dict(self.env, query.select(
            SQL("%s AS %s", flow_date, SQL.identifier('flow_date')),
            SQL(
                "SUM(%s) AS %s",
                company_residual, SQL.identifier('residual'),
            ),
        ))
        sign = 1.0 if account_type == 'asset_receivable' else -1.0
        for row in rows:
            yield row['flow_date'], float(row['residual'] or 0.0) * sign

    def _lease_flows(self):
        """(date, amount) remaining contractual lease payments from the
        IFRS 16 schedules (IFRS 16.58 requires the lease liability maturity
        analysis under IFRS 7.39). Soft lookup: raises a clear error when
        the assets and leases module is not installed. Only the lease
        component feeds the analysis (the service share is not a financial
        liability), and only payments contractually due after the reporting
        date on live (active or modified) leases."""
        self.ensure_one()
        if 'eh.lease.schedule.line' not in self.env:
            raise UserError(_(
                "Extracting lease schedules requires the Assets and Leases "
                "module (eh_account_assets_pro). Install it or untick "
                "'Extract lease schedules'."))
        Lease = self.env['eh.lease.contract']
        lease_domain = [
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('commencement_date', '<=', self.reporting_date),
            ('state', '!=', 'draft'),
        ]
        if 'exemption' in Lease._fields:
            lease_domain.append(('exemption', '=', 'none'))
        if 'lessor_mode' in Lease._fields:
            lease_domain.append(('lessor_mode', '=', 'none'))
        leases = Lease.search(lease_domain)
        eligible = Lease
        for lease in leases:
            opening_move = lease.opening_move_id \
                if 'opening_move_id' in lease._fields else False
            if 'opening_move_id' in lease._fields and not opening_move:
                raise UserError(_(
                    "Lease %s has no posted opening-entry provenance, so its "
                    "existence at the reporting date cannot be proven.",
                    lease.display_name,
                ))
            if opening_move and (
                opening_move.state != 'posted'
                or opening_move.date > self.reporting_date
            ):
                continue
            termination_date = lease.termination_date \
                if 'termination_date' in lease._fields else False
            if termination_date and termination_date <= self.reporting_date:
                continue
            if lease.state == 'terminated' and not termination_date:
                raise UserError(_(
                    "Lease %s is terminated but has no dated termination "
                    "evidence, so its lifecycle at the reporting date cannot "
                    "be reconstructed.", lease.display_name,
                ))
            modification_moves = lease.modification_move_ids.filtered(
                lambda move: move.state == 'posted'
            ) if 'modification_move_ids' in lease._fields else self.env[
                'account.move'
            ]
            future_modification = modification_moves.filtered(
                lambda move: move.date > self.reporting_date
            )
            last_modified_at = lease.last_modified_at \
                if 'last_modified_at' in lease._fields else False
            modified_after_cutoff = (
                last_modified_at
                and fields.Datetime.to_datetime(last_modified_at).date()
                > self.reporting_date
            )
            if future_modification or modified_after_cutoff:
                raise UserError(_(
                    "Lease %s was modified after the reporting date and the "
                    "prior unposted schedule was replaced. Historical "
                    "contractual cash flows cannot be reconstructed safely; "
                    "use preserved as-at evidence or a reviewed manual row.",
                    lease.display_name,
                ))
            eligible |= lease
        schedule_lines = self.env['eh.lease.schedule.line'].search([
            ('lease_id', 'in', eligible.ids),
            ('period_date', '>', self.reporting_date),
        ])
        for lease in eligible.filtered(
            lambda rec: rec.state == 'terminated'
            and rec.termination_date > self.reporting_date
        ):
            if not schedule_lines.filtered(lambda line: line.lease_id == lease):
                raise UserError(_(
                    "Lease %s was terminated after the reporting date and its "
                    "then-remaining schedule was deleted. The as-at maturity "
                    "cash flows are unavailable and cannot be fabricated.",
                    lease.display_name,
                ))
        for line in schedule_lines:
            amount = line.payment_amount
            source_currency = line.lease_id.company_id.currency_id
            if source_currency != self.currency_id:
                amount = source_currency._convert(
                    amount, self.currency_id, self.company_id,
                    line.period_date,
                )
            yield line.period_date, amount


class EhFinMaturityLine(models.Model):
    _name = 'eh.fin.maturity.line'
    _description = "Contractual maturity analysis band"
    _order = 'run_id, band_sequence, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'run_id'
    _eh_disclosure_server_fields = frozenset({
        'company_id', 'currency_id', 'origin', 'band_sequence',
        'legacy_resolved_by_id', 'legacy_resolved_at',
        'legacy_claimed_origin', 'legacy_claimed_item_class',
    })
    _eh_disclosure_engine_origins = frozenset({
        'extracted', 'legacy_ambiguous', 'legacy_excluded',
    })

    run_id = fields.Many2one(
        'eh.fin.maturity.run', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    band = fields.Selection(
        MATURITY_BANDS, required=True,
        help="Contractual maturity band (IFRS 7.39).")
    item_class = fields.Selection(
        MATURITY_ITEM_CLASSES, required=True, default='manual',
        string="Class",
        help="Class of financial instrument the band row belongs to "
             "(IFRS 7.6): the analysed liability accounts, the projected "
             "instruments, or an extracted open-item / lease class.")
    origin = fields.Selection(
        [
            ('extracted', "Extracted"),
            ('manual', "Manual"),
            ('legacy_ambiguous', "Legacy - origin ambiguous"),
            ('legacy_excluded', "Legacy - reviewed and excluded"),
        ],
        required=True, default='manual',
        help="Extracted rows are wiped and rebuilt on every populate. A "
             "manually keyed row survives the repopulate. Legacy ambiguous "
             "rows are quarantined because their historical origin cannot be "
             "proven; a manager must attest them as manual or retain them as "
             "excluded evidence before the run can continue.")
    legacy_resolved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help="Manager who explicitly resolved the legacy provenance ambiguity.",
    )
    legacy_resolved_at = fields.Datetime(
        readonly=True, copy=False,
        help="Time the legacy provenance ambiguity was explicitly resolved.",
    )
    legacy_claimed_origin = fields.Char(
        readonly=True, copy=False,
        help="Untrusted origin label found before quarantine. Blank on a "
             "direct upgrade from the schema that had no origin column.",
    )
    legacy_claimed_item_class = fields.Char(
        readonly=True, copy=False,
        help="Untrusted class label found before quarantine. It is retained "
             "for audit but never used in disclosure totals or grouping.",
    )
    band_sequence = fields.Integer(
        compute='_compute_band_sequence', store=True)
    undiscounted_amount = fields.Monetary(
        currency_field='currency_id',
        help="Undiscounted contractual cash flow bucketed into this band, "
             "shown as a positive liability outflow. This is the open "
             "principal (credit-side magnitude of the ledger balance) plus "
             "the undiscounted contractual interest to maturity when an "
             "annual interest rate is supplied on the run (IFRS 7.B11D); "
             "with no rate it is the principal only.")

    @api.depends('band')
    def _compute_band_sequence(self):
        order = {key: i for i, (key, _label) in enumerate(MATURITY_BANDS)}
        for line in self:
            line.band_sequence = order.get(line.band, 99)

    @api.model_create_multi
    def create(self, vals_list):
        # A create-append hole silently moves the run total, so appending a
        # band to a finalised run is refused (create guard is required).
        runs = self.env['eh.fin.maturity.run'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; no band can be added. "
                    "Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "edited. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its bands cannot be "
                    "removed. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().unlink()


# Coupon payments per year for each frequency.
COUPON_FREQUENCY = [
    ('annual', "Annual"),
    ('semiannual', "Semi-annual"),
    ('quarterly', "Quarterly"),
    ('monthly', "Monthly"),
    ('bullet', "At maturity only"),
]
_FREQ_PER_YEAR = {
    'annual': 1, 'semiannual': 2, 'quarterly': 4, 'monthly': 12, 'bullet': 0,
}


class EhFinMaturityInstrument(models.Model):
    _name = 'eh.fin.maturity.instrument'
    _description = "Interest-bearing instrument (IFRS 7.39)"
    _order = 'run_id, sequence, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'run_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})

    run_id = fields.Many2one(
        'eh.fin.maturity.run', required=True, ondelete='cascade', index=True,
        check_company=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="Instrument description.")
    principal = fields.Monetary(
        currency_field='currency_id',
        help="Contractual principal (face amount) repaid at maturity, as a "
             "positive outflow.")
    annual_rate = fields.Float(
        digits=(16, 6), default=0.0,
        string="Coupon rate (% p.a.)",
        help="This instrument's own annual contractual coupon rate. Each "
             "coupon is principal * rate% / coupons-per-year. A rate of zero "
             "is a non-interest-bearing (zero-coupon) instrument, whose only "
             "cash flow is the principal at maturity.")
    coupon_frequency = fields.Selection(
        COUPON_FREQUENCY, default='annual', required=True,
        help="How often the contractual coupon is paid. 'At maturity only' "
             "pays a single coupon with the principal at maturity.")
    floating_rate = fields.Boolean(
        string="Floating rate",
        help="The borrowing bears a floating (variable) rate. Floating "
             "instruments on the company's latest maturity run feed the "
             "computed IFRS 7.40 interest-rate sensitivity: a rate rise "
             "increases their interest cost, so the impact is negative.")
    maturity_date = fields.Date(
        required=True,
        help="Contractual maturity date on which the principal is repaid.")
    interest_start_date = fields.Date(
        string="Interest accrual start",
        help="Contractual date from which interest accrues. Required for a "
             "non-zero at-maturity coupon so the full contractual coupon is "
             "measured independently of the disclosure reporting date.")

    @api.constrains('interest_start_date', 'maturity_date')
    def _check_interest_term(self):
        for instrument in self:
            if instrument.interest_start_date \
                    and instrument.interest_start_date > instrument.maturity_date:
                raise UserError(_(
                    "Interest accrual start must be on or before maturity."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an instrument to a finalised run would change its projected
        # cash flows on the next populate, so the create guard is required.
        runs = self.env['eh.fin.maturity.run'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; no instrument can be "
                    "added. Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for instrument in self:
            if instrument.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its instruments cannot be "
                    "edited. Ask a manager to reopen it first.",
                    instrument.run_id.name))
        return super().write(vals)

    def unlink(self):
        for instrument in self:
            if instrument.run_id.state == 'finalised':
                raise UserError(_(
                    "Maturity run %s is finalised; its instruments cannot be "
                    "removed. Ask a manager to reopen it first.",
                    instrument.run_id.name))
        return super().unlink()

    def _contractual_cash_flows(self, reporting_date):
        """Yield (payment_date, undiscounted_amount) tuples for this
        instrument's contractual cash flows strictly after the reporting
        date, on an undiscounted basis (IFRS 7.39 / B11D).

        Interim coupons fall on their own payment dates and are bucketed into
        the band of that date; the final cash flow at maturity is the last
        coupon plus the principal. A zero rate yields no coupons, so only the
        principal at maturity is returned (a zero-coupon instrument)."""
        self.ensure_one()
        if not self.maturity_date:
            return
        per_year = _FREQ_PER_YEAR.get(self.coupon_frequency, 0)
        rate = (self.annual_rate or 0.0) / 100.0
        if self.coupon_frequency == 'bullet' and rate \
                and not self.interest_start_date:
            raise UserError(_(
                "Instrument %s needs an interest accrual start date to "
                "measure its at-maturity coupon.", self.display_name,
            ))
        # Coupon amount per period (0 when non-interest-bearing or bullet with
        # no rate). For 'bullet' there is a single coupon covering the whole
        # life, paid with the principal at maturity.
        if per_year and rate:
            coupon = self.principal * rate / per_year
            # Coupon dates counted back from maturity so the final coupon
            # lands exactly on the maturity date. months_step is the interval
            # between coupons.
            months_step = 12 // per_year
            n = 0
            # Emit interim coupons on their own dates (all before maturity),
            # newest-first walk back but yielded date-tagged so band bucketing
            # is order-independent.
            date = self.maturity_date
            while True:
                if date <= max(
                    reporting_date, self.interest_start_date or reporting_date
                ):
                    break
                if date < self.maturity_date:
                    # Interim coupon on its own payment date.
                    yield date, coupon
                n += 1
                date = self.maturity_date - relativedelta(
                    months=months_step * n)
        # Final cash flow at maturity: principal + the coupon due at maturity.
        if self.maturity_date > reporting_date:
            final = self.principal
            if per_year and rate:
                final += self.principal * rate / per_year
            elif self.coupon_frequency == 'bullet' and rate:
                days = (self.maturity_date - self.interest_start_date).days
                final += self.principal * rate * (days / 365.0)
            yield self.maturity_date, final
        elif self.maturity_date <= reporting_date:
            # Already matured / on demand: principal only, on demand band.
            if self.principal:
                yield self.maturity_date, self.principal
