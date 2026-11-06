# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7 financial-instrument risk exposures."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Measurement / input figures frozen once the exposure is finalised. Writing
# any of these on a finalised exposure is refused so a signed-off IFRS 7
# disclosure - whose loss allowance and ECL stage feed the IFRS 7.35H net
# carrying amounts - cannot be silently re-keyed. The computed figures
# (ecl_basis, net_carrying_amount, ledger_carrying_amount, carrying_residual,
# carrying_tied) are never in this set, so they still recompute; 'state' is
# never in it, so the finalise / reopen transition always passes.
_FIN_RISK_FROZEN_FIELDS = frozenset({
    'name', 'company_id', 'reporting_date', 'instrument_class',
    'risk_category', 'carrying_amount', 'maturity_band', 'sensitivity_note',
    'ecl_stage', 'loss_allowance', 'ledger_account_ids', 'floating_rate',
    'notes',
})


class EhFinRisk(models.Model):
    _name = 'eh.fin.risk'
    _description = "Financial instrument risk exposure (IFRS 7)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'risk_category, id'
    _rec_name = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'ecl_basis', 'net_carrying_amount',
        'ledger_carrying_amount', 'carrying_residual', 'carrying_tied',
    })

    name = fields.Char(required=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised risk exposure is locked: its measurement and input "
             "figures cannot be edited, and it cannot be deleted. Only a "
             "manager can finalise or reopen it. The computed net carrying "
             "amount and ledger tie-out still recompute (IFRS 7.35H).")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today)

    instrument_class = fields.Char(
        help="Class of financial instrument, e.g. 'Trade receivables', "
             "'Borrowings'.")
    risk_category = fields.Selection(
        [('credit', "Credit risk"), ('liquidity', "Liquidity risk"),
         ('market_currency', "Market risk - currency"),
         ('market_interest', "Market risk - interest rate"),
         ('market_price', "Market risk - other price")],
        default='credit', required=True,
        help="Risk arising from the instrument (IFRS 7.32-42).")
    carrying_amount = fields.Monetary(currency_field='currency_id')
    maturity_band = fields.Selection(
        [('on_demand', "On demand"), ('lt_3m', "Under 3 months"),
         ('3m_1y', "3 months to 1 year"), ('1y_5y', "1 to 5 years"),
         ('gt_5y', "Over 5 years")],
        default='lt_3m',
        help="Contractual maturity band for the liquidity maturity analysis "
             "(IFRS 7.39).")
    sensitivity_note = fields.Char(
        help="Sensitivity to the relevant risk variable (IFRS 7.40).")
    floating_rate = fields.Boolean(
        string="Floating rate",
        help="The instrument bears a floating (variable) interest rate. "
             "Flagged exposures feed the computed IFRS 7.40 interest-rate "
             "sensitivity at their carrying amount. The carrying amount is "
             "signed: positive for a net asset (a rate rise is a gain), "
             "negative for a net liability (a rate rise is a loss).")

    # --- Expected credit loss staging (IFRS 7.35A-N) --------------------
    # IFRS 7.35A-N requires a credit-risk exposure to disclose the loss
    # allowance by impairment stage under the IFRS 9 three-stage model, not a
    # free-text narrative. A stage 1 exposure carries a 12-month expected
    # credit loss; stages 2 and 3 carry a lifetime expected credit loss. The
    # staged loss allowance reduces the gross carrying amount to the net
    # carrying amount reported on the balance sheet.
    ecl_stage = fields.Selection(
        [('1', "Stage 1 - performing (12-month ECL)"),
         ('2', "Stage 2 - significant increase in credit risk (lifetime ECL)"),
         ('3', "Stage 3 - credit-impaired (lifetime ECL)")],
        string="ECL stage",
        help="IFRS 9 impairment stage for a credit-risk exposure "
             "(IFRS 7.35A-N). Leave empty when not applicable, e.g. for a "
             "liquidity- or market-risk exposure. Stage 1 measures a 12-month "
             "expected credit loss; stages 2 and 3 measure a lifetime "
             "expected credit loss.")
    ecl_basis = fields.Selection(
        [('12m', "12-month expected credit loss"),
         ('lifetime', "Lifetime expected credit loss")],
        compute='_compute_ecl', store=True, string="ECL measurement basis",
        help="Measurement basis implied by the stage: 12-month for stage 1, "
             "lifetime for stages 2 and 3 (IFRS 7.35A-N). Empty when no stage "
             "is set.")
    loss_allowance = fields.Monetary(
        currency_field='currency_id',
        help="Staged expected-credit-loss allowance recognised against the "
             "gross carrying amount (IFRS 7.35H, 35L). Defaults to zero, so "
             "an existing exposure with no allowance reports its gross "
             "carrying amount as the net.")
    net_carrying_amount = fields.Monetary(
        compute='_compute_ecl', store=True, currency_field='currency_id',
        help="Gross carrying amount less the staged loss allowance "
             "(IFRS 7.35H). Equals the carrying amount when no allowance is "
             "recognised.")

    # --- Ledger tie-out -------------------------------------------------
    # The carrying amount of a class of financial instrument is a ledger
    # figure. Naming the backing accounts lets the register derive the
    # carrying amount straight from posted balances at the reporting date, so
    # a hand-keyed carrying amount that drifts from the books is visible.
    ledger_account_ids = fields.Many2many(
        'account.account', 'eh_fin_risk_account_rel',
        'risk_id', 'account_id', string="Backing accounts",
        check_company=True,
        help="Optional. When set, the ledger carrying amount below is the "
             "net posted balance of these accounts on or before the "
             "reporting date, and the entered carrying amount is tied out "
             "against it.")
    ledger_carrying_amount = fields.Monetary(
        compute='_compute_ledger', currency_field='currency_id',
        help="Net posted balance of the backing accounts at the reporting "
             "date (positive for a net asset, negative for a net liability).")
    carrying_residual = fields.Monetary(
        compute='_compute_ledger', currency_field='currency_id',
        help="Entered carrying amount less the ledger carrying amount. Zero "
             "when the figure ties to the books.")
    carrying_tied = fields.Boolean(
        compute='_compute_ledger',
        help="True when no backing account is set (not applicable) or the "
             "entered carrying amount equals the ledger balance within "
             "currency rounding. False signals drift from the ledger.")
    notes = fields.Text()

    @api.depends('ecl_stage', 'carrying_amount', 'loss_allowance',
                 'company_id')
    def _compute_ecl(self):
        for risk in self:
            currency = risk.currency_id or risk.company_id.currency_id
            # Stage 1 is a 12-month ECL; stages 2 and 3 are a lifetime ECL.
            if risk.ecl_stage == '1':
                risk.ecl_basis = '12m'
            elif risk.ecl_stage in ('2', '3'):
                risk.ecl_basis = 'lifetime'
            else:
                risk.ecl_basis = False
            net = risk.carrying_amount - risk.loss_allowance
            risk.net_carrying_amount = currency.round(net) if currency else net

    @api.depends('ledger_account_ids', 'carrying_amount', 'reporting_date',
                 'company_id', 'include_branch_companies')
    def _compute_ledger(self):
        for risk in self:
            currency = risk.currency_id or risk.company_id.currency_id
            ledger = risk._derive_ledger_carrying_amount()
            risk.ledger_carrying_amount = ledger
            residual = risk.carrying_amount - ledger
            if currency:
                residual = currency.round(residual)
            risk.carrying_residual = residual
            if not risk.ledger_account_ids:
                # No backing accounts -> tie-out is not applicable, treat as
                # tied so a narrative-only exposure never shows as drifted.
                risk.carrying_tied = True
            else:
                risk.carrying_tied = currency.is_zero(residual) \
                    if currency else residual == 0.0

    def _derive_ledger_carrying_amount(self):
        """Return the net posted balance of the backing accounts on or before
        the reporting date. The ledger balance is debit - credit, positive
        for a net asset (e.g. trade receivables) and negative for a net
        liability (e.g. borrowings), matching the natural sign of the
        instrument class."""
        self.ensure_one()
        if not self.ledger_account_ids or not self.company_id \
                or not self.reporting_date:
            return 0.0
        move_lines = self.env['account.move.line'].search([
            ('account_id', 'in', self.ledger_account_ids.ids),
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.reporting_date),
        ])
        return sum(move_lines.mapped(lambda ml: ml.debit - ml.credit))

    # --- Draft / finalised lock -----------------------------------------
    # The loss allowance and ECL stage feed the IFRS 7.35H net carrying
    # amounts. Once an exposure is finalised these must not silently drift, so
    # the measurement / input fields freeze at the write layer; the only way
    # to change a figure is a manager-gated reopen, which unlocks it again.

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "financial risk exposure."))

    @api.model_create_multi
    def create(self, vals_list):
        # Creating an exposure already finalised would skip the manager-gated
        # action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        return super().create(vals_list)

    def write(self, vals):
        frozen = _FIN_RISK_FROZEN_FIELDS.intersection(vals)
        finalised = self.filtered(lambda r: r.state == 'finalised')
        # A write touching a frozen measurement / input figure while any
        # record is finalised is always blocked (a signed-off figure is frozen
        # for everyone; restate via a manager-gated reopen). The state field
        # itself is owned by the inherited eh.workflow.guard, which refuses any
        # non-superuser direct write; the sanctioned finalise / reopen actions
        # run under sudo.
        if frozen and finalised:
            raise UserError(_(
                "Figures on a finalised financial risk exposure are frozen "
                "(%(fields)s). Reopen it first (EH Accounting Manager only) "
                "to change it (IFRS 7.35H).",
                fields=', '.join(sorted(frozen))))
        return super().write(vals)

    def unlink(self):
        finalised = self.filtered(lambda r: r.state == 'finalised')
        if finalised:
            raise UserError(_(
                "A finalised financial risk exposure cannot be deleted. "
                "Reopen it first (EH Accounting Manager only)."))
        return super().unlink()

    def action_finalise(self):
        """Lock the risk exposure: measurement / input figures freeze.
        Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self._compute_ecl()
        self._compute_ledger()
        self.flush_recordset([
            'ecl_basis', 'net_carrying_amount',
        ])
        for risk in self:
            if risk.state == 'finalised':
                raise UserError(_(
                    "Risk exposure %s is already finalised.", risk.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised risk exposure to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True
