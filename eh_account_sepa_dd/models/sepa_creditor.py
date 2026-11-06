# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.creditor: per-company SEPA creditor identifier and journal.

A creditor identifier is the registration number a national authority
issues to a company that wants to collect via SEPA Direct Debit.  Its
country/check-digit prefix follows the ISO 7064 mod-97-10 algorithm;
the three-character national business code is excluded from the check.

One creditor record per (company, journal) pair so a multi-currency or
multi-bank business can hold multiple identifiers without contention.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)


def normalize_creditor_identifier(value):
    """Return canonical SEPA CI or raise ``ValidationError``.

    EPC CI checksum input excludes positions 5..7 (national business code),
    then applies IBAN-style alphanumeric expansion to ``national + CC00``.
    """
    identifier = ''.join((value or '').split()).upper()
    if not 8 <= len(identifier) <= 35:
        raise ValidationError(_(
            "SEPA creditor identifier must contain 8 to 35 characters.",
        ))
    if not (
        identifier.isascii()
        and identifier.isalnum()
        and identifier[:2].isalpha()
        and identifier[2:4].isdigit()
    ):
        raise ValidationError(_(
            "SEPA creditor identifier must start with a two-letter country "
            "code and two check digits, followed only by ASCII letters or "
            "digits.",
        ))
    checksum_input = identifier[7:] + identifier[:4]
    expanded = ''.join(
        str(ord(char) - 55) if char.isalpha() else char
        for char in checksum_input
    )
    if int(expanded) % 97 != 1:
        raise ValidationError(_(
            "SEPA creditor identifier failed ISO 7064 mod-97-10 "
            "check-digit validation.",
        ))
    return identifier


class EhSepaCreditor(models.Model):
    _name = 'eh.sepa.creditor'
    _description = "SEPA Direct Debit creditor (per company / journal)"
    _order = 'company_id, journal_id'
    _check_company_auto = True

    # Keep this field translatable.  Odoo stores translated fields as JSONB;
    # removing ``translate=True`` leaves upgraded databases with a JSONB
    # column while the ORM writes scalar text values.
    name = fields.Char(required=True, translate=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    journal_id = fields.Many2one(
        'account.journal', required=True,
        domain="[('type', '=', 'bank'), ('company_id', '=', company_id)]",
        ondelete='cascade', check_company=True,
    )
    creditor_identifier = fields.Char(
        required=True,
        help=(
            "SEPA Creditor Identifier (CI) issued by the company's "
            "national authority. Canonicalised to uppercase without "
            "spaces and checked using ISO 7064 mod-97-10."
        ),
    )
    creditor_name = fields.Char(
        required=True,
        help=(
            "Name to render in the Cdtr block of every PmtInf section. "
            "Capped at 70 characters per the scheme. Should match the "
            "name on the creditor identifier registration."
        ),
    )
    iban = fields.Char(
        string="Collection IBAN", required=True,
        help="Where collected funds land. Validated locally on save.",
    )
    bic = fields.Char(string="BIC / SWIFT")
    default_local_instrument = fields.Selection(
        [
            ('CORE', "CORE (consumer)"),
            ('B2B', "B2B (business-to-business)"),
        ],
        required=True,
        default=lambda self: (
            self.env.company.eh_sepa_dd_default_instrument or 'CORE'
        ),
        help=(
            "Default scheme variant. CORE is the consumer scheme. B2B "
            "is for collections from business debtors and gives no "
            "refund right. Withdrawn COR1 is deliberately unavailable. "
            "Default is seeded from the company's Heritage settings."
        ),
    )
    batch_booking = fields.Boolean(
        string="Batch booking",
        default=False,
        help=(
            "Request one consolidated account-statement booking for each "
            "PAIN.008 payment-information block. Bank support varies."
        ),
    )
    pre_notification_days = fields.Integer(
        default=14,
        help=(
            "Minimum days between a pre-notification (sent to the "
            "debtor) and the collection date. Default 14 follows the "
            "scheme rule for first / one-off collections; many "
            "banks accept fewer days for recurring once a mandate is "
            "active."
        ),
    )
    notes = fields.Text()

    _unique_per_company_journal = models.Constraint(
        'unique(company_id, journal_id)',
        'Only one SEPA creditor per company per journal.',
    )

    @api.constrains('iban')
    def _check_iban(self):
        for rec in self:
            try:
                validate_iban(rec.iban)
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Collection IBAN failed validation: %s",
                ) % str(exc))

    @api.constrains('bic')
    def _check_bic(self):
        for rec in self:
            if not rec.bic:
                continue
            try:
                validate_bic(rec.bic)
            except BicValidationError as exc:
                raise ValidationError(_(
                    "BIC failed validation: %s",
                ) % str(exc))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('creditor_identifier'):
                vals['creditor_identifier'] = normalize_creditor_identifier(
                    vals['creditor_identifier'],
                )
            if vals.get('iban'):
                try:
                    vals['iban'] = validate_iban(vals['iban'])
                except IbanValidationError as exc:
                    raise ValidationError(_(
                        "Collection IBAN failed validation: %s",
                    ) % str(exc))
            if vals.get('bic'):
                try:
                    vals['bic'] = validate_bic(vals['bic'])
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "BIC failed validation: %s",
                    ) % str(exc))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('creditor_identifier'):
            vals['creditor_identifier'] = normalize_creditor_identifier(
                vals['creditor_identifier'],
            )
        if vals.get('iban'):
            try:
                vals['iban'] = validate_iban(vals['iban'])
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Collection IBAN failed validation: %s",
                ) % str(exc))
        if vals.get('bic'):
            try:
                vals['bic'] = validate_bic(vals['bic'])
            except BicValidationError as exc:
                raise ValidationError(_(
                    "BIC failed validation: %s",
                ) % str(exc))
        return super().write(vals)

    @api.constrains('creditor_identifier')
    def _check_creditor_identifier(self):
        for rec in self:
            if rec.creditor_identifier:
                normalize_creditor_identifier(rec.creditor_identifier)

    @api.constrains('creditor_name')
    def _check_name_length(self):
        for rec in self:
            if rec.creditor_name and len(rec.creditor_name) > 70:
                raise ValidationError(_(
                    "Creditor name is capped at 70 characters per the "
                    "SEPA scheme; got %d.",
                ) % len(rec.creditor_name))
