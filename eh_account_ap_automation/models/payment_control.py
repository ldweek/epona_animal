# -*- encoding: utf-8 -*-
"""Enforce AP release controls at every standard settlement boundary."""

from collections import defaultdict

from odoo import api, models


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def _create_payments(self):
        self.ensure_one()
        self.line_ids.move_id._eh_assert_released_for_payment()
        return super()._create_payments()


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def _eh_ap_source_moves(self):
        sources = (
            self.mapped('invoice_ids')
            if 'invoice_ids' in self._fields
            else self.env['account.move']
        )
        # ERP Heritage batch payments expose their reviewed source documents
        # on this optional field.  Checking by field presence keeps this AP
        # module independent while still enforcing the integration whenever
        # the batch module is installed.
        if 'eh_source_move_ids' in self._fields:
            sources |= self.mapped('eh_source_move_ids')
        return sources

    def _eh_assert_ap_sources_released(self):
        self._eh_ap_source_moves()._eh_assert_released_for_payment()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            payments = super().create(vals_list)
            payments._eh_assert_ap_sources_released()
        return payments

    def write(self, vals):
        source_fields = set()
        if 'invoice_ids' in self._fields:
            source_fields.add('invoice_ids')
        if 'eh_source_move_ids' in self._fields:
            source_fields.add('eh_source_move_ids')
        if not source_fields.intersection(vals):
            return super().write(vals)
        with self.env.cr.savepoint():
            result = super().write(vals)
            self._eh_assert_ap_sources_released()
        return result

    def action_post(self):
        self._eh_assert_ap_sources_released()
        return super().action_post()

    def action_validate(self):
        self._eh_assert_ap_sources_released()
        parent = getattr(super(), 'action_validate', None)
        # Odoo 16/17 expose only action_post; preserve one guarded accounting
        # boundary when callers use the newer alias against an older series.
        return parent() if parent else self.action_post()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @staticmethod
    def _eh_reversal_number(value):
        return '0' if not value else format(value, '.16g')

    @staticmethod
    def _eh_reversal_signature(move):
        totals = defaultdict(lambda: [0.0, 0.0])
        for line in move.line_ids:
            key = (
                line.account_id.id,
                line.currency_id.id or 0,
                line.partner_id.id or 0,
            )
            totals[key][0] += line.balance
            totals[key][1] += line.amount_currency
        return {
            key: (
                AccountMoveLine._eh_reversal_number(values[0]),
                AccountMoveLine._eh_reversal_number(values[1]),
            )
            for key, values in totals.items()
        }

    def _eh_is_bill_reversal_only(self, bill):
        counterparts = (self.move_id - bill)
        if len(counterparts) != 1:
            return False
        reversal = counterparts
        if (
                reversal.state != 'posted'
                or reversal.company_id != bill.company_id
                or reversal.reversed_entry_id != bill):
            return False
        bill_signature = self._eh_reversal_signature(bill)
        reversal_signature = self._eh_reversal_signature(reversal)
        return {
            key: (
                self._eh_reversal_number(-float(values[0])),
                self._eh_reversal_number(-float(values[1])),
            )
            for key, values in bill_signature.items()
        } == reversal_signature

    def _eh_assert_ap_reconciliation_allowed(self):
        if self and not self.env.su:
            self._eh_check_access('read')
        bills = self.move_id.filtered(
            lambda move: move.move_type == 'in_invoice'
            and move.state == 'posted',
        )
        blocked_candidates = bills.filtered(
            lambda bill: not self._eh_is_bill_reversal_only(bill),
        )
        blocked_candidates._eh_assert_released_for_payment()
        return True

    def reconcile(self):
        self._eh_assert_ap_reconciliation_allowed()
        return super().reconcile()


class AccountPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    @api.model_create_multi
    def create(self, vals_list):
        MoveLine = self.env['account.move.line']
        for vals in vals_list:
            lines = MoveLine.browse([
                line_id for line_id in (
                    vals.get('debit_move_id'), vals.get('credit_move_id'),
                ) if line_id
            ]).exists()
            lines._eh_assert_ap_reconciliation_allowed()
        return super().create(vals_list)
