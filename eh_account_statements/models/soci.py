# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.soci / eh.soci.line: statement of comprehensive income (IAS 1.81A-82A).

Profit for the period plus the components of other comprehensive income give
total comprehensive income, which is attributed to owners of the parent and
to non-controlling interests. OCI components are grouped by whether they will
subsequently be reclassified to profit or loss (IAS 1.82A).
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

from . import presentation

# Account types that make up profit or loss for the period (IAS 1). Income
# accounts carry credit-negative balances and expenses debit-positive, so the
# net profit is the negated sum of these account balances.
# Single source of truth: the presentation module's authoritative P&L type
# set (which includes 'expense_other'). Keeping a private copy here silently
# dropped Other Expenses from every profit derivation and tie-out, so it is
# derived from presentation.PL_ACCOUNT_TYPES instead of re-listed.
_PL_ACCOUNT_TYPES = tuple(presentation.PL_ACCOUNT_TYPES)
_IAS1_PL_DISCLOSURE_FIELDS = (
    'pl_revenue', 'pl_interest_revenue_eir',
    'pl_derecognition_gain_loss', 'pl_finance_costs',
    'pl_impairment_losses', 'pl_share_associates_jv',
    'pl_reclassification_amortised_cost', 'pl_reclassification_fvoci',
    'pl_tax_expense', 'pl_discontinued_operations',
)


class EhSoci(models.Model):
    _name = 'eh.soci'
    _description = "Statement of comprehensive income"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_end desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True

    # State is a workflow field: it may only move through this model's own
    # actions (which run under sudo), never a direct RPC/ORM write. The
    # inherited eh.workflow.guard blocks a non-superuser write to it, closing
    # the "RPC-write state=confirmed to skip action_confirm and its IAS 1
    # tie-out checks" bypass. The frozen-figure protection in write() below is
    # a separate, always-on data-integrity control.
    _eh_guarded_fields = (
        'state', 'currency_id', 'oci_will_reclassify',
        'oci_no_reclassify', 'total_oci', 'total_comprehensive_income',
        'attribution_residual', 'attribution_tied', 'consol_run_name',
        'consol_nci_amount', 'consol_nci_available',
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

    profit_for_period = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Profit or loss for the period (IAS 1.81A(a)).")
    # IAS 1.82 minimum face line items. Values use a signed contribution-to-
    # profit convention: income/gains positive; costs, losses and tax expense
    # negative. ``profit_for_period`` remains the signed ledger anchor, while
    # the total/residual below make the preparer's disaggregation auditable.
    pl_revenue = fields.Monetary(
        string="Revenue", currency_field='currency_id')
    pl_interest_revenue_eir = fields.Monetary(
        string="Interest revenue (effective interest method)",
        currency_field='currency_id')
    pl_derecognition_gain_loss = fields.Monetary(
        string="Derecognition gain/loss (amortised cost)",
        currency_field='currency_id')
    pl_finance_costs = fields.Monetary(
        string="Finance costs", currency_field='currency_id')
    pl_impairment_losses = fields.Monetary(
        string="Impairment losses (IFRS 9)", currency_field='currency_id')
    pl_share_associates_jv = fields.Monetary(
        string="Share of profit/loss of associates and joint ventures",
        currency_field='currency_id')
    pl_reclassification_amortised_cost = fields.Monetary(
        string="Amortised-cost reclassification gain/loss",
        currency_field='currency_id')
    pl_reclassification_fvoci = fields.Monetary(
        string="FVOCI reclassification gain/loss",
        currency_field='currency_id')
    pl_tax_expense = fields.Monetary(
        string="Tax expense", currency_field='currency_id')
    pl_discontinued_operations = fields.Monetary(
        string="Discontinued operations, net of tax",
        currency_field='currency_id')
    pl_line_items_total = fields.Monetary(
        string="IAS 1.82 line items total", compute='_compute_pl_line_items',
        store=True, currency_field='currency_id')
    pl_line_items_residual = fields.Monetary(
        string="Profit less IAS 1.82 line items", compute='_compute_pl_line_items',
        store=True, currency_field='currency_id',
        help="Profit for the period less the disclosed IAS 1.82 line items; "
             "zero when the face disaggregation is complete.")
    line_ids = fields.One2many('eh.soci.line', 'soci_id', copy=True)

    oci_will_reclassify = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    oci_no_reclassify = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_oci = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_comprehensive_income = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Profit for the period plus total other comprehensive income.")

    attributable_to_owners = fields.Monetary(
        currency_field='currency_id',
        help="Total comprehensive income attributable to owners of the "
             "parent (IAS 1.81B).")
    attributable_to_nci = fields.Monetary(
        currency_field='currency_id',
        help="Total comprehensive income attributable to non-controlling "
             "interests.")
    attribution_residual = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Total comprehensive income less the amounts attributed to "
             "owners and NCI; should be zero.")
    attribution_tied = fields.Boolean(
        compute='_compute_totals', store=True,
        help="True when the attribution residual is zero within currency "
             "rounding (owners + NCI reconcile to total comprehensive "
             "income).")

    # ---- IAS 34 interim presentation (presentation only) ----------------
    period_type = fields.Selection(
        presentation.PERIOD_TYPES, default='annual', required=True,
        tracking=True,
        help="Annual keeps the classic IAS 1 presentation. Interim marks "
             "this statement as an IAS 34 interim report: it is labelled as "
             "such, can carry the IAS 34.20 comparatives (comparable interim "
             "period of the immediately preceding financial year plus the "
             "immediately preceding annual period, on the year-to-date "
             "convention) and may be flagged condensed per IAS 34.8. "
             "Presentation only; no figure changes.")
    condensed = fields.Boolean(
        string="Condensed (IAS 34.8)", tracking=True,
        help="IAS 34.8(b) permits a condensed statement of profit or loss "
             "and other comprehensive income containing only the mandatory "
             "minimum line items (headings and subtotals). When set, the "
             "form collapses the OCI detail lines to the two IAS 1.82A "
             "subtotals (items that may be reclassified and items that will "
             "not be). Presentation only: the detail lines stay recorded "
             "and keep feeding the subtotals.")
    comparative_interim_id = fields.Many2one(
        'eh.soci', string="Prior Interim Comparative", copy=False,
        check_company=True,
        domain="[('period_type', '=', 'interim'),"
               " ('company_id', '=', company_id)]",
        help="Comparable interim period of the immediately preceding "
             "financial year (IAS 34.20(b) year-to-date convention).")
    comparative_annual_id = fields.Many2one(
        'eh.soci', string="Prior Annual Comparative", copy=False,
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
        help="Reviewed or closed consolidation run whose exact-period NCI "
             "profit/OCI flow the statement was prefilled from or compared "
             "to.")
    consol_nci_amount = fields.Monetary(
        string="NCI Period Comprehensive Income", readonly=True, copy=False,
        currency_field='currency_id',
        help="Reviewed NCI profit plus OCI flow for this exact period from "
             "the consolidation run, credit-positive. Reference figure for "
             "attributable_to_nci; never the closing NCI equity stock.")
    consol_nci_available = fields.Boolean(
        readonly=True, copy=False,
        help="True when a reviewed/closed exact-period, same-currency "
             "consolidation run's signed NCI flow was snapshotted here.")
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
    nci_consol_discrepancy = fields.Monetary(
        compute='_compute_nci_consol_discrepancy',
        currency_field='currency_id',
        help="Statement NCI attribution less the consolidation run's NCI "
             "carve-out; zero when the two agree.")
    nci_consol_tied = fields.Boolean(
        compute='_compute_nci_consol_discrepancy',
        help="True when the statement's NCI attribution equals the "
             "consolidation run's NCI carve-out within currency rounding "
             "(always true while no run figure is available).")

    recycling_discrepancy_count = fields.Integer(
        compute='_compute_recycling_discrepancy_count',
        help="Number of OCI lines whose manual reclassification flag "
             "disagrees with the recycling tag on their source account "
             "(IAS 1.82A).")

    # ---- IAS 1.82A OCI recycling completeness guard ---------------------
    oci_recycling_misfit_note = fields.Text(
        compute='_compute_oci_recycling_misfit_note',
        help="OCI components with a non-zero amount whose source account "
             "carries no EH OCI recycling tag (or which name no source "
             "account). Their reclassification section rests on the manual "
             "flag rather than the account classification (IAS 1.82A); "
             "confirmation is blocked while this list is non-empty unless "
             "the override is set with a reason.")
    oci_untagged_count = fields.Integer(
        compute='_compute_oci_recycling_misfit_note',
        help="Number of OCI components whose reclassification section is "
             "not derived from an account recycling tag (IAS 1.82A).")
    oci_tag_override = fields.Boolean(
        string="Override OCI Recycling Check", copy=False, tracking=True,
        help="Confirm despite OCI components with no tag-derived "
             "reclassification section (IAS 1.82A). Requires a reason; the "
             "override is logged to the chatter.")
    oci_tag_override_reason = fields.Text(
        copy=False,
        help="Mandatory justification when the IAS 1.82A OCI recycling "
             "completeness check is overridden.")

    notes = fields.Text()

    _check_period = models.Constraint(
        'CHECK (period_start <= period_end)',
        'Period start must be on or before period end.')
    _unique_company_period = models.Constraint(
        'UNIQUE (company_id, period_start, period_end)',
        'Only one comprehensive-income statement may exist per company and '
        'period.')

    # Figure inputs frozen once the statement is confirmed. A confirmed
    # primary statement is signed off and must not silently drift from the
    # general ledger; the only way to change a figure is a manager-gated
    # set-to-draft, which unlocks it again (IAS 1.106-108).
    _FROZEN_AFTER_CONFIRM = (
        'profit_for_period', 'attributable_to_owners', 'attributable_to_nci',
        *_IAS1_PL_DISCLOSURE_FIELDS,
        'period_start', 'period_end', 'line_ids',
        'company_id',
        'period_type', 'condensed',
        'comparative_interim_id', 'comparative_annual_id',
        'classification_override', 'classification_override_reason',
        'oci_tag_override', 'oci_tag_override_reason',
    )

    @api.depends(*_IAS1_PL_DISCLOSURE_FIELDS, 'profit_for_period')
    def _compute_pl_line_items(self):
        for statement in self:
            statement.pl_line_items_total = sum(
                statement[field_name]
                for field_name in _IAS1_PL_DISCLOSURE_FIELDS
            )
            statement.pl_line_items_residual = (
                statement.profit_for_period - statement.pl_line_items_total
            )

    @api.depends('profit_for_period', 'line_ids.net_amount',
                 'line_ids.will_reclassify',
                 'attributable_to_owners', 'attributable_to_nci')
    def _compute_totals(self):
        for s in self:
            reclass = s.line_ids.filtered(lambda l: l.will_reclassify)
            no_reclass = s.line_ids.filtered(lambda l: not l.will_reclassify)
            s.oci_will_reclassify = sum(reclass.mapped('net_amount'))
            s.oci_no_reclassify = sum(no_reclass.mapped('net_amount'))
            s.total_oci = s.oci_will_reclassify + s.oci_no_reclassify
            s.total_comprehensive_income = s.profit_for_period + s.total_oci
            s.attribution_residual = (
                s.total_comprehensive_income
                - (s.attributable_to_owners + s.attributable_to_nci))
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            s.attribution_tied = float_is_zero(
                s.attribution_residual, precision_rounding=rounding or 0.01)

    @api.depends('period_type', 'condensed')
    def _compute_presentation_label(self):
        for s in self:
            s.presentation_label = presentation.presentation_label(
                s, _("statement of comprehensive income"))

    @api.depends('company_id', 'period_end', 'state')
    def _compute_classification_misfit_note(self):
        for s in self:
            pairs = presentation.classification_misfit_pairs(s)
            s.classification_misfit_note = (
                presentation.format_misfit_note(s, pairs) if pairs
                else False)

    @api.depends('attributable_to_nci', 'consol_nci_amount',
                 'consol_nci_available')
    def _compute_nci_consol_discrepancy(self):
        for s in self:
            if not s.consol_nci_available:
                s.nci_consol_discrepancy = 0.0
                s.nci_consol_tied = True
                continue
            s.nci_consol_discrepancy = (
                s.attributable_to_nci - s.consol_nci_amount)
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            s.nci_consol_tied = float_is_zero(
                s.nci_consol_discrepancy,
                precision_rounding=rounding or 0.01)

    @api.depends('line_ids.reclassify_discrepancy')
    def _compute_recycling_discrepancy_count(self):
        for s in self:
            s.recycling_discrepancy_count = len(
                s.line_ids.filtered('reclassify_discrepancy'))

    @api.depends('line_ids.amount', 'line_ids.account_id',
                 'line_ids.account_id.tag_ids')
    def _compute_oci_recycling_misfit_note(self):
        for s in self:
            misfits = presentation.oci_recycling_misfit_lines(s)
            s.oci_untagged_count = len(misfits)
            s.oci_recycling_misfit_note = (
                presentation.format_oci_misfit_note(misfits) if misfits
                else False)

    @api.constrains('period_type', 'condensed', 'comparative_interim_id',
                    'comparative_annual_id', 'period_start', 'company_id')
    def _check_interim_fields(self):
        for s in self:
            presentation.check_interim_fields(s)

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
                    "A comprehensive-income statement must belong to one "
                    "of the actor's allowed companies."
                ))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                sequence_date = vals.get('period_end') or vals.get(
                    'period_start') or fields.Date.context_today(self)
                vals['name'] = self.env['ir.sequence'].with_context(
                    ir_sequence_date=sequence_date,
                ).next_by_code('eh.soci') or '/'
        # Chart classification is deliberate configuration, never a side
        # effect of opening or creating a statement. Managers can invoke the
        # explicit Apply OCI Recycling Tags action after reviewing sources.
        return super().create(vals_list)

    def write(self, vals):
        self._eh_lock_headers()
        if vals.get('company_id') and not self.env.su:
            company = self.env['res.company'].browse(vals['company_id'])
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise AccessError(_(
                    "A comprehensive-income statement cannot be moved to "
                    "a company outside the actor's allowed companies."
                ))
        frozen = [f for f in self._FROZEN_AFTER_CONFIRM if f in vals]
        confirmed = self.filtered(lambda s: s.state == 'confirmed')
        # Reopening a confirmed statement (state -> draft) is a manager-gated
        # control. Enforce it at the write layer so a raw ORM write cannot
        # bypass the manager-gated action_set_to_draft.
        if confirmed and vals.get('state') == 'draft':
            self._check_manager()
        # Figures on a confirmed statement are frozen (IAS 1). A state-only
        # reopen (action_set_to_draft writes just {'state': 'draft'}) carries
        # no frozen field and passes; a write that touches a frozen figure
        # while any record is still confirmed is always blocked, even when it
        # tries to flip state to draft in the same call.
        if frozen and confirmed:
            raise UserError(_(
                "Figures on a confirmed statement of comprehensive income "
                "are frozen (%(fields)s). Set it back to draft first "
                "(EH Accounting Manager only) to edit it (IAS 1).",
                fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_headers()
        confirmed = self.filtered(lambda s: s.state == 'confirmed')
        if confirmed:
            raise UserError(_(
                "A confirmed statement of comprehensive income cannot be "
                "deleted. Set it back to draft first (EH Accounting Manager "
                "only)."))
        return super().unlink()

    def _pl_ledger_profit_at(self, date_from, date_to):
        """Net profit per the posted ledger over the period (credit-positive).

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

    def _has_ledger_pl_at(self, date_from, date_to):
        """True when a ledger profit figure is genuinely derivable.

        ``_pl_ledger_profit_at`` returns 0.0 both for a real nil result and
        for the total absence of any posted P&L items in the period. Only the
        former is a meaningful ledger figure to tie the worksheet profit
        against; the latter must leave the profit tie-out advisory. This
        probes whether at least one posted income / expense journal item
        exists for this company within the period.
        """
        self.ensure_one()
        if not (self.company_id and self.period_start and self.period_end):
            return False
        company_ids = presentation.statement_ledger_company_ids(self)
        domain = [
            ('company_id', 'in', company_ids),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', list(_PL_ACCOUNT_TYPES)),
            ('date', '>=', self.period_start),
            ('date', '<=', self.period_end),
        ]
        year_end_move_ids = self._eh_year_end_entry_move_ids()
        if year_end_move_ids:
            domain.append(('move_id', 'not in', year_end_move_ids))
        return bool(self.env['account.move.line'].search_count(domain))

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can confirm or reopen the "
                "statement of comprehensive income."))

    def _eh_lock_headers(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_soci WHERE id IN %s ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        self._check_company()
        if not self.env.su and any(
            statement.company_id not in self.env.companies
            for statement in self
        ):
            raise AccessError(_(
                "The comprehensive-income statement company is outside "
                "the actor's allowed companies."
            ))
        return self

    def _eh_lock_for_action(self):
        self._eh_lock_headers()
        lines = self.mapped('line_ids')
        if lines:
            lines.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_soci_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(lines.ids)),),
            )
            lines.invalidate_recordset()
        return self

    def action_confirm(self):
        self._check_manager()
        self._eh_lock_for_action()
        for source_statement in self:
            presentation.validate_statement_consol_source(
                source_statement, 'period_flow')
        # Header totals are guarded, stored outputs.  Caller manager role,
        # write ACL, record rules and allowed company were proved above; only
        # their action-owned recompute and final state write run elevated.
        statements = self.sudo()
        # ``will_reclassify`` is stored but intentionally user-overridable;
        # forcing its compute here would erase a disclosed manual override.
        # The pre-lock flush has already persisted pending dependency-driven
        # recomputes, so only the dependent header totals are refreshed.
        statements._compute_totals()
        statements._compute_nci_consol_discrepancy()
        statements.flush_recordset([
            'oci_will_reclassify', 'oci_no_reclassify', 'total_oci',
            'total_comprehensive_income', 'attribution_residual',
            'attribution_tied',
        ])
        for s in statements:
            if s.consol_nci_available and not s.nci_consol_tied:
                raise UserError(_(
                    "NCI comprehensive-income attribution does not match "
                    "the reviewed consolidation period flow: statement "
                    "%(statement)s, source %(source)s, difference %(diff)s. "
                    "Reconcile or re-prefill before confirming.",
                    statement=s.attributable_to_nci,
                    source=s.consol_nci_amount,
                    diff=s.nci_consol_discrepancy,
                ))
            # IAS 1.60 completeness: block while posted balances sit on
            # accounts outside the recognised current / non-current sets,
            # unless a manager override with reason is recorded (logged).
            presentation.check_classification_completeness(
                s, _("statement of comprehensive income"))
            # IAS 1.82A OCI recycling completeness: block while any OCI
            # component with a non-zero amount has no tag-derived
            # reclassification section (no source account, or an untagged
            # source account), unless a manager override with reason is
            # recorded (logged). This enforces that the section placement is
            # structural, not honour-system.
            presentation.check_oci_recycling_completeness(s)
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            # Ledger profit tie-out: conditional-blocking. When a ledger net
            # profit figure is genuinely derivable (at least one posted P&L
            # journal item exists in the period) the statement's profit for
            # the period must tie to it within currency rounding, otherwise a
            # mis-keyed profit would confirm silently. When no ledger figure
            # is derivable (no P&L postings) the behaviour stays advisory: no
            # block, preserving existing flows on empty-ledger companies.
            if s._has_ledger_pl_at(s.period_start, s.period_end):
                ledger_profit = s._pl_ledger_profit_at(
                    s.period_start, s.period_end)
                profit_residual = s.profit_for_period - ledger_profit
                if not float_is_zero(
                        profit_residual, precision_rounding=rounding or 0.01):
                    raise UserError(_(
                        "Profit for the period does not tie to the general "
                        "ledger: the statement reports %(reported)s but the "
                        "posted ledger shows %(ledger)s (difference "
                        "%(diff)s). Reconcile the profit to the ledger before "
                        "confirming.",
                        reported=s.profit_for_period,
                        ledger=ledger_profit,
                        diff=profit_residual))
            # Only enforce the attribution tie-out when the statement actually
            # attributes comprehensive income to owners or NCI; statements that
            # leave both attribution amounts blank behave exactly as before.
            has_attribution = not (
                float_is_zero(
                    s.attributable_to_owners,
                    precision_rounding=rounding or 0.01)
                and float_is_zero(
                    s.attributable_to_nci,
                    precision_rounding=rounding or 0.01))
            if has_attribution and not s.attribution_tied:
                raise UserError(_(
                    "Comprehensive income attribution does not tie out: "
                    "owners (%(owners)s) plus non-controlling interests "
                    "(%(nci)s) must equal total comprehensive income "
                    "(%(total)s). Residual is %(residual)s.",
                    owners=s.attributable_to_owners,
                    nci=s.attributable_to_nci,
                    total=s.total_comprehensive_income,
                    residual=s.attribution_residual))
            # Structural recycling discrepancy warning (IAS 1.82A): a line
            # whose manual flag disagrees with the recycling tag on its
            # source account confirms, but never silently - the overridden
            # classifications are recorded in the chatter.
            discrepant = s.line_ids.filtered('reclassify_discrepancy')
            if discrepant:
                s.message_post(body=_(
                    "OCI recycling classification overridden against the "
                    "account tags (IAS 1.82A) on: %(lines)s. The manual "
                    "reclassification flag was kept; verify the section "
                    "placement is deliberate.",
                    lines=', '.join(discrepant.mapped('name'))))
        # The state write runs under sudo so it passes the inherited
        # eh.workflow.guard (env.su, not a forgeable context key); the real
        # env.user is preserved for the audit stamps.
        statements.write({'state': 'confirmed'})

    def action_set_to_draft(self):
        self._check_manager()
        self._eh_lock_for_action()
        self.sudo().write({'state': 'draft'})

    def action_derive_profit_from_ledger(self):
        """Set ``profit_for_period`` from posted P&L account balances so the
        statement's net profit ties to the general ledger.

        Net profit = -(sum of income + expense account balances) over posted
        journal items dated within the statement period, because income
        accounts carry credit-negative balances and expenses debit-positive.
        """
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            s.profit_for_period = s._pl_ledger_profit_at(
                s.period_start, s.period_end)
        return True

    def action_apply_oci_recycling_tags(self):
        """Runnable action: scan the installed suite modules' OCI account
        settings and apply the default recycling tags (IAS 1.82A) to any
        reserve account not yet classified. Never re-tags an account that
        already carries either tag."""
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            applied = presentation.apply_default_oci_recycling_tags(
                self.env, s.company_id)
            s.message_post(body=_(
                "Applied the default EH OCI recycling tags to %(count)s "
                "ledger account(s) (IAS 1.82A structural classification).",
                count=applied))
        return True

    def action_prefill_nci_from_consolidation(self):
        """Prefill NCI attribution from reviewed exact-period flow output.

        Soft registry lookup (no hard dependency on the consolidation
        module). The run's reviewed NCI period profit plus OCI is snapshotted
        with source provenance; closing NCI equity is never used. A manually
        keyed figure is kept and its discrepancy surfaced.
        """
        self._check_manager()
        self._eh_lock_for_action()
        for s in self:
            if s.state == 'confirmed':
                raise UserError(_(
                    "Figures on a confirmed statement of comprehensive "
                    "income are frozen; set it back to draft before "
                    "prefilling the NCI attribution."))
            run = presentation.find_covering_consol_run(s)
            if run is None:
                raise UserError(_(
                    "No unique reviewed/closed consolidation run matches "
                    "this company, exact period and currency (or the "
                    "consolidation module is not installed)."))
            flow, provenance = presentation.consol_source_snapshot(
                s, run, 'period_flow')
            s.sudo().write(dict(provenance, **{
                'consol_nci_amount': flow,
                'consol_nci_available': True,
            }))
            rounding = (s.currency_id or s.company_id.currency_id).rounding
            if float_is_zero(s.attributable_to_nci,
                             precision_rounding=rounding or 0.01):
                s.attributable_to_nci = flow
            else:
                s.message_post(body=_(
                    "NCI attribution kept at the manually entered "
                    "%(manual)s; the covering consolidation run "
                    "%(run)s reports exact-period NCI comprehensive income "
                    "of %(flow)s. See the NCI "
                    "discrepancy field.",
                    manual=presentation.format_statement_amount(
                        s, s.attributable_to_nci),
                    run=run.name,
                    flow=presentation.format_statement_amount(s, flow)))
        return True


class EhSociLine(models.Model):
    _name = 'eh.soci.line'
    _description = "Statement of comprehensive income OCI line"
    _order = 'soci_id, sequence, id'
    _check_company_auto = True

    _SERVER_OWNED_FIELDS = frozenset({'company_id', 'currency_id'})

    soci_id = fields.Many2one(
        'eh.soci', required=True, ondelete='cascade', index=True,
        check_company=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='soci_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='soci_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="OCI component.")
    oci_type = fields.Selection(
        [('revaluation', "Revaluation surplus (PPE/intangibles)"),
         ('fvoci', "Gains/losses on FVOCI financial assets"),
         ('cashflow_hedge', "Cash flow hedge reserve"),
         ('translation', "Foreign operation translation"),
         ('actuarial', "Remeasurement of defined benefit plans"),
         ('associates_reclassify',
          "Share of associates/JVs OCI - may be reclassified"),
         ('associates_no_reclassify',
          "Share of associates/JVs OCI - will not be reclassified"),
         ('other', "Other")],
        default='other', required=True)
    amount = fields.Monetary(
        currency_field='currency_id',
        help="OCI amount before tax for the period (positive = gain).")
    tax_amount = fields.Monetary(
        string="Related tax", currency_field='currency_id',
        help="Tax effect allocated to this OCI item, signed as its effect on "
             "OCI (a tax expense is negative; a tax benefit is positive).")
    net_amount = fields.Monetary(
        string="Net of tax", compute='_compute_net_amount', store=True,
        currency_field='currency_id',
        help="OCI amount after related tax, used by statement subtotals.")
    account_id = fields.Many2one(
        'account.account', string="Source OCI Account",
        check_company=True,
        domain="[('account_type', 'in', ('equity', 'equity_unaffected'))]",
        help="Ledger OCI reserve account this component derives from. When "
             "the account carries one of the EH OCI recycling tags, the "
             "reclassification section of this line is derived structurally "
             "from the tag (IAS 1.82A) instead of the manual flag.")
    will_reclassify = fields.Boolean(
        string="May be reclassified to P&L",
        compute='_compute_will_reclassify', store=True, readonly=False,
        help="True for items that may later be reclassified to profit or "
             "loss (e.g. cash-flow hedges, translation); false for items "
             "that never are (e.g. revaluation surplus, actuarial "
             "remeasurements) per IAS 1.82A. Derived from the recycling tag "
             "on the source OCI account when one is set; editable as a "
             "manual override, in which case the discrepancy against the "
             "tag is flagged.")
    tag_reclassify = fields.Selection(
        [('recyclable', "Recyclable (tag)"),
         ('non_recyclable', "Non-recyclable (tag)"),
         ('none', "No tag")],
        compute='_compute_tag_reclassify', string="Tag Classification",
        help="What the EH OCI recycling tag on the source account says. "
             "'No tag' when no source account is set or the account is "
             "untagged (the manual flag then governs alone).")
    reclassify_discrepancy = fields.Boolean(
        compute='_compute_tag_reclassify', string="Tag Disagrees",
        help="True when the source account carries a recycling tag and the "
             "line's reclassification flag disagrees with it (manual "
             "override, IAS 1.82A). Confirming the statement records the "
             "overridden lines in the chatter.")

    @api.depends('amount', 'tax_amount')
    def _compute_net_amount(self):
        for line in self:
            line.net_amount = line.amount + line.tax_amount

    def _eh_tag_verdict(self):
        """The recycling verdict of the tags on the source account:
        'recyclable', 'non_recyclable' or None (no account / no tag)."""
        self.ensure_one()
        rec_tag, non_tag = presentation.oci_recycling_tags(self.env)
        tags = self.account_id.tag_ids
        if rec_tag and rec_tag in tags:
            return 'recyclable'
        if non_tag and non_tag in tags:
            return 'non_recyclable'
        return None

    @api.depends('account_id', 'account_id.tag_ids', 'oci_type')
    def _compute_will_reclassify(self):
        for line in self:
            # Confirmed statements are frozen: a later re-tagging of the
            # account must never silently move a signed statement's
            # sections. The stored value is kept as-is.
            if line.soci_id.state == 'confirmed':
                line.will_reclassify = line.will_reclassify
                continue
            verdict = line._eh_tag_verdict()
            if verdict == 'recyclable':
                line.will_reclassify = True
            elif verdict == 'non_recyclable':
                line.will_reclassify = False
            elif line.oci_type == 'associates_reclassify':
                line.will_reclassify = True
            elif line.oci_type == 'associates_no_reclassify':
                line.will_reclassify = False
            else:
                # No structural signal: keep the manual flag (old
                # behaviour preserved for untagged / account-less lines).
                line.will_reclassify = line.will_reclassify

    @api.depends('account_id', 'account_id.tag_ids', 'will_reclassify')
    def _compute_tag_reclassify(self):
        for line in self:
            verdict = line._eh_tag_verdict()
            line.tag_reclassify = verdict or 'none'
            line.reclassify_discrepancy = bool(verdict) and (
                line.will_reclassify != (verdict == 'recyclable'))

    def _check_parent_not_confirmed(self):
        confirmed = self.filtered(lambda l: l.soci_id.state == 'confirmed')
        if confirmed:
            raise UserError(_(
                "OCI lines on a confirmed statement of comprehensive income "
                "are frozen. Set the statement back to draft first "
                "(EH Accounting Manager only)."))

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an OCI line to a confirmed statement would recompute its
        # totals and silently move the parent figures, bypassing the freeze
        # that write()/unlink() enforce. Block create when the target parent is
        # confirmed (IAS 1). The manager-gated set-to-draft path reopens it.
        if not self.env.su and any(
            self._SERVER_OWNED_FIELDS.intersection(vals)
            for vals in vals_list
        ):
            raise AccessError(_(
                "OCI line company and currency anchors are server-owned."
            ))
        parents = self._eh_lock_and_authorize_parents([
            vals.get('soci_id') for vals in vals_list if vals.get('soci_id')
        ])
        if parents.filtered(lambda statement: statement.state == 'confirmed'):
            raise UserError(_(
                "OCI lines cannot be added to a confirmed statement of "
                "comprehensive income; its figures are frozen. Set the "
                "statement back to draft first (EH Accounting Manager "
                "only)."))
        return super().create(vals_list)

    def _eh_lock_and_authorize_parents(self, parent_ids):
        parents = self.env['eh.soci'].browse(
            sorted({parent_id for parent_id in parent_ids if parent_id}))
        if parents and not self.env.su:
            parents._eh_check_access('write')
        if parents:
            parents.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_soci WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(parents.ids),),
            )
            parents.invalidate_recordset()
        return parents

    def write(self, vals):
        if not self.env.su and self._SERVER_OWNED_FIELDS.intersection(vals):
            raise AccessError(_(
                "OCI line company and currency anchors are server-owned."
            ))
        parents = self.mapped('soci_id')
        if vals.get('soci_id'):
            parents |= self.env['eh.soci'].browse(vals['soci_id'])
        parents = self._eh_lock_and_authorize_parents(parents.ids)
        if parents.filtered(lambda statement: statement.state == 'confirmed'):
            raise UserError(_(
                "OCI lines on a confirmed statement are immutable and cannot "
                "be changed or reparented."
            ))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_and_authorize_parents(self.mapped('soci_id').ids)
        self._check_parent_not_confirmed()
        return super().unlink()
