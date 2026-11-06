# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Per-bank CSV import profile.

A profile captures the column layout and decimal/date conventions of a
specific bank's CSV export. Set up once per bank, reused on every import
from that bank.

OFX and CAMT.053 imports do not need a profile because the format
self-describes the columns, but they can still be paired with a profile
to scope downstream defaults (currency, default journal).
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhBankStatementImportProfile(models.Model):
    _name = 'eh.account.bank.statement.import.profile'
    _description = "Bank statement import profile"
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    journal_id = fields.Many2one(
        'account.journal',
        string="Default Bank Journal",
        domain="[('type', 'in', ['bank', 'cash'])]",
        help="Default journal selected when this profile is chosen in the import wizard. The user can override per import.",
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_code = fields.Char(
        size=3,
        help="ISO 4217 currency code, e.g. AUD. Used as a fallback when the file does not declare a currency.",
    )

    # ---- CSV-specific fields ----

    csv_delimiter = fields.Char(default=',', size=1)
    csv_quotechar = fields.Char(default='"', size=1)
    csv_encoding = fields.Char(default='utf-8-sig')
    csv_header_rows = fields.Integer(
        default=1,
        help="Number of header rows to skip at the top of the file.",
    )

    decimal_separator = fields.Selection(
        [
            ('.', "Period (1234.56)"),
            (',', "Comma (1234,56)"),
        ],
        default='.', required=True,
    )
    date_format = fields.Char(
        default='%Y-%m-%d',
        help="Python strptime format for the date column. Leave blank to try common formats automatically.",
    )

    col_date = fields.Integer(default=0, required=True,
        help="Zero-based index of the date column.")
    col_amount = fields.Integer(default=1,
        help="Zero-based index of the signed amount column. Use -1 if the bank ships separate debit and credit columns.")
    col_debit = fields.Integer(default=-1,
        help="Zero-based index of the debit column. Use -1 if amount is single-column.")
    col_credit = fields.Integer(default=-1,
        help="Zero-based index of the credit column. Use -1 if amount is single-column.")
    col_ref = fields.Integer(default=-1,
        help="Zero-based index of the payment reference column.")
    col_narration = fields.Integer(default=-1,
        help="Zero-based index of the narration / memo column.")
    col_partner = fields.Integer(default=-1,
        help="Zero-based index of the counter party name column.")

    notes = fields.Text()

    _check_amount_columns = models.Constraint(
        'CHECK (col_amount >= 0 OR (col_debit >= 0 AND col_credit >= 0))',
        'Either col_amount must be set, or both col_debit and col_credit must be set.',
    )

    @api.constrains('journal_id', 'company_id')
    def _check_journal_company(self):
        for rec in self:
            if rec.journal_id and rec.journal_id.company_id != rec.company_id:
                raise ValidationError(_(
                    "The default journal must belong to the profile company."
                ))

    @api.constrains('csv_delimiter', 'csv_quotechar')
    def _check_csv_chars(self):
        for rec in self:
            if rec.csv_delimiter and len(rec.csv_delimiter) != 1:
                raise ValidationError(_(
                    "csv_delimiter must be a single character.",
                ))
            if rec.csv_quotechar and len(rec.csv_quotechar) != 1:
                raise ValidationError(_(
                    "csv_quotechar must be a single character.",
                ))

    @api.constrains('currency_code')
    def _check_currency(self):
        for rec in self:
            if rec.currency_code and len(rec.currency_code) != 3:
                raise ValidationError(_(
                    "currency_code must be the 3-letter ISO 4217 code.",
                ))
