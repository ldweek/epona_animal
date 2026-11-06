# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Wizard to replace a bounced cheque with a new one.

Creates a new eh.cheque record carrying the same direction, partner,
journal, and amount (defaulted, editable). Links the original via
replaces_id and stamps the original as state=replaced.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhChequeReplaceWizard(models.TransientModel):
    _name = 'eh.cheque.replace.wizard'
    _description = "Replace Bounced Cheque"

    cheque_id = fields.Many2one(
        'eh.cheque', required=True, readonly=True,
    )
    new_cheque_number = fields.Char(string="New Cheque Number", required=True)
    new_value_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    new_amount = fields.Monetary(required=True)
    new_book_id = fields.Many2one(
        'eh.cheque.book',
        domain="[('state', '=', 'in_use')]",
    )
    currency_id = fields.Many2one(
        related='cheque_id.currency_id', readonly=True,
    )
    notes = fields.Text()

    @api.constrains('new_amount')
    def _check_new_amount(self):
        for wizard in self:
            if wizard.new_amount <= 0:
                raise ValidationError(_(
                    "Replacement cheque amount must be greater than zero.",
                ))

    def action_confirm(self):
        self.ensure_one()
        self._eh_check_access('write')
        original = self.cheque_id
        # The wizard target is client-controlled. Prove access/company and
        # manager authority before any sudo create or serial consumption.
        original._eh_authorize_cheque_action()
        if original.state != 'bounced':
            raise UserError(_(
                "Only bounced cheques can be replaced.",
            ))
        new_vals = {
            'direction': original.direction,
            'partner_id': original.partner_id.id,
            'journal_id': original.journal_id.id,
            'cheque_number': self.new_cheque_number,
            'amount': self.new_amount or original.amount,
            'currency_id': original.currency_id.id,
            'company_id': original.company_id.id,
            'issue_date': fields.Date.context_today(self),
            'value_date': self.new_value_date,
            'issuer_bank_name': original.issuer_bank_name,
            'issuer_account': original.issuer_account,
            'invoice_id': original.invoice_id.id,
            'notes': self.notes or False,
            'replaces_id': original.id,
        }
        if original.direction == 'outgoing':
            book = self.new_book_id or original.book_id
            if not book:
                raise UserError(_(
                    "An outgoing replacement requires an active cheque book.",
                ))
            book._eh_check_access('write')
            if book.company_id != original.company_id \
                    or book.journal_id != original.journal_id:
                raise UserError(_(
                    "The replacement cheque book must belong to the same "
                    "company and bank journal as the original cheque.",
                ))
            if book.state != 'in_use':
                raise UserError(_(
                    "The replacement cheque book is not active.",
                ))
            new_vals['book_id'] = book.id
        # ``replaces_id`` is an immutable audit link. The wizard is the only
        # sanctioned creator of that link, so establish it under sudo while
        # retaining the real user for create_uid and workflow stamps.
        new_cheque = self.env['eh.cheque'].sudo().create(new_vals)
        new_cheque.action_register()
        original._mark_replaced_by(new_cheque)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.cheque',
            'res_id': new_cheque.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
