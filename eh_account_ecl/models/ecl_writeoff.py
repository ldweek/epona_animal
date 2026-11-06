# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.writeoff: derecognition of an uncollectible receivable against the
loss allowance of a posted ECL run.

IFRS 9.5.4.4: the gross carrying amount of a financial asset is reduced
(written off) when there is no reasonable expectation of recovery. The
write-off consumes the recognised allowance rather than creating a new
loss, so the entry is Dr loss allowance / Cr receivable, partial amounts
allowed, and the write-off appears in its stage's column of the IFRS 7.35H
reconciliation. The total written off against a run can never exceed the
allowance that run recognised.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .ecl_recon import STAGES


class EhEclWriteoff(models.Model):
    _name = 'eh.ecl.writeoff'
    _description = "ECL allowance write-off"
    _inherit = ['mail.thread', 'eh.workflow.guard']
    _order = 'id desc'

    # State moves only through action_post_writeoff (which runs as su);
    # eh.workflow.guard refuses a direct non-superuser write to state, so a
    # plain user cannot RPC ``write({'state': 'posted'})`` past the GL entry
    # and allowance-consumption checks.
    _eh_guarded_fields = ('state', 'move_id')

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('posted', "Posted")],
        default='draft', required=True, tracking=True, index=True)

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='restrict', index=True,
        domain="[('state', '=', 'posted')]",
        help="Posted ECL run whose recognised allowance this write-off "
             "consumes; the write-off appears in that run's reconciliation.")
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    move_line_id = fields.Many2one(
        'account.move.line', string="Receivable Line", required=True,
        domain="[('account_id.account_type', '=', 'asset_receivable'),"
               " ('company_id', '=', company_id),"
               " ('parent_state', '=', 'posted'),"
               " ('reconciled', '=', False)]",
        help="Open posted receivable line being written off.")
    partner_id = fields.Many2one(
        related='move_line_id.partner_id', store=True, readonly=True)
    amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Amount written off; a partial write-off of the line's "
             "residual is allowed (IFRS 9.5.4.4).")
    stage = fields.Selection(
        STAGES, required=True, default='3',
        help="Reconciliation stage the written-off exposure sat in; "
             "write-offs are usually credit-impaired (Stage 3) or POCI.")
    date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')

    _check_amount = models.Constraint(
        'CHECK (amount >= 0)', 'A write-off amount cannot be negative.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_validate_reference_vals(vals, operation='read')
        run_ids = {vals.get('run_id') for vals in vals_list if vals.get('run_id')}
        line_ids = {
            vals.get('move_line_id') for vals in vals_list
            if vals.get('move_line_id')
        }
        self._eh_lock_for_transition(
            extra_run_ids=run_ids, extra_line_ids=line_ids)
        for vals in vals_list:
            self._eh_validate_reference_vals(vals, operation='read')
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.ecl.writeoff') or '/'
        writeoffs = super().create(vals_list)
        writeoffs._eh_validate_references(operation='read')
        return writeoffs

    @api.model
    def _eh_validate_reference_vals(self, vals, current=None, operation='read'):
        run_id = vals.get('run_id', current.run_id.id if current else False)
        line_id = vals.get(
            'move_line_id', current.move_line_id.id if current else False)
        run = self.env['eh.ecl.run'].browse(run_id)
        line = self.env['account.move.line'].browse(line_id)
        if not run or not line:
            return
        if not self.env.su:
            run._eh_validate_references()
            line._eh_check_access(operation)
        if (
            line.company_id != run.company_id
            or line.move_id.company_id != run.company_id
            or line.parent_state != 'posted'
            or line.account_id.account_type != 'asset_receivable'
        ):
            raise AccessError(_(
                "The receivable selected for an ECL write-off must be a posted, "
                "open receivable in the same authorised company as the ECL run."
            ))

    def _eh_validate_references(self, operation='read'):
        for writeoff in self:
            self._eh_validate_reference_vals(
                {}, current=writeoff, operation=operation)
        return self

    def _eh_lock_for_transition(self, extra_run_ids=(), extra_line_ids=()):
        runs = self.mapped('run_id') | self.env['eh.ecl.run'].browse(
            [run_id for run_id in extra_run_ids if run_id])
        runs._eh_lock_for_transition()
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_ecl_writeoff WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()
        line_ids = tuple(sorted(
            set(self.mapped('move_line_id').ids) |
            {int(line_id) for line_id in extra_line_ids if line_id}
        ))
        if line_ids:
            self.env.cr.execute(
                "SELECT id FROM account_move_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (line_ids,),
            )
            self.env['account.move.line'].browse(line_ids).invalidate_recordset()
        return self

    @api.onchange('move_line_id')
    def _onchange_move_line_id(self):
        for record in self:
            if record.move_line_id and not record.amount:
                record.amount = record.move_line_id.amount_residual

    # Once the write-off has posted its GL entry, its figures are the ledger
    # counterpart of a consumed allowance; only state/audit fields may move.
    _FROZEN_AFTER_POST = ('run_id', 'move_line_id', 'amount', 'stage', 'date')

    def write(self, vals):
        if not self.env.su or set(vals) & set(self._FROZEN_AFTER_POST):
            for writeoff in self:
                self._eh_validate_reference_vals(
                    vals, current=writeoff, operation='read')
            self._eh_lock_for_transition(
                extra_run_ids=(vals.get('run_id'),),
                extra_line_ids=(vals.get('move_line_id'),),
            )
            for writeoff in self:
                self._eh_validate_reference_vals(
                    vals, current=writeoff, operation='read')
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        posted = self.filtered(lambda w: w.state == 'posted')
        if frozen and posted:
            raise UserError(_(
                "Inputs on a posted write-off are frozen (%(fields)s).",
                fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        self._eh_validate_references(operation='write')
        self._eh_lock_for_transition()
        self._eh_validate_references(operation='write')
        if any(w.state == 'posted' for w in self):
            raise UserError(_(
                "A posted write-off cannot be deleted; it is the ledger "
                "counterpart of a consumed allowance."))
        return super().unlink()

    def action_post_writeoff(self):
        caller = self
        caller._eh_validate_references(operation='write')
        caller._eh_lock_for_transition()
        caller._eh_validate_references(operation='write')
        self = caller._eh_workflow_action()
        for record in self:
            if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise UserError(_(
                    "Only an EH Accounting Manager can post an ECL "
                    "write-off."))
            if record.state != 'draft':
                raise UserError(_("Only a draft write-off can post."))
            run = record.run_id
            if run.state != 'posted':
                raise UserError(_(
                    "Write-offs consume a recognised allowance; run %s must "
                    "be posted first.", run.display_name))
            run._eh_assert_posted_provenance()
            run._eh_assert_no_active_posted_successor(
                _("accept a new write-off"),
            )
            if record.date < run.reporting_date:
                raise UserError(_(
                    "Write-off %(writeoff)s is dated before its source ECL "
                    "run %(run)s. A write-off cannot consume an allowance "
                    "before that allowance snapshot exists.",
                    writeoff=record.display_name,
                    run=run.display_name,
                ))
            currency = record.currency_id
            if currency.is_zero(record.amount) or record.amount < 0:
                raise UserError(_("The write-off amount must be positive."))
            line = record.move_line_id
            if currency.compare_amounts(
                    record.amount, line.amount_residual) > 0:
                raise UserError(_(
                    "The write-off (%(amount)s) exceeds the receivable "
                    "line's open residual (%(residual)s).",
                    amount=record.amount, residual=line.amount_residual))
            available = run.closing_allowance - run._writeoff_posted_total()
            if currency.compare_amounts(record.amount, available) > 0:
                raise UserError(_(
                    "The write-off (%(amount)s) exceeds the remaining loss "
                    "allowance of run %(run)s (%(available)s); a write-off "
                    "consumes the allowance and cannot create one.",
                    amount=record.amount, run=run.display_name,
                    available=available))
            move = record._build_move()
            record.sudo().write({'state': 'posted', 'move_id': move.id})
            run._rebuild_recon()
        return True

    def _build_move(self):
        self.ensure_one()
        run = self.run_id
        line = self.move_line_id
        amount = self.currency_id.round(self.amount)
        foreign_currency = line.currency_id.filtered(
            lambda currency: currency != self.currency_id
        )
        source_amount = 0.0
        if foreign_currency:
            company_residual = abs(line.amount_residual)
            currency_residual = abs(line.amount_residual_currency)
            if self.currency_id.is_zero(company_residual):
                raise UserError(_(
                    "The foreign-currency receivable has no company-currency "
                    "residual to write off. Revalue or correct it first."
                ))
            if self.currency_id.compare_amounts(
                amount, company_residual,
            ) == 0:
                source_amount = currency_residual
            else:
                source_amount = foreign_currency.round(
                    currency_residual * amount / company_residual,
                )
        allowance_vals = {
            'name': _("Allowance utilised %s", self.name),
            'account_id': run.loss_allowance_account_id.id,
            'debit': amount,
            'credit': 0.0,
        }
        receivable_vals = {
            'name': _("Receivable written off %s", self.name),
            'account_id': line.account_id.id,
            'partner_id': line.partner_id.id,
            'debit': 0.0,
            'credit': amount,
        }
        if foreign_currency:
            allowance_vals.update({
                'currency_id': foreign_currency.id,
                'amount_currency': source_amount,
            })
            receivable_vals.update({
                'currency_id': foreign_currency.id,
                'amount_currency': -source_amount,
            })
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.date,
            'journal_id': run.journal_id.id,
            'ref': _("ECL write-off %s", self.name),
            'eh_sealed': True,
            'line_ids': [
                (0, 0, allowance_vals),
                (0, 0, receivable_vals),
            ],
        })
        move.action_post()
        credit_line = move.line_ids.filtered(
            lambda l: l.account_id == line.account_id)
        (line + credit_line).reconcile()
        return move

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No write-off entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }
