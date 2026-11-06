# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.elimination: an intercompany elimination journal entry.

A consolidation pulls every member's gross balance, but transactions
between members appear twice (once on each side) and must be
eliminated to produce a single-entity-equivalent set. This model
holds those elimination entries: a free-form list of debit / credit
lines against the consolidated chart, with an audit trail per
elimination so the reviewer can see exactly which IC transactions
were eliminated and by whom.

Lifecycle: draft -> posted (counts toward the run's totals) ->
cancelled.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero


_BALANCE_ABS_TOLERANCE = 1e-9


class EhConsolElimination(models.Model):
    _name = 'eh.consol.elimination'
    _description = "Consolidation elimination entry"
    _order = 'run_id, sequence, id'
    _inherit = [
        'mail.thread', 'eh.workflow.guard',
        'eh.consol.company.scope.mixin',
    ]

    # State advances only through the record's own actions (post / cancel /
    # reset), which run under sudo. A direct non-superuser write to 'state' is
    # refused by eh.workflow.guard, so a plain user cannot RPC-write
    # {'state': 'posted'} past action_post and its balance check.
    _eh_guarded_fields = ('state', 'posted_at', 'posted_by_id')

    _EH_FROZEN_FIELDS = (
        'run_id', 'sequence', 'name', 'description', 'line_ids',
        'posted_at', 'posted_by_id',
    )

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        required=True, copy=False, default='/',
        help=(
            "Display name for the elimination, e.g. 'IC sales: "
            "AU sub vs UK sub' or 'IC loan principal'."
        ),
    )
    description = fields.Text(
        help=(
            "Document why this elimination exists, what underlying "
            "IC transactions it covers, and any cross-reference to "
            "the original journal entries."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.consol.elimination.line', 'elimination_id', copy=True,
        help=(
            "Per-account debit / credit lines. Total debit must "
            "equal total credit for the elimination to post."
        ),
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        run_ids = {vals.get('run_id') for vals in vals_list if vals.get('run_id')}
        for vals in vals_list:
            run = self.env['eh.consol.run'].browse(vals.get('run_id'))
            if run:
                run._eh_assert_company_closure()
                self._eh_assert_parent_runs_mutable(run)
        self._eh_lock_for_transition(extra_run_ids=run_ids)
        for vals in vals_list:
            run = self.env['eh.consol.run'].browse(vals.get('run_id'))
            if run:
                run._eh_assert_company_closure()
                run._eh_validate_reference_closure()
                self._eh_assert_parent_runs_mutable(run)
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code(
                        'eh.consol.elimination',
                    ) or '/'
                )
        eliminations = super().create(vals_list)
        eliminations.mapped('run_id')._eh_assert_company_closure()
        return eliminations

    def _eh_assert_company_closure(self):
        if not self.env.su:
            self._eh_check_access('write')
            self.mapped('run_id')._eh_assert_company_closure()
        return self

    @api.model
    def _eh_assert_parent_runs_mutable(self, runs):
        frozen = runs.filtered(lambda run: (
            run.state != 'draft'
            or run.engine_provenance_version
            or run.line_ids
            or run.move_id
            or run.reversal_move_id
            or run.move_history_ids
            or run.legacy_evidence_quarantined
        ))
        if frozen:
            raise UserError(_(
                "Elimination inputs and lifecycle are frozen once their "
                "parent run has computed figures or GL history. Reset a "
                "clean run to draft before correcting eliminations."
            ))
        return runs

    def _eh_lock_for_transition(self, extra_run_ids=()):
        runs = self.mapped('run_id') | self.env['eh.consol.run'].browse(
            [run_id for run_id in extra_run_ids if run_id])
        runs._eh_lock_for_transition()
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id, run_id FROM eh_consol_elimination WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A consolidation elimination changed concurrently. Retry "
                    "the operation."
                ))
            if not {row[1] for row in locked_rows}.issubset(set(runs.ids)):
                raise UserError(_(
                    "A consolidation elimination changed run concurrently. "
                    "Retry the operation."
                ))
            self.invalidate_recordset()
        return self

    def _eh_prepare_transition(self):
        caller = self
        caller._eh_assert_company_closure()
        caller.mapped('run_id')._eh_validate_reference_closure()
        caller._eh_lock_for_transition()
        caller._eh_assert_parent_runs_mutable(caller.mapped('run_id'))
        caller._eh_restamp_line_outputs()
        caller._eh_assert_company_closure()
        caller.mapped('run_id')._eh_validate_reference_closure()
        return caller._eh_workflow_action()

    def _eh_restamp_line_outputs(self):
        """Rebuild stored line outputs from locked source columns."""
        self._eh_lock_for_transition()
        elimination_ids = tuple(sorted(self.ids))
        if not elimination_ids:
            return self
        self.env['eh.consol.elimination.line'].flush_model([
            'elimination_id', 'debit', 'credit',
        ])
        self.env['eh.consol.run'].flush_model(['presentation_currency_id'])
        self.env.cr.execute(
            """
            UPDATE eh_consol_elimination_line AS line
               SET amount = COALESCE(line.debit, 0.0)
                            - COALESCE(line.credit, 0.0),
                   presentation_currency_id = run.presentation_currency_id
              FROM eh_consol_elimination AS elimination
              JOIN eh_consol_run AS run ON run.id = elimination.run_id
             WHERE line.elimination_id = elimination.id
               AND elimination.id IN %s
            """,
            (elimination_ids,),
        )
        self.mapped('line_ids').invalidate_recordset([
            'amount', 'presentation_currency_id',
        ])
        return self

    def write(self, vals):
        target = self.env['eh.consol.run'].browse(vals.get('run_id'))
        if target and not self.env.su:
            target._eh_assert_company_closure()
            target._eh_validate_reference_closure()
        self._eh_assert_company_closure()
        self._eh_lock_for_transition(extra_run_ids=(vals.get('run_id'),))
        self._eh_assert_parent_runs_mutable(
            self.mapped('run_id') | target,
        )
        self._eh_assert_company_closure()
        target = self.env['eh.consol.run'].browse(vals.get('run_id'))
        if target:
            target._eh_assert_company_closure()
            target._eh_validate_reference_closure()
        if set(vals) & set(self._EH_FROZEN_FIELDS):
            settled = self.filtered(lambda elimination: elimination.state != 'draft')
            if settled:
                raise UserError(_(
                    "A posted or cancelled consolidation elimination is "
                    "immutable. Reset it through the manager workflow before "
                    "changing its header or lines."
                ))
        return super().write(vals)

    def unlink(self):
        self._eh_assert_company_closure()
        self._eh_lock_for_transition()
        self._eh_assert_parent_runs_mutable(self.mapped('run_id'))
        self._eh_assert_company_closure()
        if self.filtered(lambda elimination: elimination.state != 'draft'):
            raise UserError(_(
                "Posted or cancelled consolidation eliminations are retained "
                "as audit evidence and cannot be deleted."
            ))
        return super().unlink()

    def action_post(self):
        with self.env.cr.savepoint():
            eliminations = self._eh_prepare_transition()
            eliminations._eh_post_locked()
        return True

    def _eh_validated_line_totals(self):
        """Read locked numeric inputs without Monetary cache conversion.

        Legacy/raw rows can contain finite floats too large for currency
        rounding. Validate database primitives before ORM conversion so
        posting fails with a controlled audit error, not OverflowError.
        """
        self.ensure_one()
        self.env['eh.consol.elimination.line'].flush_model([
            'elimination_id', 'debit', 'credit',
        ])
        self.env.cr.execute(
            "SELECT COALESCE(debit, 0.0), COALESCE(credit, 0.0) "
            "FROM eh_consol_elimination_line "
            "WHERE elimination_id = %s ORDER BY id",
            (self.id,),
        )
        rows = self.env.cr.fetchall()
        rounding = float(
            self.run_id.presentation_currency_id.rounding or 0.01,
        )
        debits = []
        credits = []
        for raw_debit, raw_credit in rows:
            try:
                debit = float(raw_debit or 0.0)
                credit = float(raw_credit or 0.0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise UserError(_(
                    "Elimination %s contains an invalid numeric amount.",
                    self.display_name,
                )) from exc
            if (
                not math.isfinite(debit)
                or not math.isfinite(credit)
                or not math.isfinite(debit / rounding)
                or not math.isfinite(credit / rounding)
            ):
                raise UserError(_(
                    "Elimination %s contains a non-finite or unsafe amount.",
                    self.display_name,
                ))
            if debit < 0 or credit < 0 or (debit > 0 and credit > 0):
                raise UserError(_(
                    "Elimination %s contains an invalid debit/credit line.",
                    self.display_name,
                ))
            debits.append(debit)
            credits.append(credit)
        # fsum avoids order-dependent float accumulation. Balancing tolerance
        # is deliberately independent of presentation rounding: a JPY run does
        # not get permission to hide a 0.4 imbalance in CTA.
        debit_total = math.fsum(debits)
        credit_total = math.fsum(credits)
        difference = debit_total - credit_total
        if not all(math.isfinite(value) for value in (
            debit_total, credit_total, difference,
        )):
            raise UserError(_(
                "Elimination %s has non-finite aggregate totals.",
                self.display_name,
            ))
        return debit_total, credit_total, difference

    def _eh_post_locked(self):
        for elim in self:
            if elim.state != 'draft':
                raise UserError(_(
                    "Only draft eliminations can be posted.",
                ))
            if not elim.line_ids:
                raise UserError(_(
                    "Elimination %s has no lines; nothing to post.",
                ) % elim.display_name)
            debit, credit, difference = elim._eh_validated_line_totals()
            if not math.isclose(
                difference, 0.0, rel_tol=0.0,
                abs_tol=_BALANCE_ABS_TOLERANCE,
            ):
                raise UserError(_(
                    "Elimination %(name)s is unbalanced: total "
                    "debit %(d).2f vs credit %(c).2f. Adjust the "
                    "lines so they sum to zero.",
                    name=elim.display_name, d=debit, c=credit,
                ))
            elim.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })
        return self

    def action_cancel(self):
        # 'state' is guarded by eh.workflow.guard; this sanctioned transition
        # writes under sudo (env.su), the non-forgeable provenance the guard
        # checks, so a plain-user RPC write of state is still refused.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can cancel an elimination."
            ))
        with self.env.cr.savepoint():
            eliminations = self._eh_prepare_transition()
            for elim in eliminations:
                if elim.state != 'posted':
                    raise UserError(_(
                        "Only a posted elimination can be cancelled."
                    ))
                elim.write({'state': 'cancelled'})
        return True

    def action_reset_to_draft(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can reset an elimination."
            ))
        with self.env.cr.savepoint():
            eliminations = self._eh_prepare_transition()
            for elim in eliminations:
                if elim.state == 'cancelled':
                    # Move to draft first; only draft headers may clear their
                    # prior-cycle posting stamps. Both writes are protected by
                    # the same action-owned savepoint and parent/run locks.
                    elim.write({'state': 'draft'})
                    elim.write({
                        'posted_at': False,
                        'posted_by_id': False,
                    })
        return True


class EhConsolEliminationLine(models.Model):
    _name = 'eh.consol.elimination.line'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidation elimination line"
    _order = 'elimination_id, sequence, id'

    elimination_id = fields.Many2one(
        'eh.consol.elimination', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    presentation_currency_id = fields.Many2one(
        related='elimination_id.run_id.presentation_currency_id',
        store=True, readonly=True, copy=False,
    )

    account_id = fields.Many2one(
        'account.account', required=True,
        ondelete='restrict',
        help="Account to debit or credit.",
    )

    debit = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Debit amount. Mutually exclusive with credit.",
    )
    credit = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Credit amount. Mutually exclusive with debit.",
    )
    amount = fields.Monetary(
        compute='_compute_amount', store=True,
        currency_field='presentation_currency_id', copy=False,
        help="Signed: debit positive, credit negative.",
    )

    note = fields.Char()

    @api.depends('debit', 'credit')
    def _compute_amount(self):
        for line in self:
            line.amount = (line.debit or 0.0) - (line.credit or 0.0)

    @api.constrains('debit', 'credit')
    def _check_one_side(self):
        for line in self:
            debit = float(line.debit or 0.0)
            credit = float(line.credit or 0.0)
            if not math.isfinite(debit) or not math.isfinite(credit):
                raise ValidationError(_(
                    "Debit and credit must be finite numbers.",
                ))
            if debit < 0 or credit < 0:
                raise ValidationError(_(
                    "Debit and credit must be non-negative.",
                ))
            if debit > 0 and credit > 0:
                raise ValidationError(_(
                    "A line carries either a debit or a credit, "
                    "not both.",
                ))

    @api.model
    def _eh_validate_numeric_values(self, vals, current=None):
        debit = vals.get('debit', current.debit if current else 0.0) or 0.0
        credit = vals.get(
            'credit', current.credit if current else 0.0,
        ) or 0.0
        try:
            debit = float(debit)
            credit = float(credit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_(
                "Debit and credit must be finite numbers."
            )) from exc
        if not math.isfinite(debit) or not math.isfinite(credit):
            raise ValidationError(_(
                "Debit and credit must be finite numbers."
            ))
        if debit < 0 or credit < 0:
            raise ValidationError(_(
                "Debit and credit must be non-negative."
            ))
        if debit > 0 and credit > 0:
            raise ValidationError(_(
                "A line carries either a debit or a credit, not both."
            ))
        elimination_id = vals.get(
            'elimination_id',
            current.elimination_id.id if current else False,
        )
        elimination = self.env['eh.consol.elimination'].browse(
            elimination_id,
        )
        rounding = (
            elimination.run_id.presentation_currency_id.rounding
            if elimination else 0.01
        ) or 0.01
        if not all(math.isfinite(value / rounding) for value in (debit, credit)):
            raise ValidationError(_(
                "Debit and credit exceed the presentation currency's safe "
                "numeric range."
            ))
        return True

    @api.model
    def _eh_validate_target(self, vals, current=None):
        elimination_id = vals.get(
            'elimination_id', current.elimination_id.id if current else False)
        elimination = self.env['eh.consol.elimination'].browse(elimination_id)
        if not elimination:
            return
        elimination._eh_assert_company_closure()
        elimination._eh_assert_parent_runs_mutable(elimination.run_id)
        if elimination.state != 'draft':
            raise UserError(_(
                "Lines cannot be added or moved to a posted/cancelled "
                "consolidation elimination."
            ))
        account_id = vals.get(
            'account_id', current.account_id.id if current else False)
        account = self.env['account.account'].browse(account_id)
        if account:
            if not self.env.su:
                account._eh_check_access('read')
            scope = set(elimination.run_id.entity_id._eh_scope_companies().ids)
            company_ids = (
                set(account.company_ids.ids)
                if 'company_ids' in account._fields
                else set(account.company_id.ids)
            )
            if not (company_ids & scope):
                raise ValidationError(_(
                    "The elimination account belongs outside the authorised "
                    "consolidation company closure."
                ))
        return elimination

    def _eh_lock_parent_eliminations(self, extra_elimination_ids=()):
        eliminations = (
            self.mapped('elimination_id') |
            self.env['eh.consol.elimination'].browse(
                [elimination_id for elimination_id in extra_elimination_ids
                 if elimination_id])
        )
        eliminations._eh_lock_for_transition()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, elimination_id "
                "FROM eh_consol_elimination_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A consolidation elimination line changed concurrently. "
                    "Retry the operation."
                ))
            if not {row[1] for row in locked_rows}.issubset(
                    set(eliminations.ids)):
                raise UserError(_(
                    "A consolidation elimination line changed parent "
                    "concurrently. Retry the operation."
                ))
            self.invalidate_recordset()
        return eliminations

    @api.model_create_multi
    def create(self, vals_list):
        forged = set().union(*(
            set(vals) & {'amount', 'presentation_currency_id'}
            for vals in vals_list
        ))
        if forged:
            raise AccessError(_(
                "Elimination calculated fields are server-owned and cannot "
                "be supplied directly: %s",
                ', '.join(sorted(forged)),
            ))
        for vals in vals_list:
            self._eh_validate_numeric_values(vals)
            self._eh_validate_target(vals)
        elimination_ids = {
            vals.get('elimination_id') for vals in vals_list
            if vals.get('elimination_id')
        }
        self._eh_lock_parent_eliminations(elimination_ids)
        for vals in vals_list:
            self._eh_validate_numeric_values(vals)
            self._eh_validate_target(vals)
        return super().create(vals_list)

    def write(self, vals):
        forged = set(vals) & {'amount', 'presentation_currency_id'}
        if forged:
            raise AccessError(_(
                "Elimination calculated fields are server-owned and cannot "
                "be changed directly: %s",
                ', '.join(sorted(forged)),
            ))
        for line in self:
            self._eh_validate_numeric_values(vals, current=line)
            self._eh_validate_target(vals, current=line)
        target_ids = (
            (vals.get('elimination_id'),)
            if vals.get('elimination_id') else ())
        self._eh_lock_parent_eliminations(target_ids)
        for line in self:
            self._eh_validate_numeric_values(vals, current=line)
            self._eh_validate_target(vals, current=line)
        return super().write(vals)

    def unlink(self):
        self._eh_lock_parent_eliminations()
        self.mapped('elimination_id')._eh_assert_parent_runs_mutable(
            self.mapped('elimination_id.run_id'),
        )
        if self.filtered(lambda line: line.elimination_id.state != 'draft'):
            raise UserError(_(
                "Posted/cancelled consolidation elimination lines are retained "
                "as immutable evidence."
            ))
        return super().unlink()
