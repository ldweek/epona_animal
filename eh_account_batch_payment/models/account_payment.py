# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.payment extension for batch membership.

A payment is a member of at most one batch. The pointer is nullable
so existing payments are unaffected; assignment happens via the batch
build wizard or by editing the payment directly. ondelete='set null'
keeps payments alive even if a draft batch is deleted.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .capabilities import (
    AUTHORIZED_BATCH_BULK_CREATE_CAPABILITY,
    AUTHORIZED_BATCH_BULK_CREATE_CONTEXT,
    AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
    AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT,
    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
    AUTHORIZED_BATCH_RECONCILE_CONTEXT,
    AUTHORIZED_DIRECT_PAYMENT_POST_CAPABILITY,
    AUTHORIZED_DIRECT_PAYMENT_POST_CONTEXT,
    has_authorized_batch_bulk_create_capability,
    has_authorized_payment_post_capability,
)


# Values that determine the legal entity, destination/source of funds,
# counterparty, amount, booking, or settlement evidence.  A confirmed batch
# is an approved financial instruction, so none may drift afterward through
# RPC/import/direct ORM calls.  Standard posting is invoked on a sudo()
# recordset by eh.batch.payment.action_post and is the sanctioned bypass.
_BATCH_PAYMENT_FINANCIAL_FIELDS = frozenset({
    'name', 'date', 'journal_id', 'company_id', 'state', 'move_id',
    'amount', 'currency_id', 'payment_type', 'partner_type', 'partner_id',
    'partner_bank_id', 'payment_method_line_id', 'memo', 'ref',
    'payment_reference', 'outstanding_account_id', 'destination_account_id',
    'is_reconciled', 'is_matched',
    'paired_internal_transfer_payment_id',
    'is_internal_transfer', 'destination_journal_id',
    'invoice_ids', 'eh_batch_payment_id', 'eh_source_move_ids',
})

_BATCH_SERVER_OWNED_PAYMENT_FIELDS = frozenset({
    'available_partner_bank_ids', 'require_partner_bank_account',
    'is_reconciled', 'is_matched',
})

# Draft edits to these values must leave the member internally consistent.
# A state-only change is deliberately excluded: a draft batch may retain a
# subsequently posted/reversed payment as historic evidence, but confirmation
# and posting always require every current member to be draft.
_BATCH_PAYMENT_INVARIANT_FIELDS = _BATCH_PAYMENT_FINANCIAL_FIELDS - {
    'state', 'move_id', 'paired_internal_transfer_payment_id',
}

_AUTHORIZED_BATCH_POST_FIELDS = frozenset({'state', 'move_id'})


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    eh_batch_payment_id = fields.Many2one(
        'eh.batch.payment',
        string="Batch",
        ondelete='set null',
        index=True,
        copy=False,
        help=(
            "Optional. When set, this payment is part of a batch run "
            "and is posted together with the batch's other members."
        ),
    )
    eh_batch_state = fields.Selection(
        related='eh_batch_payment_id.state',
        string="Batch state",
        readonly=True,
    )
    eh_source_move_ids = fields.Many2many(
        'account.move',
        'eh_batch_payment_source_move_rel',
        'payment_id', 'move_id',
        string="Settled documents",
        copy=False,
        help=(
            "Invoices/bills this batch payment settles. Reconciled against "
            "the payment when the batch is posted, so the source documents "
            "actually move to paid instead of leaving the payment and the "
            "invoices as separate open items."
        ),
    )

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        for field_name in _BATCH_SERVER_OWNED_PAYMENT_FIELDS:
            defaults.pop(field_name, None)
        return defaults

    def _eh_core_invoice_moves(self):
        """Return core invoice links when this Odoo series provides them."""
        if 'invoice_ids' not in self._fields:
            return self.env['account.move']
        return self.mapped('invoice_ids')

    @api.model
    def _eh_assert_batch_accepts_members(self, batch_id):
        """Reject assignment to anything except a draft batch.

        The inverse Many2one is the authoritative membership field.  Guarding
        it here covers direct payment edits as well as One2many commands sent
        through ``eh.batch.payment.payment_ids``.
        """
        if not batch_id:
            return
        batch = self.env['eh.batch.payment'].browse(batch_id).exists()
        if not batch:
            raise UserError(_("The target batch no longer exists."))
        batch._eh_check_access('write')
        if batch.state != 'draft':
            raise UserError(_(
                "Payments can only be added to a draft batch. Batch %(batch)s "
                "is %(state)s.",
                batch=batch.display_name,
                state=batch.state,
            ))

    @api.model
    def _eh_source_ids_from_commands(self, commands):
        """Return existing source ids touched by Many2many commands.

        Creating, deleting or mutating an invoice through a payment relation
        is never a legitimate batch operation.  Reject those command forms
        up front; linking/unlinking existing documents remains supported.
        """
        source_ids = set()
        for command in commands or ():
            if not isinstance(command, (tuple, list)) or not command:
                continue
            operation = command[0]
            if operation in (0, 1, 2):
                raise UserError(_(
                    "Batch payment sources must be existing documents; they "
                    "cannot be created, edited, or deleted through the "
                    "payment relation.",
                ))
            if operation in (3, 4) and len(command) > 1 and command[1]:
                source_ids.add(command[1])
            elif operation == 6 and len(command) > 2:
                source_ids.update(command[2] or ())
        return source_ids

    def _eh_lock_batch_mutation(self, target_batch_ids=(),
                                source_move_ids=(), payment_move_ids=()):
        # Rejected server callers may catch UserError. Keep the complete lock
        # acquisition/recheck unit rollback-owned so rejection releases every
        # advisory/row lock; successful RELEASE retains locks for mutation.
        with self.env.cr.savepoint():
            return self._eh_lock_batch_mutation_locked(
                target_batch_ids=target_batch_ids,
                source_move_ids=source_move_ids,
                payment_move_ids=payment_move_ids,
            )

    def _eh_lock_batch_mutation_locked(self, target_batch_ids=(),
                                       source_move_ids=(),
                                       payment_move_ids=()):
        """Lock current/target batches, their members, and all sources.

        Current membership is read directly from PostgreSQL after acquiring
        the module advisory lock.  This avoids trusting a record cache which
        may pre-date a concurrent, now-committed membership transfer.
        """
        Batch = self.env['eh.batch.payment']
        if self:
            self._eh_check_access('write')
        requested_batches = Batch.browse(
            sorted(set(target_batch_ids or ())),
        ).exists()
        if requested_batches:
            requested_batches._eh_check_access('write')
        requested_sources = self.env['account.move'].browse(
            sorted(set(source_move_ids or ())),
        )
        if requested_sources:
            requested_sources._eh_check_access('read')
            if len(requested_sources.exists()) != len(requested_sources):
                raise UserError(_(
                    "One or more selected source documents no longer exist.",
                ))
        requested_payment_moves = self.env['account.move'].browse(
            sorted(set(payment_move_ids or ())),
        )
        if requested_payment_moves:
            requested_payment_moves._eh_check_access('write')
            if len(requested_payment_moves.exists()) != len(
                requested_payment_moves
            ):
                raise UserError(_(
                    "One or more selected payment journal entries no longer "
                    "exist."
                ))

        # Discover only current batch ids here; the batch scope below owns the
        # complete lock order.  It expands every current/target batch to all
        # sibling payments and takes the globally sorted source/cash-move
        # advisory set before the module-wide claim lock.  Taking this
        # payment's move first would deadlock two sibling edits (move A ->
        # global -> move B versus move B -> global).
        self.flush_recordset()
        current_batch_ids = set()
        if self.ids:
            self.env.cr.execute(
                "SELECT eh_batch_payment_id FROM account_payment "
                "WHERE id IN %s ORDER BY id",
                (tuple(sorted(self.ids)),),
            )
            current_batch_ids.update(
                row[0] for row in self.env.cr.fetchall() if row[0]
            )
        all_batch_ids = current_batch_ids | set(target_batch_ids or ())
        batches = Batch.browse(sorted(all_batch_ids)).exists()
        if batches:
            batches._eh_check_access('write')
        locked = batches._eh_lock_integrity_scope(
            payment_ids=self.ids,
            source_move_ids=source_move_ids,
            payment_move_ids=payment_move_ids,
            include_batch_members=True,
            # With no current/target batch, per-payment and per-move advisories
            # make the authoritative membership re-read race-safe. Reserving
            # the module-wide phantom lock for an actual batch claim keeps
            # ordinary Register Payment and reconciliation independent.
            include_module_claim_lock=bool(all_batch_ids),
        )
        locked_batches, _locked_payments, locked_moves = locked
        if self.ids:
            self.env.cr.execute(
                "SELECT eh_batch_payment_id, move_id FROM account_payment "
                "WHERE id IN %s ORDER BY id",
                (tuple(sorted(self.ids)),),
            )
            authoritative_rows = self.env.cr.fetchall()
            authoritative_batch_ids = {
                row[0] for row in authoritative_rows if row[0]
            }
            authoritative_move_ids = {
                row[1] for row in authoritative_rows if row[1]
            }
            if not authoritative_batch_ids.issubset(set(locked_batches.ids)):
                raise UserError(_(
                    "A payment batch link changed concurrently. No batch "
                    "change was made; retry the operation."
                ))
            if not authoritative_move_ids.issubset(set(locked_moves.ids)):
                raise UserError(_(
                    "A payment journal-entry link changed concurrently. No "
                    "batch change was made; retry the operation."
                ))
            self.env.cr.execute(
                "SELECT move_id FROM eh_batch_payment_source_move_rel "
                "WHERE payment_id IN %s ORDER BY move_id",
                (tuple(sorted(self.ids)),),
            )
            authoritative_source_ids = {
                row[0] for row in self.env.cr.fetchall()
            }
            if 'invoice_ids' in self._fields:
                self.invalidate_recordset(['invoice_ids'])
                authoritative_source_ids.update(
                    self._eh_core_invoice_moves().ids
                )
            if not authoritative_source_ids.issubset(set(locked_moves.ids)):
                raise UserError(_(
                    "A payment source link changed concurrently. No batch "
                    "change was made; retry the operation."
                ))
        self.invalidate_recordset()
        return locked

    def _eh_assert_batch_membership_mutable(self):
        """Freeze the reviewed member set while confirmed or posted."""
        frozen = self.filtered(
            lambda payment: payment.eh_batch_payment_id.state
            in ('confirmed', 'posted'),
        )
        if frozen:
            batches = ', '.join(
                frozen.mapped('eh_batch_payment_id.display_name'),
            )
            raise UserError(_(
                "Batch membership is frozen after confirmation. Return the "
                "batch to draft before adding, removing, moving, or deleting "
                "member payments. Locked batch(es): %s",
                batches,
            ))
        currently_batched = self.filtered('eh_batch_payment_id')
        if currently_batched._eh_durable_owned_moves():
            raise UserError(_(
                "A payment carrying posted or quarantined cash evidence "
                "cannot be detached, moved to another batch, or deleted."
            ))

    def _eh_durable_owned_moves(self):
        """Return canonical/direct moves that must retain batch ownership."""
        if not self:
            return self.env['account.move']
        Move = self.env['account.move'].sudo()
        owner_field = Move._eh_batch_payment_owner_field()
        moves = self.sudo().mapped('move_id')
        if owner_field:
            moves |= Move.search([
                (owner_field, 'in', self.ids),
            ], order='id')
        return moves.filtered(
            lambda move: (
                move.state == 'posted'
                or move.posted_before
                or move.eh_sealed
                or move.eh_legacy_unverified_seal
            ),
        )

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_check_access('create')
        with self.env.cr.savepoint():
            return self._eh_create_with_batch_guard(vals_list)

    def _eh_create_with_batch_guard(self, vals_list):
        legacy_delegated_payment = (
            getattr(self, '_inherits', {}).get('account.move') == 'move_id'
        )
        supplied = {
            field_name
            for vals in vals_list
            for field_name in _BATCH_SERVER_OWNED_PAYMENT_FIELDS
            if field_name in vals
        }
        if supplied:
            raise UserError(_(
                "Payment bank-availability and reconciliation badges are "
                "server-owned and cannot be supplied on create."
            ))
        supplied_move_ids = {
            int(vals['move_id']) for vals in vals_list if vals.get('move_id')
        }
        if supplied_move_ids and not self.env.su:
            raise UserError(_(
                "A payment's canonical journal entry is created and linked "
                "by the accounting engine; only a trusted server workflow "
                "may supply move_id on payment create."
            ))
        target_batch_ids_by_row = [
            vals.get('eh_batch_payment_id') for vals in vals_list
        ]
        target_batch_ids = {
            batch_id for batch_id in target_batch_ids_by_row if batch_id
        }
        if legacy_delegated_payment and any(
            batch_id and 'name' in vals
            for vals, batch_id in zip(vals_list, target_batch_ids_by_row)
        ):
            raise UserError(_(
                "A legacy batch payment's delegated journal-entry number is "
                "assigned by the accounting engine and cannot be supplied on "
                "create."
            ))
        # Core company-paid expenses create their journal entries first, then
        # sudo-create account.payment with ``move_id``. Preserve that trusted
        # corridor while still rejecting every ordinary RPC attempt above.
        target_payment_move_ids = set(supplied_move_ids)
        source_move_ids = set()
        for vals in vals_list:
            source_move_ids.update(self._eh_source_ids_from_commands(
                vals.get('eh_source_move_ids'),
            ))
            source_move_ids.update(self._eh_source_ids_from_commands(
                vals.get('invoice_ids'),
            ))
        if target_batch_ids:
            self._eh_lock_batch_mutation(
                target_batch_ids=target_batch_ids,
                source_move_ids=source_move_ids,
                payment_move_ids=target_payment_move_ids,
            )
        for vals in vals_list:
            self._eh_assert_batch_accepts_members(
                vals.get('eh_batch_payment_id'),
            )
        create_self = self
        if target_batch_ids:
            create_self = self.with_context(**{
                AUTHORIZED_BATCH_BULK_CREATE_CONTEXT:
                    AUTHORIZED_BATCH_BULK_CREATE_CAPABILITY,
                AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT:
                    AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
            })
        create_vals_list = vals_list
        if legacy_delegated_payment and target_batch_ids:
            # Odoo 16/17 create a payment and its delegated move together, then
            # queue the move-name/sequence split.  Keep that incomplete move
            # unbatched until the capable core create has flushed its exact
            # name projection; any third-party savepoint may otherwise flush
            # the queued sequence write through an ordinary environment and
            # mistake it for an RPC edit of a workflow-owned batch member.
            create_vals_list = []
            for vals in vals_list:
                create_vals = dict(vals)
                create_vals.pop('eh_batch_payment_id', None)
                create_vals_list.append(create_vals)
        payments = super(AccountPayment, create_self).create(create_vals_list)
        if legacy_delegated_payment and target_batch_ids:
            payments.move_id.flush_recordset([
                'name', 'sequence_prefix', 'sequence_number',
            ])
            for batch_id in sorted(target_batch_ids):
                member_ids = [
                    payment.id
                    for payment, target_id in zip(
                        payments, target_batch_ids_by_row,
                    )
                    if target_id == batch_id
                ]
                capable_members = payments.browse(member_ids).with_context(**{
                    AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT:
                        AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
                })
                super(AccountPayment, capable_members).write({
                    'eh_batch_payment_id': batch_id,
                })
            payments.invalidate_recordset(['eh_batch_payment_id'])
        assigned = payments.filtered('eh_batch_payment_id')
        if assigned:
            # New rows did not exist at the pre-create lock point. Lock
            # them now before evaluating duplicate claims/invariants.
            assigned._eh_lock_batch_mutation(
                target_batch_ids=assigned.eh_batch_payment_id.ids,
                source_move_ids=(
                    assigned.eh_source_move_ids
                    | assigned._eh_core_invoice_moves()
                ).ids,
            )
            assigned.eh_batch_payment_id._eh_validate_integrity(
                require_draft=True,
            )
        # Never return process-local mutation capability to caller. Core create
        # needed it only while synchronising a draft delegated move.
        return self.browse(payments.ids)

    def _eh_has_authorized_batch_post_capability(self):
        """Return whether this is an exact locked payment-post corridor."""
        return has_authorized_payment_post_capability(self.env)

    def _eh_lock_direct_payment_post(self):
        """Lock only these payments, then authoritatively reject membership."""
        Batch = self.env['eh.batch.payment']
        Move = self.env['account.move'].sudo()
        owner_field = Move._eh_batch_payment_owner_field()
        preliminary_direct = Move.search([
            (owner_field, 'in', self.ids),
        ], order='id')
        preliminary_move_ids = set(preliminary_direct.ids)
        if self.ids:
            self.env.cr.execute(
                "SELECT move_id FROM account_payment WHERE id IN %s "
                "AND move_id IS NOT NULL ORDER BY move_id",
                (tuple(sorted(self.ids)),),
            )
            preliminary_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
        Batch._eh_advisory_move_owner_lock(preliminary_move_ids)
        if self.ids:
            self.env.cr.execute(
                "SELECT id, move_id, eh_batch_payment_id FROM account_payment "
                "WHERE id IN %s ORDER BY id FOR UPDATE",
                (tuple(sorted(self.ids)),),
            )
            rows = self.env.cr.fetchall()
        else:
            rows = []
        if len(rows) != len(self.ids):
            raise UserError(_("One or more payments no longer exist."))
        authoritative_move_ids = {row[1] for row in rows if row[1]}
        if not authoritative_move_ids.issubset(preliminary_move_ids):
            raise UserError(_(
                "A payment journal-entry link changed concurrently. Retry "
                "the posting operation."
            ))
        self.invalidate_recordset(['eh_batch_payment_id', 'move_id'])
        if any(row[2] for row in rows):
            raise UserError(_(
                "A batch member can only be posted through the batch Post "
                "workflow. Remove it from a draft batch before posting it "
                "separately."
            ))
        authoritative_direct = Move.search([
            (owner_field, 'in', self.ids),
        ], order='id')
        if not set(authoritative_direct.ids).issubset(preliminary_move_ids):
            raise UserError(_(
                "A direct payment journal-entry owner changed concurrently. "
                "Retry the posting operation."
            ))
        Payment = self.env['account.payment'].sudo()
        inverse_owners = Payment.search([
            ('move_id', 'in', sorted(authoritative_move_ids)),
        ], order='id') if authoritative_move_ids else Payment
        rows_by_payment = {row[0]: row[1] for row in rows if row[1]}
        for payment in self:
            move_id = rows_by_payment.get(payment.id)
            direct = authoritative_direct.filtered(
                lambda move: move[owner_field] == payment,
            )
            if not move_id:
                if direct:
                    raise UserError(_(
                        "An unposted payment cannot own a shadow journal "
                        "entry before its canonical posting move is created."
                    ))
                continue
            move = Move.browse(move_id).exists()
            owners = inverse_owners.filtered(
                lambda candidate: candidate.move_id.id == move_id,
            )
            if (
                owners != payment.sudo()
                or move[owner_field] != payment
                or direct != move
            ):
                raise UserError(_(
                    "A payment can post only its uniquely owned canonical "
                    "journal entry. Detached or shared move links must be "
                    "repaired first."
                ))
        return self.with_context(**{
            AUTHORIZED_DIRECT_PAYMENT_POST_CONTEXT:
                AUTHORIZED_DIRECT_PAYMENT_POST_CAPABILITY,
        })

    def action_post(self):
        if self._eh_has_authorized_batch_post_capability():
            return super().action_post()
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            capable = self._eh_lock_direct_payment_post()
            return super(AccountPayment, capable).action_post()

    def _generate_journal_entry(self, write_off_line_vals=None,
                                force_balance=None, line_ids=None):
        """Let modern core generate only a freshly locked canonical move."""
        parent = super()
        generate = getattr(parent, '_generate_journal_entry', None)
        if not generate:
            raise UserError(_(
                "This Odoo series does not generate payment journal entries "
                "through the modern payment API."
            ))
        if self._eh_has_authorized_batch_post_capability():
            return generate(
                write_off_line_vals=write_off_line_vals,
                force_balance=force_balance,
                line_ids=line_ids,
            )
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            capable = self._eh_lock_direct_payment_post()
            return super(AccountPayment, capable)._generate_journal_entry(
                write_off_line_vals=write_off_line_vals,
                force_balance=force_balance,
                line_ids=line_ids,
            )

    def write(self, vals):
        if _BATCH_SERVER_OWNED_PAYMENT_FIELDS.intersection(vals):
            raise UserError(_(
                "Payment bank-availability and reconciliation badges are "
                "server-owned and cannot be written directly."
            ))
        relevant = _BATCH_PAYMENT_FINANCIAL_FIELDS.intersection(vals)
        if not relevant:
            return super().write(vals)
        if has_authorized_batch_bulk_create_capability(self.env):
            # Odoo 18/19 completes fields such as outstanding_account_id by
            # writing each freshly-created payment in turn. The surrounding
            # model-level create owns all batch/source locks and performs one
            # authoritative validation after every new row is complete.
            return super().write(vals)
        if (
            self._eh_has_authorized_batch_post_capability()
            and set(vals).issubset(_AUTHORIZED_BATCH_POST_FIELDS)
        ):
            return super().write(vals)
        self._eh_check_access('write')
        if 'move_id' in vals:
            raise UserError(_(
                "A payment's canonical journal-entry link is workflow-owned "
                "and cannot be written directly."
            ))
        # Do not serialize ordinary, unbatched account.payment edits behind
        # the module-wide claim lock.  Re-read membership before the write;
        # if an assignment wins concurrently, PostgreSQL serializes the two
        # updates on the payment row and the post-write re-read either freezes
        # or validates our change.  Conversely, if this write wins, the later
        # assignment path validates the values we committed.  Many2many
        # source commands are excluded from this fast path because they need
        # not update/lock the account_payment row itself.
        changes_relation = bool({
            'eh_batch_payment_id', 'eh_source_move_ids', 'invoice_ids',
        }.intersection(vals))
        self.invalidate_recordset(['eh_batch_payment_id'])
        changes_payment_state = 'state' in vals
        changes_payment_move = 'move_id' in vals
        if (
            changes_payment_state
            and not changes_payment_move
            and not changes_relation
        ):
            with self.env.cr.savepoint():
                capable = self._eh_lock_direct_payment_post()
                return super(AccountPayment, capable).write(vals)
        if (
            not changes_relation
            and not changes_payment_state
            and not changes_payment_move
            and not self.eh_batch_payment_id
        ):
            with self.env.cr.savepoint():
                capable = self.with_context(**{
                    AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT:
                        AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
                })
                result = super(AccountPayment, capable).write(vals)
                self.invalidate_recordset(['eh_batch_payment_id'])
                concurrent_batches = self.eh_batch_payment_id
                if concurrent_batches:
                    frozen = self.filtered(
                        lambda payment: payment.eh_batch_payment_id.state
                        in ('confirmed', 'posted'),
                    )
                    if frozen:
                        raise UserError(_(
                            "Financial payment fields are frozen after batch "
                            "confirmation. Payment(s): %s.",
                            ', '.join(frozen.mapped('display_name')),
                        ))
                    if _BATCH_PAYMENT_INVARIANT_FIELDS.intersection(vals):
                        concurrent_batches._eh_validate_integrity()
            return result
        with self.env.cr.savepoint():
            return self._eh_write_batch_financial_locked(vals, relevant)

    def _eh_write_batch_financial_locked(self, vals, relevant):
        target_batch_id = vals.get('eh_batch_payment_id')
        target_batch_ids = [target_batch_id] if target_batch_id else []
        source_move_ids = self._eh_source_ids_from_commands(
            vals.get('eh_source_move_ids'),
        )
        source_move_ids.update(self._eh_source_ids_from_commands(
            vals.get('invoice_ids'),
        ))
        current_batches, _payments, _moves = self._eh_lock_batch_mutation(
            target_batch_ids=target_batch_ids,
            source_move_ids=source_move_ids,
            payment_move_ids=(
                [vals['move_id']] if vals.get('move_id') else []
            ),
        )
        if current_batches:
            self._eh_assert_batch_accepts_members(
                target_batch_id,
            )
        if 'eh_batch_payment_id' in vals:
            self._eh_assert_batch_membership_mutable()
        if {'state', 'move_id'}.intersection(vals) and self.filtered(
            'eh_batch_payment_id'
        ):
            raise UserError(_(
                "A batch member's payment state and journal entry are "
                "controlled exclusively by the batch Confirm/Post workflow. "
                "Remove it from a draft batch before posting it separately."
            ))
        frozen = self.filtered(
            lambda payment: payment.eh_batch_payment_id.state
            in ('confirmed', 'posted'),
        )
        if frozen:
            raise UserError(_(
                "Financial payment fields are frozen after batch "
                "confirmation. Return the batch to draft before changing "
                "%(fields)s. Payment(s): %(payments)s.",
                fields=', '.join(sorted(relevant)),
                payments=', '.join(frozen.mapped('display_name')),
            ))
        # The target check is needed for an unbatched payment, where
        # current_batches is empty.
        if 'eh_batch_payment_id' in vals and target_batch_id:
            self._eh_assert_batch_accepts_members(target_batch_id)
        capable = self.with_context(**{
            AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT:
                AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
        })
        result = super(AccountPayment, capable).write(vals)
        final_batches = self.eh_batch_payment_id
        if 'eh_batch_payment_id' in vals and target_batch_id:
            final_batches._eh_validate_integrity(require_draft=True)
        elif _BATCH_PAYMENT_INVARIANT_FIELDS.intersection(vals):
            final_batches.filtered(
                lambda batch: batch.state == 'draft',
            )._eh_validate_integrity()
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        with self.env.cr.savepoint():
            self._eh_lock_batch_mutation()
            self.invalidate_recordset(['eh_batch_payment_id'])
            self._eh_assert_batch_membership_mutable()
            capable = self.with_context(**{
                AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT:
                    AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY,
            })
            return super(AccountPayment, capable).unlink()

    def _eh_reconcile_sources(self):
        """Reconcile each posted payment against its source documents.

        A batch payment that is merely posted leaves both the payment and its
        source invoices/bills open on the partner ledger: the whole point of
        the batch (clear these documents) never happens. After posting, match
        the payment's receivable/payable leg against the open receivable/
        payable lines of the documents it settles so those documents move to
        paid. Idempotent: already-reconciled lines are skipped.
        """
        for payment in self:
            moves = payment.eh_source_move_ids
            if not moves or not payment.move_id:
                continue
            pay_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type in (
                    'asset_receivable', 'liability_payable')
                and l.account_id.reconcile and not l.reconciled)
            inv_lines = moves.line_ids.filtered(
                lambda l: l.account_id.account_type in (
                    'asset_receivable', 'liability_payable')
                and l.account_id.reconcile and not l.reconciled
                and (
                    not l.company_currency_id.is_zero(l.amount_residual)
                    or not l.currency_id.is_zero(
                        l.amount_residual_currency,
                    )
                ))
            # Reconcile per shared account: the framework matches the single
            # payment leg across every invoice leg on the same AR/AP account.
            for account in pay_lines.mapped('account_id'):
                group = (pay_lines + inv_lines).filtered(
                    lambda l: l.account_id == account and not l.reconciled)
                if len(group) >= 2:
                    group.sudo().with_context(**{
                        AUTHORIZED_BATCH_RECONCILE_CONTEXT:
                            AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
                    }).reconcile()

    def _eh_directional_source_residual(self):
        """Return the currently open source total in this payment's direction."""
        self.ensure_one()
        residual = 0.0
        for source in self.eh_source_move_ids.sudo():
            amount = source.amount_residual
            if source.move_type in ('out_refund', 'in_refund'):
                amount = -amount
            residual += amount
        return residual

    def _eh_expected_outstanding_account(self):
        """Return the only configured liquidity/outstanding account."""
        self.ensure_one()
        method_account = self.payment_method_line_id.payment_account_id
        if method_account:
            return method_account
        get_outstanding_account = getattr(
            self, '_get_outstanding_account', None,
        )
        if get_outstanding_account:
            return get_outstanding_account(self.payment_type)
        company = self.journal_id.company_id or self.company_id
        legacy_field = (
            'account_journal_payment_debit_account_id'
            if self.payment_type == 'inbound'
            else 'account_journal_payment_credit_account_id'
        )
        if legacy_field in company._fields:
            return company[legacy_field]
        if 'outstanding_account_id' in self._fields:
            return self.outstanding_account_id
        raise UserError(_(
            "Payment %(payment)s has no supported core outstanding-account "
            "resolver.",
            payment=self.display_name,
        ))

    def _eh_expected_destination_account(self):
        """Return the authoritative AR/AP account for this instruction.

        A sourced payment follows the still-open source journal items, not a
        partner property which may have changed since invoice posting. One
        payment cannot reconcile several AR/AP accounts atomically.
        """
        self.ensure_one()
        sources = self.eh_source_move_ids.sudo()
        if sources:
            source_lines = sources.line_ids.filtered(
                lambda line: (
                    line.account_id.account_type in (
                        'asset_receivable', 'liability_payable',
                    )
                    and not line.reconciled
                    and (
                        not line.company_currency_id.is_zero(
                            line.amount_residual,
                        )
                        or not line.currency_id.is_zero(
                            line.amount_residual_currency,
                        )
                    )
                ),
            )
            accounts = source_lines.mapped('account_id')
            if len(accounts) != 1:
                raise UserError(_(
                    "Payment %(payment)s sources must have exactly one open "
                    "receivable/payable account; split sources by account.",
                    payment=self.display_name,
                ))
            return accounts
        partner = self.partner_id.with_company(self.company_id)
        return (
            partner.property_account_receivable_id
            if self.partner_type == 'customer'
            else partner.property_account_payable_id
        )

    def _eh_assert_payment_move_semantics(self, expected_state):
        """Prove exact account, currency, company-balance, and partner legs."""
        def _has_coherent_draft_name(payment, move):
            if move.name in (False, '/'):
                return True
            if (
                getattr(payment, '_inherits', {}).get('account.move')
                != 'move_id'
            ):
                return False
            prefix = move.sequence_prefix or ''
            suffix = move.name[len(prefix):] if move.name.startswith(prefix) \
                else ''
            return (
                bool(prefix)
                and move.sequence_number > 0
                and suffix.isdigit()
                and int(suffix) == move.sequence_number
                and move._sequence_matches_date()
            )

        def _has_forbidden_payment_payload(line):
            relation_fields = (
                'tax_ids', 'tax_tag_ids', 'analytic_distribution',
                'product_id', 'product_uom_id', 'tax_repartition_line_id',
                'tax_line_id', 'group_tax_id', 'tax_group_id',
                'extra_tax_data', 'statement_line_id', 'statement_id',
                'full_reconcile_id', 'matching_number',
                'reconcile_model_id', 'discount_date', 'is_imported',
            )
            numeric_fields = (
                'tax_base_amount', 'discount_amount_currency',
                'discount_balance',
            )
            return any(
                field_name in line._fields and bool(line[field_name])
                for field_name in relation_fields
            ) or any(
                field_name in line._fields
                and not line.company_currency_id.is_zero(
                    line[field_name] or 0.0,
                )
                for field_name in numeric_fields
            )

        for payment in self:
            move = payment.move_id
            liquidity, counterpart, writeoff = payment._seek_for_lines()
            expected_outstanding = payment._eh_expected_outstanding_account()
            expected_destination = payment._eh_expected_destination_account()
            expected_ref = (
                payment.memo
                if 'memo' in payment._fields
                else payment.ref
            )
            signed_amount = (
                payment.amount
                if payment.payment_type == 'inbound'
                else -payment.amount
            )
            company_currency = payment.company_id.currency_id
            expected_balance = payment.currency_id._convert(
                signed_amount,
                company_currency,
                payment.company_id,
                payment.date,
            )
            liquidity_amount = sum(liquidity.mapped('amount_currency'))
            counterpart_amount = sum(counterpart.mapped('amount_currency'))
            liquidity_balance = sum(liquidity.mapped('balance'))
            counterpart_balance = sum(counterpart.mapped('balance'))
            payment_lines = liquidity | counterpart
            if (
                not move
                or move.state != expected_state
                or move.move_type != 'entry'
                or move.company_id != payment.company_id
                or move.journal_id != payment.journal_id
                or move.currency_id != payment.currency_id
                or move.date != payment.date
                or move.partner_id != payment.partner_id
                or move.partner_bank_id != payment.partner_bank_id
                or move.ref != expected_ref
                or move.statement_line_id
                or move.tax_cash_basis_rec_id
                or move.tax_cash_basis_origin_move_id
                or move.reversed_entry_id
                or move.auto_post != 'no'
                or (
                    expected_state == 'draft'
                    and (
                        move.posted_before
                        or not _has_coherent_draft_name(payment, move)
                    )
                )
                or len(liquidity) != 1
                or len(counterpart) != 1
                or writeoff
                or set(liquidity.account_id.ids)
                != {expected_outstanding.id}
                or set(counterpart.account_id.ids)
                != {expected_destination.id}
                or any(
                    line.company_id != payment.company_id
                    or line.company_currency_id != company_currency
                    or line.journal_id != payment.journal_id
                    or line.date != payment.date
                    or line.date_maturity != payment.date
                    or line.currency_id != payment.currency_id
                    or line.partner_id != payment.partner_id
                    or (
                        'payment_id' in line._fields
                        and line.payment_id != payment
                    )
                    or (
                        'move_name' in line._fields
                        and line.move_name != move.name
                    )
                    or (
                        'invoice_date' in line._fields
                        and line.invoice_date != move.invoice_date
                    )
                    or line.ref != move.ref
                    or line.display_type != 'product'
                    or line.quantity != 1.0
                    or not company_currency.is_zero(line.price_unit or 0.0)
                    or not company_currency.is_zero(line.discount or 0.0)
                    or (
                        'parent_state' in line._fields
                        and line.parent_state != expected_state
                    )
                    or _has_forbidden_payment_payload(line)
                    or not company_currency.is_zero(
                        line.debit - line.credit - line.balance,
                    )
                    or line.reconciled
                    or not company_currency.is_zero(
                        line.amount_residual - line.balance,
                    )
                    or not payment.currency_id.is_zero(
                        line.amount_residual_currency - line.amount_currency,
                    )
                    for line in payment_lines
                )
                or not payment.currency_id.is_zero(
                    liquidity_amount - signed_amount,
                )
                or not payment.currency_id.is_zero(
                    counterpart_amount + signed_amount,
                )
                or not company_currency.is_zero(
                    liquidity_balance - expected_balance,
                )
                or not company_currency.is_zero(
                    counterpart_balance + expected_balance,
                )
            ):
                raise UserError(_(
                    "Payment %(payment)s journal entry does not exactly match "
                    "its reviewed owner, partner, accounts, currency amount, "
                    "and company-currency balance.",
                    payment=payment.display_name,
                ))
        return True

    def _eh_assert_draft_move_semantics(self):
        """Validate the pre-created delegated move on Odoo 16/17."""
        return self._eh_assert_payment_move_semantics('draft')

    def _eh_assert_posted_move_semantics(self):
        """Validate the final cash movement before sealing batch evidence."""
        return self._eh_assert_payment_move_semantics('posted')

    def _eh_assert_sources_settled(self, expected_source_remainders=None):
        """Prove the full reviewed payment was applied to its source set.

        A deliberately partial payment may leave a positive source remainder;
        the payment counterpart must still be fully reconciled and the exact
        directional remainder must equal pre-post residual less payment amount.
        """
        expected_source_remainders = expected_source_remainders or {}
        for payment in self:
            sources = payment.eh_source_move_ids.sudo()
            if not sources:
                continue
            sources.invalidate_recordset([
                'amount_residual', 'payment_state',
            ])
            counterpart = payment.move_id.line_ids.filtered(
                lambda line: line.account_id
                == payment.destination_account_id,
            )
            counterpart.invalidate_recordset([
                'amount_residual', 'amount_residual_currency', 'reconciled',
            ])
            remaining = payment._eh_directional_source_residual()
            expected = expected_source_remainders.get(payment.id, 0.0)
            wrong_remainder = payment.currency_id.compare_amounts(
                remaining, expected,
            ) != 0
            if wrong_remainder or not counterpart or any(
                not line.reconciled for line in counterpart
            ):
                raise UserError(_(
                    "Payment %(payment)s did not apply its full reviewed "
                    "amount to the approved source documents. Batch posting "
                    "was rolled back.",
                    payment=payment.display_name,
                ))
        return True


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def _create_payments(self):
        """Reject reviewed sources before core creates/posts a payment."""
        self.ensure_one()
        lines = self.line_ids
        lines._eh_check_access('write')
        moves = lines.move_id
        moves._eh_check_access('write')
        Batch = self.env['eh.batch.payment']
        with self.env.cr.savepoint():
            # Core may later reconcile an already-partially-settled source.
            # Discover its complete existing graph before taking any advisory
            # so an older counterpart move is locked before this source in the
            # same canonical sorted pass used by batch confirmation.
            (
                preliminary_graph_line_ids,
                preliminary_graph_partial_ids,
                preliminary_graph_move_ids,
            ) = Batch._eh_source_reconcile_graph_ids(moves.ids)
            graph_moves = self.env['account.move'].browse(
                sorted(preliminary_graph_move_ids),
            ).exists()
            if len(graph_moves) != len(preliminary_graph_move_ids):
                raise UserError(_(
                    "A selected source reconciliation entry no longer exists."
                ))
            # Supporting reconciliation-graph rows are locked only to close a
            # race; they are not mutated by this preflight. Requiring actor
            # write access here made stock Register Payment stricter than core
            # whenever a selected invoice already had an older partial match.
            Batch._eh_advisory_move_owner_lock(
                set(moves.ids) | preliminary_graph_move_ids,
            )
            (
                authoritative_graph_line_ids,
                authoritative_graph_partial_ids,
                authoritative_graph_move_ids,
            ) = Batch._eh_source_reconcile_graph_ids(moves.ids)
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
                    "A selected source reconciliation graph changed "
                    "concurrently. Retry the operation."
                ))
            # Holding the source graph's move advisories through core create,
            # post, and reconcile prevents a reviewed claim appearing after
            # this preflight and keeps caught downstream errors atomic without
            # serialising unrelated reconciliation elsewhere in the database.
            moves.invalidate_recordset()
            reviewed = moves._eh_reviewed_batch_source_payments()
            if reviewed:
                if moves._eh_reviewed_batch_source_payments():
                    raise UserError(_(
                        "A confirmed or posted batch already owns one or more "
                        "selected source documents. Cancel and reset that "
                        "batch before registering another payment."
                    ))
            return super()._create_payments()
