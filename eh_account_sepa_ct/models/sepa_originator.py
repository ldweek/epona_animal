# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.originator: per-bank-journal originator details for SEPA CT.

Banks accept PAIN.001 files only when the InitiatingParty and the
debtor block on each PmtInf match the account holder of the IBAN
declared as the source. We collect those once per journal so the
batch export does not have to re-derive them every run.

The originator validates its IBAN and BIC at write time (not at export
time) so configuration errors surface in the configuration screen
where they are fixable, not in the middle of a payment run.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)


class EhSepaOriginator(models.Model):
    _name = 'eh.sepa.originator'
    _description = "SEPA originator (per journal)"
    _order = 'journal_id'
    _rec_name = 'journal_id'

    journal_id = fields.Many2one(
        'account.journal', required=True,
        domain="[('type', '=', 'bank')]",
        ondelete='cascade',
        help=(
            "Bank journal whose IBAN drives the Dbtr block of every "
            "PmtInf section. One originator per journal; reuse across "
            "every batch sourced from this journal."
        ),
    )
    company_id = fields.Many2one(
        related='journal_id.company_id', store=True, readonly=True,
    )
    initiating_party_name = fields.Char(
        required=True,
        help=(
            "Goes into GrpHdr/InitgPty/Nm. The exact legal name the "
            "bank has on file for the account holder. Capped at 70 "
            "characters per the scheme."
        ),
    )
    initiating_party_identifier = fields.Char(
        help=(
            "Optional identifier for InitgPty/Id/OrgId/Othr/Id. Capped "
            "at 35 characters. Used by some banks to verify the file "
            "originator against a customer reference."
        ),
    )
    iban = fields.Char(
        string="IBAN",
        required=True,
        help=(
            "Source IBAN. Validated locally at write time using mod-97 "
            "per ISO 13616; the export refuses to render if this fails."
        ),
    )
    bic = fields.Char(
        string="BIC / SWIFT",
        help=(
            "Optional. When omitted the export emits 'NOTPROVIDED', "
            "which signals the bank to look up the BIC from the IBAN. "
            "Some banks reject NOTPROVIDED; if yours does, fill this in."
        ),
    )
    requested_execution_offset_days = fields.Integer(
        default=0,
        help=(
            "Calendar days to add to the batch payment_date when computing "
            "ReqdExctnDt. 0 means same-day and 1 means the next calendar "
            "day. The export does not perform business-day adjustment; set "
            "the offset to match your bank's processing rules."
        ),
    )
    pain_001_version = fields.Selection(
        [
            ('03', "PAIN.001.001.03 (legacy SEPA rulebook)"),
            ('09', "PAIN.001.001.09 (current EU IG, 2024+)"),
        ],
        default='09',
        required=True,
        help=(
            "ISO 20022 PAIN.001 version emitted for this journal. "
            "PAIN.001.001.03 is the long-standing SEPA file. "
            "PAIN.001.001.09 is the EU Implementation Guidelines "
            "version that many banks require from 2024 onwards. "
            "Confirm with your bank which version they accept; the "
            "switch is namespace + a few element renames, not a data "
            "migration."
        ),
    )
    segregation_policy = fields.Selection(
        [
            ('two_person', "Two-person maker/checker"),
            ('three_person', "Three-person strict"),
        ],
        default='two_person',
        required=True,
        help=(
            "Two-person policy lets confirmer and poster split approval and "
            "file generation, but the person who approved the current SEPA "
            "source fingerprint still cannot export it. Three-person policy "
            "also requires the exporter to differ from both confirmer and "
            "poster."
        ),
    )
    charge_bearer = fields.Selection(
        [
            ('SLEV', "Service level (SLEV)"),
            ('SHAR', "Shared charges (SHAR)"),
            ('DEBT', "Debtor charges (DEBT)"),
            ('CRED', "Creditor charges (CRED)"),
        ],
        default='SLEV',
        required=True,
        help="PAIN.001 ChrgBr value emitted for this journal.",
    )
    instruction_priority = fields.Selection(
        [('NORM', "Normal"), ('HIGH', "High")],
        help="Optional PAIN.001 InstrPrty value.",
    )
    batch_booking = fields.Boolean(
        default=False,
        help="Request one aggregate account statement entry from the bank.",
    )

    notes = fields.Text()

    _unique_journal = models.Constraint(
        'unique(journal_id)',
        'Only one SEPA originator per bank journal.',
    )

    @api.constrains('iban')
    def _check_iban(self):
        for rec in self:
            try:
                validate_iban(rec.iban)
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "IBAN validation failed: %s",
                ) % str(exc))

    @api.constrains('journal_id', 'iban')
    def _check_journal_bank_account(self):
        # Legacy rows are also checked on every confirmation/export. Keeping
        # this constraint here makes new or edited configuration fail early.
        for rec in self:
            if rec.journal_id.bank_account_id:
                rec._eh_assert_journal_bank_account()

    def _eh_assert_journal_bank_account(self):
        """Prove the PAIN debtor IBAN is the GL journal's bank account."""
        self.ensure_one()
        journal_bank = self.journal_id.bank_account_id
        if not journal_bank:
            raise ValidationError(_(
                "Bank journal %s has no bank account. Configure its company "
                "bank account before approving or exporting SEPA payments.",
                self.journal_id.display_name,
            ))
        if journal_bank.company_id and journal_bank.company_id != self.company_id:
            raise ValidationError(_(
                "The journal bank account belongs to another company."
            ))
        if (
            journal_bank.partner_id.commercial_partner_id
            != self.company_id.partner_id.commercial_partner_id
        ):
            raise ValidationError(_(
                "The journal bank account must belong to the journal company."
            ))
        try:
            journal_iban = validate_iban(journal_bank.acc_number)
            originator_iban = validate_iban(self.iban)
        except IbanValidationError as exc:
            raise ValidationError(_(
                "Journal/originator IBAN validation failed: %s",
            ) % str(exc))
        if journal_iban != originator_iban:
            raise ValidationError(_(
                "SEPA originator IBAN %(originator)s does not match bank "
                "journal %(journal)s account %(journal_iban)s.",
                originator=originator_iban,
                journal=self.journal_id.display_name,
                journal_iban=journal_iban,
            ))
        return journal_bank

    @api.constrains('bic', 'pain_001_version')
    def _check_bic(self):
        for rec in self:
            if not rec.bic:
                continue
            try:
                validate_bic(rec.bic, version=rec.pain_001_version)
            except BicValidationError as exc:
                raise ValidationError(_(
                    "BIC validation failed: %s",
                ) % str(exc))

    @api.constrains('requested_execution_offset_days')
    def _check_requested_execution_offset_days(self):
        for rec in self:
            rec._eh_validated_requested_execution_offset_days()

    def _eh_validated_requested_execution_offset_days(self):
        """Validate on every use as well as on ORM writes.

        A pre-upgrade row can contain an out-of-range value without triggering
        a Python constraint during schema update. Never let that legacy value
        silently move a bank instruction backwards or arbitrarily far ahead.
        """
        self.ensure_one()
        offset = self.requested_execution_offset_days or 0
        if not 0 <= offset <= 365:
            raise ValidationError(_(
                "Requested execution offset must be between 0 and 365 "
                "calendar days."
            ))
        return offset

    @api.model_create_multi
    def create(self, vals_list):
        # Normalise IBAN/BIC into canonical form BEFORE the row is
        # persisted so the constrains methods stay strictly read-only
        # (writing inside @api.constrains causes re-entry crashes in
        # Odoo 19's _validate_fields path).
        for vals in vals_list:
            if vals.get('iban'):
                try:
                    vals['iban'] = validate_iban(vals['iban'])
                except IbanValidationError as exc:
                    raise ValidationError(_(
                        "IBAN validation failed: %s",
                    ) % str(exc))
            if vals.get('bic'):
                try:
                    vals['bic'] = validate_bic(
                        vals['bic'], version=vals.get('pain_001_version', '09'),
                    )
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "BIC validation failed: %s",
                    ) % str(exc))
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('iban'):
            try:
                vals['iban'] = validate_iban(vals['iban'])
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "IBAN validation failed: %s",
                ) % str(exc))
        if vals.get('bic') or ('pain_001_version' in vals and self.bic):
            for rec in self:
                bic = vals.get('bic', rec.bic)
                version = vals.get('pain_001_version', rec.pain_001_version)
                if not bic:
                    continue
                try:
                    canonical = validate_bic(bic, version=version)
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "BIC validation failed: %s",
                    ) % str(exc))
                if 'bic' in vals:
                    vals['bic'] = canonical
        return super().write(vals)

    @api.constrains('initiating_party_name')
    def _check_name_length(self):
        for rec in self:
            if rec.initiating_party_name and len(rec.initiating_party_name) > 70:
                raise ValidationError(_(
                    "InitgPty/Nm must be at most 70 characters per the "
                    "SEPA scheme; got %d.",
                ) % len(rec.initiating_party_name))
