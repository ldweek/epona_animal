# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 8 operating segments with reconciliation to entity totals."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Parent figures frozen once the report is finalised. Writing any of these on
# a finalised report is refused so a signed-off segment disclosure cannot be
# silently re-keyed. The advisory tie-out fields are computed and never in
# this set, so they still recompute.
_SEGMENT_FROZEN_FIELDS = frozenset({
    'entity_revenue', 'entity_result', 'entity_assets', 'entity_liabilities',
    'period_end',
    'date_from', 'segment_ids', 'company_id',
    'major_customer_line_ids', 'major_customer_threshold_pct',
    'ledger_total_revenue',
})


class EhSegmentReport(models.Model):
    _name = 'eh.segment.report'
    _description = "Operating segment report (IFRS 8)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'period_end desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'total_segment_revenue',
        'total_segment_result', 'total_segment_assets',
        'total_segment_liabilities', 'revenue_reconciliation',
        'result_reconciliation', 'assets_reconciliation',
        'liabilities_reconciliation', 'segments_tie_out',
        'result_tie_out', 'assets_tie_out', 'liabilities_tie_out',
        'reportable_external_revenue', 'reportable_coverage_pct',
        'coverage_75_met',
        'major_customer_count', 'ledger_total_revenue',
    })

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised report is locked: its entity figures and segment "
             "lines cannot be edited or appended. Only a manager can finalise "
             "or reopen it. The advisory tie-out flags still recompute.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_end = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    segment_ids = fields.One2many('eh.segment.line', 'report_id', copy=True)

    date_from = fields.Date(
        help="Optional period start for the ledger tie-out on each segment "
             "line. When set with an analytic account on a line, only posted "
             "move lines on or after this date are counted. Leave empty to "
             "count all dates up to and including the period end.")

    entity_revenue = fields.Monetary(
        currency_field='currency_id',
        help="Total entity revenue, for the reconciliation (IFRS 8.28).")
    entity_result = fields.Monetary(currency_field='currency_id')
    entity_assets = fields.Monetary(currency_field='currency_id')
    entity_liabilities = fields.Monetary(currency_field='currency_id')

    total_segment_revenue = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_segment_result = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_segment_assets = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_segment_liabilities = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    revenue_reconciliation = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Entity revenue less the sum of segment revenue; the "
             "reconciling item to disclose.")
    segments_tie_out = fields.Boolean(
        compute='_compute_totals', store=True,
        help="Advisory: True when the sum of segment revenue equals entity "
             "revenue within currency rounding (IFRS 8.28). Does not block.")
    result_reconciliation = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    assets_reconciliation = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    liabilities_reconciliation = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    result_tie_out = fields.Boolean(compute='_compute_totals', store=True)
    assets_tie_out = fields.Boolean(compute='_compute_totals', store=True)
    liabilities_tie_out = fields.Boolean(
        compute='_compute_totals', store=True)
    reportable_external_revenue = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="External revenue assigned to separately reportable segments.")
    reportable_coverage_pct = fields.Float(
        compute='_compute_totals', store=True, digits=(7, 4),
        help="Reportable-segment external revenue as a share of entity "
             "external revenue (IFRS 8.15).")
    coverage_75_met = fields.Boolean(
        compute='_compute_totals', store=True,
        help="Whether reportable segments cover at least 75% of external "
             "revenue (IFRS 8.15).")

    # --- Major customers (IFRS 8.34) -------------------------------------
    # IFRS 8.34 requires disclosure when revenues from transactions with a
    # single external customer amount to 10 per cent or more of entity
    # revenues: the fact, the total revenue from each such customer, and
    # the segment(s) reporting the revenues. The identity of the customer
    # is NOT required, so the note displays the count and the segment
    # attribution; the partner link is stored for the preparer's working
    # papers only.
    major_customer_threshold_pct = fields.Float(
        digits=(7, 4), default=10.0, string="Major customer threshold (%)",
        help="Revenue share above which a single external customer is a "
             "major customer (IFRS 8.34 sets 10 per cent).")
    major_customer_line_ids = fields.One2many(
        'eh.segment.major.customer', 'report_id', copy=False,
        string="Major customers")
    major_customer_count = fields.Integer(
        compute='_compute_major_customer_count', store=True,
        string="Major customers (count)",
        help="Number of single external customers at or above the "
             "threshold; the figure the note discloses (IFRS 8.34 does not "
             "require customer identities).")
    ledger_total_revenue = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        string="Ledger revenue (period)",
        help="Total posted revenue in the period window the last major "
             "customer computation ran over; the denominator of the "
             "revenue shares.")
    notes = fields.Text()

    _check_major_customer_threshold = models.Constraint(
        'CHECK (major_customer_threshold_pct >= 0 AND '
        'major_customer_threshold_pct <= 100)',
        'Major-customer threshold must be between 0 and 100 percent.',
    )

    @api.depends('major_customer_line_ids')
    def _compute_major_customer_count(self):
        for r in self:
            r.major_customer_count = len(r.major_customer_line_ids)

    def action_compute_major_customers(self):
        """Rebuild the IFRS 8.34 major-customer rows from the ledger.

        Revenue is grouped by the commercial partner of the posted income
        move lines in the report window (date_from, when set, to
        period_end). Every partner whose revenue is at or above the
        threshold share of the total posted revenue produces one row, with
        its revenue, its share, and the reporting segments it attributes to
        (the report's segment lines whose analytic account receives any of
        the partner's revenue). Rows are wiped and rebuilt (all computed:
        the standard's test is arithmetic, not judgement)."""
        self = self._eh_disclosure_prepare_action(engine=True)
        income_types = ('income', 'income_other')
        for report in self:
            if report.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; its major customers "
                    "cannot be recomputed. Ask a manager to reopen it "
                    "first.", report.name))
            currency = report.currency_id or report.company_id.currency_id
            domain = [
                ('company_id', 'in', report._eh_disclosure_company_ids()),
                ('parent_state', '=', 'posted'),
                ('date', '<=', report.period_end),
                ('account_id.account_type', 'in', income_types),
            ]
            if report.date_from:
                domain.append(('date', '>=', report.date_from))
            move_lines = self.env['account.move.line'].search(domain)
            total = 0.0
            by_partner = {}
            partner_segments = {}
            analytic_names = {}
            for segment in report.segment_ids.filtered('analytic_account_id'):
                analytic_names.setdefault(
                    str(segment.analytic_account_id.id), set()
                ).add(segment.name)
            for ml in move_lines:
                # Income magnitude is credit - debit (sign-flipped balance),
                # positive for revenue.
                magnitude = ml.credit - ml.debit
                source_currency = ml.company_id.currency_id
                if source_currency != currency:
                    magnitude = source_currency._convert(
                        magnitude, currency, report.company_id, ml.date,
                    )
                total += magnitude
                partner = ml.partner_id.commercial_partner_id
                if partner:
                    by_partner[partner] = \
                        by_partner.get(partner, 0.0) + magnitude
                    names = partner_segments.setdefault(partner, set())
                    for map_key, percent in (
                            ml.analytic_distribution or {}).items():
                        if magnitude * float(percent or 0.0) <= 0.0:
                            continue
                        for key in str(map_key).split(','):
                            names.update(analytic_names.get(key, ()))
            report.major_customer_line_ids.unlink()
            report.sudo().write({
                'ledger_total_revenue': currency.round(total),
            })
            if currency.is_zero(total) or total <= 0.0:
                continue
            threshold = total * report.major_customer_threshold_pct / 100.0
            Major = self.env['eh.segment.major.customer']
            for partner, revenue in sorted(
                    by_partner.items(), key=lambda i: -i[1]):
                # At or above the threshold ("10 per cent or more",
                # IFRS 8.34), compared at currency precision so an exact
                # 10.00% customer is included.
                if currency.compare_amounts(revenue, threshold) < 0:
                    continue
                Major.create({
                    'report_id': report.id,
                    'partner_id': partner.id,
                    'revenue': currency.round(revenue),
                    'revenue_pct': revenue / total * 100.0,
                    'segment_names': ', '.join(sorted(
                        partner_segments.get(partner, ()))),
                })
        return True

    @staticmethod
    def _partner_analytic_revenue(move_lines, partner, analytic):
        """Revenue of a commercial partner allocated to an analytic account
        within the already-fetched income move lines, pro rata to the
        analytic distribution (the same convention as the segment line
        ledger tie-out)."""
        key = str(analytic.id)
        total = 0.0
        for ml in move_lines:
            if ml.partner_id.commercial_partner_id != partner:
                continue
            pct = EhSegmentLine._distribution_percent(
                ml.analytic_distribution or {}, key)
            if pct:
                total += (ml.credit - ml.debit) * pct / 100.0
        return total

    @api.depends('segment_ids.revenue', 'segment_ids.result',
                 'segment_ids.assets', 'segment_ids.liabilities',
                 'segment_ids.is_reportable', 'entity_revenue',
                 'entity_result', 'entity_assets', 'entity_liabilities')
    def _compute_totals(self):
        for r in self:
            r.total_segment_revenue = sum(r.segment_ids.mapped('revenue'))
            r.total_segment_result = sum(r.segment_ids.mapped('result'))
            r.total_segment_assets = sum(r.segment_ids.mapped('assets'))
            r.total_segment_liabilities = sum(
                r.segment_ids.mapped('liabilities'))
            r.revenue_reconciliation = (
                r.entity_revenue - r.total_segment_revenue)
            r.result_reconciliation = (
                r.entity_result - r.total_segment_result)
            r.assets_reconciliation = (
                r.entity_assets - r.total_segment_assets)
            r.liabilities_reconciliation = (
                r.entity_liabilities - r.total_segment_liabilities)
            currency = r.currency_id or r.company_id.currency_id
            r.segments_tie_out = currency.is_zero(r.revenue_reconciliation)
            r.result_tie_out = currency.is_zero(r.result_reconciliation)
            r.assets_tie_out = currency.is_zero(r.assets_reconciliation)
            r.liabilities_tie_out = currency.is_zero(
                r.liabilities_reconciliation)
            reportable_revenue = sum(
                line.revenue for line in r.segment_ids
                if line.is_reportable
            )
            r.reportable_external_revenue = reportable_revenue
            r.reportable_coverage_pct = (
                reportable_revenue / r.entity_revenue * 100.0
                if r.entity_revenue > 0.0 else 0.0
            )
            r.coverage_75_met = (
                r.entity_revenue <= 0.0
                or r.reportable_coverage_pct >= 75.0
            )

    @api.model_create_multi
    def create(self, vals_list):
        # Creating a report already in the finalised state would skip the
        # manager-gated action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                vals['name'] = self._eh_disclosure_next_sequence(
                    'eh.segment.report', company) or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the entity figures and segment lines once finalised (a
        # signed-off report is frozen for everyone; restate via a
        # manager-gated reopen). The state field itself is owned by the
        # inherited eh.workflow.guard, which refuses any non-superuser direct
        # write; the sanctioned finalise / reopen actions run under sudo.
        if _SEGMENT_FROZEN_FIELDS.intersection(vals):
            for report in self:
                if report.state == 'finalised':
                    raise UserError(_(
                        "Segment report %s is finalised and cannot be edited. "
                        "Ask a manager to reopen it first.", report.name))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "segment report."))

    def unlink(self):
        for report in self:
            if report.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised and cannot be deleted. "
                    "Ask a manager to reopen it first.", report.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the report: figures and lines freeze. Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self.segment_ids._compute_reportable()
        self.segment_ids._compute_ledger()
        self._compute_totals()
        self._compute_major_customer_count()
        self.segment_ids.flush_recordset([
            'is_reportable', 'reportable_reason', 'ledger_revenue',
            'ledger_result', 'revenue_residual', 'result_residual',
            'revenue_tied', 'result_tied',
        ])
        self.flush_recordset([
            'total_segment_revenue', 'total_segment_result',
            'total_segment_assets', 'total_segment_liabilities',
            'revenue_reconciliation', 'result_reconciliation',
            'assets_reconciliation', 'liabilities_reconciliation',
            'segments_tie_out', 'result_tie_out', 'assets_tie_out',
            'liabilities_tie_out', 'reportable_external_revenue',
            'reportable_coverage_pct', 'coverage_75_met',
            'major_customer_count',
        ])
        for report in self:
            if report.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is already finalised.", report.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised report to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhSegmentLine(models.Model):
    _name = 'eh.segment.line'
    _description = "Operating segment line"
    _order = 'report_id, sequence, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'report_id'
    _eh_disclosure_server_fields = frozenset({
        'company_id', 'currency_id', 'is_reportable', 'reportable_reason',
        'ledger_revenue', 'ledger_result', 'revenue_residual',
        'result_residual', 'revenue_tied', 'result_tied',
    })

    report_id = fields.Many2one(
        'eh.segment.report', required=True, ondelete='cascade', index=True,
        check_company=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='report_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='report_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="Reportable segment.")
    revenue = fields.Monetary(currency_field='currency_id')
    inter_segment_revenue = fields.Monetary(currency_field='currency_id')
    result = fields.Monetary(
        currency_field='currency_id',
        help="Segment profit or loss, the measure reviewed by the chief "
             "operating decision maker (IFRS 8.23).")
    assets = fields.Monetary(currency_field='currency_id')
    liabilities = fields.Monetary(currency_field='currency_id')

    # --- Reportable-segment threshold (IFRS 8.13) -----------------------
    # IFRS 8.13 makes a segment separately reportable when it meets any of the
    # 10% quantitative thresholds: its reported revenue (external plus
    # inter-segment) is 10% or more of the combined revenue of all operating
    # segments; the absolute amount of its reported profit or loss is 10% or
    # more of the greater (in absolute amount) of the combined profit of
    # profit-making segments and the combined loss of loss-making segments; or
    # its assets are 10% or more of the combined assets of all segments.
    is_reportable = fields.Boolean(
        compute='_compute_reportable', store=True, string="Reportable",
        help="True when the segment meets any IFRS 8.13 10% quantitative "
             "threshold (revenue, absolute profit or loss, or assets) and is "
             "therefore separately reportable. Advisory: it does not change "
             "the totals or the reconciliation.")
    reportable_reason = fields.Char(
        compute='_compute_reportable', store=True,
        help="Which IFRS 8.13 threshold(s) the segment meets: revenue, "
             "result, and/or assets. Empty when it meets none.")

    @api.depends('revenue', 'inter_segment_revenue', 'result', 'assets',
                 'report_id.segment_ids.revenue',
                 'report_id.segment_ids.inter_segment_revenue',
                 'report_id.segment_ids.result',
                 'report_id.segment_ids.assets')
    def _compute_reportable(self):
        for line in self:
            peers = line.report_id.segment_ids
            # IFRS 8.13(a): total revenue is external plus inter-segment.
            combined_revenue = sum(
                p.revenue + p.inter_segment_revenue for p in peers)
            combined_assets = sum(peers.mapped('assets'))
            # IFRS 8.13(b): the profit/loss base is the greater in absolute
            # amount of the combined profit of profit-making segments and the
            # combined loss of loss-making segments.
            combined_profit = sum(p.result for p in peers if p.result > 0.0)
            combined_loss = sum(-p.result for p in peers if p.result < 0.0)
            result_base = max(combined_profit, combined_loss)

            line_revenue = line.revenue + line.inter_segment_revenue
            reasons = []
            if combined_revenue and \
                    abs(line_revenue) >= 0.10 * abs(combined_revenue):
                reasons.append('revenue')
            if result_base and abs(line.result) >= 0.10 * result_base:
                reasons.append('result')
            if combined_assets and \
                    abs(line.assets) >= 0.10 * abs(combined_assets):
                reasons.append('assets')
            line.is_reportable = bool(reasons)
            line.reportable_reason = ', '.join(reasons)

    # --- Ledger tie-out -------------------------------------------------
    # A reportable segment has a natural ledger counterpart when its activity
    # is tagged to an analytic account. Setting one lets the register derive
    # the segment revenue and result straight from posted moves, so a
    # hand-keyed figure that drifts from the books becomes visible.
    analytic_account_id = fields.Many2one(
        'account.analytic.account', string="Analytic account",
        check_company=True,
        help="Optional. When set, the ledger revenue and result below are "
             "derived from posted journal items whose analytic distribution "
             "allocates to this account, and the entered revenue/result are "
             "tied out against them.")
    ledger_revenue = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Revenue derived from posted income move lines allocated to the "
             "analytic account (positive for income).")
    ledger_result = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Result (income less expense) derived from posted move lines "
             "allocated to the analytic account.")
    revenue_residual = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Entered revenue less the ledger-derived revenue. Zero when the "
             "figure ties to the books.")
    result_residual = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Entered result less the ledger-derived result.")
    revenue_tied = fields.Boolean(
        compute='_compute_ledger', store=True,
        help="True when no analytic account is set (not applicable) or the "
             "entered revenue equals the ledger-derived revenue within "
             "currency rounding. False signals drift from the ledger.")
    result_tied = fields.Boolean(
        compute='_compute_ledger', store=True,
        help="True when no analytic account is set (not applicable) or the "
             "entered result equals the ledger-derived result within "
             "currency rounding.")

    @api.depends('analytic_account_id', 'revenue', 'result',
                 'report_id.period_end', 'report_id.date_from',
                 'report_id.include_branch_companies', 'company_id')
    def _compute_ledger(self):
        pending = self
        for report in self.mapped('report_id'):
            lines = self.filtered(lambda line, r=report: line.report_id == r)
            amounts = lines._derive_ledger_amounts_batch(report)
            for line in lines:
                line._set_ledger_amounts(*amounts.get(line.id, (0.0, 0.0)))
            pending -= lines
        for line in pending:
            line._set_ledger_amounts(0.0, 0.0)

    def _set_ledger_amounts(self, ledger_rev, ledger_res):
        self.ensure_one()
        currency = (self.currency_id or self.company_id.currency_id
                    or self.report_id.company_id.currency_id)
        self.ledger_revenue = ledger_rev
        self.ledger_result = ledger_res
        rev_res = self.revenue - ledger_rev
        res_res = self.result - ledger_res
        if currency:
            rev_res = currency.round(rev_res)
            res_res = currency.round(res_res)
        self.revenue_residual = rev_res
        self.result_residual = res_res
        if not self.analytic_account_id:
            self.revenue_tied = True
            self.result_tied = True
        else:
            self.revenue_tied = currency.is_zero(rev_res) \
                if currency else rev_res == 0.0
            self.result_tied = currency.is_zero(res_res) \
                if currency else res_res == 0.0

    def _derive_ledger_amounts_batch(self, report):
        """Return per-line amounts with one posted-ledger scan per report."""
        result = {line.id: [0.0, 0.0] for line in self}
        analytic_lines = {}
        for line in self.filtered('analytic_account_id'):
            analytic_lines.setdefault(
                str(line.analytic_account_id.id), []
            ).append(line)
        if not analytic_lines or not report.period_end:
            return result
        Line = self.env['account.move.line']
        if 'analytic_distribution' not in Line._fields:
            return result
        income_types = ('income', 'income_other')
        expense_types = ('expense', 'expense_other', 'expense_depreciation',
                         'expense_direct_cost')
        domain = [
            ('company_id', 'in', report._eh_disclosure_company_ids()),
            ('parent_state', '=', 'posted'),
            ('date', '<=', report.period_end),
            ('account_id.account_type', 'in', income_types + expense_types),
        ]
        if report.date_from:
            domain.append(('date', '>=', report.date_from))
        target_currency = report.currency_id or report.company_id.currency_id
        for move_line in Line.search(domain):
            magnitude = move_line.credit - move_line.debit
            source_currency = move_line.company_id.currency_id
            if source_currency != target_currency:
                magnitude = source_currency._convert(
                    magnitude, target_currency, report.company_id,
                    move_line.date,
                )
            account_type = move_line.account_id.account_type
            for map_key, percent in (
                    move_line.analytic_distribution or {}).items():
                weighted = magnitude * float(percent or 0.0) / 100.0
                if not weighted:
                    continue
                for key in str(map_key).split(','):
                    for line in analytic_lines.get(key, ()):
                        if account_type in income_types:
                            result[line.id][0] += weighted
                        result[line.id][1] += weighted
        return result

    def _derive_ledger_amounts(self):
        """Return (ledger_revenue, ledger_result) for this line from posted
        move lines whose analytic distribution allocates to the line's
        analytic account.

        Revenue is the income-side magnitude (credit - debit on income
        accounts, positive for income). Result is income less expense, i.e.
        the sign-flipped balance of income and expense lines together
        (credit - debit), positive for a profit. Each move line contributes
        only the percentage the analytic distribution allocates to this
        account, so a line split across segments counts pro rata."""
        self.ensure_one()
        report = self.report_id
        return tuple(self._derive_ledger_amounts_batch(report)[self.id])

    @staticmethod
    def _distribution_percent(distribution, account_key):
        """Return the total percentage an analytic_distribution map allocates
        to a single analytic account id (as a string).

        A distribution key is either that id on its own or a comma-separated
        set of ids that includes it (a multi-plan allocation); in either case
        the whole percentage attaches to each id in the key, so a key that
        contains the account contributes its full percent."""
        total = 0.0
        for map_key, percent in (distribution or {}).items():
            parts = str(map_key).split(',')
            if account_key in parts:
                total += float(percent or 0.0)
        return total

    @api.model_create_multi
    def create(self, vals_list):
        # A create-append hole silently moves the parent totals, so appending
        # a line to a finalised report is refused (create guard is required).
        reports = self.env['eh.segment.report'].browse([
            v.get('report_id') for v in vals_list if v.get('report_id')])
        for report in reports:
            if report.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; no line can be added. "
                    "Ask a manager to reopen it first.", report.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.report_id.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; its lines cannot be "
                    "edited. Ask a manager to reopen it first.",
                    line.report_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.report_id.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; its lines cannot be "
                    "removed. Ask a manager to reopen it first.",
                    line.report_id.name))
        return super().unlink()


class EhSegmentMajorCustomer(models.Model):
    _name = 'eh.segment.major.customer'
    _description = "Major customer (IFRS 8.34)"
    _order = 'report_id, revenue desc, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'report_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})
    _eh_disclosure_engine_owned = True

    report_id = fields.Many2one(
        'eh.segment.report', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='report_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='report_id.currency_id', store=True, readonly=True)

    partner_id = fields.Many2one(
        'res.partner', required=True, string="Customer",
        check_company=True,
        help="Commercial partner the revenue was posted against. Working "
             "papers only: IFRS 8.34 does not require the customer's "
             "identity to be disclosed, so the note shows the count and "
             "the segment attribution.")
    revenue = fields.Monetary(
        currency_field='currency_id',
        help="Total posted revenue from the customer in the report window "
             "(IFRS 8.34 requires the total amount per major customer).")
    revenue_pct = fields.Float(
        digits=(7, 4), string="Share of revenue (%)",
        help="Customer revenue as a percentage of total posted revenue in "
             "the window.")
    segment_names = fields.Char(
        string="Reporting segments",
        help="The reportable segment(s) reporting the revenues from this "
             "customer (IFRS 8.34), attributed through the segment lines' "
             "analytic accounts.")

    @api.model_create_multi
    def create(self, vals_list):
        # Create guard on child lines feeding a frozen parent.
        reports = self.env['eh.segment.report'].browse([
            v.get('report_id') for v in vals_list if v.get('report_id')])
        for report in reports:
            if report.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; no major-customer row "
                    "can be added. Ask a manager to reopen it first.",
                    report.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.report_id.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; its major-customer "
                    "rows cannot be edited. Ask a manager to reopen it "
                    "first.", line.report_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.report_id.state == 'finalised':
                raise UserError(_(
                    "Segment report %s is finalised; its major-customer "
                    "rows cannot be removed. Ask a manager to reopen it "
                    "first.", line.report_id.name))
        return super().unlink()
