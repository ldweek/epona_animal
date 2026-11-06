# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7.40 market-risk sensitivity analysis computed from the ledger.

Currency risk: a configurable percentage shock (default 10) is applied to
the net open monetary position in each foreign currency, read from posted
receivable, payable and cash move lines at the reporting date. Interest-rate
risk: a configurable basis-point shock (default 100) is applied to the
carrying amount of instruments flagged floating-rate in one explicitly
selected authoritative source: the latest financial-risk register snapshot,
the latest maturity run's borrowing instruments, or (soft lookup) fair-value
items flagged floating. The results are computed rows; the shock assumptions
are disclosed alongside them (IFRS 7.40(b)).

Sign convention, stated once and used everywhere: every impact is the
effect of the UPWARD shock. For currency risk that is a strengthening of
the foreign currency against the functional currency, so a net foreign
currency asset produces a positive (gain) impact and a net liability a
negative one. For interest-rate risk it is a parallel upward shift in
rates, so a floating-rate asset gains interest income (positive) and a
floating-rate borrowing bears more interest cost (negative). The downward
shock is symmetric with the opposite sign.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

from .integrity import (
    cutoff_residual_query, cutoff_residuals, execute_query_dict,
)


IR_SOURCES = [
    ('risk_register', "Financial-risk register"),
    ('maturity_run', "Contractual-maturity instruments"),
    ('fair_value', "Fair-value register"),
]

# Parent inputs frozen once the analysis is finalised.
_SENSITIVITY_FROZEN_FIELDS = frozenset({
    'name', 'company_id', 'reporting_date', 'fx_shock_pct', 'ir_shock_bp',
    'fx_monetary_account_ids', 'ir_source', 'line_ids', 'notes',
})


class EhFinSensitivity(models.Model):
    _name = 'eh.fin.sensitivity'
    _description = "Market-risk sensitivity analysis (IFRS 7.40)"
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
        'currency_id', 'total_pnl_impact', 'total_oci_impact',
    })

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised analysis is locked: its shocks and rows cannot be "
             "edited or recomputed. Only a manager can finalise or reopen "
             "it.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)

    fx_shock_pct = fields.Float(
        digits=(7, 4), default=10.0, tracking=True,
        string="Currency shock (%)",
        help="Reasonably possible change in each exchange rate "
             "(IFRS 7.40(a)). The computed impact is the profit-or-loss "
             "effect of the foreign currency STRENGTHENING by this "
             "percentage against the functional currency, applied to the "
             "net open monetary position per currency; the weakening case "
             "is symmetric with the opposite sign.")
    ir_shock_bp = fields.Float(
        digits=(7, 2), default=100.0, tracking=True,
        string="Interest-rate shock (bp)",
        help="Parallel upward shift in interest rates in basis points "
             "(IFRS 7.40(a)), applied to the carrying amount of "
             "floating-rate instruments as the annual profit-or-loss "
             "effect; the downward shift is symmetric with the opposite "
             "sign.")
    fx_monetary_account_ids = fields.Many2many(
        'account.account',
        'eh_fin_sensitivity_fx_account_rel',
        'sensitivity_id', 'account_id',
        string="Other FX monetary accounts",
        check_company=True,
        tracking=True,
        help="Foreign-currency receivable, payable and cash accounts are "
             "included automatically. Select any other monetary accounts "
             "that carry currency risk, such as foreign-currency borrowings "
             "or refundable deposits. Do not select inventory, prepayments "
             "or other non-monetary balances.",
    )
    ir_source = fields.Selection(
        IR_SOURCES,
        required=True,
        default='risk_register',
        tracking=True,
        string="Interest-rate source",
        help="The single authoritative instrument register used for the "
             "interest-rate shock. Sources are intentionally exclusive: the "
             "same facility is often represented in the risk, maturity and "
             "fair-value registers, and adding those surfaces would silently "
             "double or triple count it.",
    )

    line_ids = fields.One2many(
        'eh.fin.sensitivity.line', 'run_id', copy=False,
        string="Sensitivity rows")

    total_pnl_impact = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the profit-or-loss impacts of the upward shocks.")
    total_oci_impact = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of manually documented other-comprehensive-income impacts "
             "of the upward shocks.")
    assumption_note = fields.Text(
        compute='_compute_assumption_note',
        help="Methods and assumptions disclosed with the analysis "
             "(IFRS 7.40(b)).")
    notes = fields.Text()

    @api.depends('line_ids.pnl_impact', 'line_ids.oci_impact')
    def _compute_totals(self):
        for run in self:
            run.total_pnl_impact = sum(run.line_ids.mapped('pnl_impact'))
            run.total_oci_impact = sum(run.line_ids.mapped('oci_impact'))

    @api.depends('fx_shock_pct', 'ir_shock_bp', 'ir_source')
    def _compute_assumption_note(self):
        for run in self:
            source_label = dict(IR_SOURCES).get(run.ir_source, run.ir_source)
            run.assumption_note = _(
                "Currency risk: each impact is the profit-or-loss effect of "
                "the foreign currency strengthening %(pct)s%% against the "
                "functional currency, applied to the net open monetary "
                "position (posted receivable, payable and cash items) in "
                "that currency at the reporting date; a weakening of the "
                "same size has the opposite effect. Interest-rate risk: "
                "each impact is the annual profit-or-loss effect of a %(bp)s "
                "basis point parallel upward shift applied once to each "
                "floating-rate instrument in the authoritative '%(source)s' "
                "source; a downward shift of the same size has the opposite "
                "effect. All other variables are held constant.",
                pct=run.fx_shock_pct, bp=run.ir_shock_bp,
                source=source_label)

    # --- compute ---------------------------------------------------------

    def action_compute(self):
        """Rebuild the computed sensitivity rows. Idempotent: computed rows
        are wiped and rebuilt; manually keyed rows are preserved."""
        self = self._eh_disclosure_prepare_action(engine=True)
        Line = self.env['eh.fin.sensitivity.line']
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; it cannot be "
                    "recomputed. Ask a manager to reopen it first.",
                    run.name))
            run.line_ids.filtered(
                lambda l: l.origin == 'computed').unlink()
            vals_list = run._fx_line_vals() + run._ir_line_vals()
            for vals in vals_list:
                vals['run_id'] = run.id
                vals['origin'] = 'computed'
            if vals_list:
                Line.create(vals_list)
        return True

    def _fx_open_positions(self):
        """Net open monetary position per foreign currency: the sum of the
        open (residual) foreign-currency amounts on posted receivable and
        payable lines plus the foreign-currency amounts on posted cash and
        explicitly selected monetary-account lines, up to the reporting date.
        Positive = net asset.

        The aggregation stays in PostgreSQL.  It neither materialises the
        company's entire historical AR/AP ledger in the ORM cache nor builds
        an unbounded partial-reconcile ``IN`` list.
        """
        self.ensure_one()
        company_currency = self.company_id.currency_id
        account_scope = [
            ('account_id.account_type', 'in', (
                'asset_receivable', 'liability_payable', 'asset_cash',
            )),
        ]
        if self.fx_monetary_account_ids:
            account_scope = [
                '|', account_scope[0],
                ('account_id', 'in', self.fx_monetary_account_ids.ids),
            ]
        MoveLine = self.env['account.move.line']
        company_ids = self._eh_disclosure_company_ids()
        if len(company_ids) > 1:
            move_lines = MoveLine.search([
                ('company_id', 'in', company_ids),
                ('parent_state', '=', 'posted'),
                ('date', '<=', self.reporting_date),
                ('currency_id', '!=', False),
            ] + account_scope)
            residuals = cutoff_residuals(
                move_lines.filtered('account_id.reconcile'),
                self.reporting_date,
            )
            positions = {}
            for move_line in move_lines:
                currency = move_line.currency_id
                if currency == move_line.company_id.currency_id:
                    continue
                amount = (
                    residuals[move_line.id][1]
                    if move_line.account_id.reconcile
                    else move_line.amount_currency
                )
                positions[currency] = positions.get(currency, 0.0) + amount
            return {
                currency: currency.round(amount)
                for currency, amount in positions.items()
                if not currency.is_zero(amount)
            }
        query, _company_residual, foreign_residual = cutoff_residual_query(
            MoveLine, [
            ('company_id', '=', company_ids[0]),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.reporting_date),
            ('currency_id', '!=', False),
            ('currency_id', '!=', company_currency.id),
        ] + account_scope, self.reporting_date)
        Account = self.env['account.account']
        Account.flush_model(['reconcile'])
        aml_alias = query.table_alias
        account_reconcile = SQL(
            "COALESCE((SELECT aa.reconcile FROM account_account aa "
            "WHERE aa.id = %s), FALSE)",
            SQL.identifier(aml_alias, 'account_id'),
        )
        position = SQL(
            "CASE WHEN %s THEN (%s) ELSE %s END",
            account_reconcile,
            foreign_residual,
            SQL.identifier(aml_alias, 'amount_currency'),
        )
        currency_id = SQL.identifier(aml_alias, 'currency_id')
        query.groupby = currency_id
        rows = execute_query_dict(self.env, query.select(
            SQL("%s AS %s", currency_id, SQL.identifier('currency_id')),
            SQL("SUM(%s) AS %s", position, SQL.identifier('position')),
        ))
        positions = {}
        Currency = self.env['res.currency']
        for row in rows:
            currency = Currency.browse(row['currency_id'])
            amount = currency.round(float(row['position'] or 0.0))
            if not currency.is_zero(amount):
                positions[currency] = amount
        return positions

    def _fx_line_vals(self):
        """One row per foreign currency with a non-zero net open monetary
        position. Impact convention: pnl = exposure in functional currency
        x shock%, i.e. the gain (positive exposure) or loss (negative
        exposure) if the foreign currency strengthens by the shock."""
        self.ensure_one()
        company_currency = self.company_id.currency_id
        vals_list = []
        for currency, amount_fc in sorted(
                self._fx_open_positions().items(), key=lambda i: i[0].name):
            if currency.is_zero(amount_fc):
                continue
            exposure = currency._convert(
                amount_fc, company_currency, self.company_id,
                self.reporting_date)
            impact = company_currency.round(
                exposure * self.fx_shock_pct / 100.0)
            vals_list.append({
                'kind': 'fx',
                'name': _("Net open monetary position - %s", currency.name),
                'shock_currency_id': currency.id,
                'exposure_foreign': amount_fc,
                'exposure': company_currency.round(exposure),
                'shock': '+%s%%' % (self.fx_shock_pct,),
                'pnl_impact': impact,
                'oci_impact': 0.0,
            })
        return vals_list

    def _ir_line_vals(self):
        """One row per floating instrument from one authoritative source.

        The same facility is commonly represented in all three registers.
        There is no cross-model instrument identifier with which to prove two
        rows are or are not the same position, so guessing from a name/amount
        would introduce both false merges and silent double counts.  The run's
        explicit source selection is therefore exclusive by design.

        ``carrying amount x basis-point shock`` is an annual interest cash-flow
        sensitivity and always affects P&L.  A fair-value movement may affect
        OCI, but that requires duration/valuation inputs this formula does not
        contain and must be documented as a separate manual row.
        """
        self.ensure_one()
        company_currency = self.company_id.currency_id
        factor = self.ir_shock_bp / 10000.0
        shock = '+%sbp' % (self.ir_shock_bp,)
        if self.ir_source == 'risk_register':
            return self._ir_risk_register_vals(
                company_currency, factor, shock,
            )
        if self.ir_source == 'maturity_run':
            return self._ir_maturity_vals(company_currency, factor, shock)
        if self.ir_source == 'fair_value':
            return self._ir_fair_value_vals(company_currency, factor, shock)
        raise UserError(_("Select an authoritative interest-rate source."))

    def _ir_risk_register_vals(self, company_currency, factor, shock):
        """Latest risk-register snapshot, signed asset-positive."""
        vals_list = []
        Risk = self.env['eh.fin.risk']
        risks = Risk
        for company_id in self._eh_disclosure_company_ids():
            latest_register = Risk.search([
                ('company_id', '=', company_id),
                ('reporting_date', '<=', self.reporting_date),
            ], order='reporting_date desc', limit=1)
            if latest_register:
                risks |= Risk.search([
                    ('company_id', '=', company_id),
                    ('floating_rate', '=', True),
                    ('reporting_date', '=', latest_register.reporting_date),
                ])
        for risk in risks:
            exposure = risk.carrying_amount
            source_currency = risk.company_id.currency_id
            if source_currency != company_currency:
                exposure = source_currency._convert(
                    exposure, company_currency, self.company_id,
                    self.reporting_date,
                )
            vals_list.append({
                'kind': 'interest',
                'name': _("Floating-rate exposure - %s", risk.name),
                'exposure': company_currency.round(exposure),
                'shock': shock,
                'pnl_impact': company_currency.round(
                    exposure * factor),
                'oci_impact': 0.0,
            })
        return vals_list

    def _ir_maturity_vals(self, company_currency, factor, shock):
        """Latest maturity-run borrowings, principal as a negative exposure."""
        vals_list = []
        Run = self.env['eh.fin.maturity.run']
        maturity_runs = Run
        for company_id in self._eh_disclosure_company_ids():
            maturity_runs |= Run.search([
                ('company_id', '=', company_id),
                ('reporting_date', '<=', self.reporting_date),
            ], order='reporting_date desc, id desc', limit=1)
        for instrument in maturity_runs.instrument_ids.filtered('floating_rate'):
            exposure = -instrument.principal
            source_currency = instrument.company_id.currency_id
            if source_currency != company_currency:
                exposure = source_currency._convert(
                    exposure, company_currency, self.company_id,
                    self.reporting_date,
                )
            vals_list.append({
                'kind': 'interest',
                'name': _("Floating-rate borrowing - %s", instrument.name),
                'exposure': company_currency.round(exposure),
                'shock': shock,
                'pnl_impact': company_currency.round(
                    exposure * factor),
                'oci_impact': 0.0,
            })
        return vals_list

    def _ir_fair_value_vals(self, company_currency, factor, shock):
        """Frozen, posted fair-value snapshots; cash-flow effect is P&L."""
        if 'eh.fair.value.item' not in self.env:
            raise UserError(_(
                "The Fair Value Measurement module is required when the "
                "fair-value register is selected as the interest-rate source."
            ))
        Item = self.env['eh.fair.value.item']
        if 'floating_rate' not in Item._fields:
            raise UserError(_(
                "The installed Fair Value Measurement module does not expose "
                "floating-rate instruments. Upgrade it or select another "
                "interest-rate source."
            ))
        vals_list = []
        items = Item.search([
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('floating_rate', '=', True),
        ])
        for item in items:
            snapshot = self._eh_fv_measurement_snapshot(item)
            if not snapshot:
                continue
            exposure, _measurement_routes_to_oci = snapshot
            source_currency = item.company_id.currency_id
            if source_currency != company_currency:
                exposure = source_currency._convert(
                    exposure, company_currency, self.company_id,
                    self.reporting_date,
                )
            impact = company_currency.round(exposure * factor)
            vals_list.append({
                'kind': 'interest',
                'name': _("Floating-rate instrument - %s", item.name),
                'exposure': company_currency.round(exposure),
                'shock': shock,
                'pnl_impact': impact,
                'oci_impact': 0.0,
            })
        return vals_list

    def _eh_fv_measurement_snapshot(self, item):
        """Return ``(closing fair value, routed to OCI)`` or no evidence.

        The fair-value master carries editable inputs while it is in draft.
        A trustworthy disclosure snapshot therefore requires all of:

        * a posted item-linked entry on the master measurement date;
        * a frozen measured/derecognised master whose engine carrying anchor
          still equals its fair value;
        * measurement and lifecycle dates not after the disclosure cutoff;
        * sealed/exact-company journal provenance when those controls exist.

        The linked entry stores the movement, not an independent closing-value
        snapshot. If a later/future or draft rewrite displaced the frozen
        master anchor, historical closing value cannot be reconstructed without
        inventing an opening balance, so the method fails closed.
        """
        self.ensure_one()
        cutoff = self.reporting_date
        posted_moves = item.move_ids.filtered(
            lambda move: move.state == 'posted'
            and move.company_id == item.company_id
            and move.date <= cutoff
        )
        if item.balance_sheet_account_id:
            evidence_moves = posted_moves.filtered(
                lambda move: any(
                    line.account_id == item.balance_sheet_account_id
                    for line in move.line_ids
                )
            )
        else:
            evidence_moves = self.env['account.move']
        # An item first measured after the cutoff contributes nothing at the
        # cutoff and is not an ambiguity.
        if not evidence_moves:
            return False
        if item.state not in ('measured', 'derecognised'):
            raise UserError(_(
                "Floating fair-value item %s has posted history on or before "
                "the reporting date but its master is draft/cancelled and "
                "editable. No immutable closing measurement is available; "
                "remeasure it through the controlled workflow or add a "
                "reviewed manual sensitivity row.", item.display_name,
            ))
        if item.derecognition_date and item.derecognition_date <= cutoff:
            return False
        if not item.measurement_date or item.measurement_date > cutoff:
            raise UserError(_(
                "Floating fair-value item %s has earlier posted history but "
                "its frozen master now represents a measurement after the "
                "reporting date. The earlier closing value is not stored "
                "independently and cannot be reconstructed safely.",
                item.display_name,
            ))
        current_moves = evidence_moves.filtered(
            lambda move: move.date == item.measurement_date
        )
        current_moves = current_moves.filtered(
            lambda move: (
                ('eh_sealed' not in move._fields or move.eh_sealed)
                and ('eh_fair_value_item_id' not in move._fields
                     or move.eh_fair_value_item_id == item)
            )
        )
        if not current_moves:
            raise UserError(_(
                "Floating fair-value item %s has no valid posted measurement "
                "entry matching its frozen measurement date.",
                item.display_name,
            ))
        currency = item.currency_id or self.company_id.currency_id
        if not currency.is_zero(item.prior_carrying - item.fair_value):
            raise UserError(_(
                "Floating fair-value item %s has a carrying anchor that no "
                "longer matches its frozen posted fair value.",
                item.display_name,
            ))
        # A remeasurement move contains one position leg and one P&L or OCI
        # counter-leg. Derecognition at/before cutoff was excluded above. If
        # there are multiple same-day item moves (e.g. reserve recycling), only
        # the move containing the position account is in ``current_moves``.
        measurement_move = current_moves.sorted('id')[-1]
        counter_lines = measurement_move.line_ids.filtered(
            lambda line: line.account_id != item.balance_sheet_account_id
            and not currency.is_zero(line.balance)
        )
        if len(counter_lines) != 1:
            raise UserError(_(
                "Floating fair-value item %s has ambiguous measurement-entry "
                "legs and cannot feed the sensitivity automatically.",
                item.display_name,
            ))
        to_oci = counter_lines.account_id.internal_group == 'equity'
        return item.prior_carrying, to_oci

    # --- draft / finalised lock -------------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "sensitivity analysis."))

    @api.model_create_multi
    def create(self, vals_list):
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                vals['name'] = self._eh_disclosure_next_sequence(
                    'eh.fin.sensitivity', company) or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the shocks and rows once finalised (a signed-off analysis is
        # frozen for everyone; restate via a manager-gated reopen). The state
        # field itself is owned by the inherited eh.workflow.guard, which
        # refuses any non-superuser direct write; the sanctioned finalise /
        # reopen actions run under sudo.
        if _SENSITIVITY_FROZEN_FIELDS.intersection(vals):
            for run in self:
                if run.state == 'finalised':
                    raise UserError(_(
                        "Sensitivity analysis %s is finalised and cannot "
                        "be edited. Ask a manager to reopen it first.",
                        run.name))
        return super().write(vals)

    def unlink(self):
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised and cannot be "
                    "deleted. Ask a manager to reopen it first.", run.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the analysis: shocks and rows freeze. Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self._compute_totals()
        self.flush_recordset(['total_pnl_impact', 'total_oci_impact'])
        for run in self:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is already finalised.",
                    run.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised analysis to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhFinSensitivityLine(models.Model):
    _name = 'eh.fin.sensitivity.line'
    _description = "Market-risk sensitivity row (IFRS 7.40)"
    _order = 'run_id, kind, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'run_id'
    _eh_disclosure_server_fields = frozenset({
        'company_id', 'currency_id', 'origin',
    })
    _eh_disclosure_engine_origins = frozenset({'computed'})

    run_id = fields.Many2one(
        'eh.fin.sensitivity', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    kind = fields.Selection(
        [('fx', "Currency risk"), ('interest', "Interest-rate risk")],
        required=True, default='fx')
    name = fields.Char(required=True, help="Exposure description.")
    origin = fields.Selection(
        [('computed', "Computed"), ('manual', "Manual")],
        default='manual', required=True,
        help="Computed rows are wiped and rebuilt on every compute. A "
             "manual row (e.g. an other-price-risk exposure the ledger "
             "cannot see) survives the recompute.")
    shock_currency_id = fields.Many2one(
        'res.currency', string="Exposure currency",
        help="Foreign currency of a currency-risk row.")
    exposure_foreign = fields.Monetary(
        currency_field='shock_currency_id',
        help="Net open monetary position in the foreign currency.")
    exposure = fields.Monetary(
        currency_field='currency_id',
        help="Exposure in the functional currency the shock applies to "
             "(signed: positive = net asset).")
    shock = fields.Char(
        help="The shock applied, e.g. '+10.0%' or '+100.0bp'. The downward "
             "shock is symmetric with the opposite sign.")
    pnl_impact = fields.Monetary(
        currency_field='currency_id', string="P&L impact",
        help="Profit-or-loss effect of the upward shock (positive = gain).")
    oci_impact = fields.Monetary(
        currency_field='currency_id', string="OCI impact",
        help="Other-comprehensive-income effect of the upward shock. The "
             "computed carrying-amount x basis-point interest cash-flow "
             "effect is P&L; use a reviewed manual row for a separately "
             "modelled fair-value/OCI sensitivity.")

    @api.model_create_multi
    def create(self, vals_list):
        # Create guard on child lines feeding a frozen parent.
        runs = self.env['eh.fin.sensitivity'].browse([
            v.get('run_id') for v in vals_list if v.get('run_id')])
        for run in runs:
            if run.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; no row can be "
                    "added. Ask a manager to reopen it first.", run.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; its rows cannot "
                    "be edited. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.run_id.state == 'finalised':
                raise UserError(_(
                    "Sensitivity analysis %s is finalised; its rows cannot "
                    "be removed. Ask a manager to reopen it first.",
                    line.run_id.name))
        return super().unlink()
