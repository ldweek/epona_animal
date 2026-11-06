# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.nrv.line: one inventory item or group measured at the lower of cost and
net realisable value.

On the item-by-item basis the required write-down is the excess of cost
over net realisable value (IAS 2.9), floored at zero per line. On the
category basis (IAS 2.29 grouping of similar or related items) the run
nets surpluses and deficits within each product category before flooring
at zero, and allocates the category requirement over the deficit lines;
see eh.nrv.run._category_allocation for the allocation rules. Under both
bases a recovery only ever reverses a prior write-down and never lifts
inventory above cost (IAS 2.33).
"""

from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_FROZEN = frozenset({'posted', 'reversed'})


class EhNrvLine(models.Model):
    _name = 'eh.nrv.line'
    _description = "Inventory NRV line"
    _order = 'run_id, sequence, id'

    run_id = fields.Many2one(
        'eh.nrv.run', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True, help="Inventory item or group, e.g. 'Finished goods - "
        "Model A' or 'Raw material X'.")
    product_id = fields.Many2one(
        'product.product', help="Optional link to a product.")
    product_category_id = fields.Many2one(
        'product.category', string="Category", index=True,
        compute='_compute_product_category', store=True, readonly=False,
        help="Group of similar or related items this line is assessed in "
             "when the run uses the category basis (IAS 2.29). Auto-filled "
             "from the product; required on every line of a category-basis "
             "run.")
    cost = fields.Monetary(
        currency_field='currency_id',
        help="Carrying cost of the inventory before any write-down.")
    net_realisable_value = fields.Monetary(
        currency_field='currency_id',
        help="Estimated selling price less the costs to complete and sell "
             "(IAS 2.6).")

    required_writedown = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Excess of cost over net realisable value, floored at zero.")
    opening_writedown = fields.Monetary(
        compute='_compute_opening_writedown', store=True, readonly=True,
        currency_field='currency_id',
        help="Write-down already recognised on this line at the start of the "
             "period. Defaults to the prior posted run's closing write-down "
             "for the same product; a manager can explicitly override it.")
    opening_writedown_manual = fields.Boolean(
        default=False, copy=False,
        string="Manual Opening Override",
        help="Explicit manager-controlled switch for a manual opening. The "
             "presence of an opening value in an RPC payload never infers "
             "override intent.")
    manual_opening_writedown = fields.Monetary(
        currency_field='currency_id', copy=False,
        string="Manual Opening Write-down",
        help="Manager-approved opening amount used only while Manual Opening "
             "Override is enabled.")
    opening_override_reason = fields.Char(
        copy=False,
        help="Required manager explanation for a manual opening override.")
    opening_override_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, string="Opening Approved By")
    opening_override_at = fields.Datetime(
        readonly=True, copy=False, string="Opening Approved At")
    opening_source = fields.Selection(
        [('automatic', "Automatic roll-forward"),
         ('manual', "Manager override")],
        compute='_compute_opening_source', string="Opening Source")
    prior_closing_writedown = fields.Monetary(
        compute='_compute_prior_closing_writedown',
        store=True,
        currency_field='currency_id',
        help="Closing write-down carried by this product on the most recent "
             "posted NRV run, used to roll the opening position forward.")
    opening_tieout = fields.Boolean(
        compute='_compute_opening_tieout',
        string="Opening Does Not Tie Out",
        help="Set when the opening write-down disagrees with the prior posted "
             "run's closing write-down for the same product.")
    movement = fields.Monetary(
        compute='_compute_writedown', store=True, currency_field='currency_id',
        help="Required less opening write-down; negative = recovery.")

    _check_cost = models.Constraint(
        'CHECK (cost >= 0)', 'Cost cannot be negative.')
    _check_nrv = models.Constraint(
        'CHECK (net_realisable_value >= 0)',
        'Net realisable value cannot be negative.')
    _check_opening = models.Constraint(
        'CHECK (opening_writedown >= 0 AND opening_writedown <= cost)',
        'The opening write-down must be between zero and inventory cost.')
    _check_required = models.Constraint(
        'CHECK (required_writedown >= 0 AND required_writedown <= cost)',
        'The required write-down must be between zero and inventory cost.')

    @api.depends('product_id')
    def _compute_product_category(self):
        for line in self:
            if line.product_id:
                line.product_category_id = line.product_id.categ_id
            else:
                # Keep a manually chosen category on a free-text line.
                line.product_category_id = line.product_category_id

    @api.depends('cost', 'net_realisable_value', 'opening_writedown',
                 'run_id.assessment_basis', 'run_id.line_ids.cost',
                 'run_id.line_ids.net_realisable_value',
                 'run_id.line_ids.product_category_id')
    def _compute_writedown(self):
        # Category-basis runs are allocated run-wise so the per-line shares
        # tie exactly to each category's netted requirement (the rounding
        # residual lands on the last deficit line); see
        # eh.nrv.run._category_allocation for the IAS 2.29 rules.
        alloc = {}
        for run in self.run_id:
            if run.assessment_basis == 'category':
                alloc.update(run._category_allocation())
        for line in self:
            currency = line.currency_id
            if line.run_id and line.run_id.assessment_basis == 'category':
                required = alloc.get(line.id, 0.0)
            else:
                required = min(
                    max(line.cost - line.net_realisable_value, 0.0),
                    max(line.cost, 0.0),
                )
                required = currency.round(required) if currency else required
            line.required_writedown = required
            line.movement = line.required_writedown - line.opening_writedown

    @api.depends('opening_writedown_manual')
    def _compute_opening_source(self):
        for line in self:
            line.opening_source = (
                'manual' if line.opening_writedown_manual else 'automatic'
            )

    def _prior_closing_writedown_map(self):
        """Return prior closing allowances for every line with one search.

        Candidate history is fetched once for the full recordset, indexed by
        company/product, and then resolved against each line's own reporting
        cutoff. This keeps the roll-forward query count constant as a run
        grows instead of issuing one search per line and per compute.
        """
        result = {line.id: 0.0 for line in self}
        eligible = self.filtered(
            lambda line: line.product_id and line.run_id.company_id
        )
        if not eligible:
            return result
        cutoffs = {
            line.id: fields.Date.to_date(
                line.run_id.reporting_date
                or fields.Date.context_today(line)
            )
            for line in eligible
        }
        candidates = self.env['eh.nrv.line'].search([
            ('product_id', 'in', eligible.product_id.ids),
            ('run_id.company_id', 'in', eligible.run_id.company_id.ids),
            ('run_id.state', '=', 'posted'),
            ('run_id.reporting_date', '<', max(cutoffs.values())),
        ])
        by_company_product = defaultdict(list)
        for candidate in candidates:
            by_company_product[
                (candidate.run_id.company_id.id, candidate.product_id.id)
            ].append(candidate)
        for bucket in by_company_product.values():
            bucket.sort(
                key=lambda candidate: (
                    candidate.run_id.reporting_date,
                    candidate.run_id.revision,
                    candidate.run_id.id,
                    -candidate.sequence,
                    -candidate.id,
                ),
                reverse=True,
            )
        for line in eligible:
            key = (line.run_id.company_id.id, line.product_id.id)
            prior = next(
                (
                    candidate for candidate in by_company_product.get(key, ())
                    if candidate.run_id.reporting_date < cutoffs[line.id]
                ),
                self.env['eh.nrv.line'],
            )
            if prior:
                result[line.id] = prior.required_writedown
        return result

    def _prior_closing_writedown(self):
        """Closing write-down for this line's product on the most recent
        posted NRV run before this run's reporting date. Zero when there is
        no product link or no prior posted run."""
        self.ensure_one()
        return self._prior_closing_writedown_map().get(self.id, 0.0)

    @api.depends('prior_closing_writedown', 'opening_writedown_manual',
                 'manual_opening_writedown')
    def _compute_opening_writedown(self):
        for line in self:
            target = line.with_context(eh_nrv_auto_opening=True)
            if line.opening_writedown_manual:
                target.opening_writedown = line.manual_opening_writedown
            else:
                target.opening_writedown = line.prior_closing_writedown

    def _check_parent_not_posted(self):
        """Raise when any of these lines hangs off a posted/reversed run."""
        if any(r.state in _FROZEN for r in self.run_id):
            raise UserError(_(
                "This write-down run is posted; its lines can no longer "
                "change. Reverse it to reopen (EH Accounting Manager only)."))

    @api.model
    def _eh_lock_parent_runs(self, runs):
        """Serialize line changes with compute/post/reverse transitions."""
        if not runs:
            return runs
        runs._eh_validate_references()
        runs._eh_lock_for_transition(lock_companies=False)
        runs._eh_validate_references()
        return runs

    @api.model_create_multi
    def create(self, vals_list):
        # Adding a line to a posted run would recompute its closing write-down
        # and silently move the recognised movement, bypassing the freeze that
        # write()/unlink() enforce. Block it when the target run is posted or
        # reversed (IAS 2.34); reverse the run to reopen its lines.
        run_ids = {vals.get('run_id')
                   for vals in vals_list if vals.get('run_id')}
        if run_ids:
            runs = self.env['eh.nrv.run'].browse(run_ids)
            self._eh_lock_parent_runs(runs)
            frozen = runs.filtered(
                lambda r: r.state in _FROZEN)
            if frozen:
                raise UserError(_(
                    "Lines cannot be added to a posted NRV run; its figures "
                    "are frozen. Reverse the run to reopen it (EH Accounting "
                    "Manager only)."))
        for vals in vals_list:
            # Computed values sent back by a web client are never evidence of
            # manual intent. A manual opening needs the explicit manager-only
            # switch and its separate input field.
            vals.pop('opening_writedown', None)
            if {
                'opening_override_by_id', 'opening_override_at',
            } & set(vals):
                raise AccessError(_(
                    "Opening-override approval stamps are server-owned."
                ))
            manual_fields = {
                'opening_writedown_manual', 'manual_opening_writedown',
                'opening_override_reason',
            }
            if manual_fields & set(vals):
                self._check_opening_override_manager()
            if vals.get('opening_writedown_manual'):
                vals.update({
                    'opening_override_by_id': self.env.user.id,
                    'opening_override_at': fields.Datetime.now(),
                })
            else:
                vals.update({
                    'manual_opening_writedown': 0.0,
                    'opening_override_reason': False,
                    'opening_override_by_id': False,
                    'opening_override_at': False,
                })
        return super().create(vals_list)

    @api.depends('product_id', 'run_id.reporting_date', 'company_id')
    def _compute_prior_closing_writedown(self):
        prior_by_line = self._prior_closing_writedown_map()
        for line in self:
            line.with_context(
                eh_nrv_auto_opening=True,
            ).prior_closing_writedown = prior_by_line.get(line.id, 0.0)

    @api.depends('opening_writedown', 'prior_closing_writedown')
    def _compute_opening_tieout(self):
        for line in self:
            currency = line.currency_id
            diff = (
                line.opening_writedown - line.prior_closing_writedown
            )
            line.opening_tieout = (
                not currency.is_zero(diff) if currency
                else abs(diff) > 1e-9)

    def write(self, vals):
        vals = dict(vals)
        if 'prior_closing_writedown' in vals and not self.env.context.get(
                'eh_nrv_auto_opening'):
            raise AccessError(_(
                "The prior closing write-down is server-derived."
            ))
        if 'opening_writedown' in vals and not self.env.context.get(
                'eh_nrv_auto_opening'):
            # Ignore a client echo of the computed field; it must never turn
            # an automatic roll-forward into a manual override.
            vals.pop('opening_writedown')
        server_fields = {'opening_override_by_id', 'opening_override_at'}
        if server_fields & set(vals) and not self.env.context.get(
                'eh_nrv_opening_approval'):
            raise AccessError(_(
                "Opening-override approval stamps are server-owned."
            ))
        manual_fields = {
            'opening_writedown_manual', 'manual_opening_writedown',
            'opening_override_reason',
        }
        if manual_fields & set(vals):
            self._check_opening_override_manager()
            target_manual = vals.get('opening_writedown_manual')
            if target_manual is None:
                if self.filtered(
                        lambda line: not line.opening_writedown_manual):
                    raise UserError(_(
                        "Enable Manual Opening Override before entering a "
                        "manual opening amount or reason."
                    ))
                target_manual = True
            if target_manual:
                vals.update({
                    'opening_override_by_id': self.env.user.id,
                    'opening_override_at': fields.Datetime.now(),
                })
            else:
                vals.update({
                    'manual_opening_writedown': 0.0,
                    'opening_override_reason': False,
                    'opening_override_by_id': False,
                    'opening_override_at': False,
                })
        runs = self.run_id
        if vals.get('run_id'):
            runs |= self.env['eh.nrv.run'].browse(vals['run_id'])
        self._eh_lock_parent_runs(runs)
        locked = {
            'cost', 'net_realisable_value', 'opening_writedown',
            'opening_writedown_manual', 'manual_opening_writedown',
            'opening_override_reason', 'opening_override_by_id',
            'opening_override_at', 'prior_closing_writedown',
            'product_id', 'run_id',
            'product_category_id',
        }
        if locked.intersection(vals):
            self._check_parent_not_posted()
        # Moving a line INTO a posted / reversed run would recompute that run's
        # closing write-down. _check_parent_not_posted only inspects the
        # current (source) parent, so guard the target explicitly.
        if vals.get('run_id'):
            target = self.env['eh.nrv.run'].browse(vals['run_id'])
            if target.state in _FROZEN:
                raise UserError(_(
                    "Lines cannot be moved into a posted NRV run; its figures "
                    "are frozen. Reverse the run to reopen it (EH Accounting "
                    "Manager only)."))
        return super(EhNrvLine, self.with_context(
            eh_nrv_opening_approval=True,
        )).write(vals)

    @api.onchange('opening_writedown_manual')
    def _onchange_opening_writedown_manual(self):
        for line in self:
            if line.opening_writedown_manual \
                    and not line.manual_opening_writedown:
                line.manual_opening_writedown = line.opening_writedown

    @api.model
    def _check_opening_override_manager(self):
        if not self.env.su and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager may set or change a manual "
                "opening write-down."
            ))

    def unlink(self):
        # Deleting a line from a posted run would drop it from the closing
        # write-down and move the recognised figure; block it while the parent
        # run is posted or reversed.
        self._eh_lock_parent_runs(self.run_id)
        self._check_parent_not_posted()
        return super().unlink()
