# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Serialize reconciliation with batch review/posting integrity."""

import math
from collections import defaultdict

from odoo import _, api, models
from odoo.exceptions import UserError

from .capabilities import (
    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
    AUTHORIZED_BATCH_RECONCILE_CONTEXT,
    CORE_RECONCILE_CAPABILITY,
    CORE_RECONCILE_CONTEXT,
    has_authorized_batch_reconcile_capability,
    has_core_reconcile_capability,
)


_PARTIAL_MATERIAL_FIELDS = frozenset({
    'debit_move_id', 'credit_move_id', 'amount',
    'debit_amount_currency', 'credit_amount_currency',
    'full_reconcile_id', 'exchange_move_id',
    'draft_caba_move_vals', 'company_id', 'company_currency_id',
    'debit_currency_id', 'credit_currency_id', 'max_date',
})
_PARTIAL_CREATE_SERVER_FIELDS = frozenset({
    'draft_caba_move_vals', 'company_id', 'company_currency_id',
    'debit_currency_id', 'credit_currency_id', 'max_date',
})
_FULL_MATERIAL_FIELDS = frozenset({
    'reconciled_line_ids', 'partial_reconcile_ids', 'exchange_move_id',
})
_CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT = (
    'eh_core_reconcile_origin_no_exchange'
)
_INVALID_RECONCILIATION_PLAN_MESSAGE = (
    "Reconciliation plans may contain only journal-item recordsets and "
    "nested lists or tuples."
)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def _eh_reconcile_engine_scope(self):
        """Prelock the complete existing reconciliation graph by move."""
        lines = self.exists()
        if not lines:
            return True
        preliminary_lines = lines._all_reconciled_lines()
        preliminary_lines._eh_check_access('write')
        preliminary_partials = (
            preliminary_lines.matched_debit_ids
            | preliminary_lines.matched_credit_ids
        )
        preliminary_fulls = preliminary_lines.mapped('full_reconcile_id')
        preliminary_moves = (
            preliminary_lines.move_id
            | preliminary_partials.mapped('exchange_move_id')
            | preliminary_partials._eh_caba_moves()
        )
        if 'exchange_move_id' in preliminary_fulls._fields:
            preliminary_moves |= preliminary_fulls.mapped('exchange_move_id')
        preliminary_moves._eh_check_access('write')
        self.env['eh.batch.payment']._eh_advisory_move_owner_lock(
            preliminary_moves.ids,
        )
        preliminary_lines._eh_batch_reconcile_lock()

        # A relation committed while this call waited for the move locks may
        # expand the graph. Never acquire its move/AML locks out of canonical
        # order; fail and let the caller retry from a fresh graph.
        preliminary_lines.invalidate_recordset([
            'matched_debit_ids', 'matched_credit_ids', 'full_reconcile_id',
        ])
        preliminary_partials.invalidate_recordset([
            'debit_move_id', 'credit_move_id', 'exchange_move_id',
            'full_reconcile_id',
        ])
        authoritative_lines = lines._all_reconciled_lines()
        authoritative_partials = (
            authoritative_lines.matched_debit_ids
            | authoritative_lines.matched_credit_ids
        )
        authoritative_fulls = authoritative_lines.mapped(
            'full_reconcile_id',
        )
        authoritative_moves = (
            authoritative_lines.move_id
            | authoritative_partials.mapped('exchange_move_id')
            | authoritative_partials._eh_caba_moves()
        )
        if 'exchange_move_id' in authoritative_fulls._fields:
            authoritative_moves |= authoritative_fulls.mapped(
                'exchange_move_id',
            )
        if (
            not set(authoritative_lines.ids).issubset(
                set(preliminary_lines.ids),
            )
            or not set(authoritative_moves.ids).issubset(
                set(preliminary_moves.ids),
            )
        ):
            raise UserError(_(
                "The reconciliation graph changed concurrently. Retry the "
                "operation."
            ))
        authoritative_lines._eh_assert_batch_reconciliation_mutable()
        return True

    def _eh_batch_reconcile_lock(self):
        """Take sorted AML locks after callers prelock the graph's moves."""
        lines = self.exists()
        if not lines:
            return lines
        lines.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move_line WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(sorted(lines.ids)),),
        )
        lines.invalidate_recordset([
            'amount_residual', 'amount_residual_currency', 'reconciled',
            'matched_debit_ids', 'matched_credit_ids',
        ])
        return lines

    def _eh_reviewed_batch_payments(self):
        if not self:
            return self.env['account.payment']
        # A source invoice may also carry separately reconcilable tax,
        # clearing, or cash-basis lines. Only its AR/AP settlement leg is
        # approved by the batch and frozen; unrelated accounting close flows
        # must remain available.
        settlement_lines = self.filtered(
            lambda line: line.account_id.account_type in (
                'asset_receivable', 'liability_payable',
            ),
        )
        if not settlement_lines:
            return self.env['account.payment']
        move_ids = settlement_lines.move_id.ids
        Payment = self.env['account.payment'].sudo()
        source_domain = [('eh_source_move_ids', 'in', move_ids)]
        if 'invoice_ids' in Payment._fields:
            source_domain = [
                '|',
                ('eh_source_move_ids', 'in', move_ids),
                ('invoice_ids', 'in', move_ids),
            ]
        return Payment.search(source_domain + [
            ('eh_batch_payment_id.state', 'in', ('confirmed', 'posted')),
            ('eh_batch_payment_id.reversed_at', '=', False),
        ], limit=1)

    def _eh_assert_batch_reconciliation_mutable(self):
        if has_authorized_batch_reconcile_capability(self.env):
            return True
        if self._eh_reviewed_batch_payments():
            raise UserError(_(
                "Receivable/payable settlement lines are frozen for source "
                "documents of a confirmed or posted batch. Cancel and reset "
                "the reviewed batch before changing settlement."
            ))
        return True

    @api.model
    def _reconcile_plan(self, reconciliation_plan):
        """Lock and authorize core before its dynamic-line synchronization."""
        with self.env.cr.savepoint():
            line_ids = set()

            def collect_lines(item):
                if isinstance(item, models.BaseModel):
                    if item._name != 'account.move.line':
                        raise UserError(_INVALID_RECONCILIATION_PLAN_MESSAGE)
                    line_ids.update(item.ids)
                elif isinstance(item, (list, tuple)):
                    for child in item:
                        collect_lines(child)
                else:
                    raise UserError(_INVALID_RECONCILIATION_PLAN_MESSAGE)

            collect_lines(reconciliation_plan)
            all_amls = self.env['account.move.line'].browse(
                sorted(line_ids),
            )
            # Odoo 17+ enters dynamic invoice synchronization before calling
            # _reconcile_plan_with_sync. Establish the canonical move/line lock
            # scope and reject reviewed settlement lines first.
            all_amls._eh_reconcile_engine_scope()

            context = self.env.context
            trusted_nested_core = (
                context.get(CORE_RECONCILE_CONTEXT)
                is CORE_RECONCILE_CAPABILITY
            )
            origin_no_exchange = (
                context.get(_CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT)
                if trusted_nested_core
                else bool(context.get('no_exchange_difference'))
            )
            capable_self = self.with_context(**{
                CORE_RECONCILE_CONTEXT: CORE_RECONCILE_CAPABILITY,
                _CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT:
                    origin_no_exchange,
            })

            def make_capable(item):
                if isinstance(item, models.BaseModel):
                    if item._name != 'account.move.line':
                        raise UserError(_INVALID_RECONCILIATION_PLAN_MESSAGE)
                    return item.with_env(capable_self.env)
                if isinstance(item, list):
                    return [make_capable(child) for child in item]
                if isinstance(item, tuple):
                    return tuple(make_capable(child) for child in item)
                raise UserError(_INVALID_RECONCILIATION_PLAN_MESSAGE)

            capable_plan = make_capable(reconciliation_plan)
            result = super(
                AccountMoveLine, capable_self,
            )._reconcile_plan(capable_plan)

            def clean_result(item):
                if isinstance(item, models.BaseModel):
                    clean_context = dict(item.env.context)
                    clean_context.pop(CORE_RECONCILE_CONTEXT, None)
                    clean_context.pop(
                        _CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT, None,
                    )
                    return item.with_context(clean_context)
                if isinstance(item, list):
                    return [clean_result(child) for child in item]
                if isinstance(item, tuple):
                    return tuple(clean_result(child) for child in item)
                if isinstance(item, dict):
                    return {
                        clean_result(key): clean_result(value)
                        for key, value in item.items()
                    }
                if isinstance(item, set):
                    return {clean_result(child) for child in item}
                return item

            return clean_result(result)

    def _reconcile_plan_with_sync(self, plan_list, all_amls):
        # Core snapshots residuals at the start of this method. Lock before
        # that snapshot so a stale plan cannot resume after batch posting and
        # insert an over-reconciliation computed from old balances.
        with self.env.cr.savepoint():
            return self._eh_reconcile_plan_with_sync_locked(
                plan_list, all_amls,
            )

    def _eh_reconcile_plan_with_sync_locked(self, plan_list, all_amls):
        all_amls._eh_reconcile_engine_scope()
        context = self.env.context
        if context.get(
            CORE_RECONCILE_CONTEXT,
        ) is CORE_RECONCILE_CAPABILITY:
            capable_self = self
            capable_amls = all_amls.with_env(self.env)
        else:
            capability_context = {
                CORE_RECONCILE_CONTEXT:
                    CORE_RECONCILE_CAPABILITY,
                _CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT:
                    bool(context.get('no_exchange_difference')),
            }
            capable_self = self.with_context(**capability_context)
            capable_amls = all_amls.with_context(**capability_context)
        try:
            return super(
                AccountMoveLine, capable_self,
            )._reconcile_plan_with_sync(plan_list, capable_amls)
        finally:
            # Core stores created partial recordsets back into caller-owned
            # plan dictionaries. Never let this process-local capability
            # escape through those by-reference results.
            clean_context = dict(context)
            clean_context.pop(CORE_RECONCILE_CONTEXT, None)
            clean_context.pop(
                _CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT,
                None,
            )
            for plan in plan_list:
                partials = plan.get('partials')
                if getattr(partials, '_name', None):
                    plan['partials'] = self.env[partials._name].with_context(
                        clean_context,
                    ).browse(partials.ids)


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    @api.model
    def _eh_partial_lines_from_vals(self, vals_list):
        line_ids = {
            vals.get(field_name)
            for vals in vals_list
            for field_name in ('debit_move_id', 'credit_move_id')
            if vals.get(field_name)
        }
        return self.env['account.move.line'].browse(sorted(line_ids))

    def _eh_lock_current_partial_lines(self):
        """Lock partial rows before re-reading their authoritative endpoints."""
        partials = self.exists()
        if not partials:
            return self.env['account.move.line']
        partials.flush_recordset(['debit_move_id', 'credit_move_id'])
        self.env.cr.execute(
            "SELECT id FROM account_partial_reconcile WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(sorted(partials.ids)),),
        )
        locked_ids = {row[0] for row in self.env.cr.fetchall()}
        if locked_ids != set(partials.ids):
            raise UserError(_(
                "A reconciliation changed concurrently. Retry the operation."
            ))
        partials.invalidate_recordset([
            'debit_move_id', 'credit_move_id', 'exchange_move_id',
        ])
        return partials.debit_move_id | partials.credit_move_id

    @api.model
    def _eh_exchange_moves_from_vals(self, vals_list):
        move_ids = {
            vals.get('exchange_move_id') for vals in vals_list
            if vals.get('exchange_move_id')
        }
        return self.env['account.move'].browse(sorted(move_ids)).exists()

    def _eh_caba_moves(self):
        if not self.ids or 'tax_cash_basis_rec_id' not in self.env[
            'account.move'
        ]._fields:
            return self.env['account.move']
        return self.env['account.move'].search([
            ('tax_cash_basis_rec_id', 'in', self.ids),
        ], order='id')

    @api.model
    def _eh_assert_exchange_moves_unclaimed(self, moves):
        if not moves or has_authorized_batch_reconcile_capability(self.env):
            return True
        owners = moves._eh_batch_payment_owners().filtered(
            'eh_batch_payment_id',
        )
        sources = moves._eh_reviewed_batch_source_payments()
        if owners or sources:
            raise UserError(_(
                "A batch payment cash move or reviewed source cannot be used "
                "as exchange-difference reconciliation evidence."
            ))
        return True

    @api.model
    def _eh_validate_fresh_partial_vals(self, vals_list, lines):
        """Reject negative or collectively stale direct partial values."""
        lines_by_id = {line.id: line for line in lines}
        company_amounts = defaultdict(float)
        currency_amounts = defaultdict(float)
        context = self.env.context
        core_exchange_partial = (
            context.get(CORE_RECONCILE_CONTEXT)
            is CORE_RECONCILE_CAPABILITY
            and not context.get(
                _CORE_RECONCILE_ORIGIN_NO_EXCHANGE_CONTEXT,
            )
            and bool(context.get('no_exchange_difference'))
        )

        for vals in vals_list:
            debit = lines_by_id.get(vals.get('debit_move_id'))
            credit = lines_by_id.get(vals.get('credit_move_id'))
            if not debit or not credit:
                raise UserError(_(
                    "Every partial reconciliation needs existing debit and "
                    "credit journal items."
                ))
            if (
                debit.company_id != credit.company_id
                or debit.account_id != credit.account_id
                or not (
                    debit.account_id.reconcile
                    or debit.account_id.account_type in (
                        'asset_cash', 'liability_credit_card',
                    )
                )
                or (
                    not core_exchange_partial
                    and (
                        debit.parent_state != 'posted'
                        or credit.parent_state != 'posted'
                    )
                )
            ):
                raise UserError(_(
                    "Partial reconciliation journal items must be posted, "
                    "share one company/account, and use a reconcilable, cash, "
                    "or credit-card account."
                ))

            company_currency = debit.company_currency_id
            amount = vals.get('amount', 0.0) or 0.0
            debit_currency_amount = (
                vals.get('debit_amount_currency', 0.0) or 0.0
            )
            credit_currency_amount = (
                vals.get('credit_amount_currency', 0.0) or 0.0
            )
            if not all(math.isfinite(value) for value in (
                amount,
                debit_currency_amount,
                credit_currency_amount,
            )):
                raise UserError(_(
                    "Reconciliation amounts must be finite numbers."
                ))
            if (
                amount != company_currency.round(amount)
                or debit_currency_amount
                != debit.currency_id.round(debit_currency_amount)
                or credit_currency_amount
                != credit.currency_id.round(credit_currency_amount)
                or (
                    debit.currency_id == company_currency
                    and debit_currency_amount != amount
                )
                or (
                    credit.currency_id == company_currency
                    and credit_currency_amount != amount
                )
                or (
                    debit.currency_id == credit.currency_id
                    and debit_currency_amount != credit_currency_amount
                )
            ):
                raise UserError(_(
                    "Reconciliation amounts must use exact currency "
                    "precision and one coherent amount for matching "
                    "company or transaction currencies."
                ))
            amount_cmp = company_currency.compare_amounts(amount, 0.0)
            debit_amount_cmp = debit.currency_id.compare_amounts(
                debit_currency_amount, 0.0,
            )
            credit_amount_cmp = credit.currency_id.compare_amounts(
                credit_currency_amount, 0.0,
            )
            debit_open = (
                company_currency.compare_amounts(
                    debit.amount_residual, 0.0,
                ) > 0
                or debit.currency_id.compare_amounts(
                    debit.amount_residual_currency, 0.0,
                ) > 0
            )
            credit_open = (
                company_currency.compare_amounts(
                    credit.amount_residual, 0.0,
                ) < 0
                or credit.currency_id.compare_amounts(
                    credit.amount_residual_currency, 0.0,
                ) < 0
            )
            if (
                amount_cmp < 0
                or debit_amount_cmp < 0
                or credit_amount_cmp < 0
                or (
                    amount_cmp == 0
                    and debit_amount_cmp == 0
                    and credit_amount_cmp == 0
                )
                or not debit_open
                or not credit_open
            ):
                raise UserError(_(
                    "Reconciliation amounts must be finite, non-negative, "
                    "and not all zero, and the lines must join an open debit "
                    "to an open credit. A zero company-currency amount is "
                    "valid when a foreign-currency residual is being closed."
                ))

            company_amounts[debit.id] += amount
            company_amounts[credit.id] += amount
            currency_amounts[debit.id] += debit_currency_amount
            currency_amounts[credit.id] += credit_currency_amount

        for line_id, total in company_amounts.items():
            line = lines_by_id[line_id]
            if line.company_currency_id.compare_amounts(
                total,
                abs(line.amount_residual),
            ) > 0:
                raise UserError(_(
                    "Partial reconciliation exceeds the freshly locked "
                    "company-currency residual of %(line)s.",
                    line=line.display_name,
                ))
        for line_id, total in currency_amounts.items():
            line = lines_by_id[line_id]
            if line.currency_id.compare_amounts(
                total,
                abs(line.amount_residual_currency),
            ) > 0:
                raise UserError(_(
                    "Partial reconciliation exceeds the freshly locked "
                    "currency residual of %(line)s.",
                    line=line.display_name,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_check_access('create')
        trusted_core = (
            has_authorized_batch_reconcile_capability(self.env)
            or has_core_reconcile_capability(self.env)
        )
        supplied_server_fields = {
            field_name
            for vals in vals_list
            for field_name in _PARTIAL_CREATE_SERVER_FIELDS
            if field_name in vals
        }
        supplied_core_links = {
            field_name
            for vals in vals_list
            for field_name in ('full_reconcile_id', 'exchange_move_id')
            if vals.get(field_name)
        }
        if not trusted_core and (
            supplied_server_fields or supplied_core_links
        ):
            raise UserError(_(
                "Partial-reconciliation projections, full links, and "
                "exchange-move provenance are core-owned and cannot be "
                "supplied directly."
            ))
        lines = self._eh_partial_lines_from_vals(vals_list)
        lines._eh_check_access('write')
        exchange_moves = self._eh_exchange_moves_from_vals(vals_list)
        exchange_moves._eh_check_access('write')
        scope_moves = exchange_moves | lines.mapped('move_id')
        with self.env.cr.savepoint():
            self.env['eh.batch.payment']._eh_advisory_move_owner_lock(
                scope_moves.ids,
            )
            lines = lines._eh_batch_reconcile_lock()
            lines._eh_assert_batch_reconciliation_mutable()
            self._eh_assert_exchange_moves_unclaimed(exchange_moves)
            self._eh_validate_fresh_partial_vals(vals_list, lines)
            return super().create(vals_list)

    def write(self, vals):
        if _PARTIAL_MATERIAL_FIELDS.intersection(vals):
            if (
                has_authorized_batch_reconcile_capability(self.env)
                or has_core_reconcile_capability(self.env)
            ):
                return super().write(vals)
            self._eh_check_access('write')
            raise UserError(_(
                "Partial-reconciliation endpoints, amounts, projections, "
                "full links, and exchange provenance are core-owned. Remove "
                "the reconciliation and reconcile freshly instead of writing "
                "those fields directly."
            ))
        return super().write(vals)

    def unlink(self):
        # Core calls ``unlink()`` on the empty matched-partial recordset when
        # cancelling an unreconciled draft invoice. Preserve that no-op for
        # actors allowed to cancel the source document; no reconciliation
        # evidence exists on which an unlink ACL could operate.
        if not self:
            return super().unlink()
        self._eh_check_access('unlink')
        current_lines = self.debit_move_id | self.credit_move_id
        current_lines._eh_check_access('write')
        matching_lines = current_lines._all_reconciled_lines()
        matching_lines._eh_check_access('write')
        fulls = self.sudo().mapped('full_reconcile_id')
        actor_fulls = self.env['account.full.reconcile'].browse(fulls.ids)
        actor_fulls._eh_check_access('unlink')
        full_partials = fulls.sudo().mapped('partial_reconcile_ids')
        full_exchange_moves = (
            full_partials.mapped('exchange_move_id')
            | full_partials._eh_caba_moves()
        )
        if 'exchange_move_id' in fulls._fields:
            full_exchange_moves |= fulls.mapped('exchange_move_id')
        exchange_moves = (
            self.sudo().mapped('exchange_move_id')
            | self.sudo()._eh_caba_moves()
            | full_exchange_moves
        )
        scope_moves = exchange_moves | matching_lines.sudo().mapped('move_id')
        self.env['account.move'].browse(
            scope_moves.ids,
        )._eh_check_access('write')
        with self.env.cr.savepoint():
            self.env['eh.batch.payment']._eh_advisory_move_owner_lock(
                scope_moves.ids,
            )
            # Core unlinks the owning full reconciliation after deleting the
            # selected partial. Lock that complete graph in its canonical
            # full->partial->AML order first. Otherwise this path can own one
            # partial row while nested Full.unlink waits for a sibling
            # exchange-move advisory held by a concurrent batch claim.
            if actor_fulls:
                actor_fulls._eh_lock_full_reconcile_scope()
            lines = self._eh_lock_current_partial_lines(
            )._eh_batch_reconcile_lock()
            authoritative_matching_lines = lines._all_reconciled_lines()
            if not set(authoritative_matching_lines.move_id.ids).issubset(
                set(scope_moves.ids),
            ):
                raise UserError(_(
                    "A reconciliation graph changed concurrently. Retry the "
                    "operation."
                ))
            authoritative_matching_lines._eh_batch_reconcile_lock()
            authoritative_fulls = self.sudo().mapped('full_reconcile_id')
            if not set(authoritative_fulls.ids).issubset(set(fulls.ids)):
                raise UserError(_(
                    "A partial reconciliation's full-reconcile owner changed "
                    "concurrently. Retry the operation."
                ))
            authoritative_full_partials = authoritative_fulls.mapped(
                'partial_reconcile_ids',
            )
            authoritative_full_exchange = authoritative_full_partials.mapped(
                'exchange_move_id',
            ) | authoritative_full_partials._eh_caba_moves()
            if 'exchange_move_id' in authoritative_fulls._fields:
                authoritative_full_exchange |= authoritative_fulls.mapped(
                    'exchange_move_id',
                )
            authoritative_exchange = (
                self.sudo().mapped('exchange_move_id')
                | self.sudo()._eh_caba_moves()
                | authoritative_full_exchange
            )
            if not set(authoritative_exchange.ids).issubset(
                set(exchange_moves.ids),
            ):
                raise UserError(_(
                    "A reconciliation exchange move changed concurrently. "
                    "Retry the operation."
                ))
            self._eh_assert_exchange_moves_unclaimed(authoritative_exchange)
            authoritative_matching_lines._eh_assert_batch_reconciliation_mutable()
            return super().unlink()


class AccountFullReconcile(models.Model):
    _inherit = 'account.full.reconcile'

    @api.model
    def _eh_relation_command_ids(self, commands, allow_removals=False):
        """Return linked ids from core-supported One2many commands."""
        record_ids = set()
        for command in commands or ():
            if not isinstance(command, (tuple, list)) or not command:
                raise UserError(_(
                    "Full reconciliation relations require explicit existing "
                    "journal-item and partial-reconcile links."
                ))
            operation = command[0]
            if operation == 4 and len(command) > 1 and command[1]:
                record_ids.add(command[1])
            elif operation == 6 and len(command) > 2:
                record_ids.update(command[2] or ())
            elif allow_removals and operation in (3, 5):
                continue
            else:
                raise UserError(_(
                    "Full reconciliation relations cannot create, edit, or "
                    "delete accounting evidence through nested commands."
                ))
        return record_ids

    def _eh_lock_full_reconcile_scope(self, extra_lines=None,
                                      extra_partials=None,
                                      extra_exchange_moves=None):
        """Lock full rows, partial rows, then every authoritative AML."""
        Batch = self.env['eh.batch.payment']
        preliminary_partials = self.sudo().mapped('partial_reconcile_ids') | (
            extra_partials or self.env['account.partial.reconcile']
        ).sudo()
        preliminary_lines = self.sudo().mapped('reconciled_line_ids') | (
            extra_lines or self.env['account.move.line']
        ).sudo()
        preliminary_lines |= (
            preliminary_partials.debit_move_id
            | preliminary_partials.credit_move_id
        )
        preliminary_exchange = preliminary_partials.mapped(
            'exchange_move_id',
        ) | (extra_exchange_moves or self.env['account.move']).sudo()
        if 'exchange_move_id' in self._fields:
            preliminary_exchange |= self.sudo().mapped('exchange_move_id')
        preliminary_scope_moves = (
            preliminary_exchange | preliminary_lines.move_id
        )
        Batch._eh_advisory_move_owner_lock(preliminary_scope_moves.ids)
        fulls = self.sudo().exists()
        if self.ids:
            self.env.cr.execute(
                "SELECT id FROM account_full_reconcile WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(self.ids)),),
            )
            locked_ids = {row[0] for row in self.env.cr.fetchall()}
            if locked_ids != set(self.ids):
                raise UserError(_(
                    "A full reconciliation changed concurrently. Retry the "
                    "operation."
                ))
        invalidate_fields = [
            'reconciled_line_ids', 'partial_reconcile_ids',
        ]
        if 'exchange_move_id' in self._fields:
            invalidate_fields.append('exchange_move_id')
        fulls.invalidate_recordset(invalidate_fields)
        partials = fulls.partial_reconcile_ids | (
            extra_partials or self.env['account.partial.reconcile']
        ).sudo()
        lines = fulls.reconciled_line_ids | (
            extra_lines or self.env['account.move.line']
        ).sudo()
        if partials:
            lines |= partials._eh_lock_current_partial_lines()
        exchange_moves = partials.mapped('exchange_move_id') | (
            extra_exchange_moves or self.env['account.move']
        ).sudo()
        if 'exchange_move_id' in self._fields:
            exchange_moves |= fulls.mapped('exchange_move_id')
        if not set(exchange_moves.ids).issubset(
            set(preliminary_exchange.ids),
        ):
            raise UserError(_(
                "A full-reconciliation exchange move changed concurrently. "
                "Retry the operation."
            ))
        if not set(lines.move_id.ids).issubset(
            set(preliminary_scope_moves.ids),
        ):
            raise UserError(_(
                "A full-reconciliation journal-item graph changed "
                "concurrently. Retry the operation."
            ))
        self.env['account.partial.reconcile']._eh_assert_exchange_moves_unclaimed(
            exchange_moves,
        )
        lines = lines._eh_batch_reconcile_lock()
        lines._eh_assert_batch_reconciliation_mutable()
        return fulls, partials, lines

    @api.model_create_multi
    def create(self, vals_list):
        if has_authorized_batch_reconcile_capability(self.env):
            return super().create(vals_list)
        self._eh_check_access('create')
        line_ids = set()
        partial_ids = set()
        for vals in vals_list:
            line_ids.update(self._eh_relation_command_ids(
                vals.get('reconciled_line_ids'),
            ))
            partial_ids.update(self._eh_relation_command_ids(
                vals.get('partial_reconcile_ids'),
            ))
        lines = self.env['account.move.line'].browse(sorted(line_ids))
        partials = self.env['account.partial.reconcile'].browse(
            sorted(partial_ids),
        )
        exchange_moves = self.env['account.move']
        if 'exchange_move_id' in self._fields:
            exchange_moves = exchange_moves.browse(sorted({
                vals.get('exchange_move_id') for vals in vals_list
                if vals.get('exchange_move_id')
            })).exists()
        lines._eh_check_access('write')
        partials._eh_check_access('write')
        exchange_moves._eh_check_access('write')
        with self.env.cr.savepoint():
            self._eh_lock_full_reconcile_scope(
                lines, partials, exchange_moves,
            )
            capable = self.with_context(**{
                AUTHORIZED_BATCH_RECONCILE_CONTEXT:
                    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
            })
            fulls = super(AccountFullReconcile, capable).create(vals_list)
            return self.browse(fulls.ids)

    def write(self, vals):
        if (
            not _FULL_MATERIAL_FIELDS.intersection(vals)
            or has_authorized_batch_reconcile_capability(self.env)
        ):
            return super().write(vals)
        self._eh_check_access('write')
        line_ids = self._eh_relation_command_ids(
            vals.get('reconciled_line_ids'), allow_removals=True,
        )
        partial_ids = self._eh_relation_command_ids(
            vals.get('partial_reconcile_ids'), allow_removals=True,
        )
        lines = self.env['account.move.line'].browse(sorted(line_ids))
        partials = self.env['account.partial.reconcile'].browse(
            sorted(partial_ids),
        )
        exchange_moves = self.env['account.move']
        if 'exchange_move_id' in self._fields:
            exchange_moves = exchange_moves.browse(
                [vals['exchange_move_id']]
                if vals.get('exchange_move_id') else [],
            ).exists()
        lines._eh_check_access('write')
        partials._eh_check_access('write')
        exchange_moves._eh_check_access('write')
        with self.env.cr.savepoint():
            self._eh_lock_full_reconcile_scope(
                lines, partials, exchange_moves,
            )
            capable = self.with_context(**{
                AUTHORIZED_BATCH_RECONCILE_CONTEXT:
                    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
            })
            return super(AccountFullReconcile, capable).write(vals)

    def unlink(self):
        if has_authorized_batch_reconcile_capability(self.env):
            return super().unlink()
        self._eh_check_access('unlink')
        with self.env.cr.savepoint():
            self._eh_lock_full_reconcile_scope()
            capable = self.with_context(**{
                AUTHORIZED_BATCH_RECONCILE_CONTEXT:
                    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
            })
            return super(AccountFullReconcile, capable).unlink()
