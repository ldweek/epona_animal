# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""PDC-specific bank-journal accounting configuration.

Bank-statement suspense is not a cheque-in-hand ledger and the bank GL is
not recognised until a statement confirms settlement.  PDC therefore uses
dedicated incoming/outgoing transit accounts while clearance and dishonour
charges flow through selected Odoo outstanding-receipt/payment method lines.
Those outstanding accounts are then cleared by normal bank reconciliation.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    eh_pdc_incoming_transit_account_id = fields.Many2one(
        'account.account',
        string="Incoming Cheques in Transit",
        check_company=True,
        copy=False,
        ondelete='restrict',
        domain="[('account_type', '=', 'asset_current'), "
               "('reconcile', '=', True)]",
        help="Dedicated reconcilable current-asset account holding received "
             "cheques from presentation until bank clearance. It must not "
             "be the bank GL, bank-statement suspense, or outstanding "
             "receipts account.",
    )
    eh_pdc_outgoing_transit_account_id = fields.Many2one(
        'account.account',
        string="Issued Cheques in Transit",
        check_company=True,
        copy=False,
        ondelete='restrict',
        domain="[('account_type', '=', 'liability_current'), "
               "('reconcile', '=', True)]",
        help="Dedicated reconcilable current-liability account holding "
             "issued cheques from presentation until bank clearance. It "
             "must not be the bank GL, bank-statement suspense, or "
             "outstanding payments account.",
    )
    eh_pdc_inbound_payment_method_line_id = fields.Many2one(
        'account.payment.method.line',
        string="PDC Outstanding Receipts Method",
        check_company=True,
        copy=False,
        ondelete='restrict',
        domain="[('journal_id', '=', id), ('payment_type', '=', 'inbound'), "
               "('payment_account_id', '!=', False)]",
        help="Journal payment-method line whose outstanding-receipts "
             "account receives a cleared customer cheque. Bank-statement "
             "reconciliation later transfers that balance to the bank GL.",
    )
    eh_pdc_outbound_payment_method_line_id = fields.Many2one(
        'account.payment.method.line',
        string="PDC Outstanding Payments Method",
        check_company=True,
        copy=False,
        ondelete='restrict',
        domain="[('journal_id', '=', id), ('payment_type', '=', 'outbound'), "
               "('payment_account_id', '!=', False)]",
        help="Journal payment-method line whose outstanding-payments "
             "account receives a cleared issued cheque or bank dishonour "
             "charge. Bank-statement reconciliation later transfers that "
             "balance to the bank GL.",
    )

    eh_pdc_bounce_charge_account_id = fields.Many2one(
        'account.account',
        string="Bounce Charges Account",
        check_company=True,
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost'))]",
        help="Expense account debited when a post dated cheque bounces "
             "with bank charges. When empty, the company level account is "
             "used; when neither is set, bounce charges stay informational "
             "on the cheque and no charge entry is posted.",
    )

    @api.constrains(
        'default_account_id',
        'suspense_account_id',
        'eh_pdc_incoming_transit_account_id',
        'eh_pdc_outgoing_transit_account_id',
        'eh_pdc_inbound_payment_method_line_id',
        'eh_pdc_outbound_payment_method_line_id',
    )
    def _check_eh_pdc_account_configuration(self):
        """Reject aliases that collapse distinct bank-control stages."""
        for journal in self:
            transit_specs = (
                (
                    journal.eh_pdc_incoming_transit_account_id,
                    'asset_current',
                    _("Incoming Cheques in Transit"),
                ),
                (
                    journal.eh_pdc_outgoing_transit_account_id,
                    'liability_current',
                    _("Issued Cheques in Transit"),
                ),
            )
            for account, expected_type, label in transit_specs:
                if not account:
                    continue
                if account.account_type != expected_type:
                    raise ValidationError(_(
                        "%(label)s must use account type %(type)s.",
                        label=label,
                        type=expected_type,
                    ))
                if not account.reconcile:
                    raise ValidationError(_(
                        "%(label)s must be reconcilable so clearance and FX "
                        "differences cannot remain stranded.",
                        label=label,
                    ))
                forbidden = (
                    journal.default_account_id
                    | journal.suspense_account_id
                )
                if account in forbidden:
                    raise ValidationError(_(
                        "%(label)s must be distinct from the bank GL and "
                        "bank-statement suspense account.",
                        label=label,
                    ))

            method_specs = (
                (
                    journal.eh_pdc_inbound_payment_method_line_id,
                    'inbound',
                    journal.eh_pdc_incoming_transit_account_id,
                ),
                (
                    journal.eh_pdc_outbound_payment_method_line_id,
                    'outbound',
                    journal.eh_pdc_outgoing_transit_account_id,
                ),
            )
            for method_line, direction, transit in method_specs:
                if not method_line:
                    continue
                if (
                    method_line.journal_id != journal
                    or method_line.payment_type != direction
                ):
                    raise ValidationError(_(
                        "PDC %(direction)s payment method must belong to "
                        "journal %(journal)s and have matching direction.",
                        direction=direction,
                        journal=journal.display_name,
                    ))
                outstanding = method_line.payment_account_id
                if not outstanding or not outstanding.reconcile:
                    raise ValidationError(_(
                        "PDC %(direction)s payment method needs a "
                        "reconcilable outstanding account.",
                        direction=direction,
                    ))
                forbidden = (
                    journal.default_account_id
                    | journal.suspense_account_id
                    | transit
                )
                if outstanding in forbidden:
                    raise ValidationError(_(
                        "PDC %(direction)s outstanding account must be "
                        "distinct from bank GL, bank-statement suspense, "
                        "and cheque-transit accounts.",
                        direction=direction,
                    ))
