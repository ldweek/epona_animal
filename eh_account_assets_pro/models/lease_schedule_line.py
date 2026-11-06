# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Single line of an IFRS 16 lease amortisation schedule.

On posting (lessee default), two journal entries are produced atomically
as one move:

* Lease entry: Dr Lease Liability (principal), Dr Interest Expense
  (interest), Cr Cash/Payables (payment); plus, when a lease / non-lease
  component split is set, Dr Lease/Service Expense (service share) and a
  matching extra Cr Cash so the cash leg settles the full contractual
  payment (IFRS 16.13-16).
* ROU depreciation: Dr ROU Depreciation, Cr ROU Accumulated Depreciation.

Exempt leases (IFRS 16.6) post Dr Lease Expense / Cr Cash only.
Operating lessors (IFRS 16.81) post Dr Cash / Cr Rental Income.
Finance lessors (IFRS 16.75) post Dr Cash, Cr Interest Income
(interest), Cr Net Investment (principal recovery).

Storing all legs on one move keeps the period view tight and reconciles
the lease liability (or net investment) cleanly.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .accounting_integrity import _eh_require_exact_posting_date


class EhLeaseScheduleLine(models.Model):
    _name = 'eh.lease.schedule.line'
    _inherit = ['eh.workflow.guard']
    _description = "Lease Amortisation Schedule Line"
    _order = 'lease_id, sequence'

    # Posting identity and audit fields may only be stamped by action_post.
    # Source identity and measurement fields remain editable before booking,
    # then become immutable through _FROZEN_AFTER_POST below.
    _eh_guarded_fields = (
        'is_posted', 'move_id', 'posted_at', 'posted_by_id',
        'lease_id', 'sequence', 'period_date', 'liability_open',
        'payment_amount', 'service_amount', 'interest', 'principal',
        'liability_close', 'rou_amount', 'rou_accumulated',
        'is_event_accrual',
    )

    lease_id = fields.Many2one(
        'eh.lease.contract', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    sequence = fields.Integer(required=True, default=10)
    period_date = fields.Date(required=True)

    liability_open = fields.Monetary()
    payment_amount = fields.Monetary(
        help=(
            "Lease-component payment for the period (the amount the "
            "liability amortisation runs on). When a lease / non-lease "
            "split is set, the service share sits in service_amount and "
            "the cash leg settles payment_amount + service_amount."
        ),
    )
    service_amount = fields.Monetary(
        string="Service (non-lease) share",
        help=(
            "Non-lease component share of the period's contractual "
            "payment (IFRS 16.13-16); posts straight to the lease / "
            "service expense account, never into the liability or ROU."
        ),
    )
    interest = fields.Monetary()
    principal = fields.Monetary()
    liability_close = fields.Monetary()

    rou_amount = fields.Monetary(string="ROU Depreciation")
    rou_accumulated = fields.Monetary()
    is_event_accrual = fields.Boolean(
        string="Event-date Stub", readonly=True, copy=False,
        help=(
            "Prorated ROU depreciation and, for arrears schedules, effective "
            "interest accrued through a modification/termination date. It "
            "contains no cash payment."
        ),
    )

    is_posted = fields.Boolean(default=False, copy=False, readonly=True)
    posted_at = fields.Datetime(readonly=True, copy=False)
    posted_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )

    currency_id = fields.Many2one(
        related='lease_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='lease_id.company_id', store=True, readonly=True,
    )

    _uniq_lease_sequence = models.Constraint(
        'unique(lease_id, sequence)',
        'Sequence must be unique within a lease.',
    )

    # Measurement fields frozen once the line has produced its journal entry.
    # Re-basing an amortisation figure on a posted line would move the charge
    # away from the ledger it already booked; a correction must be a further
    # posting (or a lease modification / termination), not an in-place edit.
    _FROZEN_AFTER_POST = (
        'lease_id', 'sequence', 'period_date', 'liability_open',
        'payment_amount', 'service_amount', 'interest', 'principal',
        'liability_close', 'rou_amount', 'rou_accumulated',
        'is_event_accrual',
    )

    @api.model_create_multi
    def create(self, vals_list):
        leases = self.env['eh.lease.contract'].browse({
            vals.get('lease_id') for vals in vals_list if vals.get('lease_id')
        })
        if not self.env.su:
            raise UserError(_(
                "Lease schedule rows are engine-generated. Use Compute "
                "Schedule or the modification workflow instead of creating "
                "rows directly."
            ))
        leases._eh_validate_company_currency()
        return super().create(vals_list)

    def write(self, vals):
        # Workflow identity/audit fields are owned by the shared guard. Do not
        # mask its AccessError with the broader engine-row UserError.
        if not self.env.su and set(vals) & set(self._eh_guarded_fields):
            return super().write(vals)
        protected = set(vals) & (
            set(self._eh_guarded_fields) | set(self._FROZEN_AFTER_POST)
        )
        if protected and not self.env.su:
            self._eh_check_access('write')
            leases = self.mapped('lease_id')
            if vals.get('lease_id'):
                leases |= self.env['eh.lease.contract'].browse(vals['lease_id'])
            leases._eh_check_access('write')
            raise UserError(_(
                "Lease schedule rows are engine-generated and cannot be "
                "edited directly. Remeasure the lease instead."
            ))
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(
                lambda line: line.is_posted or line.move_id,
            )
            if posted:
                raise UserError(_(
                    "Schedule fields (%(fields)s) are frozen once the lease "
                    "line is posted; the charge must equal the journal entry "
                    "it produced. Reverse the entry (or remeasure the lease) "
                    "to correct it.",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            raise UserError(_(
                "Lease schedule rows are engine-generated and cannot be "
                "deleted directly. Recompute or remeasure the lease instead."
            ))
        booked = self.filtered(lambda line: line.is_posted or line.move_id)
        if booked:
            raise UserError(_(
                "A booked lease schedule line cannot be deleted; its source "
                "identity and journal-entry link are permanent audit "
                "evidence. Reverse or remeasure the lease to correct it.",
            ))
        return super().unlink()

    def _eh_lock_for_post(self):
        """Serialise concurrent posters on the schedule lines.

        The daily cron and the manual 'Post Due Lines' button (and a plain
        double-click / browser retry) both read-then-post the same unposted
        line. Under READ COMMITTED both would read is_posted=False and each
        create a posted move, silently doubling the ROU / interest charge.
        Take a row lock and re-read is_posted so the loser blocks then skips.
        """
        if not self.ids:
            return
        leases = self.mapped('lease_id')
        leases._eh_lock_for_transition()
        self.env.cr.execute(
            'SELECT id FROM eh_lease_schedule_line WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset(['is_posted', 'move_id'])

    def action_post(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post a lease schedule "
                "line to the general ledger. This posting is a "
                "segregation-of-duties control point.",
            ))
        self._eh_lock_for_post()
        touched_leases = self.env['eh.lease.contract']
        for line in self:
            # Idempotent: never re-book a line that already carries a live
            # posted move. Skip silently so a concurrent cron/manual race, a
            # double-submit, or a re-run is a no-op instead of a duplicate.
            if line.is_posted and not line.move_id:
                raise UserError(_(
                    "Lease line %(line)s is marked posted but has no journal "
                    "link. Repair the inconsistent legacy record first.",
                    line=line.display_name,
                ))
            if line.is_posted:
                continue
            if line.move_id:
                raise UserError(_(
                    "Lease line %(line)s has a journal link but is not marked "
                    "posted. Repair it before another posting.",
                    line=line.display_name,
                ))
            lease = line.lease_id
            touched_leases |= lease
            lease._eh_validate_company_currency()
            lease._validate_lease_setup()
            if lease.state not in ('active', 'modified'):
                raise UserError(_(
                    "Lease schedule can only post for an active or modified "
                    "contract; %(lease)s is %(state)s.",
                    lease=lease.display_name, state=lease.state,
                ))
            amounts = (
                line.liability_open, line.payment_amount, line.service_amount,
                line.interest, line.principal, line.liability_close,
                line.rou_amount, line.rou_accumulated,
            )
            if any(amount < 0 for amount in amounts):
                raise UserError(_(
                    "Lease schedule line %(line)s contains a negative engine "
                    "amount and cannot post.", line=line.display_name,
                ))
            if line.is_event_accrual:
                if (line.payment_amount or line.service_amount
                        or line.principal):
                    raise UserError(_(
                        "An event-date lease accrual cannot contain a cash "
                        "payment, service payment, or principal reduction."
                    ))
                expected_close = lease.currency_id.round(
                    line.liability_open + line.interest,
                )
                if lease.currency_id.compare_amounts(
                        line.liability_close, expected_close) != 0:
                    raise UserError(_(
                        "Event-date lease accrual %(line)s does not reconcile "
                        "opening liability plus accrued interest to closing "
                        "liability.", line=line.display_name,
                    ))
            elif lease.exemption == 'none' and lease.lessor_mode != 'operating':
                currency = lease.currency_id
                expected_close = currency.round(
                    line.liability_open - line.principal,
                )
                if currency.compare_amounts(
                        line.liability_close, expected_close) != 0:
                    raise UserError(_(
                        "Lease line %(line)s does not reconcile liability open, "
                        "principal, and liability close.",
                        line=line.display_name,
                    ))
                expected_payment = currency.round(
                    line.principal + line.interest,
                )
                if currency.compare_amounts(
                        line.payment_amount, expected_payment) != 0:
                    raise UserError(_(
                        "Lease line %(line)s does not reconcile payment to "
                        "principal plus interest.", line=line.display_name,
                    ))
            _eh_require_exact_posting_date(
                lease.company_id,
                line.period_date,
                lease.journal_id,
                _("Lease schedule row %(line)s", line=line.display_name),
            )
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'eh_sealed': True,
                'date': line.period_date,
                'journal_id': lease.journal_id.id,
                'ref': _("Lease %(name)s period %(seq)s",
                         name=lease.display_name, seq=line.sequence),
                'line_ids': line._build_move_lines(),
            })
            move.action_post()
            # is_posted / move_id are guarded; stamp through the sanctioned
            # action path (runs as su) so a real, non-superuser manager can
            # post while a direct RPC write to those fields stays blocked.
            line._eh_workflow_write({
                'is_posted': True,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
        touched_leases._maybe_mark_ended()
        return True

    def _build_move_lines(self):
        self.ensure_one()
        lease = self.lease_id
        # IFRS 16.6 exemption: straight-line expense, no ROU / liability.
        if lease.exemption != 'none':
            return [
                (0, 0, {
                    'name': _("Lease expense (exempt) %s", lease.display_name),
                    'account_id': lease.lease_expense_account_id.id,
                    'debit': self.payment_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Lease payment %s", lease.display_name),
                    'account_id': lease.cash_account_id.id,
                    'partner_id': lease.lessor_id.id,
                    'debit': 0.0,
                    'credit': self.payment_amount,
                }),
            ]
        # IFRS 16.81 operating lessor: straight-line rental income.
        if lease.lessor_mode == 'operating':
            return [
                (0, 0, {
                    'name': _("Lease receipt %s", lease.display_name),
                    'account_id': lease.cash_account_id.id,
                    'partner_id': lease.lessor_id.id,
                    'debit': self.payment_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Rental income %s", lease.display_name),
                    'account_id': lease.lessor_income_account_id.id,
                    'debit': 0.0,
                    'credit': self.payment_amount,
                }),
            ]
        # IFRS 16.75 finance lessor: receipt splits into interest income
        # and net-investment principal recovery.
        if lease.lessor_mode == 'finance':
            lines = []
            if self.payment_amount > 0:
                lines.append((0, 0, {
                    'name': _("Lease receipt %s", lease.display_name),
                    'account_id': lease.cash_account_id.id,
                    'partner_id': lease.lessor_id.id,
                    'debit': self.payment_amount,
                    'credit': 0.0,
                }))
            if self.interest > 0:
                lines.append((0, 0, {
                    'name': _("Interest income %s", lease.display_name),
                    'account_id': lease.lessor_interest_income_account_id.id,
                    'debit': 0.0,
                    'credit': self.interest,
                }))
            if self.principal > 0:
                lines.append((0, 0, {
                    'name': _("Net investment recovery %s",
                              lease.display_name),
                    'account_id': lease.net_investment_account_id.id,
                    'debit': 0.0,
                    'credit': self.principal,
                }))
            return lines
        lines = []
        if self.is_event_accrual and self.interest > 0:
            lines.append((0, 0, {
                'name': _("Accrued lease interest %s", lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'partner_id': lease.lessor_id.id,
                'debit': 0.0,
                'credit': self.interest,
            }))
        # Liability principal
        if self.principal > 0:
            lines.append((0, 0, {
                'name': _("Lease principal %s", lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': self.principal,
                'credit': 0.0,
            }))
        # Interest expense
        if self.interest > 0:
            lines.append((0, 0, {
                'name': _("Lease interest %s", lease.display_name),
                'account_id': lease.interest_expense_account_id.id,
                'debit': self.interest,
                'credit': 0.0,
            }))
        # Non-lease (service) component: straight to expense, cash leg
        # below settles the full contractual payment (IFRS 16.13-16).
        if self.service_amount > 0:
            lines.append((0, 0, {
                'name': _("Service component %s", lease.display_name),
                'account_id': lease.lease_expense_account_id.id,
                'debit': self.service_amount,
                'credit': 0.0,
            }))
        # Cash / payable
        cash_total = (self.payment_amount or 0.0) + (self.service_amount or 0.0)
        if cash_total > 0:
            lines.append((0, 0, {
                'name': _("Lease payment %s", lease.display_name),
                'account_id': lease.cash_account_id.id,
                'partner_id': lease.lessor_id.id,
                'debit': 0.0,
                'credit': cash_total,
            }))
        # ROU depreciation
        if self.rou_amount > 0:
            lines.append((0, 0, {
                'name': _("ROU depreciation %s", lease.display_name),
                'account_id': lease.rou_depreciation_account_id.id,
                'debit': self.rou_amount,
                'credit': 0.0,
            }))
            lines.append((0, 0, {
                'name': _("ROU accumulated depreciation %s", lease.display_name),
                'account_id': lease.rou_accumulated_depreciation_account_id.id,
                'debit': 0.0,
                'credit': self.rou_amount,
            }))
        return lines

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No journal entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
