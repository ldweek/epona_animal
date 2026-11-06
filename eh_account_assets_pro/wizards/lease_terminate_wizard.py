# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IFRS 16 lessee early-termination wizard.

On an early termination the lessee derecognises the remaining lease
liability and the remaining ROU asset (its gross cost less accumulated
depreciation) at the termination date, and recognises the difference as
a gain or loss in P&L (IFRS 16.46(a) applied to a full termination; a
lease that ends early is derecognised in full). Where the liability
exceeds the ROU carrying amount the result is a GAIN (the lessee is
released from a liability greater than the asset given up); where the
ROU exceeds the liability it is a LOSS. An optional cash settlement
captures any make-good or termination payment to or from the lessor,
and shifts the gain / loss by that amount.

Journal entry (no settlement):

  Dr Lease Liability             remaining liability balance
  Dr ROU Accumulated Depreciation accumulated depreciation to date
    Cr ROU Asset                  gross ROU cost
    Cr / Dr P&L                   balancing gain / loss
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.accounting_integrity import _eh_validate_accounting_company


class EhLeaseTerminateWizard(models.TransientModel):
    _name = 'eh.lease.terminate.wizard'
    _description = "Lease Termination Wizard"

    lease_id = fields.Many2one(
        'eh.lease.contract', required=True, ondelete='cascade',
    )
    termination_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    settlement_amount = fields.Monetary(
        default=0.0,
        help="Cash paid to or received from the lessor on termination. "
             "Positive amount = paid to lessor.",
    )
    settlement_account_id = fields.Many2one(
        'account.account', string="Settlement Account",
        help="Cash or payable account for the settlement.",
    )
    pl_account_id = fields.Many2one(
        'account.account', string="P/L Account", required=True,
        help="Where the termination gain or loss is booked.",
    )
    notes = fields.Text()
    processed = fields.Boolean(readonly=True, copy=False)

    currency_id = fields.Many2one(
        related='lease_id.currency_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='lease_id.company_id', readonly=True,
    )
    current_liability = fields.Monetary(
        compute='_compute_current', readonly=True,
    )
    current_rou = fields.Monetary(
        compute='_compute_current', readonly=True,
    )
    rou_accumulated = fields.Monetary(
        compute='_compute_current', readonly=True,
    )
    pl_amount = fields.Monetary(
        compute='_compute_current', readonly=True,
        help="Termination gain (positive) or loss (negative), net of any "
             "settlement: liability derecognised less ROU derecognised "
             "less settlement paid.",
    )

    @api.depends('lease_id', 'settlement_amount')
    def _compute_current(self):
        for w in self:
            if w.lease_id:
                w.current_liability = w.lease_id._liability_balance_after_last_post()
                w.current_rou = w.lease_id._rou_carrying_amount()
                w.rou_accumulated = sum(
                    w.lease_id.schedule_line_ids.filtered(
                        lambda l: l.is_posted,
                    ).mapped('rou_amount'),
                )
                # Gain / loss = liability released - ROU given up
                # - settlement paid (IFRS 16 derecognition).
                w.pl_amount = w.lease_id.currency_id.round(
                    w.current_liability - w.current_rou
                    - (w.settlement_amount or 0.0),
                )
            else:
                w.current_liability = 0.0
                w.current_rou = 0.0
                w.rou_accumulated = 0.0
                w.pl_amount = 0.0

    def action_terminate(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM eh_lease_terminate_wizard WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset(['processed'])
        if self.processed:
            raise UserError(_(
                "This termination wizard has already been applied."
            ))
        lease = self.lease_id
        # The wizard record is not a substitute for checking the company
        # rules on its source lease.  Do this before state/balance reads and
        # before creating the termination entry.
        lease._eh_check_access('write')
        lease._eh_validate_company_currency()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only EH accounting managers can terminate leases.",
            ))
        lease._eh_lock_for_transition()
        self.invalidate_recordset([
            'current_liability', 'current_rou', 'rou_accumulated', 'pl_amount',
        ])
        if lease.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be terminated.",
            ))
        lease._check_remeasurement_supported(_("terminated early"))
        posted_dates = lease.schedule_line_ids.filtered(
            'is_posted',
        ).mapped('period_date')
        if (self.termination_date < lease.commencement_date
                or posted_dates
                and self.termination_date < max(posted_dates)):
            raise UserError(_(
                "Termination date cannot precede commencement or the latest "
                "posted lease period."
            ))
        _eh_validate_accounting_company(
            self, ('settlement_account_id', 'pl_account_id'),
        )
        if (self.settlement_amount or 0.0) != 0 and not self.settlement_account_id:
            raise UserError(_(
                "Provide a settlement account when settlement amount is "
                "non zero.",
            ))

        # Earned periods and the event-date stub are accounting events, not
        # disposable forecasts. Post both before derecognition measurement.
        lease._eh_post_accrued_through(self.termination_date)
        if lease.state not in ('active', 'modified'):
            raise UserError(_(
                "The lease reached its contractual end on or before the "
                "termination date; post it through the normal schedule "
                "instead of recording an early termination.",
            ))
        lease.invalidate_recordset([
            'total_paid', 'total_interest', 'total_principal',
            'liability_balance',
        ])
        self.invalidate_recordset([
            'current_liability', 'current_rou', 'rou_accumulated', 'pl_amount',
        ])

        liability = self.current_liability
        rou_carrying = self.current_rou
        rou_accum = self.rou_accumulated
        rou_cost = rou_carrying + rou_accum

        # Derecognise both sides:
        # Dr Liability (full balance)
        # Dr ROU Accumulated Depreciation (full)
        # Cr ROU Asset (cost)
        # Plus Cr (or Dr) Settlement
        # Plus balancing P/L
        lines = []
        if liability > 0:
            lines.append((0, 0, {
                'name': _("Liability derecognition %s", lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': liability,
                'credit': 0.0,
            }))
        if rou_accum > 0:
            lines.append((0, 0, {
                'name': _("ROU accumulated depreciation %s", lease.display_name),
                'account_id': lease.rou_accumulated_depreciation_account_id.id,
                'debit': rou_accum,
                'credit': 0.0,
            }))
        if rou_cost > 0:
            lines.append((0, 0, {
                'name': _("ROU derecognition %s", lease.display_name),
                'account_id': lease.rou_asset_account_id.id,
                'debit': 0.0,
                'credit': rou_cost,
            }))
        if (self.settlement_amount or 0.0) > 0:
            lines.append((0, 0, {
                'name': _("Termination settlement %s", lease.display_name),
                'account_id': self.settlement_account_id.id,
                'debit': 0.0,
                'credit': self.settlement_amount,
            }))
        elif (self.settlement_amount or 0.0) < 0:
            lines.append((0, 0, {
                'name': _("Termination settlement (refund) %s", lease.display_name),
                'account_id': self.settlement_account_id.id,
                'debit': abs(self.settlement_amount),
                'credit': 0.0,
            }))

        # Compute the balancing amount.
        debit_total = sum(l[2]['debit'] for l in lines)
        credit_total = sum(l[2]['credit'] for l in lines)
        diff = credit_total - debit_total
        if diff > 0:
            # Net credit position: post a debit to P/L (loss).
            lines.append((0, 0, {
                'name': _("Termination loss %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': diff,
                'credit': 0.0,
            }))
        elif diff < 0:
            # Net debit position: post a credit to P/L (gain).
            lines.append((0, 0, {
                'name': _("Termination gain %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': 0.0,
                'credit': abs(diff),
            }))

        if not lines:
            raise UserError(_(
                "Nothing to derecognise; lease has zero carrying amounts.",
            ))

        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.termination_date,
            'journal_id': lease.journal_id.id,
            'ref': _("Lease termination %s", lease.display_name),
            'line_ids': lines,
        })
        move.action_post()

        unposted = lease.schedule_line_ids.filtered(lambda l: not l.is_posted)
        unposted.sudo().unlink()
        lease._eh_workflow_write({
            'state': 'terminated',
            'terminated_at': fields.Datetime.now(),
            'terminated_by_id': self.env.user.id,
            'termination_date': self.termination_date,
            'termination_move_id': move.id,
        })
        if self.notes:
            lease.message_post(body=_("Termination notes: %s", self.notes))
        self.sudo().write({'processed': True})
        return {'type': 'ir.actions.act_window_close'}

    def write(self, vals):
        if 'processed' in vals and not self.env.su:
            raise UserError(_("Processed status is server-owned."))
        return super().write(vals)
