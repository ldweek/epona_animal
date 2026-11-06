# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.cost.variance.run: the two-way variance decomposition of a period.

Sign convention (documented once, used everywhere): ADVERSE POSITIVE,
FAVOURABLE NEGATIVE. Every variance is "actual cost above standard", so a
positive amount is an adverse variance (a debit when posted) and a negative
amount is favourable (a credit when posted).

Formulas, per actual and element, with units = actual units produced and
each amount rounded to company currency (2dp) at the step shown:

  variable elements (material / labour / variable overhead)
    flexible  = round2(std_price x actual_qty)
    allowed   = std_qty_per_unit x units          (std qty allowed)
    absorbed  = round2(std_price x allowed)
    price-type variance = actual_cost - flexible
        material -> PRICE   = (actual price - std price) x actual qty
        labour   -> RATE    = (actual rate  - std rate ) x actual hours
        var. OH  -> SPEND   = actual VOH - std rate x actual driver qty
    quantity-type variance = flexible - absorbed
        material -> USAGE      = (actual qty - allowed) x std price
        labour   -> EFFICIENCY = (actual hrs - allowed) x std rate
        var. OH  -> EFFICIENCY = (driver qty - allowed) x std rate

  fixed overhead
    budget    = round2(fixed rate per unit x normal capacity)
    absorbed  = round2(fixed rate per unit x units)
    SPEND     = actual fixed OH - budget
    VOLUME    = budget - absorbed

Reconciliation identity (enforced by constraint and asserted after every
compute): because each element's two variances telescope
(price + quantity = actual - absorbed), the sum of ALL variance lines
equals total actual cost minus total standard cost absorbed, exactly, at
2dp. Total absorbed is the sum of the PER-ELEMENT rounded absorbed amounts
(the same figures inside the variance formulas), so the identity holds to
the cent by construction.

Posting is OPTIONAL and OFF by default (analysis-only mode): nothing
touches the ledger unless "Post Variances" is enabled and per-kind variance
accounts plus an absorption account are configured. The posted entry
aggregates lines per variance kind (net adverse = debit, net favourable =
credit) with the absorption account carrying the balancing leg; it is
sealed against edit and the run freezes with it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .cost_card import COST_ELEMENTS, ELEMENT_ORDER
from .costing_sequence import next_company_reference

VARIANCE_KINDS = [
    ('price', "Price"),
    ('usage', "Usage"),
    ('rate', "Rate"),
    ('efficiency', "Efficiency"),
    ('spend', "Spend"),
    ('volume', "Volume"),
]

KIND_ORDER = [key for key, _label in VARIANCE_KINDS]

# element -> (price-type kind, quantity-type kind)
ELEMENT_KINDS = {
    'material': ('price', 'usage'),
    'labour': ('rate', 'efficiency'),
    'variable_overhead': ('spend', 'efficiency'),
    'fixed_overhead': ('spend', 'volume'),
}

LINE_LABELS = {
    ('material', 'price'): "Material price variance",
    ('material', 'usage'): "Material usage variance",
    ('labour', 'rate'): "Labour rate variance",
    ('labour', 'efficiency'): "Labour efficiency variance",
    ('variable_overhead', 'spend'): "Variable overhead spend variance",
    ('variable_overhead', 'efficiency'):
        "Variable overhead efficiency variance",
    ('fixed_overhead', 'spend'): "Fixed overhead spend variance",
    ('fixed_overhead', 'volume'): "Fixed overhead volume variance",
}

COSTING_ENGINE_CTX = 'eh_costing_engine'
COSTING_MOVE_PROVENANCE_CTX = 'eh_costing_move_provenance'

_ENGINE_OWNED_FIELDS = (
    'total_actual_cost', 'total_absorbed_cost', 'total_variance',
    'reversal_move_id', 'reversed_at', 'reversed_by_id',
)

_ACCOUNT_FIELDS = (
    'price_variance_account_id', 'usage_variance_account_id',
    'rate_variance_account_id', 'efficiency_variance_account_id',
    'spend_variance_account_id', 'volume_variance_account_id',
    'absorption_account_id',
)


class EhCostVarianceRun(models.Model):
    _name = 'eh.cost.variance.run'
    _description = "Standard cost variance run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
                'eh.post.once']
    _order = 'period_start desc, id desc'

    # State moves only through the run's own actions (compute / post /
    # reset / cancel), never a direct write: a draft run's state is not
    # otherwise frozen, so a raw write({'state': 'posted'}) would skip
    # action_post and its sealed journal entry.
    _eh_guarded_fields = ('state',) + _ENGINE_OWNED_FIELDS

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('posted', "Posted"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True,
        copy=False)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)

    actual_ids = fields.Many2many(
        'eh.cost.actual', 'eh_cost_variance_run_actual_rel',
        'run_id', 'actual_id', string="Period Actuals",
        domain="[('company_id', '=', company_id)]",
        help="Actual captures decomposed by this run; each brings its own "
             "cost card.")

    line_ids = fields.One2many(
        'eh.cost.variance.line', 'run_id', string="Variance Lines",
        copy=False)

    total_actual_cost = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Sum of every element's actual cost across the selected "
             "actuals.")
    total_absorbed_cost = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        string="Total Standard Cost Absorbed",
        help="Sum of the per-element standard cost absorbed "
             "(std price x std qty allowed; fixed rate x output).")
    total_variance = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Total actual cost minus total standard cost absorbed; equals "
             "the sum of the variance lines exactly (adverse positive, "
             "favourable negative).")

    post_variances = fields.Boolean(
        default=False, tracking=True, string="Post Variances",
        help="OFF (default): analysis-only mode, nothing touches the "
             "ledger. ON: Post books the variance set as one sealed "
             "journal entry against the accounts below.")
    price_variance_account_id = fields.Many2one(
        'account.account', string="Price Variance Account", tracking=True)
    usage_variance_account_id = fields.Many2one(
        'account.account', string="Usage Variance Account", tracking=True)
    rate_variance_account_id = fields.Many2one(
        'account.account', string="Rate Variance Account", tracking=True)
    efficiency_variance_account_id = fields.Many2one(
        'account.account', string="Efficiency Variance Account",
        tracking=True)
    spend_variance_account_id = fields.Many2one(
        'account.account', string="Spend Variance Account", tracking=True)
    volume_variance_account_id = fields.Many2one(
        'account.account', string="Volume Variance Account", tracking=True)
    absorption_account_id = fields.Many2one(
        'account.account', string="Absorption Account", tracking=True,
        help="Carries the balancing leg of the variance entry (the net "
             "over- or under-absorption of standard cost).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_ids = fields.One2many(
        'account.move', 'eh_cost_variance_run_id', copy=False)
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help="Verified sealed counter-entry created by Reverse Posting.")
    reversal_date = fields.Date(
        copy=False,
        help=(
            "Required accounting date for the controlled reversal. The "
            "workflow refuses any silent lock-date shift."
        ),
    )
    reversed_at = fields.Datetime(readonly=True, copy=False)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _check_period = models.Constraint(
        'CHECK (period_end >= period_start)',
        'The period end cannot precede the period start.')

    # Once posted, the run is the audit record behind a sealed journal
    # entry; its inputs and configuration freeze.
    _FROZEN_FIELDS = (
        'period_start', 'period_end', 'actual_ids', 'post_variances',
        'company_id', 'price_variance_account_id',
        'usage_variance_account_id', 'rate_variance_account_id',
        'efficiency_variance_account_id', 'spend_variance_account_id',
        'volume_variance_account_id', 'absorption_account_id', 'journal_id')
    _FROZEN_STATES = ('computed', 'posted', 'reversed')

    def _compute_move_count(self):
        for run in self:
            run.move_count = len(run.move_ids)

    @api.model_create_multi
    def create(self, vals_list):
        actual_ids = set()
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            actual_ids.update(self._eh_command_record_ids(
                vals.get('actual_ids', [])))
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(company_id)
                sequence_date = vals.get('period_start') \
                    or fields.Date.context_today(self)
                vals['name'] = next_company_reference(
                    self.env, 'eh.cost.variance.run', company, sequence_date,
                ) or '/'
        self._eh_lock_for_transition(extra_actual_ids=actual_ids)
        runs = super().create(vals_list)
        runs._eh_validate_references()
        return runs

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A variance run may only use a company enabled in the "
                "caller's current company context."
            ))

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    def _eh_raw_actual_ids(self):
        """Return relation-table ids, including ids hidden by record rules."""
        result = {run_id: [] for run_id in self.ids}
        if not result:
            return result
        self.env.cr.execute(
            "SELECT run_id, actual_id "
            "FROM eh_cost_variance_run_actual_rel "
            "WHERE run_id IN %s ORDER BY run_id, actual_id",
            (tuple(sorted(result)),),
        )
        for run_id, actual_id in self.env.cr.fetchall():
            result[run_id].append(actual_id)
        return result

    @api.model
    def _eh_command_record_ids(self, commands):
        """Extract existing ids touched by x2many ORM commands."""
        result = set()
        for command in commands or ():
            operation = command[0]
            if operation == 6:
                result.update(command[2] or ())
            elif operation in (1, 2, 3, 4) and command[1]:
                result.add(command[1])
        return {int(record_id) for record_id in result if record_id}

    def _eh_validate_references(
        self, expected_state=None, expected_reversal_move_id=None,
        access_mode='write',
    ):
        if not self.env.su:
            self._eh_check_access(access_mode)
        actual_ids_by_run = self._eh_raw_actual_ids()
        for run in self:
            self._eh_assert_company_allowed(run.company_id.id)
            company = run.company_id
            actuals = self.env['eh.cost.actual'].browse(
                actual_ids_by_run.get(run.id, []))
            if not self.env.su:
                actuals._eh_check_access('read')
            for actual in actuals:
                if actual.company_id != company:
                    raise UserError(_(
                        "Every costing actual must belong to the variance "
                        "run company."
                    ))
                if not self.env.su:
                    actual.card_id._eh_check_access('read')
                if actual.card_id.company_id != company:
                    raise UserError(_(
                        "Every costing actual's cost card must belong to the "
                        "variance run company."
                    ))
            if run.journal_id:
                if not self.env.su:
                    run.journal_id._eh_check_access('read')
                if run.journal_id.company_id != company:
                    raise UserError(_(
                        "The costing journal must belong to the run company."
                    ))
            for field_name in _ACCOUNT_FIELDS:
                account = run[field_name]
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if company.id not in self._eh_account_company_ids(account):
                    raise UserError(_(
                        "Every variance posting account must belong to the "
                        "run company."
                    ))
            moves = self.env['account.move'].sudo().search([
                ('eh_cost_variance_run_id', '=', run.id),
            ])
            if not self.env.su:
                self.env['account.move'].browse(moves.ids)._eh_check_access(
                    'read')
            bad_moves = moves.filtered(
                lambda move: move.company_id != company
                or move.move_type != 'entry'
                or not move.eh_sealed
            )
            if bad_moves:
                raise UserError(_(
                    "A linked variance move failed its sealed provenance "
                    "check."
                ))
            effective_state = expected_state or run.state
            reversal = self.env['account.move'].browse(
                expected_reversal_move_id
                if expected_reversal_move_id is not None
                else run.reversal_move_id.id
            )
            if effective_state == 'posted':
                if (
                    len(moves) != 1
                    or moves.state != 'posted'
                    or reversal
                ):
                    raise UserError(_(
                        "A posted variance run must retain exactly one "
                        "posted, sealed source move."
                    ))
            elif effective_state == 'reversed':
                originals = moves - reversal
                if (
                    len(moves) != 2
                    or len(originals) != 1
                    or not reversal
                    or reversal not in moves
                    or originals.state != 'posted'
                    or reversal.state != 'posted'
                    or reversal.reversed_entry_id != originals
                    or not originals.eh_sealed
                    or not reversal.eh_sealed
                ):
                    raise UserError(_(
                        "A reversed variance run must retain one verified "
                        "sealed original and its verified sealed counter-entry."
                    ))
                reversal._eh_validate_verified_reversal(originals)
            elif moves or reversal:
                raise UserError(_(
                    "An unposted variance run cannot already carry a "
                    "variance-move provenance link."
                ))
        return self

    def _eh_lock_for_transition(self, extra_actual_ids=()):
        """Serialize only the run, actual and card rows being transitioned."""
        run_ids = tuple(sorted(self.ids))
        if run_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_cost_variance_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (run_ids,),
            )
            self.invalidate_recordset()
        actual_ids = tuple(sorted({
            actual_id
            for ids in self._eh_raw_actual_ids().values()
            for actual_id in ids
        } | {
            int(actual_id) for actual_id in extra_actual_ids if actual_id
        }))
        if actual_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_cost_actual WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (actual_ids,),
            )
            actuals = self.env['eh.cost.actual'].browse(actual_ids)
            actuals.invalidate_recordset()
            card_ids = tuple(sorted(set(actuals.mapped('card_id').ids)))
            if card_ids:
                self.env.cr.execute(
                    "SELECT id FROM eh_cost_card WHERE id IN %s "
                    "ORDER BY id FOR UPDATE", (card_ids,))
                self.env['eh.cost.card'].browse(
                    card_ids).invalidate_recordset()
        return self

    def _eh_prepare_transition(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        return caller._eh_workflow_action().with_context(
            **{COSTING_ENGINE_CTX: True})

    def write(self, vals):
        if set(vals) & ({'company_id', 'actual_ids', 'journal_id'}
                        | set(_ACCOUNT_FIELDS)):
            for run in self:
                self._eh_assert_company_allowed(
                    vals.get('company_id', run.company_id.id))
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        sanctioned_state_change = (
            self.env.su
            and (
                self.env.context.get('eh_costing_state_change')
                or self.env.context.get(COSTING_ENGINE_CTX)
            )
        )
        expected_state = (
            vals.get('state') if sanctioned_state_change else None)
        if frozen or set(vals).intersection(
            {'state'} | set(_ENGINE_OWNED_FIELDS)
        ):
            self._eh_validate_references(
                expected_state=expected_state,
                expected_reversal_move_id=(
                    vals.get('reversal_move_id')
                    if 'reversal_move_id' in vals else None
                ),
            )
            self._eh_lock_for_transition(extra_actual_ids=(
                self._eh_command_record_ids(vals.get('actual_ids', []))
                if 'actual_ids' in vals else ()
            ))
            self._eh_validate_references(
                expected_state=expected_state,
                expected_reversal_move_id=(
                    vals.get('reversal_move_id')
                    if 'reversal_move_id' in vals else None
                ),
            )
        if frozen and not (
            self.env.su and self.env.context.get(COSTING_ENGINE_CTX)
        ):
            posted = self.filtered(
                lambda r: r.state in self._FROZEN_STATES)
            if posted:
                raise UserError(_(
                    "A computed, posted or reversed variance run is frozen "
                    "(%(fields)s). Reset a computed run to draft before "
                    "changing its inputs or posting setup.",
                    fields=', '.join(frozen)))
        if 'state' in vals and not sanctioned_state_change:
            crossing = self.filtered(
                lambda r: r.state in self._FROZEN_STATES
                and r.state != vals['state'])
            if crossing:
                raise UserError(_(
                    "A computed, posted or reversed variance run cannot be "
                    "re-keyed directly to another state; use its workflow "
                    "actions."))
        result = super().write(vals)
        if set(vals) & (
            {'company_id', 'actual_ids', 'journal_id', 'state'}
            | set(_ACCOUNT_FIELDS)
            | set(_ENGINE_OWNED_FIELDS)
        ):
            self._eh_validate_references()
        return result

    def unlink(self):
        self._eh_validate_references(access_mode='unlink')
        self._eh_lock_for_transition()
        self._eh_validate_references(access_mode='unlink')
        posted = self.filtered(lambda r: r.state == 'posted' or r.move_ids)
        if posted:
            raise UserError(_(
                "A posted variance run cannot be deleted; its journal "
                "entry would be orphaned."))
        return super().unlink()

    # ---- reconciliation identity ----

    def _check_reconciliation(self):
        """sum(variance lines) == total actual - total absorbed, exactly.

        Called after every compute and re-checked from the line-level
        constraint, so the identity cannot silently break.
        """
        for run in self:
            if not run.line_ids:
                continue
            total = sum(run.line_ids.mapped('amount'))
            expected = run.total_actual_cost - run.total_absorbed_cost
            if run.currency_id.compare_amounts(total, expected) != 0:
                raise ValidationError(_(
                    "Variance run %(run)s no longer reconciles: the lines "
                    "sum to %(total).2f but actual minus absorbed is "
                    "%(expected).2f. Recompute the run.",
                    run=run.name, total=total, expected=expected))

    # ---- actions ----

    def action_compute(self):
        self = self._eh_prepare_transition()
        self.ensure_one()
        if self.state not in ('draft', 'computed'):
            raise UserError(_(
                "Only a draft or computed run can be (re)computed."))
        if not self.actual_ids:
            raise UserError(_(
                "Select the period actuals to decompose first."))
        bad_company = self.actual_ids.filtered(
            lambda a: a.company_id != self.company_id)
        if bad_company:
            raise UserError(_(
                "Actuals %s belong to another company.",
                ', '.join(bad_company.mapped('name'))))
        draft_cards = self.actual_ids.card_id.filtered(
            lambda c: c.state == 'draft')
        if draft_cards:
            raise UserError(_(
                "Activate cost card(s) %s first; draft standards are not "
                "final.", ', '.join(draft_cards.mapped('display_name'))))
        outside_period = self.actual_ids.filtered(
            lambda actual:
                actual.period_start < self.period_start
                or actual.period_end > self.period_end
        )
        if outside_period:
            raise UserError(_(
                "Every selected actual period must be fully contained in "
                "the variance-run period. Review: %s",
                ', '.join(outside_period.mapped('display_name')),
            ))
        missing_by_actual = []
        for actual in self.actual_ids:
            expected = set(actual.card_id.line_ids.mapped('element'))
            captured = set(actual.line_ids.mapped('element'))
            missing = expected - captured
            if missing:
                labels = dict(COST_ELEMENTS)
                missing_by_actual.append('%s: %s' % (
                    actual.display_name,
                    ', '.join(labels[element] for element in ELEMENT_ORDER
                              if element in missing),
                ))
        if missing_by_actual:
            raise UserError(_(
                "Each standard cost element needs an explicit actual line "
                "(use zero quantity/cost when nil). Missing: %s",
                '; '.join(missing_by_actual),
            ))

        engine = self
        engine.line_ids.unlink()
        currency = self.currency_id
        line_vals = []
        total_actual = total_absorbed = 0.0
        for actual in self.actual_ids:
            card = actual.card_id
            units = actual.units_produced or 0.0
            card_lines = {l.element: l for l in card.line_ids}
            act_lines = {l.element: l for l in actual.line_ids}
            for element in ELEMENT_ORDER:
                if element not in card_lines and element not in act_lines:
                    continue
                cline = card_lines.get(element)
                aline = act_lines.get(element)
                std_qty = cline.std_qty if cline else 0.0
                std_price = cline.std_price if cline else 0.0
                actual_qty = aline.actual_qty_total if aline else 0.0
                actual_cost = aline.actual_cost_total if aline else 0.0
                price_kind, qty_kind = ELEMENT_KINDS[element]
                if element == 'fixed_overhead':
                    rate = std_qty * std_price
                    budget = currency.round(
                        rate * (card.normal_capacity or 0.0))
                    absorbed = currency.round(rate * units)
                    price_var = currency.round(actual_cost - budget)
                    qty_var = currency.round(budget - absorbed)
                    allowed = card.normal_capacity or 0.0
                    flexible = budget
                else:
                    flexible = currency.round(std_price * actual_qty)
                    allowed = std_qty * units
                    absorbed = currency.round(std_price * allowed)
                    price_var = currency.round(actual_cost - flexible)
                    qty_var = currency.round(flexible - absorbed)
                total_actual += actual_cost
                total_absorbed += absorbed
                common = {
                    'run_id': self.id, 'actual_id': actual.id,
                    'element': element, 'std_price': round(std_price, 4),
                    'actual_qty': round(actual_qty, 4),
                    'std_qty_allowed': round(allowed, 4),
                    'actual_cost': actual_cost,
                    'flexible_amount': flexible,
                    'absorbed_amount': absorbed,
                }
                line_vals.append(dict(
                    common, kind=price_kind, amount=price_var,
                    name=LINE_LABELS[(element, price_kind)]))
                line_vals.append(dict(
                    common, kind=qty_kind, amount=qty_var,
                    name=LINE_LABELS[(element, qty_kind)]))
        self.env['eh.cost.variance.line'].with_context(
            **{COSTING_ENGINE_CTX: True}).create(line_vals)
        self.write({
            'total_actual_cost': currency.round(total_actual),
            'total_absorbed_cost': currency.round(total_absorbed),
            'total_variance': currency.round(
                total_actual - total_absorbed),
            'state': 'computed',
        })
        # Defensive: the telescoping construction makes this exact; a
        # failure here is an engine bug, never user error.
        self._check_reconciliation()
        return True

    def action_post(self):
        """Book the variance set as ONE sealed journal entry, aggregated
        per variance kind: net adverse = debit, net favourable = credit,
        with the absorption account carrying the balancing leg. Refused in
        analysis-only mode (posting is opt-in per run, default OFF)."""
        self = self._eh_prepare_transition()
        self.ensure_one()
        self._check_manager()
        if self.state != 'computed':
            raise UserError(_("Compute the variance run before posting."))
        if self.move_ids:
            raise UserError(_(
                "This variance run already carries journal-entry evidence."
            ))
        # Actual captures remain editable while a run is only analytical.
        # Rebuild under the source locks already held by this transaction so
        # Post can never book a stale earlier computation.
        self.action_compute()
        self.invalidate_recordset()
        # Idempotency: the same period actuals must not be booked twice. The
        # actual freeze only blocks EDITING an actual, not adding it to a
        # second run, so without this guard a second run over the same
        # actual_ids would double-count the period's variances to the GL.
        self._eh_assert_source_unposted('actual_ids')
        if not self.post_variances:
            raise UserError(_(
                "%s is in analysis-only mode (the default): the variance "
                "decomposition never touches the ledger. Enable Post "
                "Variances and configure the variance accounts to book "
                "the entry.", self.display_name))
        currency = self.currency_id
        by_kind = {}
        for line in self.line_ids:
            by_kind[line.kind] = by_kind.get(line.kind, 0.0) + line.amount
        by_kind = {k: currency.round(v) for k, v in by_kind.items()
                   if not currency.is_zero(currency.round(v))}
        if not by_kind:
            raise UserError(_(
                "Every variance nets to nil; there is nothing to post."))

        kind_accounts = {
            'price': self.price_variance_account_id,
            'usage': self.usage_variance_account_id,
            'rate': self.rate_variance_account_id,
            'efficiency': self.efficiency_variance_account_id,
            'spend': self.spend_variance_account_id,
            'volume': self.volume_variance_account_id,
        }
        missing = [_("journal")] if not self.journal_id else []
        if not self.absorption_account_id:
            missing.append(_("absorption account"))
        labels = dict(VARIANCE_KINDS)
        for kind in KIND_ORDER:
            if kind in by_kind and not kind_accounts[kind]:
                missing.append(_(
                    "%s variance account", labels[kind]))
        if missing:
            raise UserError(_(
                "Configure the %s on %s first.",
                ', '.join(missing), self.display_name))

        legs = []
        net_total = 0.0
        for kind in KIND_ORDER:
            if kind not in by_kind:
                continue
            amount = by_kind[kind]
            net_total += amount
            label = _("%(kind)s variance %(run)s",
                      kind=labels[kind], run=self.name)
            if amount > 0:
                legs.append((kind_accounts[kind], amount, 0.0, label))
            else:
                legs.append((kind_accounts[kind], 0.0, -amount, label))
        net_total = currency.round(net_total)
        if net_total > 0:
            legs.append((self.absorption_account_id, 0.0, net_total,
                         _("Under-absorption %s", self.name)))
        elif net_total < 0:
            legs.append((self.absorption_account_id, -net_total, 0.0,
                         _("Over-absorption %s", self.name)))
        self._post_move(legs)
        self.with_context(eh_costing_state_change=True).state = 'posted'
        self._eh_validate_references()
        return True

    def action_reverse(self):
        """Post one exact-date sealed counter-entry for a posted run."""
        with self.env.cr.savepoint():
            run = self._eh_prepare_transition()
            run.ensure_one()
            run._check_manager()
            run._eh_reverse_locked()
        return True

    def _eh_reverse_locked(self):
        self.ensure_one()
        if self.state != 'posted':
            raise UserError(_(
                "Only a posted variance run can be reversed."
            ))
        if not self.reversal_date:
            raise UserError(_(
                "Enter an explicit reversal accounting date first."
            ))
        if self.reversal_date > fields.Date.context_today(self):
            raise UserError(_(
                "A variance reversal cannot use a future accounting date."
            ))
        self._eh_validate_references()
        original = self.move_ids
        reversal = original._eh_reverse_with_verified_capability(
            default_values_list=[{
                'date': self.reversal_date,
                'ref': _("Reversal of variance run %s", self.name),
            }],
            cancel=False,
        )
        reversal._eh_post_verified_reversal()
        reversal.invalidate_recordset(['state', 'date', 'eh_sealed'])
        if (
            reversal.state != 'posted'
            or reversal.date != self.reversal_date
            or not reversal.eh_sealed
        ):
            raise UserError(_(
                "The variance reversal did not post on the exact requested "
                "date; the correction was rolled back."
            ))
        reversal.sudo().with_context(**{
            COSTING_MOVE_PROVENANCE_CTX: True,
        })._eh_write_sealed_metadata(
            {'eh_cost_variance_run_id': self.id},
            {'eh_cost_variance_run_id'},
        )
        reversal.invalidate_recordset(['eh_cost_variance_run_id'])
        self.with_context(eh_costing_state_change=True).write({
            'state': 'reversed',
            'reversal_move_id': reversal.id,
            'reversed_at': fields.Datetime.now(),
            'reversed_by_id': self.env.user.id,
        })
        self._eh_validate_references()
        return reversal

    def action_reset_to_draft(self):
        self = self._eh_prepare_transition()
        self.ensure_one()
        if self.state not in ('computed', 'cancelled'):
            raise UserError(_(
                "Only a computed or cancelled run can go back to draft."))
        self.line_ids.unlink()
        self.write({
            'total_actual_cost': 0.0, 'total_absorbed_cost': 0.0,
            'total_variance': 0.0, 'state': 'draft',
        })
        return True

    def action_cancel(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Only a draft or computed variance run can be cancelled; "
                    "posted and reversed journal evidence is immutable."))
            run.line_ids.unlink()
            run.write({
                'total_actual_cost': 0.0,
                'total_absorbed_cost': 0.0,
                'total_variance': 0.0,
                'state': 'cancelled',
            })
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_cost_variance_run_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can post variance entries."))

    def _post_move(self, legs):
        lines = []
        for account, debit, credit, label in legs:
            lines.append((0, 0, {
                'name': label, 'account_id': account.id,
                'debit': debit, 'credit': credit,
            }))
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.period_end or fields.Date.context_today(self),
            'journal_id': self.journal_id.id,
            'ref': self.name,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.sudo().with_context(**{
            COSTING_MOVE_PROVENANCE_CTX: True,
        })._eh_write_sealed_metadata(
            {'eh_cost_variance_run_id': self.id},
            {'eh_cost_variance_run_id'},
        )
        move.action_post()
        return move


class EhCostVarianceLine(models.Model):
    _name = 'eh.cost.variance.line'
    _description = "Standard cost variance line"
    _order = 'run_id, actual_id, id'

    run_id = fields.Many2one(
        'eh.cost.variance.run', required=True, ondelete='cascade',
        index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)
    actual_id = fields.Many2one(
        'eh.cost.actual', required=True, ondelete='restrict', index=True,
        string="Actuals")
    card_id = fields.Many2one(
        related='actual_id.card_id', store=True, string="Cost Card")

    name = fields.Char(required=True, string="Variance")
    element = fields.Selection(COST_ELEMENTS, required=True)
    kind = fields.Selection(VARIANCE_KINDS, required=True, index=True)

    std_price = fields.Float(
        digits=(16, 4), string="Std Price",
        help="Standard price / rate behind this decomposition.")
    actual_qty = fields.Float(
        digits=(16, 4), string="Actual Qty",
        help="Actual input / driver quantity of the element.")
    std_qty_allowed = fields.Float(
        digits=(16, 4), string="Std Qty Allowed",
        help="Standard quantity allowed for the actual output (for fixed "
             "overhead: the normal capacity).")
    actual_cost = fields.Monetary(
        currency_field='currency_id', string="Actual Cost")
    flexible_amount = fields.Monetary(
        currency_field='currency_id', string="Flexed Standard",
        help="Std price x actual quantity (for fixed overhead: the "
             "budget). The pivot between the price-type and quantity-type "
             "variances.")
    absorbed_amount = fields.Monetary(
        currency_field='currency_id', string="Standard Absorbed")
    amount = fields.Monetary(
        currency_field='currency_id', string="Variance Amount",
        help="Adverse positive, favourable negative.")
    is_favourable = fields.Boolean(
        compute='_compute_is_favourable', string="Favourable")

    @api.depends('amount')
    def _compute_is_favourable(self):
        for line in self:
            line.is_favourable = line.amount < 0.0

    @api.constrains('amount')
    def _check_run_reconciles(self):
        # Belt to the engine's own assert: any amount write outside the
        # engine context re-verifies the run's reconciliation identity.
        if self.env.su and self.env.context.get(COSTING_ENGINE_CTX):
            return
        self.mapped('run_id')._check_reconciliation()

    # Variance lines are engine output, never user input: any manual line
    # would break the reconciliation identity, and a line under a posted
    # run backs a sealed journal entry.
    def _check_engine(self, runs=None):
        runs = runs if runs is not None else self.mapped('run_id')
        posted = runs.filtered(lambda r: r.state == 'posted')
        if posted:
            raise UserError(_(
                "The variance lines of a posted run are frozen (%s).",
                ', '.join(posted.mapped('name'))))
        if not (self.env.su and self.env.context.get(COSTING_ENGINE_CTX)):
            raise UserError(_(
                "Variance lines are computed by the run; recompute it "
                "instead of editing them."))

    @api.model_create_multi
    def create(self, vals_list):
        self._check_engine(self.env['eh.cost.variance.run'].browse(
            [v['run_id'] for v in vals_list if v.get('run_id')]))
        lines = super().create(vals_list)
        actual_ids_by_run = lines.mapped('run_id')._eh_raw_actual_ids()
        for line in lines:
            if (
                line.actual_id.company_id != line.run_id.company_id
                or line.actual_id.id not in actual_ids_by_run.get(
                    line.run_id.id, [])
            ):
                raise UserError(_(
                    "A variance line's actual must be one of the run's "
                    "same-company source actuals."
                ))
        return lines

    def write(self, vals):
        self._check_engine()
        return super().write(vals)

    def unlink(self):
        self._check_engine()
        return super().unlink()


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_cost_variance_run_id = fields.Many2one(
        'eh.cost.variance.run', string="Variance Run", readonly=True,
        index=True, ondelete='restrict', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_cost_variance_run_id') for vals in vals_list):
            if not (
                self.env.su
                and self.env.context.get(COSTING_MOVE_PROVENANCE_CTX)
            ):
                raise AccessError(_(
                    "Variance-move provenance is server-owned."
                ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_cost_variance_run_id' in vals and not (
            self.env.su
            and self.env.context.get(COSTING_MOVE_PROVENANCE_CTX)
        ):
            raise AccessError(_(
                "Variance-move provenance is server-owned."
            ))
        return super().write(vals)
