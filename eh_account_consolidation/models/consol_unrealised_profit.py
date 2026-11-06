# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.unrealised.profit: unrealised profit in ending inventory that
arose on an intra-group sale (IFRS 10 / IAS 27).

When one group member sells inventory to another at a margin and the
buyer still holds that inventory at period end, the seller's profit is
recognised in the group accounts even though, from the group's point of
view, nothing has been sold to an outside party. Consolidated inventory
is carried above cost and consolidated profit is overstated by the same
amount.

Each record on a run names one such unrealised margin. At compute time
the run emits a balanced pair of elimination run-lines that removes it:

  Dr cost-of-sales / retained-earnings   +amount
  Cr inventory                           -amount

The two legs net to zero, so the consolidated set stays balanced while
inventory is written back to cost and the intra-group profit is
reversed. Records are optional: a run with none behaves exactly as
before.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_FROZEN_RUN_STATES = ('computed', 'reviewed', 'closed')
_COGS_OR_RE_ACCOUNT_TYPES = (
    'expense', 'expense_depreciation', 'expense_direct_cost',
    'equity', 'equity_unaffected',
)


class EhConsolUnrealisedProfit(models.Model):
    _name = 'eh.consol.unrealised.profit'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidation unrealised profit in inventory"
    _order = 'run_id, id'

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id',
        store=True, readonly=True,
    )

    name = fields.Char(
        required=True, default='/',
        help=(
            "Description of the intra-group sale carrying the unrealised "
            "margin, e.g. 'IC sales: AU sub to UK sub, ending stock'."
        ),
    )
    unrealised_amount = fields.Monetary(
        currency_field='presentation_currency_id',
        help=(
            "Profit still sitting in the buyer's ending inventory from an "
            "intra-group sale, in the presentation currency. This is the "
            "margin removed by the elimination: inventory is written back "
            "to group cost and consolidated profit is reduced by the same "
            "amount (IFRS 10 / IAS 27)."
        ),
    )
    inventory_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Inventory Account",
        help=(
            "Asset account on the consolidated chart carrying the buyer's "
            "ending inventory. Credited by the unrealised amount so the "
            "stock is restated to group cost."
        ),
    )
    cogs_or_re_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="COGS / Retained Earnings Account",
        help=(
            "Account debited to reverse the intra-group profit: the "
            "cost-of-sales account when the profit arose in the current "
            "period, or retained earnings when it was recognised in a "
            "prior period."
        ),
    )
    sale_direction = fields.Selection([
        ('downstream', "Downstream (parent to subsidiary)"),
        ('upstream', "Upstream (subsidiary to parent/group)"),
    ], default='downstream', required=True,
        help=(
            "Upstream profit originated in a subsidiary, so the eliminated "
            "profit is attributed between parent and NCI by the seller's "
            "ownership. Downstream profit is wholly parent-attributable."
        ),
    )
    seller_member_id = fields.Many2one(
        'eh.consol.member', ondelete='restrict',
        string="Upstream Seller Member",
        help="Required for an upstream sale so the NCI share is measurable.",
    )

    notes = fields.Char()

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    @api.model
    def _eh_validate_basis(self, run, vals=None, current=None):
        """Prove one inventory-margin basis without elevating privileges."""
        vals = vals or {}
        amount = vals.get(
            'unrealised_amount',
            current.unrealised_amount if current else 0.0,
        ) or 0.0
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = float('nan')
        if not math.isfinite(amount) or amount < 0:
            raise ValidationError(_(
                "Unrealised profit must be a finite, non-negative amount."
            ))

        if amount:
            missing = []
            for field_name in (
                'inventory_account_id', 'cogs_or_re_account_id',
            ):
                if not vals.get(
                    field_name,
                    current[field_name].id if current else False,
                ):
                    missing.append(self._fields[field_name].string)
            if missing:
                raise ValidationError(_(
                    "A non-zero unrealised-profit basis requires complete "
                    "posting accounts: %s.",
                    ', '.join(missing),
                ))

        scope = set(run.entity_id._eh_scope_companies().ids)
        account_specs = (
            (
                'inventory_account_id',
                ('asset_current',),
                _("The inventory account must be a current-asset account."),
            ),
            (
                'cogs_or_re_account_id',
                _COGS_OR_RE_ACCOUNT_TYPES,
                _(
                    "The COGS / retained-earnings account must be an expense "
                    "or equity account."
                ),
            ),
        )
        for field_name, account_types, type_error in account_specs:
            account_id = vals.get(
                field_name,
                current[field_name].id if current else False,
            )
            account = self.env['account.account'].browse(account_id)
            if not account:
                continue
            if not self.env.su:
                account._eh_check_access('read')
            if not (self._eh_account_company_ids(account) & scope):
                raise ValidationError(_(
                    "Unrealised-profit accounts must belong to the "
                    "authorised consolidation company closure."
                ))
            if account.account_type not in account_types:
                raise ValidationError(type_error)
        direction = vals.get(
            'sale_direction', current.sale_direction if current else 'downstream',
        )
        seller = self.env['eh.consol.member'].browse(vals.get(
            'seller_member_id',
            current.seller_member_id.id if current else False,
        ))
        if direction == 'upstream':
            if not seller or seller.entity_id != run.entity_id:
                raise ValidationError(_(
                    "An upstream unrealised-profit basis needs a seller member "
                    "from the same consolidation entity."
                ))
            if seller.method != 'full':
                raise ValidationError(_(
                    "Upstream NCI attribution is supported only for a fully "
                    "consolidated seller member."
                ))
        elif seller:
            raise ValidationError(_(
                "A seller member is used only for an upstream sale."
            ))
        return run

    @api.model
    def _eh_validate_target(self, vals, current=None):
        run_id = vals.get(
            'run_id', current.run_id.id if current else False,
        )
        run = self.env['eh.consol.run'].browse(run_id)
        if not run:
            raise ValidationError(_(
                "An unrealised-profit basis must belong to a consolidation run."
            ))
        run._eh_assert_company_closure()
        run._eh_validate_reference_closure()
        return self._eh_validate_basis(run, vals=vals, current=current)

    @api.model
    def _eh_run_is_frozen(self, run):
        return (
            run.state in _FROZEN_RUN_STATES
            or bool(run.move_id or run.reversal_move_id or run.move_history_ids)
        )

    @api.model
    def _eh_assert_run_editable(self, run):
        if self._eh_run_is_frozen(run):
            raise UserError(_(
                "Unrealised-profit evidence cannot be added, changed, moved "
                "or deleted after its consolidation run is computed or has "
                "accounting history. Create a new run instead."
            ))
        return run

    def _eh_lock_parent_runs(self, extra_run_ids=()):
        runs = self.mapped('run_id') | self.env['eh.consol.run'].browse(
            [run_id for run_id in extra_run_ids if run_id]
        )
        runs._eh_lock_for_transition()
        row_ids = tuple(sorted(self.ids))
        if row_ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, run_id FROM eh_consol_unrealised_profit "
                "WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (row_ids,),
            )
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(row_ids):
                raise UserError(_(
                    "An unrealised-profit basis changed concurrently. Retry "
                    "the operation."
                ))
            if not {row[1] for row in locked_rows}.issubset(set(runs.ids)):
                raise UserError(_(
                    "An unrealised-profit basis changed run concurrently. "
                    "Retry the operation."
                ))
            self.invalidate_recordset()
        return runs

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            self._eh_check_access('create')
        if any('presentation_currency_id' in vals for vals in vals_list):
            raise AccessError(_(
                "Unrealised-profit presentation currency is derived from "
                "its run and cannot be supplied by the caller."
            ))
        for vals in vals_list:
            self._eh_validate_target(vals)
        run_ids = {
            vals.get('run_id') for vals in vals_list if vals.get('run_id')
        }
        self._eh_lock_parent_runs(run_ids)
        for vals in vals_list:
            run = self._eh_validate_target(vals)
            self._eh_assert_run_editable(run)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        if 'presentation_currency_id' in vals:
            raise AccessError(_(
                "Unrealised-profit presentation currency is derived from "
                "its run and cannot be changed directly."
            ))
        for record in self:
            self._eh_validate_target(vals, current=record)
        target_run_ids = (
            (vals.get('run_id'),) if vals.get('run_id') else ()
        )
        runs = self._eh_lock_parent_runs(target_run_ids)
        for run in runs:
            self._eh_assert_run_editable(run)
        for record in self:
            self._eh_validate_target(vals, current=record)
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        for record in self:
            self._eh_validate_target({}, current=record)
        runs = self._eh_lock_parent_runs()
        for run in runs:
            self._eh_assert_run_editable(run)
        for record in self:
            self._eh_validate_target({}, current=record)
        return super().unlink()
