# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Protect source-document lifecycle while a reviewed batch references it."""

from contextlib import nullcontext

from odoo import _, api, models
from odoo.exceptions import UserError

from .capabilities import (
    CORE_AML_CAPABILITY,
    CORE_AML_CONTEXT,
    has_authorized_batch_post_capability,
    has_authorized_batch_reconcile_capability,
    has_authorized_batch_reversal_capability,
    has_authorized_batch_member_mutation_capability,
    has_authorized_payment_post_capability,
    has_core_aml_capability,
    has_core_reconcile_capability,
)


_BATCH_MOVE_FINANCIAL_FIELDS = frozenset({
    'date', 'name', 'ref', 'partner_id', 'journal_id', 'company_id',
    'currency_id', 'move_type', 'invoice_date', 'invoice_date_due',
    'origin_payment_id', 'payment_id', 'statement_line_id',
    'matched_payment_ids', 'reversed_entry_id',
    'reversal_move_id', 'reversal_move_ids',
    'tax_cash_basis_rec_id', 'tax_cash_basis_origin_move_id',
    'tax_cash_basis_created_move_ids',
    'line_ids', 'invoice_line_ids', 'payment_reference', 'payment_state',
    'amount_untaxed', 'amount_tax', 'amount_total', 'amount_residual',
    'amount_untaxed_signed', 'amount_untaxed_in_currency_signed',
    'amount_tax_signed', 'amount_total_signed',
    'amount_total_in_currency_signed', 'amount_residual_signed',
    'sequence_prefix', 'sequence_number',
    'secure_sequence_number', 'inalterable_hash', 'partner_bank_id',
    'auto_post', 'posted_before',
})
_BATCH_SOURCE_CORRECTABLE_FIELDS = frozenset({
    'ref', 'payment_reference', 'invoice_date_due', 'invoice_date',
    'partner_bank_id',
})
_BATCH_LINE_FINANCIAL_FIELDS = frozenset({
    'move_id', 'account_id', 'partner_id', 'currency_id', 'debit', 'credit',
    'balance', 'amount_currency', 'date', 'date_maturity', 'name', 'ref',
    'journal_id', 'company_id', 'company_currency_id',
    'analytic_distribution', 'tax_ids', 'tax_tag_ids', 'product_id',
    'quantity', 'price_unit', 'discount', 'display_type',
    'tax_repartition_line_id', 'tax_line_id', 'tax_base_amount',
    'product_uom_id', 'group_tax_id', 'tax_group_id', 'extra_tax_data',
    'price_subtotal', 'price_total', 'is_storno', 'deductible_amount',
    'amount_residual', 'amount_residual_currency', 'reconciled',
    'full_reconcile_id', 'matching_number', 'reconcile_model_id',
    'discount_date', 'discount_amount_currency', 'discount_balance',
    'is_imported',
    'move_name', 'parent_state', 'invoice_date', 'payment_id',
    'statement_line_id', 'statement_id', 'sequence',
})


def _eh_core_aml_self(records):
    """Attach the process-local AML token after the caller's preflight."""
    return records.with_context(**{
        CORE_AML_CONTEXT: CORE_AML_CAPABILITY,
    })


def _eh_clean_core_aml_result(item):
    """Remove the AML token recursively from every returned recordset."""
    if isinstance(item, models.BaseModel):
        clean_context = dict(item.env.context)
        clean_context.pop(CORE_AML_CONTEXT, None)
        return item.with_context(clean_context)
    if isinstance(item, list):
        return [_eh_clean_core_aml_result(child) for child in item]
    if isinstance(item, tuple):
        return tuple(_eh_clean_core_aml_result(child) for child in item)
    if isinstance(item, dict):
        return {
            _eh_clean_core_aml_result(key):
                _eh_clean_core_aml_result(value)
            for key, value in item.items()
        }
    if isinstance(item, set):
        return {_eh_clean_core_aml_result(child) for child in item}
    return item


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model
    def _eh_batch_payment_owner_field(self):
        if 'origin_payment_id' in self._fields:
            return 'origin_payment_id'
        if 'payment_id' in self._fields:
            return 'payment_id'
        return False

    @api.model
    def _eh_batch_reversal_inverse_field(self):
        """Return core's reversal-child field across Odoo 16-19."""
        if 'reversal_move_ids' in self._fields:
            return 'reversal_move_ids'
        if 'reversal_move_id' in self._fields:
            return 'reversal_move_id'
        return False

    def _eh_batch_payment_owners(self):
        """Return canonical inverse and direct payment owners of these moves."""
        if not self.ids:
            return self.env['account.payment']
        Payment = self.env['account.payment'].sudo()
        owners = Payment.search([('move_id', 'in', self.ids)], order='id')
        owner_field = self._eh_batch_payment_owner_field()
        if owner_field:
            self.sudo().invalidate_recordset([owner_field])
            owners |= self.sudo().mapped(owner_field)
        return owners

    def _eh_lock_batch_payment_owners(self, extra_payments=None):
        """Lock self/canonical move ids once, then re-read owner relations."""
        extra_payments = extra_payments or self.env['account.payment']
        preliminary = self._eh_batch_payment_owners() | extra_payments
        if preliminary:
            preliminary._eh_lock_batch_mutation(payment_move_ids=self.ids)
        else:
            self.env['eh.batch.payment']._eh_advisory_move_owner_lock(self.ids)
        owner_field = self._eh_batch_payment_owner_field()
        self.invalidate_recordset([owner_field])
        preliminary.invalidate_recordset([
            'eh_batch_payment_id', 'move_id',
        ])
        authoritative = self._eh_batch_payment_owners()
        if authoritative - preliminary:
            raise UserError(_(
                "A payment journal-entry owner changed concurrently. No "
                "operation was performed; retry."
            ))
        return authoritative | extra_payments

    def _eh_assert_batch_payment_financial_mutable(self, flush=True):
        """Block direct move/line edits while any batch owns the payment."""
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_authorized_batch_reversal_capability(self.env)
            or not self.ids
        ):
            return True
        self._eh_check_access('write')
        savepoint = (
            self.env.cr.savepoint()
            if flush else nullcontext()
        )
        with savepoint:
            owners = self._eh_lock_batch_payment_owners()
            if owners.filtered('eh_batch_payment_id'):
                raise UserError(_(
                    "A batch member's journal-entry lines are workflow-owned. "
                    "Edit a draft member through the payment, or post it "
                    "through the batch workflow."
                ))
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if self._eh_create_needs_guard_savepoint(vals_list):
            with self.env.cr.savepoint():
                return self._eh_create_with_batch_owner_guard(vals_list)
        return self._eh_create_with_batch_owner_guard(vals_list)

    @api.model
    def _eh_create_needs_guard_savepoint(self, vals_list):
        """Return whether create can mutate pre-existing protected evidence."""
        owner_field = self._eh_batch_payment_owner_field()
        direct_fields = {
            'matched_payment_ids', 'reversed_entry_id',
            'reversal_move_id', 'reversal_move_ids',
            'tax_cash_basis_rec_id', 'tax_cash_basis_origin_move_id',
            'tax_cash_basis_created_move_ids', owner_field,
        }
        direct_fields.discard(False)
        for vals in vals_list:
            if direct_fields.intersection(vals):
                return True
            for field_name in ('line_ids', 'invoice_line_ids'):
                for command in vals.get(field_name) or ():
                    if not isinstance(command, (tuple, list)) or not command:
                        return True
                    operation = command[0]
                    if operation != 0:
                        return True
                    if (
                        len(command) > 2
                        and isinstance(command[2], dict)
                        and command[2].get('move_id')
                    ):
                        return True
        return False

    def _eh_create_with_batch_owner_guard(self, vals_list):
        affected_moves = self._eh_prelock_combined_relation_scope(vals_list)
        self._eh_assert_batch_provenance_targets_mutable(vals_list)
        for vals in vals_list:
            self._eh_assert_matched_payment_links_mutable(vals)
        owner_field = self._eh_batch_payment_owner_field()
        target_owner_ids = {
            vals.get(owner_field) for vals in vals_list
            if owner_field and vals.get(owner_field)
        }
        if (
            target_owner_ids
            and not has_authorized_payment_post_capability(self.env)
            and not has_authorized_batch_member_mutation_capability(self.env)
        ):
            targets = self.env['account.payment'].browse(
                sorted(target_owner_ids),
            ).exists()
            if len(targets) != len(target_owner_ids):
                raise UserError(_("The target payment no longer exists."))
            targets._eh_check_access('write')
            targets = targets.sudo()
            targets._eh_lock_batch_mutation()
            targets.invalidate_recordset(['eh_batch_payment_id', 'move_id'])
            if targets.filtered('eh_batch_payment_id'):
                raise UserError(_(
                    "A batch payment journal-entry link is workflow-owned and "
                    "cannot be created outside the batch Post workflow."
                ))
        # Core accepts UPDATE/DELETE/UNLINK/LINK/SET commands against existing
        # journal items while creating a different move.  The AML capability
        # below is safe only after every current and prospective parent move
        # discovered by the canonical relation prelock has passed both seals.
        affected_moves._eh_assert_batch_payment_financial_mutable()
        affected_moves._eh_assert_batch_source_financial_mutable()
        moves = super(
            AccountMove, _eh_core_aml_self(self),
        ).create(vals_list)
        if (
            target_owner_ids
            and not has_authorized_payment_post_capability(self.env)
            and not has_authorized_batch_member_mutation_capability(self.env)
        ):
            for move, vals in zip(moves, vals_list):
                target = self.env['account.payment'].browse(
                    vals.get(owner_field),
                ).exists()
                if target and target.move_id and target.move_id != move:
                    raise UserError(_(
                        "A payment cannot own an additional shadow journal "
                        "entry."
                    ))
        return _eh_clean_core_aml_result(moves)

    def _eh_assert_batch_payment_owner_link_mutable(self, vals):
        """Lock and validate direct payment-owner link changes."""
        owner_field = self._eh_batch_payment_owner_field()
        if not owner_field or owner_field not in vals:
            return True
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
        ):
            return True
        self._eh_check_access('write')
        target = self.env['account.payment'].browse(
            vals.get(owner_field),
        ).exists()
        if target:
            target._eh_check_access('write')
            target = target.sudo()
        with self.env.cr.savepoint():
            owners = self._eh_lock_batch_payment_owners(target)
            live = owners.filtered(
                lambda payment: payment.eh_batch_payment_id.state
                in ('confirmed', 'posted'),
            )
            if live:
                raise UserError(_(
                    "A reviewed batch payment journal-entry link is immutable."
                ))
            if target and any(target.move_id != move for move in self):
                raise UserError(_(
                    "A payment cannot be linked to a shadow journal entry."
                ))
        return True

    def _eh_assert_batch_payment_move_postable(self):
        """Block posting a cash move outside its owning batch workflow.

        Odoo 16/17 delegate ``account.payment`` to a required ``move_id``.
        Raw move writes and the private posting engine can bypass the payment
        model's public guard. Lock the owning payment even when it is not
        currently assigned so a concurrent batch assignment cannot commit
        between the membership check and posting.
        """
        if has_authorized_payment_post_capability(self.env):
            return True
        if not self.ids:
            return True
        self._eh_check_access('write')
        # Rejected server calls must release every lock even when their caller
        # catches the UserError. Successful checks release only the savepoint;
        # PostgreSQL retains their transaction locks through core posting.
        with self.env.cr.savepoint():
            payments = self._eh_lock_batch_payment_owners()
            if not payments:
                return True
            assigned_members = payments.filtered('eh_batch_payment_id')
            if assigned_members:
                raise UserError(_(
                    "A batch payment journal entry can only be posted through "
                    "the batch Post workflow. Clear its payment's batch "
                    "membership before posting the entry separately."
                ))
        return True

    def _eh_reviewed_batch_source_payments(self):
        """Find authoritative reviewed source-document references."""
        if not self.ids:
            return self.env['account.payment']
        Payment = self.env['account.payment'].sudo()
        source_domain = [('eh_source_move_ids', 'in', self.ids)]
        if 'invoice_ids' in Payment._fields:
            source_domain = [
                '|',
                ('invoice_ids', 'in', self.ids),
            ] + source_domain
        return Payment.search(source_domain + [
            ('eh_batch_payment_id.state', 'in', ('confirmed', 'posted')),
            ('eh_batch_payment_id.reversed_at', '=', False),
        ], order='id')

    def _eh_live_batch_source_payments(self):
        """Find authoritative source or posted-cash evidence references."""
        sources = self._eh_reviewed_batch_source_payments()
        owners = self._eh_batch_payment_owners().filtered(
            lambda payment: payment.eh_batch_payment_id.state
            in ('confirmed', 'posted'),
        )
        return sources | owners

    def _eh_assert_batch_source_financial_mutable(self, flush=True):
        """Freeze headers and lines of every reviewed source document.

        Source assignment and this edit guard take the same per-move advisory
        lock before the global claim lock.  Therefore an empty preliminary
        lookup is authoritative: a concurrent assignment cannot appear after
        it without first waiting for this transaction's source-move lock.
        """
        if (
            not self.ids
            or has_authorized_batch_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_reversal_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
        ):
            return True
        self._eh_check_access('write')
        Batch = self.env['eh.batch.payment']
        savepoint = (
            self.env.cr.savepoint()
            if flush else nullcontext()
        )
        with savepoint:
            Batch._eh_advisory_move_owner_lock(self.ids)
            linked = self._eh_reviewed_batch_source_payments()
            if not linked:
                return True
            Batch._eh_advisory_integrity_lock()
            self.invalidate_recordset()
            linked = self._eh_reviewed_batch_source_payments()
            if linked:
                raise UserError(_(
                    "Financial headers and journal items of a confirmed or "
                    "posted batch source are frozen. Cancel and reset the "
                    "reviewed batch before editing its source evidence."
                ))
        return True

    def _eh_is_posted_source_metadata_correction(self, vals):
        """Allow narrow document metadata correction after cash is posted.

        Confirmation still freezes these values. Once the batch is posted,
        its payment instruction and export snapshot are sealed independently,
        so correcting vendor reference, document/due date, or the source bank
        must not strand the supplier/customer document forever.
        """
        if (
            not self.ids
            or not vals
            or not set(vals).issubset(_BATCH_SOURCE_CORRECTABLE_FIELDS)
        ):
            return False
        Batch = self.env['eh.batch.payment']
        Batch._eh_advisory_move_owner_lock(self.ids)
        if self._eh_batch_payment_owners().filtered('eh_batch_payment_id'):
            return False
        linked = self._eh_reviewed_batch_source_payments()
        if not linked:
            return False
        Batch._eh_advisory_integrity_lock()
        self.invalidate_recordset()
        linked = self._eh_reviewed_batch_source_payments()
        return bool(linked) and all(
            payment.eh_batch_payment_id.state == 'posted'
            for payment in linked
        )

    def _eh_assert_batch_source_lifecycle_mutable(self):
        """Prevent cascade/removal of an approved source relationship.

        A Many2many relation row is deleted by PostgreSQL when its source
        move is unlinked; that bypasses account.payment.write and therefore
        the normal confirmed-payment freeze.  Serializing with the same batch
        lock, and refusing state reset/deletion while any reviewed batch is
        live, keeps the approved source set intact.
        """
        if not self.ids:
            return True
        if (
            has_authorized_batch_post_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
        ):
            return True
        Batch = self.env['eh.batch.payment']
        with self.env.cr.savepoint():
            Batch._eh_advisory_move_owner_lock(self.ids)
            # Batch source/member assignment takes this same per-move lock
            # before the module claim lock.  Once it is held, an empty lookup
            # is authoritative and an ordinary legacy payment-owner write
            # need not serialize every batch workflow in the database.
            linked = self._eh_live_batch_source_payments()
            if not linked:
                return True
            Batch._eh_advisory_integrity_lock()
            owner_field = self._eh_batch_payment_owner_field()
            if owner_field:
                self.invalidate_recordset([owner_field])
            # Re-read after the locks. A stale pre-lock search must not either
            # invent or omit a concurrently committed cancellation/reference.
            self.invalidate_recordset()
            linked = self._eh_live_batch_source_payments()
            if linked:
                raise UserError(_(
                    "This journal entry is an approved source document or "
                    "posted cash-movement record for a confirmed/posted batch "
                    "payment. It cannot be reset, cancelled, or deleted while "
                    "that reviewed batch is live."
                ))
        return True

    @api.model
    def _eh_matched_payment_command_ids(self, commands):
        payment_ids = set()
        for command in commands or ():
            if not isinstance(command, (tuple, list)) or not command:
                raise UserError(_(
                    "Matched payments require explicit relation commands."
                ))
            operation = command[0]
            if operation in (0, 1, 2):
                raise UserError(_(
                    "Payments cannot be created, edited, or deleted through "
                    "a source document relation."
                ))
            if operation in (3, 4) and len(command) > 1 and command[1]:
                payment_ids.add(command[1])
            elif operation == 6 and len(command) > 2:
                payment_ids.update(command[2] or ())
            elif operation != 5:
                raise UserError(_(
                    "Unsupported matched-payment relation command."
                ))
        return payment_ids

    def _eh_assert_matched_payment_links_mutable(self, vals):
        """Freeze Odoo 18/19 inverse invoice links after batch review."""
        if (
            'matched_payment_ids' not in self._fields
            or 'matched_payment_ids' not in vals
            or has_authorized_batch_member_mutation_capability(self.env)
        ):
            return True
        explicit_ids = self._eh_matched_payment_command_ids(
            vals.get('matched_payment_ids'),
        )
        self._eh_check_access('write')
        Batch = self.env['eh.batch.payment']
        with self.env.cr.savepoint():
            Batch._eh_advisory_move_owner_lock(self.ids)
            self.invalidate_recordset(['matched_payment_ids'])
            payment_ids = set(self.sudo().matched_payment_ids.ids) | explicit_ids
            payments = self.env['account.payment'].browse(
                sorted(payment_ids),
            ).exists()
            if len(payments) != len(payment_ids):
                raise UserError(_(
                    "One or more matched payments no longer exist."
                ))
            payments._eh_check_access('write')
            if payments:
                self.env.cr.execute(
                    "SELECT id FROM account_payment WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    (tuple(sorted(payments.ids)),),
                )
                payments.invalidate_recordset(['eh_batch_payment_id'])
            if payments.filtered(
                lambda payment: payment.eh_batch_payment_id.state
                in ('confirmed', 'posted'),
            ):
                raise UserError(_(
                    "Matched source-document links are frozen after batch "
                    "confirmation. Cancel and reset the reviewed batch first."
                ))
        return True

    @api.model
    def _eh_prelock_combined_relation_scope(self, vals_list,
                                             extra_move_ids=()):
        """Acquire one complete scope before any relation-family guard."""
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_reversal_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_reconcile_capability(self.env)
        ):
            return self
        owner_field = self._eh_batch_payment_owner_field()
        reversal_field = self._eh_batch_reversal_inverse_field()
        guarded_fields = {
            'line_ids', 'invoice_line_ids', 'matched_payment_ids',
            'reversed_entry_id', reversal_field,
            'tax_cash_basis_rec_id', 'tax_cash_basis_origin_move_id',
            'tax_cash_basis_created_move_ids', owner_field,
        }
        guarded_fields.discard(False)
        if not any(guarded_fields.intersection(vals) for vals in vals_list):
            return self

        move_ids = set(self.ids) | set(extra_move_ids or ())
        payment_ids = set()
        line_ids = set()
        partial_ids = set()
        inverse_move_fields = tuple(
            field_name for field_name in (
                reversal_field, 'tax_cash_basis_created_move_ids',
            )
            if field_name and field_name in self._fields
        )
        for vals in vals_list:
            if owner_field and vals.get(owner_field):
                payment_ids.add(vals[owner_field])
            if (
                'matched_payment_ids' in self._fields
                and 'matched_payment_ids' in vals
            ):
                payment_ids.update(self._eh_matched_payment_command_ids(
                    vals.get('matched_payment_ids'),
                ))
            for field_name in (
                'reversed_entry_id', 'tax_cash_basis_origin_move_id',
            ):
                if field_name in self._fields and vals.get(field_name):
                    move_ids.add(vals[field_name])
            for field_name in inverse_move_fields:
                if field_name in vals:
                    move_ids.update(self._eh_provenance_relation_ids(
                        vals[field_name],
                    ))
            if (
                'tax_cash_basis_rec_id' in self._fields
                and vals.get('tax_cash_basis_rec_id')
            ):
                partial_ids.add(vals['tax_cash_basis_rec_id'])
            for field_name in ('line_ids', 'invoice_line_ids'):
                if field_name not in self._fields or field_name not in vals:
                    continue
                for command in vals.get(field_name) or ():
                    if not isinstance(command, (tuple, list)) or not command:
                        raise UserError(_(
                            "Journal-item relations require explicit ORM "
                            "commands."
                        ))
                    operation = command[0]
                    if operation in (1, 2, 3, 4):
                        if len(command) < 2 or not command[1]:
                            raise UserError(_(
                                "A journal-item relation command has no target."
                            ))
                        line_ids.add(command[1])
                    elif operation == 6:
                        if len(command) < 3:
                            raise UserError(_(
                                "A journal-item SET command has no target list."
                            ))
                        line_ids.update(command[2] or ())
                    elif operation not in (0, 5):
                        raise UserError(_(
                            "Unsupported journal-item relation command."
                        ))
                    if operation in (0, 1):
                        if (
                            len(command) < 3
                            or not isinstance(command[2], dict)
                        ):
                            raise UserError(_(
                                "Nested journal-item values must be a mapping."
                            ))
                        if command[2].get('move_id'):
                            move_ids.add(command[2]['move_id'])

        if self:
            for field_name in ('line_ids', 'invoice_line_ids'):
                if field_name in self._fields and any(
                    field_name in vals for vals in vals_list
                ):
                    line_ids.update(self.sudo().mapped(field_name).ids)
            if 'matched_payment_ids' in self._fields and any(
                'matched_payment_ids' in vals for vals in vals_list
            ):
                payment_ids.update(self.sudo().matched_payment_ids.ids)
            for field_name in (
                'reversed_entry_id', 'tax_cash_basis_origin_move_id',
                *inverse_move_fields,
            ):
                if field_name in self._fields:
                    move_ids.update(self.sudo().mapped(field_name).ids)
            if 'tax_cash_basis_rec_id' in self._fields:
                partial_ids.update(
                    self.sudo().mapped('tax_cash_basis_rec_id').ids,
                )

        lines = self.env['account.move.line'].browse(sorted(line_ids)).exists()
        payments = self.env['account.payment'].browse(
            sorted(payment_ids),
        ).exists()
        partials = self.env['account.partial.reconcile'].browse(
            sorted(partial_ids),
        ).exists()
        if (
            len(lines) != len(line_ids)
            or len(payments) != len(payment_ids)
            or len(partials) != len(partial_ids)
        ):
            raise UserError(_(
                "A journal-item relation target no longer exists."
            ))
        lines._eh_check_access('write')
        payments._eh_check_access('write')
        partials._eh_check_access('read')
        move_ids.update(lines.sudo().mapped('move_id').ids)
        move_ids.update(partials.sudo().mapped('debit_move_id.move_id').ids)
        move_ids.update(partials.sudo().mapped('credit_move_id.move_id').ids)
        move_ids.update(partials.sudo().mapped('exchange_move_id').ids)
        moves = self.env['account.move'].browse(sorted(move_ids)).exists()
        if len(moves) != len(move_ids):
            raise UserError(_(
                "A journal-entry relation target no longer exists."
            ))
        moves._eh_check_access('write')
        # This is the first lock operation for every relation family. Owner
        # discovery expands all current/prospective payments to complete batch
        # sibling/source/cash scopes, sorted before the global claim lock.
        moves._eh_lock_batch_payment_owners(payments)
        lines.invalidate_recordset(['move_id'])
        partials.invalidate_recordset([
            'debit_move_id', 'credit_move_id', 'exchange_move_id',
        ])
        authoritative_move_ids = set(lines.sudo().mapped('move_id').ids)
        authoritative_move_ids.update(
            partials.sudo().mapped('debit_move_id.move_id').ids,
        )
        authoritative_move_ids.update(
            partials.sudo().mapped('credit_move_id.move_id').ids,
        )
        authoritative_move_ids.update(
            partials.sudo().mapped('exchange_move_id').ids,
        )
        authoritative_payment_ids = set()
        if self:
            relation_names = [
                field_name for field_name in (
                    'reversed_entry_id', 'tax_cash_basis_origin_move_id',
                    *inverse_move_fields,
                )
                if field_name in self._fields
            ]
            if relation_names:
                self.invalidate_recordset(relation_names)
                for field_name in relation_names:
                    authoritative_move_ids.update(
                        self.sudo().mapped(field_name).ids,
                    )
            if 'tax_cash_basis_rec_id' in self._fields:
                self.invalidate_recordset(['tax_cash_basis_rec_id'])
                current_partials = self.sudo().mapped(
                    'tax_cash_basis_rec_id',
                )
                current_partials.invalidate_recordset([
                    'debit_move_id', 'credit_move_id', 'exchange_move_id',
                ])
                authoritative_move_ids.update(
                    current_partials.debit_move_id.move_id.ids,
                )
                authoritative_move_ids.update(
                    current_partials.credit_move_id.move_id.ids,
                )
                authoritative_move_ids.update(
                    current_partials.exchange_move_id.ids,
                )
            if (
                'matched_payment_ids' in self._fields
                and any('matched_payment_ids' in vals for vals in vals_list)
            ):
                self.invalidate_recordset(['matched_payment_ids'])
                authoritative_payment_ids.update(
                    self.sudo().matched_payment_ids.ids,
                )
        if (
            not authoritative_move_ids.issubset(move_ids)
            or not authoritative_payment_ids.issubset(payment_ids)
        ):
            raise UserError(_(
                "A journal-entry relation changed concurrently. Retry the "
                "operation."
            ))
        # ``moves`` is the actor-environment, access-checked union of the
        # authoritative existing parents and every prospective target supplied
        # in the commands.  Callers that mint the AML capability must freeze
        # this complete set, not merely ``self``.
        return moves

    def _eh_prelock_post_reconcile_scope(self):
        """Prelock every move core posting/reversal may later reconcile.

        Posting a reversal or reversing a partially settled document reaches
        reconciliation only after core has started work. Discover the full
        existing settlement graph and current provenance relations first,
        then acquire their per-move advisories in one sorted pass.
        """
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_reconcile_capability(self.env)
            or not self.ids
        ):
            return True
        self._eh_check_access('write')
        Batch = self.env['eh.batch.payment']
        reversal_field = self._eh_batch_reversal_inverse_field()
        graph_seed_moves = self
        for field_name in (
            'reversed_entry_id', reversal_field,
            'tax_cash_basis_origin_move_id',
            'tax_cash_basis_created_move_ids',
        ):
            if field_name and field_name in self._fields:
                graph_seed_moves |= self.sudo().mapped(field_name)
        if 'tax_cash_basis_rec_id' in self._fields:
            current_partials = self.sudo().mapped('tax_cash_basis_rec_id')
            graph_seed_moves |= current_partials.debit_move_id.move_id \
                | current_partials.credit_move_id.move_id \
                | current_partials.exchange_move_id
        if 'matched_payment_ids' in self._fields:
            matched_payments = self.sudo().matched_payment_ids
            graph_seed_moves |= matched_payments.move_id
            graph_seed_moves |= matched_payments._eh_core_invoice_moves()
            graph_seed_moves |= matched_payments.eh_source_move_ids
        self.env['account.move'].browse(
            graph_seed_moves.ids,
        )._eh_check_access('write')
        (
            preliminary_graph_line_ids,
            preliminary_graph_partial_ids,
            preliminary_graph_move_ids,
        ) = Batch._eh_source_reconcile_graph_ids(graph_seed_moves.ids)
        list_fields = {
            'line_ids', 'invoice_line_ids', 'matched_payment_ids',
            reversal_field, 'tax_cash_basis_created_move_ids',
        }
        list_fields.discard(False)
        relation_vals = {
            field_name: [] if field_name in list_fields else False
            for field_name in (
                'line_ids', 'invoice_line_ids', 'matched_payment_ids',
                'reversed_entry_id', reversal_field,
                'tax_cash_basis_rec_id', 'tax_cash_basis_origin_move_id',
                'tax_cash_basis_created_move_ids',
                self._eh_batch_payment_owner_field(),
            )
            if field_name and field_name in self._fields
        }
        self._eh_prelock_combined_relation_scope(
            [relation_vals],
            extra_move_ids=preliminary_graph_move_ids,
        )
        (
            authoritative_graph_line_ids,
            authoritative_graph_partial_ids,
            authoritative_graph_move_ids,
        ) = Batch._eh_source_reconcile_graph_ids(graph_seed_moves.ids)
        if (
            not authoritative_graph_line_ids.issubset(
                preliminary_graph_line_ids,
            )
            or not authoritative_graph_partial_ids.issubset(
                preliminary_graph_partial_ids,
            )
            or not authoritative_graph_move_ids.issubset(
                preliminary_graph_move_ids,
            )
        ):
            raise UserError(_(
                "The reconciliation graph changed concurrently. Retry the "
                "operation."
            ))
        return True

    @api.model
    def _eh_lock_line_command_move_scope(self, vals):
        """Prelock parent/current/target moves for inverse AML commands."""
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_reconcile_capability(self.env)
        ):
            return self
        relation_fields = tuple(
            field_name for field_name in ('line_ids', 'invoice_line_ids')
            if field_name in vals and field_name in self._fields
        )
        if not relation_fields or not self.ids:
            return self
        explicit_line_ids = set()
        explicit_move_ids = set()
        for field_name in relation_fields:
            for command in vals.get(field_name) or ():
                if not isinstance(command, (tuple, list)) or not command:
                    raise UserError(_(
                        "Journal-item relations require explicit ORM commands."
                    ))
                operation = command[0]
                if operation in (1, 2, 3, 4):
                    if len(command) < 2 or not command[1]:
                        raise UserError(_(
                            "A journal-item relation command has no target."
                        ))
                    explicit_line_ids.add(command[1])
                elif operation == 6:
                    if len(command) < 3:
                        raise UserError(_(
                            "A journal-item SET command has no target list."
                        ))
                    explicit_line_ids.update(command[2] or ())
                elif operation not in (0, 5):
                    raise UserError(_(
                        "Unsupported journal-item relation command."
                    ))
                if operation in (0, 1):
                    if len(command) < 3 or not isinstance(command[2], dict):
                        raise UserError(_(
                            "Nested journal-item values must be a mapping."
                        ))
                    if command[2].get('move_id'):
                        explicit_move_ids.add(command[2]['move_id'])
        explicit_lines = self.env['account.move.line'].browse(
            sorted(explicit_line_ids),
        ).exists()
        if len(explicit_lines) != len(explicit_line_ids):
            raise UserError(_("One or more journal items no longer exist."))
        explicit_lines._eh_check_access('write')
        current_lines = self.env['account.move.line']
        for field_name in relation_fields:
            current_lines |= self.sudo().mapped(field_name)
        actor_current_lines = self.env['account.move.line'].browse(
            current_lines.ids,
        )
        actor_current_lines._eh_check_access('write')
        explicit_moves = self.env['account.move'].browse(
            sorted(explicit_move_ids),
        ).exists()
        if len(explicit_moves) != len(explicit_move_ids):
            raise UserError(_("One or more target journal entries no longer exist."))
        explicit_moves._eh_check_access('write')
        preliminary = self | explicit_moves | current_lines.move_id \
            | explicit_lines.move_id
        # If any parent is payment-owned, expand to its complete batch scope
        # before taking the first advisory. Raw per-move locking here would let
        # two sibling edits each hold one move while waiting for the other's
        # later batch-scope expansion.
        preliminary._eh_lock_batch_payment_owners()
        self.invalidate_recordset(list(relation_fields))
        explicit_lines.invalidate_recordset(['move_id'])
        authoritative_lines = explicit_lines
        for field_name in relation_fields:
            authoritative_lines |= self.sudo().mapped(field_name)
        self.env['account.move.line'].browse(
            authoritative_lines.ids,
        )._eh_check_access('write')
        authoritative_moves = (
            self | explicit_moves | authoritative_lines.move_id
        )
        if not set(authoritative_moves.ids).issubset(set(preliminary.ids)):
            raise UserError(_(
                "A journal-item parent changed concurrently. Retry the "
                "operation."
            ))
        return authoritative_moves

    @api.model
    def _eh_provenance_relation_ids(self, commands):
        """Resolve existing inverse-provenance commands without nested edits."""
        record_ids = set()
        for command in commands or ():
            if not isinstance(command, (tuple, list)) or not command:
                raise UserError(_(
                    "Provenance relations require explicit ORM commands."
                ))
            operation = command[0]
            if operation in (3, 4) and len(command) > 1 and command[1]:
                record_ids.add(command[1])
            elif operation == 6 and len(command) > 2:
                record_ids.update(command[2] or ())
            elif operation == 5:
                continue
            else:
                raise UserError(_(
                    "Reversal and cash-basis relations cannot create, edit, "
                    "or delete journal entries through nested commands."
                ))
        return record_ids

    @api.model
    def _eh_assert_batch_provenance_targets_mutable(self, vals_list):
        """Reject reversal/CABA pointers into protected batch evidence."""
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_reversal_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_reconcile_capability(self.env)
        ):
            return True
        move_fields = ('reversed_entry_id', 'tax_cash_basis_origin_move_id')
        reversal_field = self._eh_batch_reversal_inverse_field()
        inverse_move_fields = tuple(
            field_name for field_name in (
                reversal_field, 'tax_cash_basis_created_move_ids',
            )
            if field_name and field_name in self._fields
        )
        provenance_fields = (
            set(move_fields) | set(inverse_move_fields)
            | {'tax_cash_basis_rec_id'}
        )
        if not any(provenance_fields.intersection(vals) for vals in vals_list):
            return True
        explicit_move_ids = {
            vals.get(field_name)
            for vals in vals_list
            for field_name in move_fields
            if field_name in self._fields and vals.get(field_name)
        }
        explicit_inverse_ids = set()
        for vals in vals_list:
            for field_name in inverse_move_fields:
                if field_name in vals:
                    explicit_inverse_ids.update(
                        self._eh_provenance_relation_ids(vals[field_name]),
                    )
        explicit_partial_ids = {
            vals.get('tax_cash_basis_rec_id') for vals in vals_list
            if (
                'tax_cash_basis_rec_id' in self._fields
                and vals.get('tax_cash_basis_rec_id')
            )
        }
        move_ids = set(explicit_move_ids) | explicit_inverse_ids
        for field_name in move_fields:
            if field_name in self._fields:
                move_ids.update(self.sudo().mapped(field_name).ids)
        for field_name in inverse_move_fields:
            move_ids.update(self.sudo().mapped(field_name).ids)
        partial_ids = set(explicit_partial_ids)
        if 'tax_cash_basis_rec_id' in self._fields:
            partial_ids.update(
                self.sudo().mapped('tax_cash_basis_rec_id').ids,
            )
        direct_moves = self.env['account.move'].browse(
            sorted(move_ids),
        ).exists()
        partials = self.env['account.partial.reconcile'].browse(
            sorted(partial_ids),
        ).exists()
        if len(direct_moves) != len(move_ids) or len(partials) != len(partial_ids):
            raise UserError(_(
                "A reversal or cash-basis provenance target no longer exists."
            ))
        direct_moves._eh_check_access('write')
        partials._eh_check_access('read')
        partial_moves = (
            partials.debit_move_id.move_id
            | partials.credit_move_id.move_id
            | partials.exchange_move_id
        )
        self.env['account.move'].browse(
            partial_moves.ids,
        )._eh_check_access('write')
        # Lock current and prospective provenance moves once in global id
        # order. Otherwise reciprocal A->B / B->A writes can acquire target
        # then current advisories in opposite order.
        preliminary = self | direct_moves | partial_moves
        if not preliminary and not partials:
            return True
        Batch = self.env['eh.batch.payment']
        with self.env.cr.savepoint():
            preliminary._eh_lock_batch_payment_owners()
            Batch._eh_advisory_integrity_lock()
            self.invalidate_recordset([
                field_name for field_name in (
                    *move_fields, *inverse_move_fields,
                    'tax_cash_basis_rec_id',
                )
                if field_name in self._fields
            ])
            authoritative_move_ids = (
                set(explicit_move_ids) | explicit_inverse_ids
            )
            for field_name in (*move_fields, *inverse_move_fields):
                if field_name in self._fields:
                    authoritative_move_ids.update(
                        self.sudo().mapped(field_name).ids,
                    )
            authoritative_partial_ids = set(explicit_partial_ids)
            if 'tax_cash_basis_rec_id' in self._fields:
                authoritative_partial_ids.update(
                    self.sudo().mapped('tax_cash_basis_rec_id').ids,
                )
            if (
                not authoritative_move_ids.issubset(move_ids)
                or not authoritative_partial_ids.issubset(partial_ids)
            ):
                raise UserError(_(
                    "A reversal or cash-basis provenance target changed "
                    "concurrently. Retry the operation."
                ))
            if partials:
                self.env.cr.execute(
                    "SELECT id FROM account_partial_reconcile WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    (tuple(sorted(partials.ids)),),
                )
                partials.invalidate_recordset([
                    'debit_move_id', 'credit_move_id', 'exchange_move_id',
                ])
            authoritative_direct = self.env['account.move'].browse(
                sorted(authoritative_move_ids),
            )
            authoritative_partials = self.env[
                'account.partial.reconcile'
            ].browse(sorted(authoritative_partial_ids))
            authoritative = self | authoritative_direct \
                | authoritative_partials.debit_move_id.move_id \
                | authoritative_partials.credit_move_id.move_id \
                | authoritative_partials.exchange_move_id
            if not set(authoritative.ids).issubset(set(preliminary.ids)):
                raise UserError(_(
                    "A cash-basis provenance target changed concurrently. "
                    "Retry the operation."
                ))
            owner_field = self._eh_batch_payment_owner_field()
            if owner_field:
                authoritative.invalidate_recordset([owner_field])
            owners = authoritative._eh_batch_payment_owners().filtered(
                'eh_batch_payment_id',
            )
            sources = authoritative._eh_reviewed_batch_source_payments()
            if owners or sources:
                raise UserError(_(
                    "Reversal or cash-basis provenance cannot target a batch "
                    "payment cash move or reviewed source document."
                ))
        return True

    def write(self, vals):
        if (
            'origin_payment_id' not in self._fields
            and has_authorized_batch_member_mutation_capability(self.env)
        ):
            # Odoo 16/17 create and edit payments through a delegated move.
            # Their core synchronization already runs inside the payment
            # guard's flushing savepoint.  Opening another default savepoint
            # here flushes every transaction environment before core finishes
            # that move; a pending sequence recompute can then re-enter this
            # method without the process-local capability and reject the
            # legitimate sequence_prefix write.  Keep the exact capability
            # corridor inside its owning payment savepoint.  RPC/import callers
            # cannot forge the identity-checked token and still take the normal
            # guarded savepoint below.
            return self._eh_write_with_batch_guard(vals)
        if self._eh_write_needs_guard_savepoint(vals):
            with self.env.cr.savepoint():
                return self._eh_write_with_batch_guard(vals)
        return self._eh_write_with_batch_guard(vals)

    @api.model
    def _eh_write_needs_guard_savepoint(self, vals):
        """Keep flushing savepoints off ordinary descriptive move writes."""
        return bool(_BATCH_MOVE_FINANCIAL_FIELDS.intersection(vals))

    def _eh_write_with_batch_guard(self, vals):
        financial_write = bool(
            _BATCH_MOVE_FINANCIAL_FIELDS.intersection(vals)
        )
        secure_hash_write = (
            self._eh_has_secure_hash_capability()
            and bool(vals)
            and set(vals).issubset({
                'inalterable_hash', 'secure_sequence_number',
            })
        )
        source_metadata_correction = (
            financial_write
            and self._eh_is_posted_source_metadata_correction(vals)
        )
        affected_moves = (
            self
            | self._eh_prelock_combined_relation_scope([vals])
            | self._eh_lock_line_command_move_scope(vals)
        )
        self._eh_assert_batch_provenance_targets_mutable([vals])
        self._eh_assert_matched_payment_links_mutable(vals)
        self._eh_assert_batch_payment_owner_link_mutable(vals)
        if vals.get('state') == 'posted':
            # Defend direct RPC/import state writes as well as the public Post
            # action. The public action performs this check before core can
            # mutate dynamic lines; this second gate is the fail-closed seam.
            self._eh_assert_batch_payment_move_postable()
        if financial_write and not secure_hash_write:
            # Nested O2M commands may mutate an existing AML owned by a move
            # outside ``self``.  Guard the full canonical parent/target scope
            # before granting core the AML-only capability.
            affected_moves._eh_assert_batch_payment_financial_mutable()
            if not source_metadata_correction:
                affected_moves._eh_assert_batch_source_financial_mutable()
        elif 'state' in vals and vals.get('state') != 'posted':
            self._eh_assert_batch_payment_financial_mutable()
        if (
            bool({'origin_payment_id', 'payment_id'}.intersection(vals))
            or (
                'state' in vals
                and vals.get('state') != 'posted'
                and any(move.state == 'posted' for move in self)
            )
        ):
            self._eh_check_access('write')
            self._eh_assert_batch_source_lifecycle_mutable()
        if financial_write:
            core_self = _eh_core_aml_self(self)
            if source_metadata_correction:
                # Odoo 18+ marks ``invoice_date`` readonly once the move is
                # posted, even though changing it does not alter the journal
                # entry date or any debit/credit.  This context is attached
                # only after the narrow field whitelist, reviewed-source row
                # locks, and posted-batch state recheck above have all passed.
                # It cannot be supplied by RPC to bypass either batch seal.
                core_self = core_self.with_context(skip_readonly_check=True)
            result = super(
                AccountMove, core_self,
            ).write(vals)
            return _eh_clean_core_aml_result(result)
        return super().write(vals)

    def _reverse_moves(self, default_values_list=None, cancel=False):
        with self.env.cr.savepoint():
            self._eh_check_access('write')
            self._eh_prelock_post_reconcile_scope()
            if not has_authorized_batch_reversal_capability(self.env):
                self._eh_assert_batch_payment_financial_mutable()
                self._eh_assert_batch_source_financial_mutable()
            result = super(
                AccountMove, _eh_core_aml_self(self),
            )._reverse_moves(
                default_values_list=default_values_list,
                cancel=cancel,
            )
            return _eh_clean_core_aml_result(result)

    def action_post(self):
        # Check before core posting can mutate dynamic lines.  The later
        # write(state='posted') check is re-entrant under the same row locks.
        with self.env.cr.savepoint():
            self._eh_prelock_post_reconcile_scope()
            self._eh_assert_batch_payment_move_postable()
            return super().action_post()

    def _post(self, soft=True):
        # Core payment posting calls this private engine directly on every
        # supported series. Guard before date/sequence/analytic-line work so a
        # server caller which catches our UserError cannot commit a prefix.
        with self.env.cr.savepoint():
            self._eh_prelock_post_reconcile_scope()
            self._eh_assert_batch_payment_move_postable()
            self._eh_assert_batch_source_financial_mutable()
            result = super(
                AccountMove, _eh_core_aml_self(self),
            )._post(soft=soft)
            return _eh_clean_core_aml_result(result)

    def button_draft(self):
        with self.env.cr.savepoint():
            self._eh_check_access('write')
            self._eh_assert_batch_payment_financial_mutable()
            self._eh_assert_batch_source_lifecycle_mutable()
            result = super(
                AccountMove, _eh_core_aml_self(self),
            ).button_draft()
            return _eh_clean_core_aml_result(result)

    def unlink(self):
        with self.env.cr.savepoint():
            self._eh_check_access('unlink')
            self._eh_assert_batch_payment_financial_mutable()
            self._eh_assert_batch_source_financial_mutable()
            self._eh_assert_batch_source_lifecycle_mutable()
            result = super(
                AccountMove, _eh_core_aml_self(self),
            ).unlink()
            return _eh_clean_core_aml_result(result)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def _eh_batch_target_moves(self, vals_list):
        move_ids = {
            vals.get('move_id') for vals in vals_list
            if vals.get('move_id')
        }
        default_move_id = self.env.context.get('default_move_id')
        if default_move_id:
            move_ids.add(default_move_id)
        return self.env['account.move'].browse(sorted(move_ids)).exists()

    @api.model_create_multi
    def create(self, vals_list):
        if (
            any('full_reconcile_id' in vals for vals in vals_list)
            and not (
                has_authorized_batch_reconcile_capability(self.env)
                or has_core_reconcile_capability(self.env)
            )
        ):
            raise UserError(_(
                "Journal-item full-reconciliation links are core-owned and "
                "cannot be supplied directly."
            ))
        if has_core_aml_capability(self.env):
            lines = super().create(vals_list)
            return _eh_clean_core_aml_result(lines)
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_reconcile_capability(self.env)
        ):
            return super().create(vals_list)
        with self.env.cr.savepoint():
            target_moves = self._eh_batch_target_moves(vals_list)
            target_moves._eh_check_access('write')
            target_moves._eh_assert_batch_payment_financial_mutable(
                flush=False,
            )
            target_moves._eh_assert_batch_source_financial_mutable(
                flush=False,
            )
            lines = super(
                AccountMoveLine, _eh_core_aml_self(self),
            ).create(vals_list)
            return _eh_clean_core_aml_result(lines)

    def write(self, vals):
        if (
            'full_reconcile_id' in vals
            and not (
                has_authorized_batch_reconcile_capability(self.env)
                or has_core_reconcile_capability(self.env)
            )
        ):
            raise UserError(_(
                "Journal-item full-reconciliation links are core-owned and "
                "cannot be written directly."
            ))
        if (
            not _BATCH_LINE_FINANCIAL_FIELDS.intersection(vals)
            or has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_aml_capability(self.env)
            or has_core_reconcile_capability(self.env)
            or self.move_id._eh_has_secure_hash_capability()
            or self.move_id._eh_has_account_engine_capability()
        ):
            return super().write(vals)
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            moves = self.move_id | self._eh_batch_target_moves([vals])
            moves._eh_assert_batch_payment_financial_mutable(flush=False)
            moves._eh_assert_batch_source_financial_mutable(flush=False)
            result = super(
                AccountMoveLine, _eh_core_aml_self(self),
            ).write(vals)
            return _eh_clean_core_aml_result(result)

    def unlink(self):
        # Stock/account cancellation may ask every installed override to
        # unlink an empty filtered set (for example, no COGS journal items).
        # Preserve core's no-op before any model-ACL or graph guard: no batch
        # evidence exists to protect on an empty recordset.
        if not self:
            return super().unlink()
        if (
            has_authorized_payment_post_capability(self.env)
            or has_authorized_batch_reconcile_capability(self.env)
            or has_authorized_batch_member_mutation_capability(self.env)
            or has_core_aml_capability(self.env)
            or has_core_reconcile_capability(self.env)
        ):
            return super().unlink()
        self._eh_check_access('unlink')
        with self.env.cr.savepoint():
            self.move_id._eh_assert_batch_payment_financial_mutable(
                flush=False,
            )
            self.move_id._eh_assert_batch_source_financial_mutable(
                flush=False,
            )
            result = super(
                AccountMoveLine, _eh_core_aml_self(self),
            ).unlink()
            return _eh_clean_core_aml_result(result)
