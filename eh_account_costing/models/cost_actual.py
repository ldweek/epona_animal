# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.cost.actual: one period's actual production capture for a cost card.

Manual, CSV-friendly entry: the units produced plus, per cost element, the
TOTAL input quantity and TOTAL cost for the period. The element lines mirror
the card's elements:

* material: total input quantity (e.g. kg) and total material cost;
* labour: total hours worked and total labour cost;
* variable overhead: the DRIVER quantity (normally the same actual hours as
  the labour line, when overhead is applied on labour hours) and the total
  variable overhead incurred;
* fixed overhead: total fixed overhead incurred (the quantity is unused and
  stays zero).

v1 scope note (documented, deliberate): there is NO stock-module coupling.
Actuals are keyed or imported, not pulled from stock moves or work orders;
inventory valuation integration is a later wave. This keeps the module
installable on any inventory setup, including none.

Freeze rule: once a POSTED variance run references an actual, its
measurement (card, period, units, element lines) is frozen; the posted
variance journal entry derived from these numbers must stay reconcilable to
them.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .cost_card import COST_ELEMENTS
from .costing_sequence import next_company_reference


class EhCostActual(models.Model):
    _name = 'eh.cost.actual'
    _description = "Period actual costs for a cost card"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_start desc, id desc'

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    card_id = fields.Many2one(
        'eh.cost.card', required=True, index=True, tracking=True,
        string="Cost Card", domain="[('state', 'in', ('active', 'superseded')), ('company_id', '=', company_id)]",
        help="Standard cost card these actuals are measured against. The "
             "card must be activated (draft standards are not final).")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)
    units_produced = fields.Float(
        digits=(16, 4), tracking=True, string="Units Produced",
        help="Actual output of the period; drives the standard quantity "
             "allowed and the fixed overhead absorbed.")

    line_ids = fields.One2many(
        'eh.cost.actual.line', 'actual_id', string="Actual Cost Elements")
    total_actual_cost = fields.Monetary(
        compute='_compute_total_actual_cost', store=True,
        currency_field='currency_id', string="Total Actual Cost")

    notes = fields.Text()

    _check_units = models.Constraint(
        'CHECK (units_produced >= 0)',
        'Units produced cannot be negative.')
    _check_period = models.Constraint(
        'CHECK (period_end >= period_start)',
        'The period end cannot precede the period start.')

    _FROZEN_FIELDS = (
        'card_id', 'units_produced', 'period_start', 'period_end',
        'company_id')

    @api.depends('line_ids.actual_cost_total')
    def _compute_total_actual_cost(self):
        for actual in self:
            total = sum(actual.line_ids.mapped('actual_cost_total'))
            actual.total_actual_cost = (
                actual.currency_id.round(total) if actual.currency_id
                else round(total, 2))

    @api.constrains('card_id', 'company_id')
    def _check_card_company(self):
        for actual in self:
            if actual.card_id.company_id != actual.company_id:
                raise ValidationError(_(
                    "The cost card %(card)s belongs to %(card_company)s; "
                    "the actuals are captured in %(company)s.",
                    card=actual.card_id.display_name,
                    card_company=actual.card_id.company_id.display_name,
                    company=actual.company_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        card_ids = set()
        for vals in vals_list:
            if 'total_actual_cost' in vals and not self.env.su:
                raise AccessError(_(
                    "The actual-cost total is computed by the server."
                ))
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            card = self.env['eh.cost.card'].browse(vals.get('card_id'))
            if card:
                card_ids.add(card.id)
                if not self.env.su:
                    card._eh_check_access('read')
                if card.company_id.id != company_id:
                    raise UserError(_(
                        "The cost card must belong to the actual's company."
                    ))
        self._eh_lock_sources(extra_card_ids=card_ids)
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            card = self.env['eh.cost.card'].browse(vals.get('card_id'))
            if card:
                if not self.env.su:
                    card._eh_check_access('read')
                if card.company_id.id != company_id:
                    raise UserError(_(
                        "The cost card must belong to the actual's company."
                    ))
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                sequence_date = vals.get('period_start') \
                    or fields.Date.context_today(self)
                vals['name'] = next_company_reference(
                    self.env, 'eh.cost.actual', company, sequence_date,
                ) or '/'
        actuals = super().create(vals_list)
        actuals._eh_validate_references()
        return actuals

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "Cost actuals may only use a company enabled in the "
                "caller's current company context."
            ))

    def _eh_validate_references(self, access_mode='write'):
        if not self.env.su:
            self._eh_check_access(access_mode)
        for actual in self:
            self._eh_assert_company_allowed(actual.company_id.id)
            if not self.env.su:
                actual.card_id._eh_check_access('read')
            if actual.card_id.company_id != actual.company_id:
                raise UserError(_(
                    "The cost card must belong to the actual's company."
                ))
        return self

    def _eh_lock_sources(self, extra_card_ids=()):
        """Lock only actual/card rows whose consistency can change."""
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_cost_actual WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        card_ids = tuple(sorted(
            set(self.mapped('card_id').ids) |
            {int(card_id) for card_id in extra_card_ids if card_id}
        ))
        if card_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_cost_card WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (card_ids,))
            self.env['eh.cost.card'].browse(card_ids).invalidate_recordset()
        return self

    def _posted_runs(self):
        """Posted variance runs referencing these actuals. Only called on
        existing records (write/unlink paths), so ids are real."""
        if not self.ids:
            return self.env['eh.cost.variance.run']
        return self.env['eh.cost.variance.run'].sudo().search([
            ('state', '=', 'posted'), ('actual_ids', 'in', self.ids)])

    def _check_open(self, actuals=None):
        actuals = actuals if actuals is not None else self
        posted = actuals._posted_runs()
        if posted:
            raise UserError(_(
                "These actuals feed a posted variance run and are "
                "frozen; the posted variance entry must stay reconcilable "
                "to them."))

    def write(self, vals):
        if 'total_actual_cost' in vals and not self.env.su:
            raise AccessError(_(
                "The actual-cost total is computed by the server."
            ))
        if not self.env.su:
            self._eh_check_access('write')
        for actual in self:
            company_id = vals.get('company_id', actual.company_id.id)
            self._eh_assert_company_allowed(company_id)
            card = self.env['eh.cost.card'].browse(
                vals.get('card_id', actual.card_id.id))
            if not self.env.su:
                card._eh_check_access('read')
            if card.company_id.id != company_id:
                raise UserError(_(
                    "The cost card must belong to the actual's company."
                ))
        self._eh_lock_sources(
            extra_card_ids=(vals.get('card_id'),),
        )
        for actual in self:
            company_id = vals.get('company_id', actual.company_id.id)
            self._eh_assert_company_allowed(company_id)
            card = self.env['eh.cost.card'].browse(
                vals.get('card_id', actual.card_id.id))
            if not self.env.su:
                card._eh_check_access('read')
            if card.company_id.id != company_id:
                raise UserError(_(
                    "The cost card must belong to the actual's company."
                ))
        if any(f in vals for f in self._FROZEN_FIELDS):
            self._check_open()
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def unlink(self):
        # Draft-run references disappear with the run's lines; a posted run
        # would be orphaned of its evidence.
        self._eh_validate_references(access_mode='unlink')
        self._eh_lock_sources()
        self._eh_validate_references(
            access_mode='unlink',
        )._check_open()
        runs = self.env['eh.cost.variance.run'].sudo().search([
            ('state', 'in', ('draft', 'computed')),
            ('actual_ids', 'in', self.ids)]) if self.ids else None
        if runs:
            raise UserError(_(
                "These actuals are selected on a variance run. Remove them "
                "from the run first."))
        return super().unlink()


class EhCostActualLine(models.Model):
    _name = 'eh.cost.actual.line'
    _description = "Period actual cost element"
    _order = 'actual_id, id'

    actual_id = fields.Many2one(
        'eh.cost.actual', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='actual_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='actual_id.currency_id', store=True, readonly=True)

    element = fields.Selection(
        COST_ELEMENTS, required=True, default='material')
    actual_qty_total = fields.Float(
        digits=(16, 4), string="Actual Qty (Total)",
        help="Total input quantity of the period (kg, hours, driver units). "
             "Leave zero for fixed overhead.")
    actual_cost_total = fields.Monetary(
        currency_field='currency_id', string="Actual Cost (Total)")
    actual_price_unit = fields.Float(
        compute='_compute_actual_price_unit', digits=(16, 4),
        string="Actual Price",
        help="actual cost / actual quantity; informational.")

    _check_qty = models.Constraint(
        'CHECK (actual_qty_total >= 0)',
        'An actual quantity cannot be negative.')
    _check_cost = models.Constraint(
        'CHECK (actual_cost_total >= 0)',
        'An actual cost cannot be negative.')

    @api.depends('actual_qty_total', 'actual_cost_total')
    def _compute_actual_price_unit(self):
        for line in self:
            line.actual_price_unit = round(
                line.actual_cost_total / line.actual_qty_total, 4) \
                if line.actual_qty_total else 0.0

    @api.constrains('element', 'actual_id')
    def _check_element_unique(self):
        # Cache-based sibling check (no search): safe on the create path.
        for line in self:
            siblings = line.actual_id.line_ids.filtered(
                lambda l: l.element == line.element)
            if len(siblings) > 1:
                raise ValidationError(_(
                    "%(actual)s already has a %(element)s line; one line "
                    "per element.",
                    actual=line.actual_id.display_name,
                    element=dict(COST_ELEMENTS)[line.element]))

    # The parent's measurement freezes once a posted variance run references
    # it; these lines feed that measurement, so they freeze with it at
    # create, write and unlink.
    @api.model
    def _eh_assert_unique_values(self, values):
        seen = set()
        for actual_id, element, exclude_id in values:
            key = (actual_id, element)
            if key in seen:
                raise ValidationError(_(
                    "Cost actuals may contain only one line per cost element."
                ))
            seen.add(key)
            query = (
                "SELECT id FROM eh_cost_actual_line "
                "WHERE actual_id = %s AND element = %s"
            )
            params = [actual_id, element]
            if exclude_id:
                query += " AND id != %s"
                params.append(exclude_id)
            query += " LIMIT 1"
            self.env.cr.execute(query, tuple(params))
            if self.env.cr.fetchone():
                raise ValidationError(_(
                    "Cost actuals may contain only one line per cost element."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        parents = self.env['eh.cost.actual'].browse(
            [v['actual_id'] for v in vals_list if v.get('actual_id')])
        if not self.env.su:
            parents._eh_check_access('write')
        parents._eh_lock_sources()._eh_validate_references()._check_open()
        self._eh_assert_unique_values([
            (vals.get('actual_id'), vals.get('element', 'material'), False)
            for vals in vals_list
        ])
        return super().create(vals_list)

    def write(self, vals):
        parents = self.mapped('actual_id')
        target = self.env['eh.cost.actual']
        if vals.get('actual_id'):
            target = self.env['eh.cost.actual'].browse(vals['actual_id'])
            parents |= target
        if not self.env.su:
            self._eh_check_access('write')
            parents._eh_check_access('write')
        parents._eh_lock_sources()._eh_validate_references()._check_open()
        self._eh_assert_unique_values([
            (
                vals.get('actual_id', line.actual_id.id),
                vals.get('element', line.element),
                line.id,
            )
            for line in self
        ])
        return super().write(vals)

    def unlink(self):
        parents = self.mapped('actual_id')
        if not self.env.su:
            self._eh_check_access('unlink')
            parents._eh_check_access('write')
        parents._eh_lock_sources()._eh_validate_references()._check_open()
        return super().unlink()
