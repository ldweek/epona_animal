# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.soce / eh.soce.line: statement of changes in equity (IAS 1.106-108).

Each line is one equity component; its closing balance is the opening balance
plus profit, other comprehensive income, issues of shares and other changes,
less dividends. The header totals across components.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

from . import presentation

# Account types that make up total equity for the ledger derivation.
# ``equity_unaffected`` is the current-year-earnings holding account.
_EQUITY_ACCOUNT_TYPES = ('equity', 'equity_unaffected')

# Account types that make up profit or loss for the period. Income accounts
# carry credit-negative balances and expenses debit-positive, so net profit is
# the negated sum of these balances.
# Single source of truth: the presentation module's authoritative P&L type
# set (which includes 'expense_other'). tieout.py imports this name, so
# deriving it from presentation.PL_ACCOUNT_TYPES fixes the tie-out anchor too.
_PL_ACCOUNT_TYPES = tuple(presentation.PL_ACCOUNT_TYPES)


class EhSoce(models.Model):
    _name = 'eh.soce'
    _description = "Statement of changes in equity"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # State is a workflow field: it may only move through this model's own
    # actions (which run under sudo), never a direct RPC/ORM write. The
    # inherited eh.workflow.guard blocks a non-superuser write to it, closing
    # the "RPC-write state=confirmed to skip action_confirm and its GL tie-out
    # checks" bypass. The frozen-figure protection in write() below is a
    # separate, always-on data-integrity control.
    _eh_guarded_fields = (
        'state', 'currency_id', 'total_opening',
        'total_retrospective_adjustment', 'total_restated_opening',
        'total_profit',
        'total_oci', 'total_dividends', 'total_closing', 'consol_run_name',
        'consol_nci_amount', 'consol_nci_available',
        'ledger_closing', 'ledger_derivable', 'closing_tie_out', 'tied',
    ) + presentation.CONSOL_PROVENANCE_FIELDS

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('confirmed', "Confirmed")],
        default='draft', required=True, tracking=True, copy=False)

    line_ids = fields.One2many('eh.soce.line', 'soce_id', copy=True)

    total_opening = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_retrospective_adjustment = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Aggregate effects of retrospective application or restatement "
             "across equity components (IAS 1.106(b)).")
    total_restated_opening = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Opening equity after retrospective application/restatement.")
    total_profit = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_oci = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_dividends = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    dividends_per_share = fields.Monetary(
        string="Dividends per share", currency_field='currency_id',
        help="Aggregate dividends recognised as distributions to owners per "
             "share for the period (IAS 1.107).")
    dividends_disclosure_note = fields.Text(
        string="Dividend disclosure",
        help="Nature, timing and class-level detail supporting total "
             "dividends and dividends per share (IAS 1.107).")
    total_closing = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')

    ledger_closing = fields.Monetary(
        compute='_compute_ledger_tie_out', store=True,
        currency_field='currency_id',
        help="Total equity per the general ledger at period end (posted "
             "equity account balances, credit-positive).")
    ledger_derivable = fields.Boolean(
        compute='_compute_ledger_tie_out', store=True,
        help="True when a ledger closing equity figure can actually be "
             "derived, i.e. at least one posted equity journal item exists "
             "at period end. When false the ledger figure is not meaningful "
             "(it reads zero for the absence of postings, not a genuine nil "
             "balance) and the closing tie-out stays advisory only.")
    closing_tie_out = fields.Monetary(
        compute='_compute_ledger_tie_out', store=True,
        currency_field='currency_id',
        help="Worksheet total closing equity less the ledger closing equity; "
             "should be zero when the statement ties to the GL.")
    tied = fields.Boolean(
        compute='_compute_ledger_tie_out', store=True,
        help="True when the worksheet closing equity ties to the general "
             "ledger within currency rounding.")

    # --- Profit movement tie-out (advisory) ------------------------------
    # The profit taken to equity across components should equal the net
    # profit reported in the statement of comprehensive income / P&L. Leaving
    # ``reported_profit`` zero keeps prior behaviour: profit_ties reads True
    # only when the components carry no profit either.
    reported_profit = fields.Monetary(
        currency_field='currency_id',
        help="Net profit for the period per the statement of comprehensive "
             "income (P&L). Compared to the profit taken to equity across "
             "components as an advisory tie-out.")
    profit_movement_tie_out = fields.Monetary(
        compute='_compute_profit_ties', currency_field='currency_id',
        help="Profit taken to equity across components less the reported net "
             "profit; should be zero.")
    profit_ties = fields.Boolean(
        compute='_compute_profit_ties',
        help="True when the profit taken to equity across components equals "
             "the reported net profit within currency rounding.")

    # ---- IAS 34 interim presentation (presentation only) ----------------
    period_type = fields.Selection(
        presentation.PERIOD_TYPES, default='annual', required=True,
        tracking=True,
        help="Annual keeps the classic IAS 1 presentation. Interim marks "
             "this statement as an IAS 34 interim report: it is labelled as "
             "such and can carry the IAS 34.20 comparatives (comparable "
             "interim period of the immediately preceding financial year "
             "plus the immediately preceding annual period, on the "
             "year-to-date convention). Presentation only; no figure "
             "changes.")
    condensed = fields.Boolean(
        string="Condensed (IAS 34.8)", tracking=True,
        help="IAS 34.8(c) permits a condensed statement of changes in "
             "equity. The per-component roll-forward the worksheet already "
             "carries IS the mandatory minimum for this statement, so the "
             "flag only qualifies the heading. Presentation only.")
    comparative_interim_id = fields.Many2one(
        'eh.soce', string="Prior Interim Comparative", copy=False,
        check_company=True,
        domain="[('period_type', '=', 'interim'),"
               " ('company_id', '=', company_id)]",
        help="Comparable interim period of the immediately preceding "
             "financial year (IAS 34.20(c) year-to-date convention).")
    comparative_annual_id = fields.Many2one(
        'eh.soce', string="Prior Annual Comparative", copy=False,
        check_company=True,
        domain="[('period_type', '=', 'annual'),"
               " ('company_id', '=', company_id)]",
        help="Immediately preceding annual statement (IAS 34.20).")
    presentation_label = fields.Char(
        compute='_compute_presentation_label',
        help="Statement heading derived from the period type: annual, "
             "interim (IAS 34) or condensed interim (IAS 34.8).")

    # ---- IAS 1.60 current / non-current completeness guard --------------
    classification_misfit_note = fields.Text(
        compute='_compute_classification_misfit_note',
        help="Accounts with posted balances at period end whose account "
             "type falls outside the recognised current / non-current / "
             "equity / P&L sets (IAS 1.60). Confirmation is blocked while "
             "this list is non-empty unless the override is set with a "
             "reason.")
    classification_override = fields.Boolean(
        string="Override Classification Check", copy=False, tracking=True,
        help="Confirm despite unclassified account balances (IAS 1.60). "
             "Requires a reason; the override is logged to the chatter.")
    classification_override_reason = fields.Text(
        copy=False,
        help="Mandatory justification when the IAS 1.60 classification "
             "completeness check is overridden.")

    # ---- NCI linkage to a covering consolidation run --------------------
    consol_run_name = fields.Char(
        string="Consolidation Run", readonly=True, copy=False,
        help="The settled consolidation run whose NCI carve-out the "
             "worksheet's NCI component was prefilled from / compared to.")
    consol_nci_amount = fields.Monetary(
        string="NCI per Consolidation Run", readonly=True, copy=False,
        currency_field='currency_id',
        help="Non-controlling interest carve-out of the covering "
             "consolidation run, credit-positive. Reference figure for the "
             "NCI component closing balance.")
    consol_nci_available = fields.Boolean(
        readonly=True, copy=False,
        help="True when a reviewed/closed exact-period, same-currency "
             "consolidation run's closing NCI equity was snapshotted here.")
    consol_run_res_id = fields.Integer(
        string="Consolidation Run ID", readonly=True, copy=False)
    consol_run_state = fields.Selection(
        presentation.CONSOL_SOURCE_STATE_SELECTION, string="Source State",
        readonly=True, copy=False)
    consol_period_start = fields.Date(
        string="Source Period Start", readonly=True, copy=False)
    consol_period_end = fields.Date(
        string="Source Period End", readonly=True, copy=False)
    consol_currency_id = fields.Many2one(
        'res.currency', string="Source Currency", readonly=True, copy=False)
    consol_engine_provenance_version = fields.Integer(
        string="Source Engine Version", readonly=True, copy=False)
    consol_source_move_version = fields.Integer(
        string="Source Ledger Version", readonly=True, copy=False)
    consol_reviewed_at = fields.Datetime(
        string="Source Reviewed At", readonly=True, copy=False)
    consol_reviewed_by_id = fields.Many2one(
        'res.users', string="Source Reviewed By", readonly=True, copy=False)
    nci_component_closing = fields.Monetary(
        compute='_compute_nci_consol_discrepancy',
        currency_field='currency_id',
        help="Closing balance summed across the worksheet's "
             "non-controlling interest component lines.")
    nci_consol_discrepancy = fields.Monetary(
        compute='_compute_nci_consol_discrepancy',
        currency_field='currency_id',
        help="NCI component closing balance less the consolidation run's "
             "NCI carve-out; zero when the two agree.")
    nci_consol_tied = fields.Boolean(
        compute='_compute_nci_consol_discrepancy',
        help="True when the NCI component closing balance equals the "
             "consolidation run's NCI carve-out within currency rounding "
             "(always true while no run figure is available).")

    notes = fields.Text()

    _check_period = models.Constraint(
        'CHECK (period_start <= period_end)',
        'Period start must be on or before period end.')
    _unique_company_period = models.Constraint(
        'UNIQUE (company_id, period_start, period_end)',
        'Only one changes-in-equity statement may exist per company and '
        'period.')

    # Figure inputs frozen once the statement is confirmed. A confirmed
    # primary statement is signed off and must not silently drift from the
    # general ledger; the only way to change a figure is a manager-gated
    # set-to-draft, which unlocks it again (IAS 1.106-108).
    _FROZEN_AFTER_CONFIRM = (
        'reported_profit', 'period_start', 'period_end', 'line_ids',
        'dividends_per_share', 'dividends_disclosure_note',
        'company_id',
        'period_type', 'condensed',
        'comparative_interim_id', 'comparative_annual_id',
        'classification_override', 'classification_override_reason',
    )

    @api.depends('line_ids.opening_balance',
                 'line_ids.retrospective_adjustment', 'line_ids.profit',
                 'line_ids.oci_movement', 'line_ids.dividends',
                 'line_ids.closing_balance')
    def _compute_totals(self):
        for s in self:
            s.total_opening = sum(s.line_ids.mapped('opening_balance'))
            s.total_retrospective_adjustment = sum(
                s.line_ids.mapped('retrospective_adjustment'))
            s.total_restated_opening = (
                s.total_opening + s.total_retrospective_adjustment)
            s.total_profit = sum(s.line_ids.mapped('profit'))
            s.total_oci = sum(s.line_ids.mapped('oci_movement'))
            s.total_dividends = sum(s.line_ids.mapped('dividends'))
            s.total_closing = sum(s.line_ids.mapped('closing_balance'))

    @api.depends('total_closing', 'company_id', 'period_end', 'currency_id')
    def _compute_ledger_tie_out(self):
        for s in self:
            if s.company_id and s.period_end:
                s.ledger_closing = s._equity_balance_at(s.period_end)
                s.ledger_derivable = s._has_ledger_equity_at(s.period_end)
            else:
                s.ledger_closing = 0.0
                s.ledger_derivable = False
            s.closing_tie_out = s.total_closing - s.ledger_closing
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            s.tied = float_is_zero(
                s.closing_tie_out, precision_rounding=rounding or 0.01)

    @api.depends('total_profit', 'reported_profit', 'currency_id')
    def _compute_profit_ties(self):
        for s in self:
            s.profit_movement_tie_out = s.total_profit - s.reported_profit
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            s.profit_ties = float_is_zero(
                s.profit_movement_tie_out, precision_rounding=rounding or 0.01)

    @api.depends('period_type', 'condensed')
    def _compute_presentation_label(self):
        for s in self:
            s.presentation_label = presentation.presentation_label(
                s, _("statement of changes in equity"))

    @api.depends('company_id', 'period_end', 'state')
    def _compute_classification_misfit_note(self):
        for s in self:
            pairs = presentation.classification_misfit_pairs(s)
            s.classification_misfit_note = (
                presentation.format_misfit_note(s, pairs) if pairs
                else False)

    @api.depends('line_ids.component', 'line_ids.closing_balance',
                 'consol_nci_amount', 'consol_nci_available')
    def _compute_nci_consol_discrepancy(self):
        for s in self:
            nci_lines = s.line_ids.filtered(
                lambda l: l.component == 'nci')
            s.nci_component_closing = sum(
                nci_lines.mapped('closing_balance'))
            if not s.consol_nci_available:
                s.nci_consol_discrepancy = 0.0
                s.nci_consol_tied = True
                continue
            s.nci_consol_discrepancy = (
                s.nci_component_closing - s.consol_nci_amount)
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            s.nci_consol_tied = float_is_zero(
                s.nci_consol_discrepancy,
                precision_rounding=rounding or 0.01)

    @api.constrains('period_type', 'condensed', 'comparative_interim_id',
                    'comparative_annual_id', 'period_start', 'company_id')
    def _check_interim_fields(self):
        for s in self:
            presentation.check_interim_fields(s)

    def _pl_net_profit_at(self, date_from, date_to):
        """Net profit per the ledger over the period (credit-positive).

        Sums posted income + expense balances between ``date_from`` and
        ``date_to`` inclusive and negates them, because income accounts carry
        credit-negative balances and expenses debit-positive.
        """
        self.ensure_one()
        company_ids = presentation.statement_ledger_company_ids(self)
        domain = [
            ('company_id', 'in', company_ids),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(_PL_ACCOUNT_TYPES)),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ]
        year_end_move_ids = self._eh_year_end_entry_move_ids()
        if year_end_move_ids:
            domain.append(('move_id', 'not in', year_end_move_ids))
        groups = read_group_compat(
            self.env['account.move.line'], domain, [], ['balance:sum'],
        )
        return -((groups[0][0] or 0.0) if groups else 0.0)

    def _has_ledger_pl_at(self, date_from, date_to):
        """Return whether posted P&L evidence exists for this period."""
        self.ensure_one()
        if not (self.company_id and date_from and date_to):
            return False
        domain = [
            ('company_id', 'in',
             presentation.statement_ledger_company_ids(self)),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(_PL_ACCOUNT_TYPES)),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ]
        year_end_move_ids = self._eh_year_end_entry_move_ids()
        if year_end_move_ids:
            domain.append(('move_id', 'not in', year_end_move_ids))
        return bool(self.env['account.move.line'].search_count(domain))

    def _eh_year_end_entry_move_ids(self):
        """Trusted close/reversal ids when Year-End Closing is installed.

        The optional module's guarded run links are the authoritative
        provenance.  ``sudo`` is confined to those marker links; the journal
        item search itself remains in the statement actor's environment.
        """
        self.ensure_one()
        if 'eh.year.end.run' not in self.env:
            return []
        Run = self.env['eh.year.end.run'].sudo()
        runs = Run.search([(
            'company_id', 'in',
            presentation.statement_ledger_company_ids(self),
        )])
        moves = self.env['account.move'].sudo()
        for field_name in (
            'move_id', 'reversal_move_id',
            'closing_move_ids', 'reversal_move_ids',
        ):
            if field_name in Run._fields:
                moves |= runs.mapped(field_name)
        return moves.ids

    def action_derive_profit_from_ledger(self):
        """Set ``reported_profit`` from posted P&L account balances.

        Net profit = -(sum of income + expense balances) over posted journal
        items dated within the statement period. Advisory: it feeds the
        profit_ties flag but never blocks confirmation.
        """
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            if not (s.company_id and s.period_start and s.period_end):
                continue
            s.reported_profit = s._pl_net_profit_at(
                s.period_start, s.period_end)
        return True

    def _equity_balance_at(self, date_to):
        """Total equity per the ledger at ``date_to`` inclusive.

        Sums posted journal-item balances on equity, unaffected earnings, and
        still-unallocated P&L accounts through ``date_to``.  This is the
        accounting equation's equity stock before a formal close and remains
        correct after a close: the closing entry zeroes P&L while increasing
        retained earnings by the same amount.  The combined balance therefore
        never omits, or double-counts, current-year earnings.
        """
        self.ensure_one()
        company_ids = presentation.statement_ledger_company_ids(self)
        domain = [
            ('company_id', 'in', company_ids),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(
                _EQUITY_ACCOUNT_TYPES + _PL_ACCOUNT_TYPES)),
            ('date', '<=', date_to),
        ]
        groups = read_group_compat(
            self.env['account.move.line'], domain, [], ['balance:sum'],
        )
        return -((groups[0][0] or 0.0) if groups else 0.0)

    def _has_ledger_equity_at(self, date_to):
        """True when a ledger closing equity figure is genuinely derivable.

        ``_equity_balance_at`` returns 0.0 both for a real nil balance and for
        the total absence of any posted equity postings. Only the former is a
        meaningful ledger figure to tie a worksheet against; the latter must
        leave the closing tie-out advisory. This probes whether at least one
        posted equity, unaffected-earnings, or unallocated P&L journal item
        exists for this company up to and including ``date_to``.
        """
        self.ensure_one()
        if not (self.company_id and date_to):
            return False
        company_ids = presentation.statement_ledger_company_ids(self)
        domain = [
            ('company_id', 'in', company_ids),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(
                _EQUITY_ACCOUNT_TYPES + _PL_ACCOUNT_TYPES)),
            ('date', '<=', date_to),
        ]
        return bool(self.env['account.move.line'].search_count(domain))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            companies = self.env['res.company'].browse({
                vals.get('company_id', self.env.company.id)
                for vals in vals_list
            })
            companies._eh_check_access('read')
            if companies - self.env.companies:
                raise AccessError(_(
                    "A changes-in-equity statement must belong to one of "
                    "the actor's allowed companies."
                ))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                sequence_date = vals.get('period_end') or vals.get(
                    'period_start') or fields.Date.context_today(self)
                vals['name'] = self.env['ir.sequence'].with_context(
                    ir_sequence_date=sequence_date,
                ).next_by_code('eh.soce') or '/'
        return super().create(vals_list)

    def write(self, vals):
        self._eh_lock_headers()
        if vals.get('company_id') and not self.env.su:
            company = self.env['res.company'].browse(vals['company_id'])
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "A changes-in-equity statement cannot be moved to a "
                    "company outside the actor's allowed companies."
                ))
        frozen = [f for f in self._FROZEN_AFTER_CONFIRM if f in vals]
        confirmed = self.filtered(lambda s: s.state == 'confirmed')
        # Reopening a confirmed statement (state -> draft) is manager-gated;
        # enforce it here so a raw ORM write cannot bypass action_set_to_draft.
        if confirmed and vals.get('state') == 'draft':
            self._check_manager()
        # Figures on a confirmed statement are frozen (IAS 1). A state-only
        # reopen carries no frozen field and passes; a write touching a frozen
        # figure while any record is still confirmed is always blocked, even if
        # it also flips state to draft in the same call.
        if frozen and confirmed:
            raise UserError(_(
                "Figures on a confirmed statement of changes in equity "
                "are frozen (%(fields)s). Set it back to draft first "
                "(EH Accounting Manager only) to edit it (IAS 1).",
                fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_headers()
        confirmed = self.filtered(lambda s: s.state == 'confirmed')
        if confirmed:
            raise UserError(_(
                "A confirmed statement of changes in equity cannot be "
                "deleted. Set it back to draft first (EH Accounting Manager "
                "only)."))
        return super().unlink()

    def _equity_balance_of_accounts_at(self, accounts, date_to):
        """Equity per the ledger at ``date_to`` for a specific account set.

        Same credit-positive derivation as ``_equity_balance_at`` but scoped
        to the given ``accounts``. Used to split opening equity across the
        SoCE components by their mapped ledger equity accounts.
        """
        self.ensure_one()
        if not accounts:
            return 0.0
        company_ids = presentation.statement_ledger_company_ids(self)
        domain = [
            ('company_id', 'in', company_ids),
            ('parent_state', '=', 'posted'),
            ('account_id', 'in', accounts.ids),
            ('date', '<=', date_to),
        ]
        groups = read_group_compat(
            self.env['account.move.line'], domain, [], ['balance:sum'],
        )
        return -((groups[0][0] or 0.0) if groups else 0.0)

    def action_derive_from_ledger(self):
        """Populate each line's ``opening_balance`` from the general ledger.

        Opening equity is the total equity per the ledger at the day before
        ``period_start``. When lines carry a per-component account mapping
        (``equity_account_ids``), the opening equity is split so each
        component receives the opening balance of exactly its mapped ledger
        equity accounts; any opening equity on accounts mapped to no line
        (a genuine residual, e.g. current-year earnings not yet allocated)
        lands on the fallback line so the worksheet total opening still equals
        the ledger figure. When no line carries a mapping the prior behaviour
        is kept: the whole opening equity lands on the first line and the rest
        are zeroed. The line ``closing_balance`` stays a computed roll-forward
        and the header ``ledger_closing`` / ``tied`` fields show the tie-out.
        """
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            if not (s.company_id and s.period_start and s.line_ids):
                continue
            opening_date = fields.Date.to_date(s.period_start) - timedelta(
                days=1)
            opening_equity = s._equity_balance_at(opening_date)
            mapped_lines = s.line_ids.filtered(lambda l: l.equity_account_ids)
            if not mapped_lines:
                # Backward-compatible path: no per-component account mapping,
                # so the whole opening equity lands on the first line and the
                # remaining lines are zeroed.
                s.line_ids[0].opening_balance = opening_equity
                for line in s.line_ids[1:]:
                    line.opening_balance = 0.0
                continue
            # Per-component path: each mapped line gets the opening balance of
            # exactly its own equity accounts. Any account may only feed one
            # line; opening equity on unmapped accounts is a residual assigned
            # to the fallback line so the worksheet total ties to the ledger.
            claimed = self.env['account.account']
            assigned_total = 0.0
            for line in s.line_ids:
                if not line.equity_account_ids:
                    line.opening_balance = 0.0
                    continue
                line_open = s._equity_balance_of_accounts_at(
                    line.equity_account_ids, opening_date)
                line.opening_balance = line_open
                assigned_total += line_open
                claimed |= line.equity_account_ids
            # Residual opening equity on accounts no line claims goes to the
            # fallback line (first retained-earnings line, else the first
            # line) so total opening equity still equals the ledger figure.
            residual = opening_equity - assigned_total
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            if not float_is_zero(residual, precision_rounding=rounding or 0.01):
                fallback = s.line_ids.filtered(
                    lambda l: l.component == 'retained_earnings')[:1]
                if not fallback:
                    fallback = s.line_ids[:1]
                fallback.opening_balance += residual
        return True

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can confirm or reopen the "
                "statement of changes in equity."))

    def _eh_lock_headers(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_soce WHERE id IN %s ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        self._check_company()
        if not self.env.su and any(
            statement.company_id not in self.env.companies
            for statement in self
        ):
            raise AccessError(_(
                "The changes-in-equity statement company is outside the "
                "actor's allowed companies."
            ))
        return self

    def _eh_lock_for_action(self):
        self._eh_lock_headers()
        lines = self.mapped('line_ids')
        if lines:
            lines.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_soce_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(lines.ids)),),
            )
            lines.invalidate_recordset()
        return self

    def action_prefill_nci_from_consolidation(self):
        """Prefill closing NCI equity from a reviewed exact-period run.

        Soft registry lookup (no hard dependency on the consolidation
        module). The run's NCI carve-out is snapshotted as the reference
        closing-equity reference. A missing NCI component line is created at
        zero, but no closing stock is ever written into ``opening_balance``:
        opening equity must come from prior-period evidence and period
        movements must remain flows. Manual roll-forward figures are kept and
        the closing discrepancy is surfaced.
        """
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            if s.state == 'confirmed':
                raise UserError(_(
                    "Figures on a confirmed statement of changes in equity "
                    "are frozen; set it back to draft before prefilling the "
                    "NCI component."))
            run = presentation.find_covering_consol_run(s)
            if run is None:
                raise UserError(_(
                    "No unique reviewed/closed consolidation run matches "
                    "this company, exact period and currency (or the "
                    "consolidation module is not installed)."))
            carve, provenance = presentation.consol_source_snapshot(
                s, run, 'closing_equity')
            s.sudo().write(dict(provenance, **{
                'consol_nci_amount': carve,
                'consol_nci_available': True,
            }))
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            nci_lines = s.line_ids.filtered(lambda l: l.component == 'nci')
            if not nci_lines:
                s.line_ids = [(0, 0, {
                    'component': 'nci',
                })]
            if not float_is_zero(
                sum(s.line_ids.filtered(
                    lambda line: line.component == 'nci'
                ).mapped('closing_balance')),
                precision_rounding=rounding or 0.01,
            ):
                s.message_post(body=_(
                    "NCI component kept at the manually entered closing of "
                    "%(manual)s; the covering consolidation run %(run)s "
                    "carves out %(carve)s. See the NCI discrepancy "
                    "field.",
                    manual=presentation.format_statement_amount(
                        s, sum(nci_lines.mapped('closing_balance'))),
                    run=run.name,
                    carve=presentation.format_statement_amount(s, carve)))
        return True

    def action_confirm(self):
        self._check_manager()
        self._eh_lock_for_action()
        for source_statement in self:
            presentation.validate_statement_consol_source(
                source_statement, 'closing_equity')
        # Line closing balances and header totals are guarded, stored outputs.
        # Caller manager role, write ACL, record rules and allowed company were
        # proved above; elevate only their action-owned recompute/state write.
        statements = self.sudo()
        statements.line_ids._compute_closing()
        statements._compute_totals()
        statements._compute_ledger_tie_out()
        statements._compute_profit_ties()
        statements._compute_nci_consol_discrepancy()
        statements.line_ids.flush_recordset(['closing_balance'])
        statements.flush_recordset([
            'total_opening', 'total_retrospective_adjustment',
            'total_restated_opening', 'total_profit', 'total_oci',
            'total_dividends',
            'total_closing', 'ledger_closing', 'ledger_derivable',
            'closing_tie_out', 'tied',
        ])
        for s in statements:
            if s.consol_nci_available and not s.nci_consol_tied:
                raise UserError(_(
                    "NCI closing equity does not match the reviewed "
                    "consolidation source: worksheet %(worksheet)s, source "
                    "%(source)s, difference %(diff)s. Complete the NCI "
                    "roll-forward before confirming.",
                    worksheet=s.nci_component_closing,
                    source=s.consol_nci_amount,
                    diff=s.nci_consol_discrepancy,
                ))
            # IAS 1.60 completeness: block while posted balances sit on
            # accounts outside the recognised current / non-current sets,
            # unless a manager override with reason is recorded (logged).
            presentation.check_classification_completeness(
                s, _("statement of changes in equity"))
            # Profit movement tie-out: when posted P&L exists, ledger profit
            # is authoritative even if the preparer left reported_profit at
            # its zero default. Snapshot that ledger anchor and require the
            # component movement to tie. This distinguishes genuine zero
            # profit from omitted input and prevents a default zero bypass.
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            enforce_profit_tie = False
            if s._has_ledger_pl_at(s.period_start, s.period_end):
                ledger_profit = s._pl_net_profit_at(
                    s.period_start, s.period_end)
                s.reported_profit = ledger_profit
                s._compute_profit_ties()
                enforce_profit_tie = True
            elif not float_is_zero(
                s.reported_profit, precision_rounding=rounding or 0.01,
            ):
                enforce_profit_tie = True
            if enforce_profit_tie and not s.profit_ties:
                raise UserError(_(
                    "Profit movement does not tie out: the profit taken to "
                    "equity across components (%(components)s) must equal "
                    "the reported net profit (%(reported)s). Residual is "
                    "%(residual)s.",
                    components=s.total_profit,
                    reported=s.reported_profit,
                    residual=s.profit_movement_tie_out))
            # SoCE closing tie-out to the general ledger: conditional-blocking,
            # mirroring the SoCI attribution gate above. When a ledger closing
            # equity figure is genuinely derivable (at least one posted equity
            # journal item exists at period end) the worksheet closing equity
            # must tie to it within currency rounding, otherwise confirmation
            # is blocked. A manager may still override by not deriving the
            # ledger figure, but where the figure exists it is authoritative.
            # When no ledger figure is derivable the behaviour stays advisory:
            # a chatter warning only, never a block.
            if s.line_ids and not s.tied:
                if s.ledger_derivable:
                    raise UserError(_(
                        "Statement of changes in equity does not tie to the "
                        "general ledger: worksheet closing equity is "
                        "%(worksheet)s but the ledger shows %(ledger)s "
                        "(difference %(diff)s). Reconcile the worksheet to the "
                        "ledger before confirming.",
                        worksheet=s.total_closing,
                        ledger=s.ledger_closing,
                        diff=s.closing_tie_out))
                s.message_post(body=_(
                    "Statement of changes in equity does not tie to the "
                    "general ledger: worksheet closing equity is %(worksheet)s "
                    "but the ledger shows %(ledger)s (difference %(diff)s).",
                    worksheet=s.total_closing,
                    ledger=s.ledger_closing,
                    diff=s.closing_tie_out))
        # The state write runs under sudo so it passes the inherited
        # eh.workflow.guard (env.su, not a forgeable context key); the real
        # env.user is preserved for the audit stamps.
        statements.write({'state': 'confirmed'})

    def action_set_to_draft(self):
        self._check_manager()
        self._eh_lock_for_action()
        self.sudo().write({'state': 'draft'})


class EhSoceLine(models.Model):
    _name = 'eh.soce.line'
    _description = "Statement of changes in equity line"
    _order = 'soce_id, sequence, id'
    _check_company_auto = True

    _SERVER_OWNED_FIELDS = frozenset({
        'company_id', 'currency_id', 'closing_balance',
        'restated_opening_balance', 'oci_analysis_total',
        'oci_analysis_residual',
    })

    soce_id = fields.Many2one(
        'eh.soce', required=True, ondelete='cascade', index=True,
        check_company=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='soce_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='soce_id.currency_id', store=True, readonly=True)

    component = fields.Selection(
        [('share_capital', "Share capital"),
         ('share_premium', "Share premium"),
         ('retained_earnings', "Retained earnings"),
         ('revaluation_reserve', "Revaluation reserve"),
         ('oci_reserve', "Other reserves (OCI)"),
         ('translation_reserve', "Translation reserve"),
         ('nci', "Non-controlling interests"),
         ('other', "Other")],
        default='retained_earnings', required=True)
    equity_account_ids = fields.Many2many(
        'account.account', 'eh_soce_line_account_rel',
        'line_id', 'account_id',
        string="Ledger equity accounts",
        check_company=True,
        domain=[('account_type', 'in', list(_EQUITY_ACCOUNT_TYPES))],
        help="The ledger equity accounts this component maps to. When set, "
             "action_derive_from_ledger attributes each component its own "
             "opening balance from exactly these accounts, instead of dumping "
             "the whole opening equity onto the first line.")

    opening_balance = fields.Monetary(currency_field='currency_id')
    retrospective_adjustment = fields.Monetary(
        string="Retrospective application/restatement",
        currency_field='currency_id',
        help="Effect on this component's opening balance of retrospective "
             "application or retrospective restatement (IAS 1.106(b)).")
    restated_opening_balance = fields.Monetary(
        string="Restated opening", compute='_compute_restated_opening',
        store=True, currency_field='currency_id')
    profit = fields.Monetary(
        currency_field='currency_id',
        help="Profit or loss for the period attributed to this component.")
    oci_movement = fields.Monetary(
        currency_field='currency_id',
        help="Other comprehensive income attributed to this component.")
    oci_revaluation = fields.Monetary(
        string="OCI: revaluation", currency_field='currency_id')
    oci_fvoci = fields.Monetary(
        string="OCI: FVOCI instruments", currency_field='currency_id')
    oci_cashflow_hedge = fields.Monetary(
        string="OCI: cash-flow hedges", currency_field='currency_id')
    oci_translation = fields.Monetary(
        string="OCI: foreign-operation translation",
        currency_field='currency_id')
    oci_actuarial = fields.Monetary(
        string="OCI: defined-benefit remeasurements",
        currency_field='currency_id')
    oci_associates_jv = fields.Monetary(
        string="OCI: associates and joint ventures",
        currency_field='currency_id')
    oci_other = fields.Monetary(
        string="OCI: other", currency_field='currency_id')
    oci_analysis_total = fields.Monetary(
        string="OCI item analysis total", compute='_compute_oci_analysis',
        store=True, currency_field='currency_id',
        help="IAS 1.106A item-by-item OCI analysis for this equity component.")
    oci_analysis_residual = fields.Monetary(
        string="OCI movement less item analysis",
        compute='_compute_oci_analysis', store=True,
        currency_field='currency_id')
    issue_of_shares = fields.Monetary(currency_field='currency_id')
    dividends = fields.Monetary(
        currency_field='currency_id',
        help="Dividends and other distributions (entered positive).")
    other_movement = fields.Monetary(currency_field='currency_id')
    closing_balance = fields.Monetary(
        compute='_compute_closing', store=True, currency_field='currency_id')

    # Input fields (everything except the computed closing_balance); editing
    # any of these on a confirmed statement is what must be frozen. The ORM's
    # own recompute of closing_balance is not an input edit and stays allowed.
    _INPUT_FIELDS = (
        'component', 'equity_account_ids', 'opening_balance',
        'retrospective_adjustment', 'profit',
        'oci_movement', 'issue_of_shares', 'dividends', 'other_movement',
        'oci_revaluation', 'oci_fvoci', 'oci_cashflow_hedge',
        'oci_translation', 'oci_actuarial', 'oci_associates_jv', 'oci_other',
        'sequence',
    )

    @api.depends('opening_balance', 'retrospective_adjustment')
    def _compute_restated_opening(self):
        for line in self:
            line.restated_opening_balance = (
                line.opening_balance + line.retrospective_adjustment)

    @api.depends(
        'oci_movement', 'oci_revaluation', 'oci_fvoci',
        'oci_cashflow_hedge', 'oci_translation', 'oci_actuarial',
        'oci_associates_jv', 'oci_other',
    )
    def _compute_oci_analysis(self):
        for line in self:
            line.oci_analysis_total = (
                line.oci_revaluation + line.oci_fvoci
                + line.oci_cashflow_hedge + line.oci_translation
                + line.oci_actuarial + line.oci_associates_jv
                + line.oci_other
            )
            line.oci_analysis_residual = (
                line.oci_movement - line.oci_analysis_total)

    @api.depends('opening_balance', 'retrospective_adjustment',
                 'profit', 'oci_movement',
                 'issue_of_shares', 'dividends', 'other_movement')
    def _compute_closing(self):
        for line in self:
            line.closing_balance = (
                line.opening_balance + line.retrospective_adjustment
                + line.profit + line.oci_movement
                + line.issue_of_shares + line.other_movement - line.dividends)

    @api.constrains('soce_id', 'equity_account_ids')
    def _check_equity_account_mapping_is_unique(self):
        for statement in self.mapped('soce_id'):
            owners = {}
            for line in statement.line_ids:
                for account in line.equity_account_ids:
                    prior = owners.get(account.id)
                    if prior and prior != line:
                        raise UserError(_(
                            "Ledger equity account %(account)s is mapped to "
                            "both %(first)s and %(second)s. One account may "
                            "feed only one SoCE component; shared mappings "
                            "double-count opening equity.",
                            account=account.display_name,
                            first=prior.display_name,
                            second=line.display_name,
                        ))
                    owners[account.id] = line

    def _check_parent_not_confirmed(self):
        confirmed = self.filtered(lambda l: l.soce_id.state == 'confirmed')
        if confirmed:
            raise UserError(_(
                "Lines on a confirmed statement of changes in equity are "
                "frozen. Set the statement back to draft first "
                "(EH Accounting Manager only)."))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a line to a confirmed statement would recompute its totals
        # and silently move the parent figures, bypassing the freeze that
        # write()/unlink() enforce. Block create when the target parent is
        # confirmed (IAS 1). The manager-gated set-to-draft path reopens it.
        if not self.env.su and any(
            self._SERVER_OWNED_FIELDS.intersection(vals)
            for vals in vals_list
        ):
            raise AccessError(_(
                "Equity line company, currency, and closing balance are "
                "server-owned."
            ))
        parents = self._eh_lock_and_authorize_parents([
            vals.get('soce_id') for vals in vals_list if vals.get('soce_id')
        ])
        if parents.filtered(lambda statement: statement.state == 'confirmed'):
            raise UserError(_(
                "Lines cannot be added to a confirmed statement of changes "
                "in equity; its figures are frozen. Set the statement back "
                "to draft first (EH Accounting Manager only)."))
        return super().create(vals_list)

    def _eh_lock_and_authorize_parents(self, parent_ids):
        parents = self.env['eh.soce'].browse(
            sorted({parent_id for parent_id in parent_ids if parent_id}))
        if parents and not self.env.su:
            parents._eh_check_access('write')
        if parents:
            parents.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_soce WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(parents.ids),),
            )
            parents.invalidate_recordset()
        return parents

    def write(self, vals):
        if not self.env.su and self._SERVER_OWNED_FIELDS.intersection(vals):
            raise AccessError(_(
                "Equity line company, currency, and closing balance are "
                "server-owned."
            ))
        parents = self.mapped('soce_id')
        if vals.get('soce_id'):
            parents |= self.env['eh.soce'].browse(vals['soce_id'])
        parents = self._eh_lock_and_authorize_parents(parents.ids)
        if parents.filtered(lambda statement: statement.state == 'confirmed'):
            raise UserError(_(
                "Lines on a confirmed statement are immutable and cannot be "
                "changed or reparented."
            ))
        if any(f in vals for f in self._INPUT_FIELDS):
            self._check_parent_not_confirmed()
        return super().write(vals)

    def unlink(self):
        self._eh_lock_and_authorize_parents(self.mapped('soce_id').ids)
        self._check_parent_not_confirmed()
        return super().unlink()
