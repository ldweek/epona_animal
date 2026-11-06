# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.contribution.report: contribution margin and CVP analysis of a period.

Per line (one product / cost card): revenue is keyed manually or fetched
from the posted ledger (sum of posted customer invoice and credit note
income lines carrying the card's product within the period, via classic
read_group so the same code runs on every supported series); variable cost
is the card's standard variable cost per unit times the units sold.

    contribution      = revenue - variable cost
    CM ratio (pct)    = contribution / revenue x 100

Company totals and CVP set, computed in this documented order. Displayed
ratios are rounded to 4 decimals and money amounts to company currency. The
break-even revenue uses the exact aggregate contribution/revenue fraction,
not the rounded display ratio:

    total contribution      = sum(line contributions)
    CM ratio (pct)          = total contribution / total revenue x 100
    unit CM                 = total contribution / total units sold
    operating income        = total contribution - fixed costs
    break-even units        = fixed costs / unit CM
    break-even revenue      = fixed costs x revenue / contribution
    margin of safety (pct)  = (revenue - break-even revenue) / revenue x 100
    target-profit units     = (fixed costs + target profit) / unit CM
    operating leverage      = total contribution / operating income

Break-even projections require positive revenue, units and contribution. A
loss-making mix is explicitly labelled as having no break-even and its
otherwise misleading negative projections are suppressed. The report is an
analysis document: it never posts to the ledger.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .costing_sequence import next_company_reference


class EhContributionReport(models.Model):
    _name = 'eh.contribution.report'
    _description = "Contribution margin / CVP report"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_start desc, id desc'

    # State moves only through action_done / action_reset_to_draft, never a
    # direct write: completing the report freezes its inputs and totals.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('done', "Done")],
        default='draft', required=True, tracking=True, index=True,
        copy=False)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)

    line_ids = fields.One2many(
        'eh.contribution.report.line', 'report_id', string="Product Lines")

    fixed_costs = fields.Monetary(
        currency_field='currency_id', tracking=True, string="Fixed Costs",
        help="Total fixed costs of the period (keyed input; the CVP set "
             "derives from it).")
    target_profit = fields.Monetary(
        currency_field='currency_id', tracking=True, string="Target Profit",
        help="Desired operating income; drives the target-profit units.")
    include_branch_companies = fields.Boolean(
        string="Include Branch Companies",
        help="Include posted invoice lines from every readable descendant "
             "branch of the report company. Disabled keeps the ledger "
             "extract scoped to the report company only.")

    total_revenue = fields.Monetary(
        compute='_compute_cvp', store=True, currency_field='currency_id')
    total_variable_cost = fields.Monetary(
        compute='_compute_cvp', store=True, currency_field='currency_id')
    total_contribution = fields.Monetary(
        compute='_compute_cvp', store=True, currency_field='currency_id',
        string="Total Contribution Margin")
    total_units = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Total Units Sold")
    cm_ratio_pct = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="CM Ratio (%)")
    unit_cm = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Unit CM",
        help="Weighted contribution margin per unit sold.")
    operating_income = fields.Monetary(
        compute='_compute_cvp', store=True, currency_field='currency_id')
    breakeven_units = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Break-even Units")
    breakeven_revenue = fields.Monetary(
        compute='_compute_cvp', store=True, currency_field='currency_id',
        string="Break-even Revenue")
    margin_of_safety_pct = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Margin of Safety (%)")
    target_profit_units = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Target-profit Units")
    operating_leverage = fields.Float(
        compute='_compute_cvp', store=True, digits=(16, 4),
        string="Operating Leverage (DOL)",
        help="Degree of operating leverage: contribution margin / "
             "operating income.")
    cvp_status = fields.Selection(
        [
            ('available', "Break-even available"),
            ('no_sales', "No break-even — positive sales and units required"),
            ('no_positive_contribution',
             "No break-even — contribution margin is not positive"),
        ],
        compute='_compute_cvp', store=True, string="CVP Status",
        help="Explains whether break-even and other forward CVP projections "
             "are meaningful for the current sales mix.")

    notes = fields.Text()

    _check_period = models.Constraint(
        'CHECK (period_end >= period_start)',
        'The period end cannot precede the period start.')
    _check_fixed = models.Constraint(
        'CHECK (fixed_costs >= 0)', 'Fixed costs cannot be negative.')

    _FROZEN_FIELDS = (
        'period_start', 'period_end', 'fixed_costs', 'target_profit',
        'include_branch_companies', 'company_id')
    _FROZEN_STATES = ('done',)

    @api.depends('line_ids.revenue', 'line_ids.variable_cost',
                 'line_ids.contribution', 'line_ids.units_sold',
                 'fixed_costs', 'target_profit')
    def _compute_cvp(self):
        for report in self:
            currency = report.currency_id

            def money(value):
                return currency.round(value) if currency \
                    else round(value, 2)

            revenue = money(sum(report.line_ids.mapped('revenue')))
            variable = money(sum(report.line_ids.mapped('variable_cost')))
            contribution = money(
                sum(report.line_ids.mapped('contribution')))
            units = round(sum(report.line_ids.mapped('units_sold')), 4)
            cm_ratio = round(contribution / revenue * 100.0, 4) \
                if revenue else 0.0
            unit_cm = round(contribution / units, 4) if units else 0.0
            operating_income = money(contribution - report.fixed_costs)
            if revenue <= 0.0 or units <= 0.0:
                cvp_status = 'no_sales'
            elif contribution <= 0.0:
                cvp_status = 'no_positive_contribution'
            else:
                cvp_status = 'available'
            projections_available = cvp_status == 'available'
            breakeven_units = round(report.fixed_costs / unit_cm, 4) \
                if projections_available and unit_cm > 0.0 else 0.0
            breakeven_revenue = money(
                report.fixed_costs * revenue / contribution,
            ) if projections_available else 0.0
            mos_pct = round(
                (revenue - breakeven_revenue) / revenue * 100.0, 4) \
                if projections_available else 0.0
            target_units = round(
                (report.fixed_costs + report.target_profit) / unit_cm, 4) \
                if projections_available and unit_cm > 0.0 else 0.0
            leverage = round(contribution / operating_income, 4) \
                if projections_available and operating_income else 0.0

            report.total_revenue = revenue
            report.total_variable_cost = variable
            report.total_contribution = contribution
            report.total_units = units
            report.cm_ratio_pct = cm_ratio
            report.unit_cm = unit_cm
            report.operating_income = operating_income
            report.breakeven_units = breakeven_units
            report.breakeven_revenue = breakeven_revenue
            report.margin_of_safety_pct = mos_pct
            report.target_profit_units = target_units
            report.operating_leverage = leverage
            report.cvp_status = cvp_status

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                sequence_date = vals.get('period_start') \
                    or fields.Date.context_today(self)
                vals['name'] = next_company_reference(
                    self.env, 'eh.contribution.report', company,
                    sequence_date,
                ) or '/'
        return super().create(vals_list)

    def write(self, vals):
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            done = self.filtered(
                lambda r: r.state in self._FROZEN_STATES)
            if done:
                raise UserError(_(
                    "A completed contribution report is frozen "
                    "(%(fields)s). Reset it to draft to revise it.",
                    fields=', '.join(frozen)))
        if vals.get('company_id'):
            target_company = self.env['res.company'].browse(
                vals['company_id'])
            for report in self:
                if report.line_ids.filtered(
                    lambda line: line.card_id.company_id != target_company
                ):
                    raise ValidationError(_(
                        "Move or remove contribution lines whose cost cards "
                        "belong to another company before changing the "
                        "report company."
                    ))
        return super().write(vals)

    # ---- actions ----

    def _eh_ledger_company_ids(self):
        """Return an explicit, actor-authorised ledger company scope."""
        self.ensure_one()
        company = self.company_id
        if (
            not self.include_branch_companies
            or 'parent_id' not in company._fields
        ):
            return company.ids
        Company = self.env['res.company']
        complete = Company.sudo().search([
            ('id', 'child_of', company.id),
        ])
        readable = Company.search([('id', 'in', complete.ids)])
        if set(readable.ids) != set(complete.ids):
            raise AccessError(_(
                "Branch ledger extraction requires access to every branch "
                "under %(company)s.", company=company.display_name,
            ))
        if len(complete.mapped('currency_id')) != 1:
            raise UserError(_(
                "Branch ledger extraction cannot combine companies with "
                "different currencies."
            ))
        return sorted(complete.ids)

    def action_fetch_ledger_revenue(self):
        """Fill revenue and units from posted customer invoice lines.

        Classic read_group (not _read_group with aggregates) on purpose:
        this helper is shared across all supported series and 16 has no
        modern signature. Revenue is -sum(balance): income lines credit,
        so a credit balance is positive revenue and a refund reduces it.
        """
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Reset %s to draft before refetching ledger revenue.",
                self.display_name))
        ledger_lines = self.line_ids.filtered(
            lambda l: l.revenue_source == 'ledger')
        if not ledger_lines:
            raise UserError(_(
                "No line uses the posted-invoice revenue source."))
        product_by_line = {}
        for line in ledger_lines:
            product = line.card_id.product_id
            if not product:
                raise UserError(_(
                    "Line for card %s has no product; ledger revenue "
                    "aggregation needs one.", line.card_id.display_name))
            if product.id in product_by_line:
                raise UserError(_(
                    "Use only one ledger-sourced contribution line per "
                    "product; %(product)s appears more than once.",
                    product=product.display_name,
                ))
            product_by_line[product.id] = line

        base_domain = [
            ('company_id', 'in', self._eh_ledger_company_ids()),
            ('product_id', 'in', sorted(product_by_line)),
            ('parent_state', '=', 'posted'),
            ('date', '>=', self.period_start),
            ('date', '<=', self.period_end),
            ('account_id.account_type', 'in', ('income', 'income_other')),
        ]
        totals = {
            product_id: {'revenue': 0.0, 'units': 0.0}
            for product_id in product_by_line
        }
        Aml = self.env['account.move.line']
        # Two grouped queries, independent of the number of report lines:
        # invoice quantities add and refund quantities subtract. Revenue is
        # always -balance, whose accounting sign already handles refunds.
        for move_type, unit_sign in (
            ('out_invoice', 1.0), ('out_refund', -1.0),
        ):
            groups = Aml.read_group(
                base_domain + [('move_id.move_type', '=', move_type)],
                ['product_id', 'balance:sum', 'quantity:sum'],
                ['product_id'],
            )
            for group in groups:
                product_value = group.get('product_id')
                product_id = product_value[0] \
                    if isinstance(product_value, (tuple, list)) \
                    else product_value
                if product_id not in totals:
                    continue
                totals[product_id]['revenue'] -= group.get('balance') or 0.0
                totals[product_id]['units'] += unit_sign * (
                    group.get('quantity') or 0.0)
        for product_id, line in product_by_line.items():
            values = totals[product_id]
            line.write({
                'revenue': self.currency_id.round(values['revenue']),
                'units_sold': round(values['units'], 4),
            })
        return True

    def action_done(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("Only a draft report can be completed."))
        if not self.line_ids:
            raise UserError(_(
                "Add at least one product line before completing %s.",
                self.display_name))
        self.state = 'done'
        return True

    def action_reset_to_draft(self):
        self = self._eh_workflow_action()
        self.ensure_one()
        if self.state != 'done':
            raise UserError(_(
                "Only a completed report can go back to draft."))
        self.state = 'draft'
        return True


class EhContributionReportLine(models.Model):
    _name = 'eh.contribution.report.line'
    _description = "Contribution margin report line"
    _order = 'report_id, id'

    report_id = fields.Many2one(
        'eh.contribution.report', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='report_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='report_id.currency_id', store=True, readonly=True)

    card_id = fields.Many2one(
        'eh.cost.card', required=True, index=True, string="Cost Card",
        domain="[('state', 'in', ('active', 'superseded')), ('company_id', '=', company_id)]",
        help="Supplies the standard variable cost per unit.")
    product_id = fields.Many2one(
        related='card_id.product_id', store=True, string="Product")

    units_sold = fields.Float(digits=(16, 4), string="Units Sold")
    revenue_source = fields.Selection(
        [('manual', "Manual"), ('ledger', "Posted Invoice Lines")],
        default='manual', required=True, string="Revenue Source",
        help="Manual: key the revenue. Posted Invoice Lines: Fetch Ledger "
             "Revenue sums the posted customer invoice income lines of the "
             "card's product over the report period.")
    revenue = fields.Monetary(currency_field='currency_id')
    variable_cost = fields.Monetary(
        compute='_compute_amounts', store=True,
        currency_field='currency_id',
        help="Standard variable cost per unit x units sold.")
    contribution = fields.Monetary(
        compute='_compute_amounts', store=True,
        currency_field='currency_id', string="Contribution Margin")
    cm_ratio_pct = fields.Float(
        compute='_compute_amounts', store=True, digits=(16, 4),
        string="CM Ratio (%)")

    _check_units = models.Constraint(
        'CHECK (units_sold >= 0)', 'Units sold cannot be negative.')

    @api.depends('card_id.std_variable_cost_unit', 'units_sold', 'revenue')
    def _compute_amounts(self):
        for line in self:
            currency = line.currency_id

            def money(value):
                return currency.round(value) if currency \
                    else round(value, 2)

            variable = money(
                line.card_id.std_variable_cost_unit * line.units_sold)
            contribution = money(line.revenue - variable)
            line.variable_cost = variable
            line.contribution = contribution
            line.cm_ratio_pct = round(
                contribution / line.revenue * 100.0, 4) \
                if line.revenue else 0.0

    @api.constrains('revenue_source', 'card_id')
    def _check_ledger_needs_product(self):
        for line in self:
            if line.revenue_source == 'ledger' \
                    and not line.card_id.product_id:
                raise ValidationError(_(
                    "Ledger revenue aggregation needs a product on cost "
                    "card %s; use the manual revenue source for free-form "
                    "items.", line.card_id.display_name))

    @api.constrains('card_id', 'report_id')
    def _check_card_company(self):
        for line in self:
            if line.card_id.company_id != line.report_id.company_id:
                raise ValidationError(_(
                    "Cost card %(card)s belongs to %(card_company)s; the "
                    "report is for %(company)s.",
                    card=line.card_id.display_name,
                    card_company=line.card_id.company_id.display_name,
                    company=line.report_id.company_id.display_name))

    # The parent freezes when done; its lines feed the frozen totals, so
    # they freeze with it at create, write and unlink.
    def _check_parent_open(self, reports=None):
        reports = (reports if reports is not None
                   else self.mapped('report_id'))
        done = reports.filtered(
            lambda r: r.state in r._FROZEN_STATES)
        if done:
            raise UserError(_(
                "The lines of a completed contribution report are frozen "
                "(%s). Reset it to draft to revise it.",
                ', '.join(done.mapped('display_name'))))

    @api.model_create_multi
    def create(self, vals_list):
        # Guard BEFORE the insert: a line added to a completed report must
        # never reach the table.
        self._check_parent_open(self.env['eh.contribution.report'].browse(
            [v['report_id'] for v in vals_list if v.get('report_id')]))
        return super().create(vals_list)

    def write(self, vals):
        self._check_parent_open()
        if vals.get('report_id'):
            self._check_parent_open(
                self.env['eh.contribution.report'].browse(
                    vals['report_id']))
        return super().write(vals)

    def unlink(self):
        self._check_parent_open()
        return super().unlink()
