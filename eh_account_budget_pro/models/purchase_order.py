# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
purchase.order extension: encumbrance lifecycle.

When a purchase order is confirmed, this module looks up matching
budget lines (by account, date, optional analytic) and creates a
commitment per PO line. The commitments deduct availability until
the PO is billed (commitment released, actual takes over) or
cancelled (commitment released, no actual).

The match-then-create logic intentionally tolerates missing budget
lines: a PO for an account that nobody has budgeted simply does not
create commitments. The overrun policy on the budget governs what
happens when a matching line is found and would go negative.

The button_confirm override consults the budget's overrun_policy:

* off: no commitments, no gate. The historical behaviour for sites
  that have not adopted encumbrance accounting.
* warn: commitments are created, the gate posts a chatter warning
  on the budget when availability goes negative, but does not
  block the confirm.
* block: commitments are pre-checked; if any matched budget line
  would go negative after this PO confirms, the confirm raises a
  UserError naming the line.
"""

import logging
import math

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_amount

from .budget_commitment import (
    EH_BUDGET_COMMITMENT_CAPABILITY,
    _CAPABILITY_CONTEXT_KEY,
)


_logger = logging.getLogger(__name__)

_ANALYTIC_PERCENT_TOLERANCE = 1e-6
_ANALYTIC_SHARE_TOLERANCE = _ANALYTIC_PERCENT_TOLERANCE / 100.0


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    eh_budget_commitment_ids = fields.One2many(
        'eh.budget.commitment',
        compute='_compute_eh_commitments',
        string="Budget commitments",
        help=(
            "Commitments this purchase order has created against "
            "matching budget lines. One per (PO line, budget line) "
            "pair where a budget line was resolved at confirm time."
        ),
    )
    eh_budget_commitment_count = fields.Integer(
        compute='_compute_eh_commitments',
        help="Quick count for the smart button.",
    )

    @api.depends('order_line')
    def _compute_eh_commitments(self):
        Commitment = self.env['eh.budget.commitment']
        for po in self:
            rows = Commitment.search([
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', po.id),
            ])
            po.eh_budget_commitment_ids = rows
            po.eh_budget_commitment_count = len(rows)

    # ---- confirm + cancel hooks ----

    @api.private
    def _eh_lock_budget_source(self, operation='write'):
        """Canonical source lock shared by confirm, cancel, and bill post."""
        if not self:
            return self
        if not self.env.su:
            self._eh_check_access(operation)
            self.order_line._eh_check_access('read')
        ids = sorted(self.ids)
        self.env.cr.execute(
            "SELECT id FROM purchase_order WHERE id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.env.cr.execute(
            "SELECT id FROM purchase_order_line WHERE order_id = ANY(%s) "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset(['order_line', 'currency_id', 'date_order'])
        self.order_line.invalidate_recordset()
        return self

    @api.private
    def _eh_governed_orders(
        self, operation='write', extra_company_ids=(),
    ):
        """Return orders needing source locks under stable company policy.

        Commercial writes in a company that has never activated purchase
        budget governance do not need to lock every purchase source row.
        A shared advisory lock makes that negative discriminator race-safe:
        budget activation takes the matching exclusive lock and cannot pass
        until the source mutation finishes. Orders already carrying a live
        commitment stay governed even after their controlling budget closes.
        """
        if not self:
            return self
        if not self.env.su:
            self._eh_check_access(operation)
        company_ids = set(self.mapped('company_id').ids)
        company_ids.update(
            int(company_id) for company_id in extra_company_ids
            if company_id
        )
        companies = self.env['res.company'].sudo().browse(
            sorted(company_ids),
        )
        companies._eh_lock_budget_governance(shared=True)
        self.env.cr.execute(
            "SELECT id FROM res_company "
            "WHERE id = ANY(%s) AND eh_budget_governance_active",
            (sorted(company_ids),),
        )
        governed_company_ids = {row[0] for row in self.env.cr.fetchall()}

        Commitment = self.env['eh.budget.commitment'].sudo()
        committed_order_ids = set(Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', 'in', self.ids),
            ('state', '=', 'reserved'),
            ('active', '=', True),
        ]).mapped('source_id'))
        extra_governed = bool(
            governed_company_ids.intersection(
                int(company_id) for company_id in extra_company_ids
                if company_id
            )
        )
        return self.filtered(
            lambda order: (
                order.company_id.id in governed_company_ids
                or order.id in committed_order_ids
                or extra_governed
            )
        )

    @api.private
    def _eh_lock_budget_source_if_governed(
        self, operation='write', extra_company_ids=(),
    ):
        governed = self._eh_governed_orders(
            operation=operation,
            extra_company_ids=extra_company_ids,
        )
        governed._eh_lock_budget_source(operation)
        return governed

    @api.private
    def _eh_commitment_amount(
        self, po_line, budget_line, date, proportion=1.0,
    ):
        """PO subtotal converted once into budget/company currency."""
        self.ensure_one()
        amount = max(0.0, po_line.price_subtotal) * proportion
        source_currency = self.currency_id
        target_currency = budget_line.currency_id
        if source_currency and target_currency and source_currency != target_currency:
            amount = source_currency._convert(
                amount, target_currency, self.company_id, date,
            )
        return target_currency.round(amount)

    @api.private
    def _eh_assert_budget_basis_mutable(self, extra_company_ids=()):
        """Reject stale writes that resume after a concurrent confirmation."""
        if not self:
            return
        self.invalidate_recordset(['state', 'date_order'])
        confirmed = self.filtered(lambda po: po.state in ('purchase', 'done'))
        if not confirmed:
            return
        Commitment = self.env['eh.budget.commitment'].sudo()
        committed_po_ids = set(Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', 'in', confirmed.ids),
        ]).mapped('source_id'))
        # A new product/date/line on a previously unbudgeted PO can become a
        # tracked line. Checking only the OLD date/company lets a stale write
        # move an out-of-period PO into a governed budget without reserve or
        # block enforcement. Once a company has any live commitment policy,
        # all confirmed commercial changes must use cancel/draft/reconfirm.
        Budget = self.env['eh.budget.budget'].sudo()
        company_ids = set(confirmed.mapped('company_id').ids)
        company_ids.update(int(value) for value in extra_company_ids if value)
        governed_company_ids = set(Budget.search([
            ('company_id', 'in', sorted(company_ids)),
            ('state', '=', 'confirmed'),
            ('overrun_policy', '!=', 'off'),
        ]).mapped('company_id').ids)
        protected = confirmed.filtered(
            lambda po: po.id in committed_po_ids
            or po.company_id.id in governed_company_ids
            or bool(governed_company_ids.intersection(extra_company_ids))
        )
        if protected:
            raise UserError(_(
                "Confirmed purchase order %(orders)s has a governed budget "
                "basis. Cancel and return it to draft before changing "
                "commercial terms, then reconfirm to rebuild commitments.",
                orders=', '.join(protected.mapped('display_name')),
            ))

    def write(self, vals):
        basis_fields = {
            'partner_id', 'company_id', 'currency_id', 'date_order',
            'fiscal_position_id', 'order_line',
        }
        if basis_fields.intersection(vals):
            governed = self._eh_lock_budget_source_if_governed(
                'write',
                (vals.get('company_id'),) if vals.get('company_id') else (),
            )
            governed._eh_assert_budget_basis_mutable(
                (vals.get('company_id'),) if vals.get('company_id') else (),
            )
        return super().write(vals)

    def button_confirm(self):
        """Reserve commitments at confirm; honour the overrun policy.

        Commitment creation runs BEFORE super() so the gate can
        refuse to confirm when the policy is 'block'. The check is
        per matched budget line: a PO line that lands on a non-
        budgeted account is silently allowed.
        """
        governed = self._eh_lock_budget_source_if_governed('write')
        # Pre-check the block policy. Computed without writing rows
        # so a refused confirm leaves no draft commitments behind.
        for po in governed:
            po._eh_check_block_policy()
        result = super().button_confirm()
        # Core routes immediately-approved orders through button_approve();
        # that method owns final block enforcement and reservation for both
        # one-step and two-step approval paths.
        return result

    def button_approve(self, force=False):
        """Recheck and reserve when two-step approval becomes final.

        ``button_confirm`` can stop at ``to approve``. Commercial terms and
        competing commitments may change before a manager approves, so final
        approval must lock and re-run the block gate, then create commitments
        only after core reaches ``purchase``.
        """
        governed = self._eh_lock_budget_source_if_governed('write')
        for po in governed:
            po._eh_check_block_policy()
        result = super().button_approve(force=force)
        for po in governed.filtered(
            lambda p: p.state in ('purchase', 'done'),
        ):
            po._eh_create_commitments()
        return result

    def button_cancel(self):
        """Release every commitment when the PO is cancelled."""
        governed = self._eh_lock_budget_source_if_governed('write')
        for po in governed:
            po._eh_release_commitments(reason='Cancelled')
        return super().button_cancel()

    # ---- internals ----

    @api.private
    def _eh_commitment_percentage(self, raw_value, line):
        """Return one safe analytic percentage from stored JSON."""
        try:
            percentage = float(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UserError(_(
                "Purchase order line %(line)s has an invalid "
                "analytic distribution.",
                line=line.display_name,
            )) from exc
        if not math.isfinite(percentage) or percentage < 0.0:
            raise UserError(_(
                "Purchase order line %(line)s has an invalid "
                "analytic percentage.",
                line=line.display_name,
            ))
        return percentage

    @api.private
    def _eh_commitment_analytic_id(self, raw_value, line):
        """Return one PostgreSQL-safe analytic account identifier."""
        try:
            analytic_id = int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UserError(_(
                "Purchase order line %(line)s has an invalid "
                "analytic distribution.",
                line=line.display_name,
            )) from exc
        # Odoo record identifiers are PostgreSQL int4. Reject malformed JSON
        # before browse().exists() can abort the transaction on an overflow.
        if analytic_id <= 0 or analytic_id > 2147483647:
            raise UserError(_(
                "Purchase order line %(line)s has an invalid "
                "analytic distribution.",
                line=line.display_name,
            ))
        return analytic_id

    def _eh_iter_budget_targets(self):
        """Yield (line, account, date, analytic, share) per PO line.

        The date used for budget resolution is the PO's `date_order`
        (or `date_planned` if set on the line). Simple analytic
        distributions are allocated by percentage. Composite keys span
        several analytic plans but carry one shared percentage; reserving
        that percentage once per component would double-count the PO.
        Reject those keys until commitments have an explicit plan axis.
        """
        self.ensure_one()
        for line in self.order_line:
            account = self.env['account.account']
            if line.product_id:
                product = line.product_id.with_company(self.company_id)
                account = product.product_tmpl_id.get_product_accounts(
                    fiscal_pos=self.fiscal_position_id,
                ).get('expense')
            if not account:
                continue
            date = line.date_planned or self.date_order
            distribution = (
                line.analytic_distribution
                if hasattr(line, 'analytic_distribution')
                else False
            ) or {}
            if not distribution:
                yield line, account, (
                    date.date() if hasattr(date, 'date') else date
                ), None, 1.0
                continue

            allocations = {}
            allocated_pct = 0.0
            for raw_key, raw_pct in sorted(
                distribution.items(), key=lambda item: str(item[0]),
            ):
                parts = [
                    part.strip() for part in str(raw_key).split(',')
                    if part.strip()
                ]
                if len(parts) != 1:
                    raise UserError(_(
                        "Purchase order line %(line)s uses a cross-plan "
                        "analytic distribution. Budget commitments cannot "
                        "reserve that allocation safely; split the line by "
                        "analytic plan before confirming.",
                        line=line.display_name,
                    ))
                analytic_id = self._eh_commitment_analytic_id(
                    parts[0], line,
                )
                percentage = self._eh_commitment_percentage(raw_pct, line)
                if not percentage:
                    continue
                analytic = self.env['account.analytic.account'].browse(
                    analytic_id,
                ).exists()
                if not analytic:
                    raise UserError(_(
                        "Purchase order line %(line)s references a missing "
                        "analytic account.",
                        line=line.display_name,
                    ))
                if analytic.id not in allocations:
                    allocations[analytic.id] = [analytic, 0.0]
                allocations[analytic.id][1] += percentage
                allocated_pct += percentage
            if allocated_pct > 100.0 + _ANALYTIC_PERCENT_TOLERANCE:
                raise UserError(_(
                    "Purchase order line %(line)s allocates more than 100%% "
                    "across analytic accounts.",
                    line=line.display_name,
                ))
            effective_date = (
                date.date() if hasattr(date, 'date') else date
            )
            for analytic, percentage in allocations.values():
                yield line, account, effective_date, analytic, percentage / 100.0
            if allocated_pct < 100.0 - _ANALYTIC_PERCENT_TOLERANCE:
                yield (
                    line, account, effective_date, None,
                    (100.0 - allocated_pct) / 100.0,
                )

    @api.private
    def _eh_resolved_budget_targets(self, Commitment):
        """Resolve shares and allocate one currency-exact amount per target.

        Rounding every analytic share independently can create or destroy a
        minor unit (for example, 0.01 split 50/50 becoming 0.01 + 0.01).
        Allocate deterministic rounded shares, then place the exact residual
        on the final resolved target for that purchase line.
        """
        self.ensure_one()
        grouped = {}
        for po_line, account, date, analytic, share in (
            self._eh_iter_budget_targets()
        ):
            budget_line = Commitment._resolve_budget_line(
                account, date, self.company_id,
                analytic_account=analytic,
            )
            if not budget_line:
                continue
            if budget_line.budget_id.eh_commitment_integrity_review_required:
                raise UserError(_(
                    "Budget %(budget)s has quarantined legacy commitment "
                    "provenance. Create and approve a reviewed replacement "
                    "budget version before confirming purchase orders.",
                    budget=budget_line.budget_id.display_name,
                ))
            if budget_line.budget_id.company_id != self.company_id:
                raise UserError(_(
                    "The resolved budget line must belong to the purchase "
                    "order company."
                ))
            key = (po_line.id, budget_line.id, date)
            if key not in grouped:
                grouped[key] = [po_line, budget_line, date, 0.0]
            grouped[key][3] += share
        targets_by_po_line = {}
        for key in sorted(grouped):
            target = grouped[key]
            targets_by_po_line.setdefault(target[0].id, []).append(target)

        allocated = []
        for po_line_id in sorted(targets_by_po_line):
            targets = targets_by_po_line[po_line_id]
            po_line = targets[0][0]
            first_budget_line = targets[0][1]
            effective_date = targets[0][2]
            currency = first_budget_line.currency_id
            if any(target[1].currency_id != currency for target in targets):
                raise UserError(_(
                    "One purchase line cannot reserve budgets in different "
                    "currencies."
                ))
            # Validation tolerates sub-micro percentage noise, but a fixed
            # percentage tolerance must never become material money on a
            # large purchase. Snap a near-complete resolved subtotal to
            # exactly 100%, and cap any tolerated overshoot there too.
            total_share = sum(
                max(0.0, target[3]) for target in targets
            )
            if total_share >= 1.0 - _ANALYTIC_SHARE_TOLERANCE:
                total_share = 1.0
            else:
                total_share = min(1.0, total_share)
            # Convert matched source subtotal once, then round. Converting the
            # full line first and multiplying its rounded target amount gives
            # wrong half-minor results when only part of a distribution maps
            # to a budget.
            remaining = self._eh_commitment_amount(
                po_line, first_budget_line, effective_date,
                proportion=total_share,
            )
            for index, (line, budget_line, date, share) in enumerate(targets):
                if index == len(targets) - 1:
                    amount = remaining
                else:
                    amount = min(
                        self._eh_commitment_amount(
                            line, budget_line, date, proportion=share,
                        ),
                        remaining,
                    )
                    amount = currency.round(max(0.0, amount))
                remaining = currency.round(remaining - amount)
                allocated.append((line, budget_line, date, amount))
        return allocated

    def _eh_check_block_policy(self):
        """Refuse confirm when overrun_policy='block' and any line
        would go negative.

        Walks the PO lines, resolves the matching budget line for
        each, computes the post-confirm availability (current
        available minus the line's price_subtotal), and raises
        UserError if any drops below zero on a budget whose policy
        is 'block'.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
            self.order_line._eh_check_access('read')
            if self.company_id.id not in self.env.companies.ids:
                raise UserError(_(
                    "The purchase order company is outside your allowed "
                    "companies."
                ))
        Commitment = self.env['eh.budget.commitment'].with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        }).sudo()
        # Accumulate the required amount per resolved budget line before
        # checking availability. Several PO lines can land on the same
        # budget line; each one individually may fit, yet their sum may
        # exceed availability. Checking line-by-line against the same
        # untouched available_amount would let that overrun through.
        need_by_line = {}
        for po_line, budget_line, date, amount in (
            self._eh_resolved_budget_targets(Commitment)
        ):
            policy = budget_line.budget_id.overrun_policy
            if policy == 'off':
                continue
            need_by_line.setdefault(budget_line, 0.0)
            need_by_line[budget_line] += amount
        if not need_by_line:
            return
        # Serialise concurrent confirms on the shared availability total.
        # available_amount is a non-stored compute over reserved commitment
        # rows, so there is no stored column or DB constraint to protect it:
        # two PO confirms resolving to the same 'block' budget line could each
        # read the pre-confirm availability, both pass this gate, then both
        # write commitments and drive availability negative -- breaking the
        # 'block' guarantee. Take a FOR UPDATE row lock on every resolved
        # budget line before reading availability. Because the lock is held to
        # commit (through super().button_confirm and _eh_create_commitments),
        # the second confirm queues behind the first's transaction and, once it
        # proceeds, re-reads the already-encumbered availability and correctly
        # raises. Mirrors the discipline in
        # eh_fx_revaluation_run._eh_lock_for_post and cheque_book serial
        # consumption. ORDER BY id gives a deterministic lock order so two POs
        # touching the same set of lines cannot deadlock.
        locked = self.env['eh.budget.line'].sudo().browse(
            [bl.id for bl in need_by_line],
        )
        locked.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_budget_line WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(locked.ids),),
        )
        # Drop the cached availability so the read below recomputes against
        # commitment rows committed by any transaction we just queued behind.
        locked.invalidate_recordset(['committed_amount', 'available_amount'])
        violations = []
        for budget_line, need in need_by_line.items():
            if budget_line.budget_id.overrun_policy != 'block':
                continue
            available = budget_line.available_amount
            if budget_line.currency_id.compare_amounts(available, need) < 0:
                violations.append((budget_line, available, need))
        if violations:
            lines = [
                _(
                    "%(budget)s line %(account)s: available "
                    "%(avail)s, needs %(need)s",
                    budget=v[0].budget_id.display_name,
                    account=v[0].account_id.display_name,
                    avail=format_amount(
                        self.env, v[1], v[0].currency_id,
                    ),
                    need=format_amount(
                        self.env, v[2], v[0].currency_id,
                    ),
                )
                for v in violations
            ]
            raise UserError(_(
                "Purchase order %(po)s would exceed available "
                "budget on %(n)d line(s):\n  %(detail)s",
                po=self.display_name, n=len(violations),
                detail="\n  ".join(lines),
            ))

    def _eh_create_commitments(self):
        """Create one commitment per (PO line, matched budget line).

        Idempotent: re-running on a re-confirmed PO updates existing
        commitments rather than duplicating.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
            self.order_line._eh_check_access('read')
            if self.company_id.id not in self.env.companies.ids:
                raise UserError(_(
                    "The purchase order company is outside your allowed "
                    "companies."
                ))
        Commitment = self.env['eh.budget.commitment'].with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        }).sudo()
        for po_line, budget_line, date, amount in (
            self._eh_resolved_budget_targets(Commitment)
        ):
            policy = budget_line.budget_id.overrun_policy
            if policy == 'off':
                continue
            # Idempotency key includes the PO line id. Without it, two
            # PO lines that resolve to the same budget line (same
            # expense account, period and analytic) would collapse onto
            # one commitment, the second overwriting the first line's
            # reserved amount and silently dropping its encumbrance.
            existing = Commitment.search([
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', self.id),
                ('source_line_id', '=', po_line.id),
                ('budget_line_id', '=', budget_line.id),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            if existing:
                existing.write({
                    'amount': amount,
                    # Re-confirmation begins a fresh approved reservation
                    # cycle. Downward and upward amendments both replace the
                    # old baseline; partial bill releases never call here.
                    'original_amount': amount,
                    'state': 'reserved',
                    'source_ref': self.name,
                    'active': True,
                    'eh_integrity_review_reason': False,
                })
            else:
                Commitment.create({
                    'budget_line_id': budget_line.id,
                    'amount': amount,
                    'source_model': 'purchase.order',
                    'source_id': self.id,
                    'source_line_id': po_line.id,
                    'source_ref': self.name,
                    'state': 'reserved',
                })
            # Warn-mode: post a chatter note on the budget when this
            # commitment puts availability into negative territory.
            if policy == 'warn':
                budget_line.invalidate_recordset(['available_amount'])
                if budget_line.currency_id.compare_amounts(
                    budget_line.available_amount, 0.0,
                ) < 0:
                    budget_line.budget_id.message_post(body=_(
                        "Budget overrun warning: line for %(acc)s is "
                        "now %(avail)s after PO %(po)s commitment.",
                        acc=budget_line.account_id.display_name,
                        avail=format_amount(
                            self.env, budget_line.available_amount,
                            budget_line.currency_id,
                        ),
                        po=self.name,
                    ))

    def _eh_release_commitments(self, reason=''):
        """Release every reserved commitment owned by this PO."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
            if self.company_id.id not in self.env.companies.ids:
                raise UserError(_(
                    "The purchase order company is outside your allowed "
                    "companies."
                ))
        Commitment = self.env['eh.budget.commitment'].with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        }).sudo()
        rows = Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', self.id),
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'reserved'),
        ])
        if not rows:
            return
        rows.action_release()
        for row in rows:
            row.message_post(body=_(
                "Released. %(reason)s",
                reason=reason or '',
            ))

    @api.private
    def _eh_rebuild_commitments_after_planned_date_change(self):
        """Re-resolve a confirmed PO after an operational date update.

        Old commitment rows remain as released audit evidence when the date
        moves to a different budget line. If the same line still controls the
        order, the existing row is re-reserved by the idempotent source key.
        """
        self._eh_lock_budget_source('write')
        show_budget_detail = self.env.user.has_group(
            'eh_account_base.group_eh_user',
        )
        for po in self.filtered(lambda order: order.state in ('purchase', 'done')):
            # The caller has already passed purchase-line ACLs and record
            # rules through super().write(). Budget resolution is an internal
            # source-document consequence and must not depend on a vendor or
            # operational user's access to account.account.
            engine_po = po.sudo()
            engine_po._eh_release_commitments(
                reason='Planned delivery date changed; budget re-resolved',
            )
            try:
                engine_po._eh_check_block_policy()
                engine_po._eh_create_commitments()
            except UserError:
                if show_budget_detail:
                    raise
                raise UserError(_(
                    "The planned delivery date could not pass budget "
                    "control. Contact Finance for review."
                ))

    def action_eh_view_commitments(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Budget commitments"),
            'res_model': 'eh.budget.commitment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [
                ('source_model', '=', 'purchase.order'),
                ('source_id', '=', self.id),
            ],
        }


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    _BUDGET_BASIS_FIELDS = frozenset({
        'order_id', 'product_id', 'product_qty', 'product_uom_qty',
        'product_uom_id', 'product_uom', 'price_unit', 'discount',
        'tax_ids', 'taxes_id', 'analytic_distribution', 'date_planned',
        'product_packaging_id', 'product_packaging_qty',
    })

    @api.model_create_multi
    def create(self, vals_list):
        order_ids = {
            vals.get('order_id') for vals in vals_list if vals.get('order_id')
        }
        orders = self.env['purchase.order'].browse(order_ids)
        governed = orders._eh_lock_budget_source_if_governed('write')
        governed._eh_assert_budget_basis_mutable()
        return super().create(vals_list)

    def write(self, vals):
        basis_fields = self._BUDGET_BASIS_FIELDS.intersection(vals)
        if basis_fields - {'date_planned'}:
            orders = self.order_id
            if vals.get('order_id'):
                orders |= self.env['purchase.order'].browse(vals['order_id'])
            governed = orders._eh_lock_budget_source_if_governed('write')
            governed._eh_assert_budget_basis_mutable()
            return super().write(vals)

        if 'date_planned' not in vals:
            return super().write(vals)

        expected_date = (
            fields.Datetime.to_datetime(vals['date_planned'])
            if vals['date_planned']
            else False
        )
        changed = self.filtered(
            lambda line: line.date_planned != expected_date
        )
        if not changed:
            return super().write(vals)

        orders = changed.mapped('order_id')
        governed_orders = orders._eh_lock_budget_source_if_governed('write')
        if not governed_orders:
            return super().write(vals)
        changed.invalidate_recordset(['date_planned', 'order_id'])
        changed = changed.filtered(
            lambda line: line.date_planned != expected_date
        )
        governed = changed.filtered(
            lambda line: (
                line.order_id in governed_orders
                and line.order_id.state in ('purchase', 'done')
            )
        )
        if governed and self.env['account.move.line'].sudo().search([
            ('purchase_line_id', 'in', governed.ids),
            ('move_id.state', '=', 'posted'),
            ('move_id.move_type', 'in', ('in_invoice', 'in_refund')),
        ], limit=1):
            raise UserError(_(
                "A planned delivery date cannot be moved after a vendor "
                "bill or refund has posted for that purchase line."
            ))

        old_dates = {
            line.id: line.date_planned for line in governed
        }
        result = super().write(vals)
        changed_orders = governed.filtered(
            lambda line: old_dates.get(line.id) != line.date_planned
        ).mapped('order_id')
        changed_orders._eh_rebuild_commitments_after_planned_date_change()
        return result

    def unlink(self):
        orders = self.order_id
        governed = orders._eh_lock_budget_source_if_governed('write')
        governed._eh_assert_budget_basis_mutable()
        return super().unlink()
