# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.borrowing.cost: capitalisation of borrowing costs on a qualifying asset.

The capitalisable amount is the borrowing costs on specific borrowings net of
temporary investment income, plus the borrowing costs on general borrowings
applied by the capitalisation rate to the expenditure, capped at the
borrowing costs actually incurred (IAS 23.10-14).
"""

import calendar
import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .account_move import BORROWING_MOVE_CAPABILITY, BORROWING_MOVE_CONTEXT


_EVIDENCE_STATES = ('capitalised', 'reversed')
_QUALIFYING_ASSET_TYPES = {
    'asset_current', 'asset_non_current', 'asset_fixed',
}
_BORROWING_COST_ACCOUNT_TYPES = {'expense', 'expense_other'}


def _lock_borrowing_cost_parents(env, parent_ids):
    """Parent-first lock shared by both child tables and posting."""
    parents = env['eh.borrowing.cost'].browse(
        sorted({parent_id for parent_id in parent_ids if parent_id})
    )
    if parents:
        parents._eh_lock_transition()
    return parents


class EhBorrowingCost(models.Model):
    _name = 'eh.borrowing.cost'
    _description = "Borrowing cost capitalisation (IAS 23)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # State moves only through the record's own actions (action_capitalise /
    # action_cancel), never a direct RPC/ORM write: otherwise a plain user
    # could write({'state': 'capitalised'}) to skip the manager check and the
    # journal entry entirely.
    _eh_guarded_fields = (
        'state', 'move_id', 'reversal_move_id', 'currency_id',
        'posted_accounting_date', 'posted_reversal_date',
        'weighted_average_base', 'capitalisable', 'uncapped_amount',
        'eh_integrity_review_required', 'eh_integrity_review_reason',
        'integrity_reviewed_by_id', 'integrity_reviewed_at',
    )
    _eh_create_guarded_fields = (
        'state', 'move_id', 'reversal_move_id',
        'posted_accounting_date', 'posted_reversal_date',
        'eh_integrity_review_required', 'eh_integrity_review_reason',
        'integrity_reviewed_by_id', 'integrity_reviewed_at',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    qualifying_asset = fields.Char(
        required=True, tracking=True,
        help="The qualifying asset under construction or production.")
    state = fields.Selection(
        [('draft', "Draft"), ('capitalised', "Capitalised"),
         ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_start = fields.Date(
        tracking=True,
        help="Start of the capitalisation period. Used to time-apportion "
             "dated expenditure into a weighted-average base (IAS 23.14). "
             "Defaults to the earliest expenditure date when left blank.")
    period_end = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    accounting_date = fields.Date(
        tracking=True,
        help="Journal date used for capitalisation. Leave blank to use the "
             "measurement period end. When that period is locked, select an "
             "explicit open correction date; measurement remains anchored to "
             "the original period end.")
    posted_accounting_date = fields.Date(
        readonly=True, copy=False,
        help="Exact journal date accepted by Odoo when capitalisation posted. "
             "A silent lock-date shift is rejected instead of being recorded "
             "against the wrong source period.")
    reversal_date = fields.Date(
        tracking=True,
        help="Requested accounting date for the reversal. Leave blank to use "
             "today. The date cannot precede the capitalisation entry or fall "
             "inside a locked period.")
    posted_reversal_date = fields.Date(
        readonly=True, copy=False,
        help="Exact date accepted by Odoo for the sealed reversal entry.")

    # Capitalisation-period controls (IAS 23.17-25). All optional: when unset
    # the flat single-period behaviour above is preserved unchanged.
    commencement_date = fields.Date(
        tracking=True,
        help="Date capitalisation commences: expenditure and borrowing costs "
             "are being incurred and activities to prepare the asset are in "
             "progress (IAS 23.17-18). Borrowing cost before this date is not "
             "capitalised. Leave blank to capitalise from the period start.")
    cessation_date = fields.Date(
        tracking=True,
        help="Date the asset is substantially ready for its intended use or "
             "sale; capitalisation ceases (IAS 23.22-25). Borrowing cost after "
             "this date is not capitalised. Leave blank to capitalise to the "
             "period end.")
    suspension_line_ids = fields.One2many(
        'eh.borrowing.cost.suspension', 'borrowing_cost_id',
        string="Suspension Periods",
        help="Spans during which active development was suspended; "
             "capitalisation is suspended over these spans (IAS 23.20-21).")

    specific_borrowing_cost = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Borrowing costs on funds borrowed specifically for the asset.")
    temporary_investment_income = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Income on the temporary investment of specific borrowings, "
             "deducted from the amount capitalised (IAS 23.12-13).")
    general_expenditure = fields.Monetary(
        string="Undated General Expenditure",
        currency_field='currency_id', tracking=True,
        help="Expenditure on the asset funded from general borrowings. Used "
             "as the base only when no dated expenditure lines are entered. "
             "This amount and dated lines are mutually exclusive (IAS 23.14).")
    expenditure_line_ids = fields.One2many(
        'eh.borrowing.cost.line', 'borrowing_cost_id',
        string="Dated Expenditure",
        help="Expenditure on the asset funded from general borrowings, dated "
             "so it can be time-apportioned into a weighted-average base "
             "(IAS 23.14).")
    weighted_average_base = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id',
        help="Weighted-average accumulated expenditure over the period to "
             "which the capitalisation rate is applied (IAS 23.14).")
    capitalisation_rate = fields.Float(
        digits=(6, 3), tracking=True,
        help="Weighted-average rate of general borrowings, as a percentage "
             "(IAS 23.14).")
    capitalisation_rate_basis = fields.Selection(
        [
            ('annual', "Annual rate"),
            ('period', "Rate for this period"),
        ],
        required=True,
        default='annual',
        tracking=True,
        help=(
            "Annual rate apportions general borrowing cost by actual days "
            "in each calendar year. Rate for this period applies the entered "
            "percentage once to the period's weighted-average base. Legacy "
            "records keep period-rate semantics during upgrade."
        ),
    )
    actual_borrowing_cost = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Total borrowing costs actually incurred in the period; the cap "
             "on the amount capitalised (IAS 23.14).")
    apply_actual_borrowing_cost_cap = fields.Boolean(
        string="Apply Actual-Cost Cap",
        tracking=True,
        help=(
            "When selected, the capitalisable amount cannot exceed Actual "
            "Borrowing Cost. Entering that amount explicitly selects the cap, "
            "including an explicit zero; clear this option only when the "
            "actual period cost is not yet available."
        ),
    )

    capitalisable = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id',
        help="Amount capitalised this period.")
    uncapped_amount = fields.Monetary(
        compute='_compute_capitalisable', store=True,
        currency_field='currency_id')

    asset_account_id = fields.Many2one(
        'account.account', string="Qualifying Asset Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ('asset_current', "
               "'asset_non_current', 'asset_fixed')), "
               "('company_ids', 'parent_of', company_id)]")
    borrowing_cost_account_id = fields.Many2one(
        'account.account', string="Interest / Borrowing Cost Account",
        tracking=True, check_company=True,
        domain="[('account_type', 'in', ('expense', 'expense_other')), "
               "('company_ids', 'parent_of', company_id)]",
        help="The interest expense account from which the capitalised amount "
             "is reclassified to the asset.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), "
               "('company_id', 'parent_of', company_id)]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True)
    reversal_move_id = fields.Many2one(
        'account.move', string="Reversal Entry", readonly=True, copy=False,
        ondelete='restrict', check_company=True)
    move_ids = fields.One2many(
        'account.move', 'eh_borrowing_cost_id', string="Journal History",
        readonly=True)
    move_count = fields.Integer(compute='_compute_move_count')
    eh_integrity_review_required = fields.Boolean(
        readonly=True, copy=False, index=True,
        help="Legacy move provenance could not be proved during upgrade.")
    eh_integrity_review_reason = fields.Text(readonly=True, copy=False)
    integrity_review_note = fields.Text(
        copy=False,
        tracking=True,
        help=(
            "Manager attestation explaining evidence reviewed before a "
            "legacy integrity quarantine is released."
        ),
    )
    integrity_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    integrity_reviewed_at = fields.Datetime(readonly=True, copy=False)

    notes = fields.Text()

    def _compute_move_count(self):
        for record in self:
            record.move_count = len(record.move_ids)

    @api.depends('specific_borrowing_cost', 'temporary_investment_income',
                 'general_expenditure', 'capitalisation_rate',
                 'capitalisation_rate_basis',
                 'actual_borrowing_cost', 'apply_actual_borrowing_cost_cap',
                 'period_start', 'period_end',
                 'commencement_date', 'cessation_date',
                 'suspension_line_ids.date_start', 'suspension_line_ids.date_end',
                 'expenditure_line_ids.amount', 'expenditure_line_ids.date')
    def _compute_capitalisable(self):
        for c in self:
            base = c._weighted_average_base()
            c.weighted_average_base = (
                c.currency_id.round(base) if c.currency_id else base)
            # Investment income on temporarily invested specific borrowings is
            # deducted from that specific borrowing's OWN capitalisable amount
            # (IAS 23.12). Floor the net at zero so an excess of investment
            # income over the specific borrowing cost cannot spill over and
            # erode the general-borrowing component: income can reduce the
            # specific tranche to zero but never subsidise the general pool.
            specific_net = max(
                c.specific_borrowing_cost - c.temporary_investment_income, 0.0)
            general = (
                base
                * c.capitalisation_rate / 100.0
                * c._capitalisation_rate_factor()
            )
            uncapped = specific_net + general
            c.uncapped_amount = (
                c.currency_id.round(uncapped) if c.currency_id else uncapped)
            capped = (
                min(uncapped, c.actual_borrowing_cost)
                if c.apply_actual_borrowing_cost_cap else uncapped
            )
            c.capitalisable = max(
                c.currency_id.round(capped) if c.currency_id else capped, 0.0)

    @api.constrains(
        'period_start', 'period_end', 'commencement_date', 'cessation_date',
        'specific_borrowing_cost', 'temporary_investment_income',
        'general_expenditure', 'capitalisation_rate',
        'actual_borrowing_cost', 'expenditure_line_ids',
    )
    def _check_measurement_inputs(self):
        """Reject ambiguous or economically impossible IAS 23 inputs."""
        for record in self:
            if (
                record.period_start
                and record.period_end
                and record.period_start > record.period_end
            ):
                raise ValidationError(_(
                    "Capitalisation period start cannot be after period end."
                ))
            if (
                record.commencement_date
                and record.cessation_date
                and record.commencement_date > record.cessation_date
            ):
                raise ValidationError(_(
                    "Capitalisation commencement cannot be after cessation."
                ))
            negative = [
                label for field_name, label in (
                    ('specific_borrowing_cost', "Specific borrowing cost"),
                    ('temporary_investment_income',
                     "Temporary investment income"),
                    ('general_expenditure', "General expenditure"),
                    ('capitalisation_rate', "Capitalisation rate"),
                    ('actual_borrowing_cost', "Actual borrowing cost"),
                ) if record[field_name] < 0.0
            ]
            if negative:
                raise ValidationError(_(
                    "Borrowing-cost inputs cannot be negative: %s.",
                    ', '.join(negative),
                ))
            if record.general_expenditure and record.expenditure_line_ids:
                raise ValidationError(_(
                    "Use either Undated General Expenditure or Dated "
                    "Expenditure lines for one period, not both."
                ))

    def _capitalisation_rate_factor(self):
        """Return period fraction for an annual general-borrowing rate.

        Uses Actual/Actual by calendar year. Period endpoints are inclusive,
        so a full calendar year is exactly 1.0 and a cross-year period is
        split across each year's 365/366 denominator. A period-rate input is
        already scoped and therefore has factor 1.0.
        """
        self.ensure_one()
        if self.capitalisation_rate_basis != 'annual':
            return 1.0
        dated = self.expenditure_line_ids.filtered(lambda line: line.date)
        period_start = self.period_start or (
            min(dated.mapped('date')) if dated else None
        )
        period_end = self.period_end
        if not period_start or not period_end or period_end < period_start:
            # Draft preview remains available while a start is incomplete;
            # action_capitalise blocks a general component in this state.
            return 1.0
        cursor = period_start
        fraction = 0.0
        while cursor <= period_end:
            year_end = datetime.date(cursor.year, 12, 31)
            segment_end = min(year_end, period_end)
            days = (segment_end - cursor).days + 1
            days_in_year = (
                366.0  # noqa: gcclog-hardcode leap-year day count
                if calendar.isleap(cursor.year)
                else 365.0  # noqa: gcclog-hardcode common-year day count
            )
            fraction += days / days_in_year
            cursor = segment_end + datetime.timedelta(days=1)
        return fraction

    def _has_capitalisation_window(self):
        """Whether any IAS 23.17-25 window control is set.

        When none is set, the flat single-period behaviour is preserved
        exactly; the window logic is opt-in.
        """
        self.ensure_one()
        return bool(self.commencement_date or self.cessation_date
                    or self.suspension_line_ids)

    def _active_spans(self, window_start, window_end):
        """Return the active (non-suspended) sub-spans of the window.

        Each span is a ``(start, end)`` pair of dates. The window
        ``[window_start, window_end]`` has the suspension periods
        (IAS 23.20-21) carved out. Returns an empty list when the window is
        degenerate or fully suspended.
        """
        self.ensure_one()
        if not window_start or not window_end or window_end < window_start:
            return []
        # Work internally with half-open spans while the business dates remain
        # inclusive. A suspension's date_end is the day activity resumes, so
        # [date_start, date_end) is carved out of [start, end + 1 day).
        boundary_end = window_end + datetime.timedelta(days=1)
        suspensions = []
        for s in self.suspension_line_ids:
            if not s.date_start or not s.date_end:
                continue
            lo = max(s.date_start, window_start)
            hi = min(s.date_end, boundary_end)
            if hi > lo:
                suspensions.append((lo, hi))
        suspensions.sort()
        active = []
        cursor = window_start
        for lo, hi in suspensions:
            if lo > cursor:
                active.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < boundary_end:
            active.append((cursor, boundary_end))
        return active

    def _weighted_average_base(self):
        """Return the base to which the capitalisation rate is applied.

        Where dated expenditure lines exist, each amount is time-apportioned
        by the fraction of the capitalisation period for which it was
        outstanding (from its date to the period end), giving the
        weighted-average accumulated expenditure required by IAS 23.14. When
        no dated lines are entered, the flat ``general_expenditure`` is used
        unchanged.

        When any capitalisation-period control is set (commencement,
        cessation or a suspension span, IAS 23.17-25) apportionment is
        restricted to the active window: borrowing cost is not capitalised
        before commencement, during a suspension, or after the asset is ready
        for use. A cost whose window is empty (for example entirely past the
        cessation date) yields a nil base and nothing is capitalised.
        """
        self.ensure_one()
        if self._has_capitalisation_window():
            return self._windowed_base()
        lines = self.expenditure_line_ids.filtered(lambda l: l.date)
        if not lines:
            return self.general_expenditure
        period_end = self.period_end
        if not period_end:
            return sum(lines.mapped('amount'))
        starts = [line.date for line in lines]
        period_start = self.period_start or min(starts)
        total_days = (period_end - period_start).days + 1
        if total_days <= 0:
            return 0.0
        base = 0.0
        for line in lines:
            # Expenditure outstanding from its date (clamped into the period)
            # to the period end.
            outstanding_from = max(line.date, period_start)
            days_out = (period_end - outstanding_from).days + 1
            if days_out <= 0:
                continue
            base += line.amount * days_out / total_days
        return base

    def _windowed_base(self):
        """Weighted-average base restricted to the active capitalisation
        window (IAS 23.17-25).

        The window runs from ``commencement_date`` (falling back to
        ``period_start`` then the earliest expenditure date) to
        ``cessation_date`` (falling back to ``period_end``), with suspension
        spans removed. Each dated expenditure is weighted by the active days
        it is outstanding within the window, over the full period length so
        the rate is applied consistently with the flat case. Flat
        ``general_expenditure`` is scaled by the active fraction of the
        period.
        """
        self.ensure_one()
        lines = self.expenditure_line_ids.filtered(lambda l: l.date)
        starts = [line.date for line in lines]
        earliest = min(starts) if starts else None
        period_start = self.commencement_date or self.period_start or earliest
        period_end = self.cessation_date or self.period_end
        if not period_start or not period_end:
            # Not enough dating to form a window; nothing to apportion.
            return 0.0
        # Denominator: the full nominal reporting period.
        nominal_start = self.period_start or self.commencement_date or earliest
        nominal_end = self.period_end or period_end
        total_days = (nominal_end - nominal_start).days + 1
        if total_days <= 0:
            return 0.0
        # Clamp the active window INSIDE the reporting period. Capitalisation
        # for days before the period start (an earlier commencement) or after
        # the period end belongs to other periods, so the active window can
        # never exceed the nominal period and windowing only ever reduces the
        # base relative to the flat case (never inflates it).
        if nominal_start and period_start < nominal_start:
            period_start = nominal_start
        if nominal_end and period_end > nominal_end:
            period_end = nominal_end
        if period_end < period_start:
            return 0.0
        spans = self._active_spans(period_start, period_end)
        if not spans:
            return 0.0
        if not lines:
            active_days = sum((hi - lo).days for lo, hi in spans)
            return self.general_expenditure * active_days / total_days
        base = 0.0
        for line in lines:
            outstanding_days = 0
            for lo, hi in spans:
                # Expenditure is outstanding from its date onward; count the
                # active days within each span that fall on or after it.
                out_from = max(line.date, lo)
                if hi > out_from:
                    outstanding_days += (hi - out_from).days
            if outstanding_days:
                base += line.amount * outstanding_days / total_days
        return base

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for incoming in vals_list:
            vals = dict(incoming)
            if 'actual_borrowing_cost' in vals:
                # Float/Monetary fields collapse False and 0.0. Preserve the
                # caller's explicit presence separately so a stated zero is a
                # real IAS 23.14 cap, while untouched legacy blanks remain
                # uncapped until the period cost is known.
                vals['apply_actual_borrowing_cost_cap'] = True
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id,
                ).exists()
                if not company:
                    raise ValidationError(_(
                        "Select an existing company for the borrowing cost."
                    ))
                if not self.env.su and company not in self.env.companies:
                    raise AccessError(_(
                        "The borrowing-cost company is outside your allowed "
                        "companies."
                    ))
                sequence_date = fields.Date.to_date(
                    vals.get('period_end') or fields.Date.context_today(self)
                )
                sequence = self._eh_company_sequence(company)
                vals['name'] = sequence.with_context(
                    ir_sequence_date=sequence_date,
                ).next_by_id(sequence_date=sequence_date) or '/'
            prepared.append(vals)
        return super().create(prepared)

    @api.model
    def _eh_company_sequence(self, company):
        """Return one race-safe counter for this company and period year."""
        company.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (company.id,),
        )
        Sequence = self.env['ir.sequence'].sudo().with_context(
            active_test=False,
        )
        sequence = Sequence.search([
            ('code', '=', 'eh.borrowing.cost'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not sequence:
            sequence = Sequence.create({
                'name': _(
                    "EH Borrowing Cost Reference - %s",
                    company.display_name,
                ),
                'code': 'eh.borrowing.cost',
                'prefix': 'BORR/%s/%%(year)s/' % company.id,
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
        return sequence

    def _eh_engine_write(self, vals):
        return self.sudo().with_context(**{
            BORROWING_MOVE_CONTEXT: BORROWING_MOVE_CAPABILITY,
        }).write(vals)

    # Inputs to the capitalisable amount. Once the period has been capitalised
    # they are frozen: editing them would recompute the capitalisable figure
    # away from the amount already posted to the qualifying asset (IAS 23).
    _FROZEN_AFTER_CAPITALISED = (
        'specific_borrowing_cost', 'temporary_investment_income',
        'general_expenditure', 'capitalisation_rate', 'actual_borrowing_cost',
        'apply_actual_borrowing_cost_cap', 'capitalisation_rate_basis',
        'period_start', 'period_end',
        'accounting_date', 'posted_accounting_date',
        'commencement_date', 'cessation_date',
        'name', 'qualifying_asset', 'company_id', 'asset_account_id',
        'borrowing_cost_account_id', 'journal_id', 'expenditure_line_ids',
        'suspension_line_ids',
        'weighted_average_base', 'capitalisable', 'uncapped_amount',
        'currency_id', 'move_id', 'reversal_move_id',
        'posted_reversal_date',
    )

    def write(self, vals):
        vals = dict(vals)
        internal = (
            self.env.context.get(BORROWING_MOVE_CONTEXT)
            is BORROWING_MOVE_CAPABILITY
        )
        if not internal and 'actual_borrowing_cost' in vals:
            vals['apply_actual_borrowing_cost_cap'] = True
        transition_sensitive = bool(
            set(vals) & (
                set(self._FROZEN_AFTER_CAPITALISED)
                | {'integrity_review_note'}
            )
        )
        if not internal and transition_sensitive:
            self._eh_lock_transition()
        frozen = [] if internal else [
            f for f in self._FROZEN_AFTER_CAPITALISED if f in vals
        ]
        if frozen:
            posted = self.filtered(lambda c: c.state == 'capitalised')
            posted |= self.filtered(lambda c: c.state == 'reversed')
            if posted:
                raise UserError(_(
                    "Measurement inputs (%(fields)s) are frozen once the "
                    "period is capitalised; the capitalisable amount must "
                    "equal the entry already posted. Reverse the entry to "
                    "re-measure (IAS 23).",
                    fields=', '.join(frozen)))
        if 'integrity_review_note' in vals:
            reviewed = self.filtered('integrity_reviewed_by_id')
            if reviewed:
                raise UserError(_(
                    "A completed legacy integrity attestation is immutable."
                ))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_transition()
        if self.filtered(lambda record: record.move_ids):
            raise UserError(_(
                "A borrowing-cost period with journal history cannot be "
                "deleted; its "
                "sealed journal evidence would be orphaned."
            ))
        return super().unlink()

    def action_capitalise(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can capitalise borrowing "
                "costs."))
        self._eh_lock_transition()
        self._check_company()
        self._eh_validate_posting_references()
        self = self._eh_workflow_action()
        if self.state != 'draft':
            raise UserError(_("Only a draft record can be capitalised."))
        if not self.journal_id or not self.asset_account_id \
                or not self.borrowing_cost_account_id:
            raise UserError(_(
                "Configure the journal, qualifying asset account and "
                "borrowing cost account first."))
        # Stored computes are cache/reporting artifacts, not trusted inputs.
        # Re-derive them while the source row is locked immediately before
        # the amount is consumed by the journal entry.
        self._compute_capitalisable()
        self.flush_recordset([
            'weighted_average_base', 'uncapped_amount', 'capitalisable',
        ])
        currency = self.currency_id
        if (
            self.capitalisation_rate_basis == 'annual'
            and self.capitalisation_rate
            and (self.general_expenditure or self.expenditure_line_ids)
            and not (
                self.period_start
                or self.expenditure_line_ids.filtered(lambda line: line.date)
            )
        ):
            raise UserError(_(
                "Set Period Start before applying an annual capitalisation "
                "rate to general borrowing expenditure."
            ))
        amount = currency.round(self.capitalisable)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "There is no capitalisable borrowing cost for this period."))
        if self.eh_integrity_review_required:
            raise UserError(_(
                "Legacy journal provenance on %s requires controlled review "
                "before another accounting transition.", self.display_name,
            ))
        posting_date = self.accounting_date or self.period_end
        if posting_date < self.period_end:
            raise UserError(_(
                "Capitalisation accounting date cannot precede period end."
            ))
        # Odoo 16/17 changes a locked journal date inside core posting.  A
        # verified sealed draft correctly rejects that intermediate mutation,
        # but the generic immutable-evidence error then hides the actionable
        # lock-date policy.  Ask core's own lock resolver before creating any
        # move, so a caller which catches this UserError cannot retain a draft
        # prefix and every series requires the same explicit open date.
        lock_probe = self.env['account.move'].new({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'journal_id': self.journal_id.id,
            'date': posting_date,
        })
        violated_locks = lock_probe._get_violated_lock_dates(
            posting_date, False,
        )
        if violated_locks:
            first_open_date = violated_locks[-1][0] + datetime.timedelta(
                days=1,
            )
            raise UserError(_(
                "Odoo would move the capitalisation date from %(requested)s "
                "to %(posted)s because the requested period is locked. "
                "Choose an explicit open accounting date and post again.",
                requested=posting_date,
                posted=first_open_date,
            ))
        move = self.env['account.move'].with_context(**{
            BORROWING_MOVE_CONTEXT: BORROWING_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': posting_date,
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_borrowing_cost_id': self.id,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': _("Capitalised borrowing cost %s", self.name),
                    'account_id': self.asset_account_id.id,
                    'debit': amount, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Reclassify interest to asset %s", self.name),
                    'account_id': self.borrowing_cost_account_id.id,
                    'debit': 0.0, 'credit': amount}),
            ],
        })
        move.action_post()
        move.invalidate_recordset(['date', 'state'])
        if move.state != 'posted' or move.date != posting_date:
            raise UserError(_(
                "Odoo moved the capitalisation date from %(requested)s to "
                "%(posted)s because the requested period is locked. Choose "
                "an explicit open accounting date and post again.",
                requested=posting_date,
                posted=move.date,
            ))
        self._eh_engine_write({
            'state': 'capitalised',
            'move_id': move.id,
            'reversal_move_id': False,
            'posted_accounting_date': move.date,
        })
        return True

    def _eh_validate_posting_references(self):
        """Enforce account/journal policy server-side before elevation."""
        self.ensure_one()
        if not self.env.su:
            for reference in (
                self.journal_id,
                self.asset_account_id,
                self.borrowing_cost_account_id,
            ):
                reference._eh_check_access('read')
            if self.company_id.id not in self.env.companies.ids:
                raise AccessError(_(
                    "The borrowing-cost company is outside your allowed "
                    "companies."
                ))
        if self.journal_id and self.journal_id.type != 'general':
            raise UserError(_(
                "Borrowing-cost capitalisation requires a general journal."
            ))
        if self.asset_account_id and (
            self.asset_account_id.account_type not in _QUALIFYING_ASSET_TYPES
        ):
            raise UserError(_(
                "The qualifying-asset account must be a current, non-current "
                "or fixed-asset account."
            ))
        if self.borrowing_cost_account_id and (
            self.borrowing_cost_account_id.account_type
            not in _BORROWING_COST_ACCOUNT_TYPES
        ):
            raise UserError(_(
                "The borrowing-cost account must be an expense account "
                "(including Other Expenses)."
            ))
        if (
            self.asset_account_id
            and self.asset_account_id == self.borrowing_cost_account_id
        ):
            raise UserError(_(
                "Asset and borrowing-cost accounts must be different."
            ))

    def _eh_lock_transition(self):
        """Serialize lifecycle actions before rechecking state and posting."""
        self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_borrowing_cost WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        return self

    def action_cancel(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can cancel borrowing-cost "
                "periods."
            ))
        self._eh_lock_transition()
        self = self._eh_workflow_action()
        for c in self:
            if c.state != 'draft':
                raise UserError(_(
                    "Only a draft borrowing-cost period can be cancelled. "
                    "Reverse and reset posted evidence first (%s).",
                    c.display_name))
            c.state = 'cancelled'

    def _eh_move_signature(self, move):
        self.ensure_one()
        return sorted(
            (
                line.account_id.id,
                self.currency_id.round(line.debit),
                self.currency_id.round(line.credit),
            )
            for line in move.line_ids
            if not self.currency_id.is_zero(line.debit - line.credit)
        )

    def _eh_expected_signature(self):
        self.ensure_one()
        amount = self.currency_id.round(self.capitalisable)
        return sorted([
            (self.asset_account_id.id, amount, 0.0),
            (self.borrowing_cost_account_id.id, 0.0, amount),
        ])

    def _eh_proves_original_move(self, move, require_link=True):
        self.ensure_one()
        return bool(
            move
            and move.state == 'posted'
            and move.eh_sealed
            and not move.eh_legacy_unverified_seal
            and move.company_id == self.company_id
            and move.journal_id == self.journal_id
            and move.date == (
                self.posted_accounting_date
                or self.accounting_date
                or self.period_end
            )
            and not move.reversed_entry_id
            and (
                not require_link
                or move.eh_borrowing_cost_id == self
            )
            and self._eh_move_signature(move)
            == self._eh_expected_signature()
        )

    def _eh_validate_original_move(self):
        self.ensure_one()
        if not self.env.su:
            self.move_id._eh_check_access('read')
        if not self._eh_proves_original_move(self.move_id):
            raise UserError(_(
                "The capitalisation entry for %s failed sealed provenance "
                "validation and will not be reversed.", self.display_name,
            ))

    def _eh_validate_reversal_move(self, reversal, expected_date=None):
        self.ensure_one()
        provenance_date = expected_date or self.posted_reversal_date
        expected = sorted(
            (account_id, credit, debit)
            for account_id, debit, credit in self._eh_expected_signature()
        )
        if (
            not reversal
            or reversal.state != 'posted'
            or not reversal.eh_sealed
            or reversal.eh_legacy_unverified_seal
            or reversal.eh_borrowing_cost_id != self
            or reversal.reversed_entry_id != self.move_id
            or reversal.company_id != self.company_id
            or reversal.journal_id != self.journal_id
            or (provenance_date and reversal.date != provenance_date)
            or self._eh_move_signature(reversal) != expected
        ):
            raise UserError(_(
                "The borrowing-cost reversal failed sealed provenance "
                "validation."
            ))

    def _eh_validate_legacy_history_for_release(self):
        """Prove every linked historical entry before clearing quarantine."""
        self.ensure_one()
        history = self.move_ids
        if history:
            history.sudo(False)._eh_check_access('read')
        invalid_common = history.filtered(lambda move: (
            move.state != 'posted'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
            or move.company_id != self.company_id
            or move.journal_id != self.journal_id
            or move.eh_borrowing_cost_id != self
        ))
        if invalid_common:
            raise UserError(_(
                "Legacy journal history contains an unposted, unverified, "
                "foreign-scope, or ambiguously linked entry. Quarantine "
                "cannot be released."
            ))
        originals = history.filtered(lambda move: not move.reversed_entry_id)
        reversals = history - originals
        for original in originals:
            matching = reversals.filtered(
                lambda reversal: reversal.reversed_entry_id == original
            )
            if original == self.move_id and self.state == 'capitalised':
                if matching:
                    raise UserError(_(
                        "Current capitalisation already has a reversal; "
                        "source state is inconsistent."
                    ))
                continue
            if len(matching) != 1:
                raise UserError(_(
                    "Each superseded capitalisation must have exactly one "
                    "sealed reversal before quarantine can be released."
                ))
            source_signature = sorted(
                (account_id, credit, debit)
                for account_id, debit, credit
                in self._eh_move_signature(original)
            )
            if self._eh_move_signature(matching) != source_signature:
                raise UserError(_(
                    "A legacy reversal does not exactly offset its source "
                    "capitalisation."
                ))
        if reversals.filtered(
            lambda reversal: reversal.reversed_entry_id not in originals
        ):
            raise UserError(_(
                "Legacy reversal history points outside this borrowing-cost "
                "record."
            ))
        if self.state == 'capitalised':
            if not self.move_id or not self._eh_proves_original_move(
                self.move_id
            ):
                raise UserError(_(
                    "Current capitalisation does not match source "
                    "measurement and sealed provenance."
                ))
        elif self.state == 'reversed':
            self._eh_validate_original_move()
            self._eh_validate_reversal_move(self.reversal_move_id)
        elif self.move_id or self.reversal_move_id:
            raise UserError(_(
                "Draft or cancelled legacy records cannot retain current "
                "journal pointers."
            ))
        return True

    def action_release_integrity_review(self):
        """Release only legacy evidence that can be proved from current GL."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can release a legacy "
                "integrity review."
            ))
        self._eh_lock_transition()
        if not self.eh_integrity_review_required:
            raise UserError(_("This record is not under integrity review."))
        if not (self.integrity_review_note or '').strip():
            raise UserError(_(
                "Enter a review attestation before releasing quarantine."
            ))
        self._check_company()
        bad_lines = self.expenditure_line_ids.filtered(lambda line: (
            line.company_id != self.company_id
            or line.currency_id != self.currency_id
        ))
        bad_suspensions = self.suspension_line_ids.filtered(
            lambda span: span.company_id != self.company_id
        )
        if bad_lines or bad_suspensions:
            raise UserError(_(
                "Legacy child rows still have a company or currency scope "
                "mismatch. Correct them before release."
            ))
        if self.move_id and not self.move_id.eh_borrowing_cost_id:
            if not self._eh_proves_original_move(
                self.move_id, require_link=False,
            ):
                raise UserError(_(
                    "Legacy capitalisation cannot be linked because its "
                    "sealed signature does not match source measurement."
                ))
            self.move_id.with_context(**{
                BORROWING_MOVE_CONTEXT: BORROWING_MOVE_CAPABILITY,
            })._eh_write_sealed_metadata(
                {'eh_borrowing_cost_id': self.id},
                {'eh_borrowing_cost_id'},
            )
            self.move_id.invalidate_recordset(['eh_borrowing_cost_id'])
            self.invalidate_recordset(['move_ids'])
        self._eh_validate_legacy_history_for_release()
        reviewer = self.env.user
        reviewed_at = fields.Datetime.now()
        self._eh_engine_write({
            'eh_integrity_review_required': False,
            'eh_integrity_review_reason': False,
            'integrity_reviewed_by_id': reviewer.id,
            'integrity_reviewed_at': reviewed_at,
        })
        self.message_post(body=_(
            "Legacy integrity quarantine released by %(reviewer)s after "
            "sealed journal provenance and child scope were re-proved. "
            "Attestation: %(note)s",
            reviewer=reviewer.display_name,
            note=self.integrity_review_note,
        ))
        return True

    def action_reverse(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can reverse borrowing costs."
            ))
        self._eh_lock_transition()
        if self.state != 'capitalised':
            raise UserError(_(
                "Only a capitalised borrowing-cost period can be reversed."
            ))
        if self.eh_integrity_review_required:
            raise UserError(_(
                "Legacy journal provenance on %s requires controlled review.",
                self.display_name,
            ))
        self._eh_validate_original_move()
        original = self.move_id
        reversal_date = fields.Date.to_date(
            self.reversal_date or fields.Date.context_today(self)
        )
        if reversal_date < original.date:
            raise UserError(_(
                "Reversal date %(reversal)s cannot precede the original "
                "capitalisation date %(original)s.",
                reversal=reversal_date,
                original=original.date,
            ))
        lock_probe = self.env['account.move'].new({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'journal_id': self.journal_id.id,
            'date': reversal_date,
        })
        violated_locks = lock_probe._get_violated_lock_dates(
            reversal_date, False,
        )
        if violated_locks:
            first_open_date = violated_locks[-1][0] + datetime.timedelta(
                days=1,
            )
            raise UserError(_(
                "Odoo would move the reversal date from %(requested)s to "
                "%(posted)s because the requested period is locked. Choose "
                "an explicit open reversal date and try again.",
                requested=reversal_date,
                posted=first_open_date,
            ))
        reversal = self.env['account.move'].with_context(**{
            BORROWING_MOVE_CONTEXT: BORROWING_MOVE_CAPABILITY,
        })._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': reversal_date,
            'journal_id': self.journal_id.id,
            'ref': _("Reversal of %s", self.name),
            'eh_borrowing_cost_id': self.id,
            'reversed_entry_id': original.id,
            'eh_sealed': True,
            'line_ids': [(0, 0, {
                'name': _("Reversal: %s", line.name or self.name),
                'account_id': line.account_id.id,
                'debit': line.credit,
                'credit': line.debit,
            }) for line in original.line_ids],
        })
        reversal.action_post()
        reversal.invalidate_recordset(['date', 'state'])
        if reversal.state != 'posted' or reversal.date != reversal_date:
            raise UserError(_(
                "Odoo moved the reversal date from %(requested)s to "
                "%(posted)s. Choose an explicit open reversal date and try "
                "again.",
                requested=reversal_date,
                posted=reversal.date,
            ))
        self._eh_validate_reversal_move(
            reversal, expected_date=reversal_date,
        )
        self._eh_engine_write({
            'state': 'reversed', 'reversal_move_id': reversal.id,
            'posted_reversal_date': reversal.date,
        })
        return True

    def action_reset_to_draft(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can reopen borrowing costs."
            ))
        self._eh_lock_transition()
        if self.state not in ('reversed', 'cancelled'):
            raise UserError(_(
                "Only a cancelled or fully reversed borrowing-cost period "
                "can be reopened."
            ))
        if self.state == 'reversed':
            self._eh_validate_original_move()
            self._eh_validate_reversal_move(self.reversal_move_id)
        elif self.move_id or self.reversal_move_id:
            raise UserError(_(
                "A cancelled period with journal pointers cannot be reopened."
            ))
        self._eh_engine_write({
            'state': 'draft', 'move_id': False, 'reversal_move_id': False,
            'posted_accounting_date': False, 'reversal_date': False,
            'posted_reversal_date': False,
        })
        return True

    def action_view_move(self):
        self.ensure_one()
        self._eh_check_access('read')
        if not self.move_id:
            raise UserError(_("No entry has been posted yet."))
        self.move_id._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_view_moves(self):
        self.ensure_one()
        self._eh_check_access('read')
        self.move_ids._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Borrowing-cost Journal History"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_borrowing_cost_id', '=', self.id)],
        }


class EhBorrowingCostLine(models.Model):
    _name = 'eh.borrowing.cost.line'
    _description = "Dated expenditure for borrowing cost capitalisation"
    _order = 'date, id'
    _check_company_auto = True

    borrowing_cost_id = fields.Many2one(
        'eh.borrowing.cost', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='borrowing_cost_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='borrowing_cost_id.currency_id', store=True, readonly=True)
    date = fields.Date(
        required=True,
        help="Date the expenditure was incurred. Determines the fraction of "
             "the period it is outstanding for the weighted average.")
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Expenditure on the asset funded from general borrowings.")
    label = fields.Char(string="Description")

    # Dated expenditure drives the weighted-average base, so its measurement
    # fields are frozen once the parent period is capitalised.
    _FROZEN_AFTER_CAPITALISED = ('amount', 'date')

    @staticmethod
    def _frozen_error(fields_):
        return UserError(_(
            "Dated expenditure (%(fields)s) is frozen once the period is "
            "capitalised; it feeds the weighted-average base of an amount "
            "already posted (IAS 23).",
            fields=', '.join(fields_)))

    @api.model_create_multi
    def create(self, vals_list):
        # A line cannot be added to a period that is already capitalised: it
        # would change the weighted-average base behind a posted entry.
        if not self.env.su and any(
                {'company_id', 'currency_id'}.intersection(vals)
                for vals in vals_list):
            raise AccessError(_(
                "Borrowing-cost line company and currency are derived from "
                "the parent and cannot be supplied directly."))
        parent_ids = [v.get('borrowing_cost_id') for v in vals_list
                      if v.get('borrowing_cost_id')]
        if parent_ids:
            parents = _lock_borrowing_cost_parents(self.env, parent_ids)
            posted = parents.filtered(
                lambda c: c.state in _EVIDENCE_STATES)
            if posted:
                raise self._frozen_error(('date', 'amount'))
            if parents.filtered('general_expenditure'):
                raise ValidationError(_(
                    "Use either Undated General Expenditure or Dated "
                    "Expenditure lines, not both."
                ))
        if any(vals.get('amount', 0.0) < 0.0 for vals in vals_list):
            raise ValidationError(_(
                "Dated expenditure amounts cannot be negative."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and {'company_id', 'currency_id'}.intersection(vals):
            raise AccessError(_(
                "Borrowing-cost line company and currency are derived from "
                "the parent and cannot be written directly."))
        parent_ids = self.mapped('borrowing_cost_id').ids
        if vals.get('borrowing_cost_id'):
            parent_ids.append(vals['borrowing_cost_id'])
        parents = _lock_borrowing_cost_parents(self.env, parent_ids)
        frozen = [f for f in self._FROZEN_AFTER_CAPITALISED if f in vals]
        if frozen:
            posted = self.filtered(
                lambda l: l.borrowing_cost_id.state in _EVIDENCE_STATES)
            if posted:
                raise self._frozen_error(frozen)
        # Re-parenting a line into or out of a capitalised period is equally a
        # measurement change behind a posted entry.
        if 'borrowing_cost_id' in vals:
            target = parents.filtered(
                lambda parent: parent.id == vals['borrowing_cost_id'])
            if self.filtered(
                    lambda l: l.borrowing_cost_id.state
                    in _EVIDENCE_STATES) \
                    or target.state in _EVIDENCE_STATES:
                raise self._frozen_error(('borrowing_cost_id',))
            if target.filtered('general_expenditure'):
                raise ValidationError(_(
                    "Use either Undated General Expenditure or Dated "
                    "Expenditure lines, not both."
                ))
        if vals.get('amount', 0.0) < 0.0:
            raise ValidationError(_(
                "Dated expenditure amounts cannot be negative."
            ))
        return super().write(vals)

    def unlink(self):
        _lock_borrowing_cost_parents(
            self.env, self.mapped('borrowing_cost_id').ids)
        posted = self.filtered(
            lambda l: l.borrowing_cost_id.state in _EVIDENCE_STATES)
        if posted:
            raise self._frozen_error(('date', 'amount'))
        return super().unlink()


class EhBorrowingCostSuspension(models.Model):
    _name = 'eh.borrowing.cost.suspension'
    _description = "Suspension span for borrowing cost capitalisation"
    _order = 'date_start, id'
    _check_company_auto = True

    borrowing_cost_id = fields.Many2one(
        'eh.borrowing.cost', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='borrowing_cost_id.company_id', store=True, index=True)
    date_start = fields.Date(
        required=True,
        help="First day active development was suspended (IAS 23.20-21).")
    date_end = fields.Date(
        required=True,
        help="Day active development resumed; capitalisation resumes on this "
             "day (IAS 23.20-21).")
    label = fields.Char(string="Reason")

    _order_constraint = models.Constraint(
        'CHECK (date_end > date_start)',
        "A suspension period must end after it starts.")

    @staticmethod
    def _frozen_error():
        return UserError(_(
            "Suspension periods are frozen once the borrowing cost is "
            "capitalised; they define the active window of an amount already "
            "posted (IAS 23)."))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
                'company_id' in vals for vals in vals_list):
            raise AccessError(_(
                "Suspension company is derived from its borrowing-cost "
                "parent and cannot be supplied directly."))
        parent_ids = [v.get('borrowing_cost_id') for v in vals_list
                      if v.get('borrowing_cost_id')]
        if parent_ids:
            parents = _lock_borrowing_cost_parents(self.env, parent_ids)
            posted = parents.filtered(
                lambda c: c.state in _EVIDENCE_STATES)
            if posted:
                raise self._frozen_error()
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and 'company_id' in vals:
            raise AccessError(_(
                "Suspension company is derived from its borrowing-cost "
                "parent and cannot be written directly."))
        parent_ids = self.mapped('borrowing_cost_id').ids
        if vals.get('borrowing_cost_id'):
            parent_ids.append(vals['borrowing_cost_id'])
        parents = _lock_borrowing_cost_parents(self.env, parent_ids)
        guarded = {'date_start', 'date_end', 'borrowing_cost_id'} & set(vals)
        if guarded and self.filtered(
                lambda s: s.borrowing_cost_id.state in _EVIDENCE_STATES):
            raise self._frozen_error()
        if 'borrowing_cost_id' in vals:
            target = parents.filtered(
                lambda parent: parent.id == vals['borrowing_cost_id'])
            if target.state in _EVIDENCE_STATES:
                raise self._frozen_error()
        return super().write(vals)

    def unlink(self):
        _lock_borrowing_cost_parents(
            self.env, self.mapped('borrowing_cost_id').ids)
        if self.filtered(
                lambda s: s.borrowing_cost_id.state in _EVIDENCE_STATES):
            raise self._frozen_error()
        return super().unlink()
