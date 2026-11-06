# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IFRS 16 lease modification wizard (lessee remeasurement, IFRS 16.39-46).

The wizard handles a modification that is NOT a separate lease. Two
paths:

1. Remeasurement (IFRS 16.39-43) - a change in the discount rate and / or
   the lease payments (a floating-rate reassessment, an index / rate
   change, a residual-value guarantee reassessment, or a modification
   that changes consideration without decreasing scope). The liability
   is remeasured to the PV of the revised payments at the revised
   discount rate; the ROU asset is adjusted by the SAME amount. If a
   DECREASE would take the ROU below zero, the ROU is floored at zero
   and the excess goes to P&L (IFRS 16.39).

     Dr or Cr Lease Liability   delta_liability
     Dr or Cr ROU Asset         (bounded to keep ROU >= 0)
     Cr or Dr P&L               (only the ROU-floor excess)

2. Partial scope decrease (IFRS 16.45-46) - the lease term is shortened
   or the leased capacity reduced. The ROU asset is decreased in
   PROPORTION to the reduction in scope; the liability is remeasured to
   the PV of the revised payments; the difference between the reduction
   in the liability and the proportionate reduction in the ROU is a
   gain or loss to P&L (IFRS 16.46(a)).

     Dr Lease Liability          liability reduction
       Cr ROU Asset              proportionate ROU reduction
       Cr / Dr P&L               difference (gain / loss)

Then the unposted schedule is wiped and a new one is built starting
from the modification date.
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.accounting_integrity import _eh_validate_accounting_company


CADENCE_MONTHS = {
    'monthly': 1,
    'quarterly': 3,
    'semi_annual': 6,
    'annual': 12,
}


class EhLeaseModifyWizard(models.TransientModel):
    _name = 'eh.lease.modify.wizard'
    _description = "Lease Modification Wizard"

    lease_id = fields.Many2one(
        'eh.lease.contract', required=True, ondelete='cascade',
    )
    modification_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    modification_type = fields.Selection(
        [
            ('remeasure', "Remeasurement (rate / payment change)"),
            ('scope_decrease', "Partial scope decrease"),
        ],
        required=True, default='remeasure',
        help=(
            "Remeasurement (IFRS 16.39-43): a change in the discount "
            "rate and / or the payments; the ROU is adjusted by the same "
            "amount as the liability (floored at zero, excess to P&L). "
            "Partial scope decrease (IFRS 16.45-46): the ROU is reduced "
            "in proportion to the scope given up and the difference "
            "against the liability reduction posts to P&L."
        ),
    )
    scope_decrease_pct = fields.Float(
        string="Scope decrease %", digits=(5, 2),
        help=(
            "Percentage of the right-of-use given up in a partial scope "
            "decrease (IFRS 16.46(a)): the ROU asset is reduced by this "
            "proportion of its carrying amount."
        ),
    )
    pl_account_id = fields.Many2one(
        'account.account', string="P/L Account",
        help=(
            "Gain / loss account for the P&L effect of the modification: "
            "the ROU-floor excess on a remeasurement decrease "
            "(IFRS 16.39) or the difference between the liability and "
            "proportionate ROU reductions on a partial scope decrease "
            "(IFRS 16.46(a)). Required whenever the modification produces "
            "a P&L amount."
        ),
    )
    new_term_months = fields.Integer(required=True)
    new_payment_amount = fields.Monetary(required=True)
    new_ibr = fields.Float(string="New IBR (annual %)", required=True)
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
    new_liability = fields.Monetary(
        compute='_compute_new_liability', readonly=True,
    )
    delta = fields.Monetary(
        compute='_compute_new_liability', readonly=True,
    )
    rou_reduction = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help="Reduction applied to the ROU asset by this modification.",
    )
    pl_amount = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help="P&L effect of this modification (positive = gain).",
    )
    scope_gain_loss = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help=(
            "IFRS 16.46(a) gain/loss from the proportionate liability and "
            "ROU derecognition only; excludes subsequent remeasurement."
        ),
    )
    remeasurement_delta = fields.Monetary(
        compute='_compute_effects', readonly=True,
        help=(
            "Change from the post-scope-decrease liability to the newly "
            "remeasured liability."
        ),
    )

    @api.depends('lease_id')
    def _compute_current(self):
        for w in self:
            if w.lease_id:
                w.current_liability = w.lease_id._liability_balance_after_last_post()
                w.current_rou = w.lease_id._rou_carrying_amount()
            else:
                w.current_liability = 0.0
                w.current_rou = 0.0

    @api.depends('new_term_months', 'new_payment_amount', 'new_ibr',
                 'lease_id', 'lease_id.payment_service_pct',
                 'modification_date')
    def _compute_new_liability(self):
        for w in self:
            if not w.lease_id:
                w.new_liability = 0.0
                w.delta = 0.0
                continue
            try:
                pv = w._compute_new_pv()
            except Exception:  # noqa: BLE001
                w.new_liability = 0.0
                w.delta = 0.0
                continue
            w.new_liability = w.lease_id.currency_id.round(pv)
            w.delta = w.lease_id.currency_id.round(pv - w.current_liability)

    def _compute_new_pv(self):
        self.ensure_one()
        cadence = self.lease_id.cadence
        period_months = CADENCE_MONTHS[cadence]
        if self.new_term_months % period_months:
            raise UserError(_(
                "Revised term must be a whole multiple of the cadence.",
            ))
        n = int(self.new_term_months // period_months)
        annual = self.new_ibr / 100.0
        r = (1.0 + annual) ** (period_months / 12.0) - 1.0
        pmt = self._new_lease_component_payment()
        if r == 0:
            pv = pmt * n
        else:
            pv = pmt * (1.0 - (1.0 + r) ** (-n)) / r
            if self.lease_id.payment_timing == 'advance':
                pv = pv * (1.0 + r)
        return pv

    def _new_lease_component_payment(self):
        """Lease share of revised contractual consideration.

        Modification does not change documented component allocation: only
        lease consideration enters IFRS 16 liability measurement; service
        consideration remains period expense.
        """
        self.ensure_one()
        pct = (self.lease_id.payment_service_pct or 0.0) / 100.0
        return self.lease_id.currency_id.round(
            self.new_payment_amount * (1.0 - pct),
        )

    def _new_service_component_payment(self):
        self.ensure_one()
        return self.lease_id.currency_id.round(
            self.new_payment_amount - self._new_lease_component_payment(),
        )

    @api.depends('modification_type', 'scope_decrease_pct',
                 'new_term_months', 'new_payment_amount', 'new_ibr',
                 'lease_id', 'lease_id.payment_service_pct',
                 'modification_date')
    def _compute_effects(self):
        for w in self:
            try:
                effects = w._effects()
            except Exception:  # noqa: BLE001
                w.rou_reduction = 0.0
                w.pl_amount = 0.0
                w.scope_gain_loss = 0.0
                w.remeasurement_delta = 0.0
                continue
            w.rou_reduction = -effects['rou_change']
            w.pl_amount = effects['pl_amount']
            w.scope_gain_loss = effects.get('scope_gain_loss', 0.0)
            w.remeasurement_delta = effects.get('remeasurement_delta', 0.0)

    def _effects(self):
        """Resolve the accounting effects of the modification, rounded to
        the company currency. Returns a dict with:

        * delta          - change in the lease liability (new - current);
        * rou_change      - signed change applied to the ROU asset
                            (negative reduces it);
        * pl_amount       - P&L effect (positive = gain, credit);
        * new_liability   - remeasured liability the schedule rebuilds on.

        Remeasurement (IFRS 16.39-43): the ROU moves with the liability;
        a decrease is bounded so the ROU never goes below zero, with the
        excess to P&L. Partial scope decrease (IFRS 16.45-46): the ROU is
        reduced proportionately and the difference against the liability
        reduction is the P&L gain / loss.
        """
        self.ensure_one()
        lease = self.lease_id
        rnd = lease.currency_id.round
        current_liability = lease._liability_balance_after_last_post()
        current_rou = lease._rou_carrying_amount()
        new_liability = rnd(self._compute_new_pv())

        if self.modification_type == 'scope_decrease':
            pct = (self.scope_decrease_pct or 0.0) / 100.0
            rou_reduction = rnd(current_rou * pct)
            liability_reduction = rnd(current_liability * pct)
            # IFRS 16.46(a) derecognition is measured proportionately before
            # the remaining liability is remeasured. Keeping the stages
            # separate prevents a changed discount rate/payment from being
            # mislabeled as the scope-decrease gain or loss.
            scope_gain_loss = rnd(liability_reduction - rou_reduction)
            remaining_liability = rnd(
                current_liability - liability_reduction,
            )
            remaining_rou = rnd(current_rou - rou_reduction)
            remeasurement_delta = rnd(
                new_liability - remaining_liability,
            )
            if remeasurement_delta >= 0:
                remeasurement_rou_change = remeasurement_delta
                floor_gain = 0.0
            else:
                decrease = -remeasurement_delta
                absorbed = min(remaining_rou, decrease)
                remeasurement_rou_change = -rnd(absorbed)
                floor_gain = rnd(decrease - absorbed)
            rou_change = rnd(-rou_reduction + remeasurement_rou_change)
            pl_amount = rnd(scope_gain_loss + floor_gain)
            return {
                'delta': rnd(new_liability - current_liability),
                'rou_change': rou_change,
                'pl_amount': pl_amount,
                'new_liability': new_liability,
                'current_rou': current_rou,
                'scope_gain_loss': scope_gain_loss,
                'remeasurement_delta': remeasurement_delta,
                'remeasurement_floor_gain': floor_gain,
            }

        # Remeasurement (rate / payment change).
        delta = rnd(new_liability - current_liability)
        if delta >= 0:
            # Increase (or no change): ROU rises by the full delta, no P&L.
            return {
                'delta': delta,
                'rou_change': delta,
                'pl_amount': 0.0,
                'new_liability': new_liability,
                'current_rou': current_rou,
            }
        # Decrease: ROU falls, floored at zero; excess to P&L (gain).
        decrease = -delta
        rou_absorbed = min(current_rou, decrease)
        excess = rnd(decrease - rou_absorbed)
        return {
            'delta': delta,
            'rou_change': -rnd(rou_absorbed),
            'pl_amount': excess,
            'new_liability': new_liability,
            'current_rou': current_rou,
        }

    def action_modify(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM eh_lease_modify_wizard WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset(['processed'])
        if self.processed:
            raise UserError(_(
                "This modification wizard has already been applied."
            ))
        lease = self.lease_id
        # Check the source record, not merely the unrestricted transient,
        # before reading balances or posting against a supplied lease id.
        lease._eh_check_access('write')
        lease._eh_validate_company_currency()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only EH accounting managers can modify leases.",
            ))
        lease._eh_lock_for_transition()
        self.invalidate_recordset([
            'current_liability', 'current_rou', 'new_liability', 'delta',
            'rou_reduction', 'pl_amount',
        ])
        if lease.state not in ('active', 'modified'):
            raise UserError(_(
                "Only active leases can be modified.",
            ))
        lease._check_remeasurement_supported(_("modified"))
        if self.new_term_months <= 0 or self.new_payment_amount <= 0:
            raise UserError(_(
                "Revised term and payment must be positive."
            ))
        if self.new_ibr < 0:
            raise UserError(_("Revised discount rate cannot be negative."))
        if self.modification_date > fields.Date.context_today(self):
            raise UserError(_(
                "A lease modification cannot be posted before its effective "
                "date. Process it on or after %(date)s.",
                date=self.modification_date,
            ))
        if self.new_term_months % CADENCE_MONTHS[lease.cadence]:
            raise UserError(_(
                "Revised term must be a whole multiple of the cadence."
            ))
        if self.modification_type == 'scope_decrease' and not (
                0 < self.scope_decrease_pct < 100):
            raise UserError(_(
                "A partial scope decrease must be greater than 0% and less "
                "than 100%; use termination for a full decrease."
            ))
        posted_dates = lease.schedule_line_ids.filtered(
            'is_posted',
        ).mapped('period_date')
        if (self.modification_date < lease.commencement_date
                or posted_dates
                and self.modification_date < max(posted_dates)):
            raise UserError(_(
                "Modification date cannot precede commencement or the latest "
                "posted lease period."
            ))
        _eh_validate_accounting_company(self, ('pl_account_id',))

        # Preserve full earned rows and recognise the ROU/effective-interest
        # stub through the modification date before remeasurement.
        lease._eh_post_accrued_through(self.modification_date)
        if lease.state not in ('active', 'modified'):
            raise UserError(_(
                "Lease term was already complete before the modification "
                "date; an ended lease cannot be remeasured.",
            ))
        lease.invalidate_recordset([
            'total_paid', 'total_interest', 'total_principal',
            'liability_balance',
        ])
        self.invalidate_recordset([
            'current_liability', 'current_rou', 'new_liability', 'delta',
            'rou_reduction', 'pl_amount',
        ])

        rnd = lease.currency_id.round
        effects = self._effects()
        delta = effects['delta']            # liability change (signed)
        rou_change = effects['rou_change']  # ROU change (signed)
        pl_amount = effects['pl_amount']    # P&L (positive = gain)

        if pl_amount and not self.pl_account_id:
            raise UserError(_(
                "This modification produces a P&L amount of %(amt)s; "
                "select a P/L account for the gain or loss "
                "(IFRS 16.39 ROU-floor excess or IFRS 16.46(a) scope-"
                "decrease difference).",
                amt=pl_amount,
            ))

        lines = []
        # Lease liability leg (Dr when it decreases, Cr when it increases).
        if delta > 0:
            lines.append((0, 0, {
                'name': _("Lease modification liability uplift %s",
                          lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': 0.0, 'credit': delta,
            }))
        elif delta < 0:
            lines.append((0, 0, {
                'name': _("Lease modification liability decrease %s",
                          lease.display_name),
                'account_id': lease.lease_liability_account_id.id,
                'debit': -delta, 'credit': 0.0,
            }))
        # ROU asset leg (Dr when it increases, Cr when it decreases).
        if rou_change > 0:
            lines.append((0, 0, {
                'name': _("Lease modification ROU uplift %s",
                          lease.display_name),
                'account_id': lease.rou_asset_account_id.id,
                'debit': rou_change, 'credit': 0.0,
            }))
        elif rou_change < 0:
            lines.append((0, 0, {
                'name': _("Lease modification ROU decrease %s",
                          lease.display_name),
                'account_id': lease.rou_asset_account_id.id,
                'debit': 0.0, 'credit': -rou_change,
            }))
        # P&L leg: a positive P&L amount is a gain (credit), negative a
        # loss (debit).
        if pl_amount > 0:
            lines.append((0, 0, {
                'name': _("Lease modification gain %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': 0.0, 'credit': pl_amount,
            }))
        elif pl_amount < 0:
            lines.append((0, 0, {
                'name': _("Lease modification loss %s", lease.display_name),
                'account_id': self.pl_account_id.id,
                'debit': -pl_amount, 'credit': 0.0,
            }))
        move = self.env['account.move']
        if lines:
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'date': self.modification_date,
                'journal_id': lease.journal_id.id,
                'ref': _("Lease modification %s", lease.display_name),
                'line_ids': lines,
            })
            move.action_post()

        # Update lease parameters and rebuild the remaining schedule. The
        # ROU carrying amount going into the rebuild moves by rou_change
        # (which already reflects the floor / proportionate reduction).
        posted_rou = sum(lease.schedule_line_ids.filtered(
            lambda l: l.is_posted,
        ).mapped('rou_amount'))
        lease_vals = {
            'payment_amount': self.new_payment_amount,
            'incremental_borrowing_rate': self.new_ibr,
            'current_measurement_term_months': self.new_term_months,
            'liability_initial_value': rnd(effects['new_liability']),
            'rou_initial_value': rnd(
                effects['current_rou'] + rou_change + posted_rou,
            ),
            'state': 'modified',
            'modification_count': lease.modification_count + 1,
            'last_modified_at': fields.Datetime.now(),
        }
        if move:
            lease_vals['modification_move_ids'] = [(4, move.id)]
        lease._eh_workflow_write(lease_vals)

        unposted = lease.schedule_line_ids.filtered(lambda l: not l.is_posted)
        unposted.sudo().unlink()
        self._build_modified_schedule(lease, effects)
        lease.message_post(
            body=_("Lease modified at %(date)s (%(kind)s): revised measurement "
                   "term=%(term)sm, "
                   "payment=%(pmt)s, IBR=%(ibr)s%%, liability delta=%(delta)s, "
                   "ROU change=%(rou)s, scope gain/loss=%(scope)s, "
                   "remeasurement delta=%(remeasure)s, P&L=%(pl)s. %(notes)s",
                   date=self.modification_date,
                   kind=self.modification_type,
                   term=self.new_term_months,
                   pmt=self.new_payment_amount,
                   ibr=self.new_ibr,
                   delta=delta, rou=rou_change, pl=pl_amount,
                   scope=effects.get('scope_gain_loss', 0.0),
                   remeasure=effects.get('remeasurement_delta', delta),
                   notes=self.notes or '/'),
        )
        self.sudo().write({'processed': True})
        return {'type': 'ir.actions.act_window_close'}

    def write(self, vals):
        if 'processed' in vals and not self.env.su:
            raise UserError(_("Processed status is server-owned."))
        return super().write(vals)

    def _build_modified_schedule(self, lease, effects):
        cadence = lease.cadence
        period_months = CADENCE_MONTHS[cadence]
        n = int(self.new_term_months // period_months)
        annual = self.new_ibr / 100.0
        r = (1.0 + annual) ** (period_months / 12.0) - 1.0
        pmt = self._new_lease_component_payment()
        service_pmt = self._new_service_component_payment()

        # ROU carrying amount to amortise over the revised term already
        # reflects the floor / proportionate reduction (rou_change).
        rou_remaining = lease.currency_id.round(
            effects['current_rou'] + effects['rou_change'],
        )
        rou_accumulated_at_mod = sum(
            lease.schedule_line_ids.filtered(
                lambda l: l.is_posted,
            ).mapped('rou_amount'),
        )

        posted_seqs = lease.schedule_line_ids.filtered(
            lambda l: l.is_posted,
        ).mapped('sequence')
        last_seq = max(posted_seqs) if posted_seqs else 0

        period_date = self._first_modified_period_date(lease)

        # Reuse the lease's consistent amortisation builder so the
        # modified schedule satisfies the same invariant: principal +
        # interest == payment_amount on every row, liability_close ==
        # liability_open - principal, and the last row trues up to zero.
        rows = lease._compute_amortisation_rows(
            opening_liability=effects['new_liability'], n=n, r=r, pmt=pmt,
        )
        payment_rows = []
        for row in rows:
            payment_rows.append({
                'period_date': period_date,
                **row,
                'service_amount': service_pmt,
            })
            period_date = self._next_period_date(period_date, period_months)
        lease._eh_create_monthly_rou_schedule(
            payment_rows=payment_rows,
            opening_liability=effects['new_liability'],
            rou_total=rou_remaining,
            rou_months=self.new_term_months,
            # The remeasurement date establishes the revised carrying amount;
            # consumption of that revised amount starts in the following
            # monthly period.
            first_rou_date=self._next_period_date(
                self._month_end(self.modification_date), 1,
            ),
            rou_accumulated_start=rou_accumulated_at_mod,
            sequence_start=last_seq,
        )

    def _first_modified_period_date(self, lease):
        period_months = CADENCE_MONTHS[lease.cadence]
        if lease.payment_timing == 'advance':
            return self._month_end(self.modification_date)
        d = self.modification_date + relativedelta(months=period_months)
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_date(self, current, months):
        nxt = current + relativedelta(months=months)
        return self._month_end(nxt)
