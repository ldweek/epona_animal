# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.cost.card: the standard cost card of one product (or free-form item).

One line per cost element (material, labour, variable overhead, fixed
overhead), each carrying the standard input quantity PER UNIT OF OUTPUT and
the standard price per input unit. The standard cost per unit is the sum of
the element standard costs; the variable standard cost excludes the fixed
overhead element.

Fixed overhead budgeting: the fixed overhead line expresses the standard
fixed rate per unit (std_qty x std_price), and the card's normal capacity
(budgeted output units per period) turns that rate into the period fixed
overhead budget used by the volume variance:

    budget_fixed_overhead = round2(fixed rate per unit x normal_capacity)

Lifecycle: draft -> active -> superseded, one way. A card freezes when it
activates (variance runs and contribution reports reference its standards;
editing them in place would silently move every analysis built on them). A
revision is a NEW card: Activate on the new card supersedes the old active
card of the same product automatically. One active card per product and
company is enforced.

v1 scope note: the card is deliberately ledger- and stock-independent.
Inventory valuation integration (standard-cost valuation layers, WIP
clearing against stock moves) is a documented later wave.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .costing_sequence import next_company_reference

COST_ELEMENTS = [
    ('material', "Material"),
    ('labour', "Labour"),
    ('variable_overhead', "Variable Overhead"),
    ('fixed_overhead', "Fixed Overhead"),
]

ELEMENT_ORDER = [key for key, _label in COST_ELEMENTS]


class EhCostCard(models.Model):
    _name = 'eh.cost.card'
    _description = "Standard cost card"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'id desc'

    # State moves only through action_activate / action_supersede, never a
    # direct write: the one-way lifecycle freezes standards that variance
    # runs and contribution reports already reference.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('superseded', "Superseded")],
        default='draft', required=True, tracking=True, index=True,
        copy=False,
        help="A card freezes when it activates; a revision is a new card "
             "that supersedes this one on activation. Duplicating a card "
             "starts the copy in draft: that IS the revision workflow.")

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    product_id = fields.Many2one(
        'product.product', string="Product", tracking=True, index=True,
        help="Product this card standard-costs. Leave empty and use the "
             "item name for a costing object that is not a product record.")
    item_name = fields.Char(
        string="Item Name", tracking=True,
        help="Free-form costing object when no product record is used "
             "(a service line, a cost centre output, a job class).")

    date_from = fields.Date(
        string="Valid From", tracking=True,
        help="Start of the period this standard is meant for (informative; "
             "runs pick their card through the actuals).")
    date_to = fields.Date(string="Valid To", tracking=True)

    normal_capacity = fields.Float(
        digits=(16, 4), tracking=True, string="Normal Capacity (Units)",
        help="Budgeted output units per period. The fixed overhead budget "
             "is the standard fixed rate per unit times this capacity, and "
             "the volume variance measures actual output against it.")

    line_ids = fields.One2many(
        'eh.cost.card.line', 'card_id', string="Standard Cost Elements")

    std_cost_unit = fields.Float(
        compute='_compute_std_costs', store=True, digits=(16, 4),
        string="Standard Cost / Unit",
        help="Sum of the element standard costs per unit of output.")
    std_variable_cost_unit = fields.Float(
        compute='_compute_std_costs', store=True, digits=(16, 4),
        string="Variable Cost / Unit",
        help="Standard cost per unit excluding the fixed overhead element; "
             "feeds the contribution margin report.")
    std_fixed_cost_unit = fields.Float(
        compute='_compute_std_costs', store=True, digits=(16, 4),
        string="Fixed Overhead / Unit",
        help="Standard fixed overhead rate per unit of output.")
    budget_fixed_overhead = fields.Monetary(
        compute='_compute_std_costs', store=True,
        currency_field='currency_id', string="Fixed Overhead Budget",
        help="Standard fixed rate per unit x normal capacity: the period "
             "fixed overhead budget behind the spend and volume variances.")

    notes = fields.Text()

    _check_capacity = models.Constraint(
        'CHECK (normal_capacity >= 0)',
        'Normal capacity cannot be negative.')

    # Standards frozen once the card is active or superseded: variance runs
    # and contribution reports reference them, and an in-place edit would
    # silently move every analysis already built on the card. A revision is
    # a new card.
    _FROZEN_FIELDS = (
        'product_id', 'item_name', 'normal_capacity', 'company_id')
    _FROZEN_STATES = ('active', 'superseded')

    @api.depends('line_ids.std_cost', 'line_ids.element', 'normal_capacity')
    def _compute_std_costs(self):
        for card in self:
            total = variable = fixed = 0.0
            for line in card.line_ids:
                total += line.std_cost
                if line.element == 'fixed_overhead':
                    fixed += line.std_cost
                else:
                    variable += line.std_cost
            card.std_cost_unit = round(total, 4)
            card.std_variable_cost_unit = round(variable, 4)
            card.std_fixed_cost_unit = round(fixed, 4)
            budget = fixed * (card.normal_capacity or 0.0)
            card.budget_fixed_overhead = (
                card.currency_id.round(budget) if card.currency_id
                else round(budget, 2))

    @api.depends('name', 'product_id', 'item_name')
    def _compute_display_name(self):
        for card in self:
            label = card.product_id.display_name or card.item_name
            card.display_name = ('%s (%s)' % (card.name, label)
                                 if label else card.name)

    @api.constrains('product_id', 'item_name')
    def _check_costing_object(self):
        for card in self:
            if not card.product_id and not card.item_name:
                raise ValidationError(_(
                    "A cost card needs a costing object: pick a product or "
                    "enter an item name."))

    @api.constrains('state', 'product_id', 'company_id')
    def _check_one_active_per_product(self):
        for card in self:
            if card.state != 'active' or not card.product_id:
                continue
            domain = [
                ('state', '=', 'active'),
                ('product_id', '=', card.product_id.id),
                ('company_id', '=', card.company_id.id),
            ]
            if isinstance(card.id, int):
                domain.append(('id', '!=', card.id))
            other = self.search(domain, limit=1)
            if other:
                raise ValidationError(_(
                    "%(product)s already has an active standard cost card "
                    "(%(card)s) in %(company)s. Activate the new card "
                    "through its Activate action, which supersedes the old "
                    "one, or supersede it first.",
                    product=card.product_id.display_name,
                    card=other.name, company=card.company_id.display_name))

    @api.constrains('date_from', 'date_to')
    def _check_validity_dates(self):
        for card in self:
            if card.date_from and card.date_to \
                    and card.date_to < card.date_from:
                raise ValidationError(_(
                    "The validity end date cannot precede the start date."))

    @api.model_create_multi
    def create(self, vals_list):
        active_product_ids = set()
        for vals in vals_list:
            if vals.get('state', 'draft') != 'draft' and not self.env.su:
                raise AccessError(_(
                    "A cost card must be activated through its workflow."
                ))
            self._eh_validate_values(vals)
            if vals.get('state', 'draft') == 'active' \
                    and vals.get('product_id'):
                active_product_ids.add(vals['product_id'])
        self._eh_lock_products(active_product_ids)
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                sequence_date = vals.get('date_from') \
                    or fields.Date.context_today(self)
                vals['name'] = next_company_reference(
                    self.env, 'eh.cost.card', company, sequence_date,
                ) or '/'
        return super().create(vals_list)

    @api.model
    def _eh_validate_values(self, vals, current=None):
        if current and not self.env.su:
            current._eh_check_access('write')
        company_id = vals.get(
            'company_id', current.company_id.id if current else self.env.company.id)
        if (
            not self.env.su
            and company_id not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A cost card may only use a company enabled in the caller's "
                "current company context."
            ))
        product = self.env['product.product'].browse(vals.get(
            'product_id', current.product_id.id if current else False))
        if product:
            if not self.env.su:
                product._eh_check_access('read')
            if product.company_id and product.company_id.id != company_id:
                raise UserError(_(
                    "A company-specific cost-card product must belong to the "
                    "card company."
                ))
        return company_id

    @api.model
    def _eh_lock_products(self, product_ids):
        product_ids = tuple(sorted({
            int(product_id) for product_id in product_ids if product_id
        }))
        if product_ids:
            self.env.cr.execute(
                "SELECT id FROM product_product WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (product_ids,))

    def _eh_lock_for_update(self, lock_products=False):
        """Lock only the costing objects that can race with this write.

        A company-wide lock serialized unrelated cards, actual captures and
        reports.  Product locks are needed only by activation, where they
        protect the one-active-standard invariant for that exact product.
        """
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_cost_card WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            self.invalidate_recordset()
        if lock_products:
            self._eh_lock_products(self.mapped('product_id').ids)
        return self

    # One-way lifecycle: reopening an activated card would silently unfreeze
    # standards that variance runs and contribution reports already used.
    _ALLOWED_TRANSITIONS = {('draft', 'active'), ('active', 'superseded')}

    def write(self, vals):
        for card in self:
            self._eh_validate_values(vals, current=card)
        self._eh_lock_for_update()
        for card in self:
            self._eh_validate_values(vals, current=card)
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        if frozen:
            posted = self.filtered(
                lambda c: c.state in self._FROZEN_STATES)
            if posted:
                raise UserError(_(
                    "Standards (%(fields)s) are frozen on an activated cost "
                    "card. Create a new card and activate it; activation "
                    "supersedes this one.",
                    fields=', '.join(frozen)))
        if 'state' in vals:
            bad = self.filtered(
                lambda c: c.state != vals['state']
                and (c.state, vals['state'])
                not in self._ALLOWED_TRANSITIONS)
            if bad:
                raise UserError(_(
                    "A cost card only moves draft -> active -> superseded; "
                    "%s cannot be re-keyed backwards. A revision is a new "
                    "card.", ', '.join(bad.mapped('display_name'))))
        return super().write(vals)

    def unlink(self):
        for card in self:
            self._eh_validate_values({}, current=card)
        self._eh_lock_for_update()
        for card in self:
            self._eh_validate_values({}, current=card)
        referenced = self.filtered(lambda c: c.state != 'draft')
        if referenced:
            raise UserError(_(
                "An activated cost card cannot be deleted; actuals and "
                "variance runs may reference its standards. Supersede it "
                "instead."))
        return super().unlink()

    # ---- actions ----

    def action_activate(self):
        self.ensure_one()
        caller = self
        caller._eh_validate_values({}, current=caller)
        caller._eh_lock_for_update(lock_products=True)
        caller._eh_validate_values({}, current=caller)
        self = caller._eh_workflow_action()
        if self.state != 'draft':
            raise UserError(_("Only a draft cost card can be activated."))
        if not self.line_ids:
            raise UserError(_(
                "Add at least one standard cost element before activating "
                "%s.", self.display_name))
        if self.product_id:
            others = self.search([
                ('state', '=', 'active'),
                ('product_id', '=', self.product_id.id),
                ('company_id', '=', self.company_id.id),
                ('id', '!=', self.id),
            ])
            for old in others:
                old.state = 'superseded'
                old.message_post(body=_(
                    "Superseded by %s on activation.", self.display_name))
                self.message_post(body=_(
                    "Supersedes %s.", old.display_name))
        self.state = 'active'
        return True

    def action_supersede(self):
        caller = self
        for card in caller:
            card._eh_validate_values({}, current=card)
        caller._eh_lock_for_update()
        for card in caller:
            card._eh_validate_values({}, current=card)
        self = caller._eh_workflow_action()
        for card in self:
            if card.state != 'active':
                raise UserError(_(
                    "Only an active cost card can be superseded."))
            card.state = 'superseded'
        return True


class EhCostCardLine(models.Model):
    _name = 'eh.cost.card.line'
    _description = "Standard cost card element"
    _order = 'card_id, id'

    card_id = fields.Many2one(
        'eh.cost.card', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='card_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='card_id.currency_id', store=True, readonly=True)

    element = fields.Selection(
        COST_ELEMENTS, required=True, default='material',
        help="One line per element and card: the variance run splits each "
             "element into its price/rate/spend and usage/efficiency/volume "
             "components against a single standard quantity and price. "
             "Multi-input bills per element are a later wave.")
    name = fields.Char(string="Description")
    uom_name = fields.Char(
        string="Input Unit", help="Free-text input unit (kg, hr, kWh).")
    std_qty = fields.Float(
        digits=(16, 4), string="Std Qty / Unit",
        help="Standard input quantity per unit of output.")
    std_price = fields.Float(
        digits=(16, 4), string="Std Price",
        help="Standard price per input unit.")
    std_cost = fields.Float(
        compute='_compute_std_cost', store=True, digits=(16, 4),
        string="Std Cost / Unit",
        help="std_qty x std_price, rounded to 4 decimals.")

    _check_qty = models.Constraint(
        'CHECK (std_qty >= 0)', 'A standard quantity cannot be negative.')
    _check_price = models.Constraint(
        'CHECK (std_price >= 0)', 'A standard price cannot be negative.')

    @api.depends('std_qty', 'std_price')
    def _compute_std_cost(self):
        for line in self:
            line.std_cost = round(
                (line.std_qty or 0.0) * (line.std_price or 0.0), 4)

    @api.constrains('element', 'card_id')
    def _check_element_unique(self):
        # Cache-based sibling check (no search): safe on the create path and
        # under NewId in onchange.
        for line in self:
            siblings = line.card_id.line_ids.filtered(
                lambda l: l.element == line.element)
            if len(siblings) > 1:
                raise ValidationError(_(
                    "Cost card %(card)s already has a %(element)s line; one "
                    "line per element per card.",
                    card=line.card_id.display_name,
                    element=dict(COST_ELEMENTS)[line.element]))

    # The parent's standards freeze when it activates; its element lines ARE
    # the standards, so they freeze with it at create, write and unlink (a
    # raw line edit would silently desync every analysis on the card).
    def _check_parent_open(self, cards=None):
        cards = cards if cards is not None else self.mapped('card_id')
        frozen = cards.filtered(
            lambda c: c.state in c._FROZEN_STATES)
        if frozen:
            raise UserError(_(
                "The standard cost elements of an activated card are frozen "
                "(%s). Create a new card revision instead.",
                ', '.join(frozen.mapped('display_name'))))

    def _eh_lock_parents(self, extra_card_ids=()):
        cards = self.mapped('card_id') | self.env['eh.cost.card'].browse(
            [card_id for card_id in extra_card_ids if card_id])
        if not self.env.su:
            cards._eh_check_access('write')
        for card in cards:
            card._eh_validate_values({}, current=card)
        cards._eh_lock_for_update()
        for card in cards:
            card._eh_validate_values({}, current=card)
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_cost_card_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            self.invalidate_recordset()
        return cards

    @api.model
    def _eh_assert_unique_values(self, values):
        seen = set()
        for card_id, element, exclude_id in values:
            key = (card_id, element)
            if key in seen:
                raise ValidationError(_(
                    "A cost card may contain only one line per cost element."
                ))
            seen.add(key)
            query = (
                "SELECT id FROM eh_cost_card_line "
                "WHERE card_id = %s AND element = %s"
            )
            params = [card_id, element]
            if exclude_id:
                query += " AND id != %s"
                params.append(exclude_id)
            query += " LIMIT 1"
            self.env.cr.execute(query, tuple(params))
            if self.env.cr.fetchone():
                raise ValidationError(_(
                    "A cost card may contain only one line per cost element."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        # Guard BEFORE the insert: a line added to an activated card must
        # never reach the table.
        cards = self._eh_lock_parents(
            [v['card_id'] for v in vals_list if v.get('card_id')])
        self._check_parent_open(cards)
        self._eh_assert_unique_values([
            (vals.get('card_id'), vals.get('element', 'material'), False)
            for vals in vals_list
        ])
        return super().create(vals_list)

    def write(self, vals):
        cards = self._eh_lock_parents((vals.get('card_id'),))
        self._check_parent_open(cards)
        self._eh_assert_unique_values([
            (
                vals.get('card_id', line.card_id.id),
                vals.get('element', line.element),
                line.id,
            )
            for line in self
        ])
        return super().write(vals)

    def unlink(self):
        cards = self._eh_lock_parents()
        self._check_parent_open(cards)
        return super().unlink()
