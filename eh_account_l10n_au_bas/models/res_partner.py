# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: Australian Business Number with mod-89 validation.

Adds eh_au_abn and eh_au_gst_registered. The ABN is validated at write
time using the standalone validator in tools/abn_validator.py; storage
is the canonical 11-digit form (whitespace and punctuation stripped).

This is independent of any e-invoicing module: the ABN is needed for
BAS reporting, partner identity verification, and the W4 (no-ABN
withholding) check, regardless of whether the site sends PEPPOL.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.eh_account_l10n_au_bas.tools.abn_validator import (
    validate_abn, normalise_abn, AbnValidationError, is_valid_abn,
)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    eh_au_abn = fields.Char(
        string="ABN",
        help=(
            "Australian Business Number. 11 digits with mod-89 weighted "
            "checksum. This is a synchronized Australian alias of Odoo's "
            "standard VAT field: changing either valid ABN updates both. "
            "Whitespace and punctuation are stripped on save."
        ),
    )
    eh_au_gst_registered = fields.Boolean(
        string="GST registered",
        help=(
            "Classification supplied by the operator. It does not calculate "
            "or post W4 no-ABN withholding; that amount remains a reviewed "
            "BAS manual input."
        ),
    )
    eh_au_abn_valid = fields.Boolean(
        compute='_compute_eh_au_abn_valid',
        store=False,
        help=(
            "Live indicator that the stored ABN passes the mod-89 "
            "checksum. Surfaces on the partner form so a typo is "
            "visible without saving."
        ),
    )

    @api.depends('eh_au_abn')
    def _compute_eh_au_abn_valid(self):
        for partner in self:
            partner.eh_au_abn_valid = bool(
                partner.eh_au_abn and is_valid_abn(partner.eh_au_abn),
            )

    @api.constrains('eh_au_abn')
    def _check_eh_au_abn(self):
        for partner in self:
            if not partner.eh_au_abn:
                continue
            try:
                validate_abn(partner.eh_au_abn)
            except AbnValidationError as exc:
                raise ValidationError(_(
                    "%(partner)s: ABN validation failed. %(detail)s",
                    partner=partner.display_name,
                    detail=str(exc),
                ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_prepare_abn_alias_values(vals)
        return super().create(vals_list)

    def write(self, vals):
        prepared = dict(vals)
        self._eh_prepare_abn_alias_values(prepared)
        if 'vat' in prepared and prepared.get('vat'):
            vat_abn = self._eh_valid_abn_or_false(prepared['vat'])
            if not vat_abn and self.filtered('eh_au_abn'):
                raise ValidationError(_(
                    "A partner with an ABN alias cannot be assigned a "
                    "different non-ABN VAT value. Clear the ABN first."
                ))
        return super().write(prepared)

    @api.model
    def _eh_valid_abn_or_false(self, value):
        canonical = normalise_abn(value)
        return canonical if is_valid_abn(canonical) else False

    @api.model
    def _eh_prepare_abn_alias_values(self, vals):
        has_abn = 'eh_au_abn' in vals
        has_vat = 'vat' in vals
        abn = False
        vat_abn = False
        if has_abn and vals.get('eh_au_abn'):
            abn = normalise_abn(vals['eh_au_abn'])
            vals['eh_au_abn'] = abn
        elif has_abn:
            vals['eh_au_abn'] = False
        if has_vat and vals.get('vat'):
            vat_abn = self._eh_valid_abn_or_false(vals['vat'])
            if vat_abn:
                vals['vat'] = vat_abn

        if has_abn and has_vat and vals.get('vat') and vat_abn != abn:
            raise ValidationError(_(
                "ABN and standard VAT values conflict. Supply one valid "
                "Australian Business Number or matching values."
            ))
        if has_abn:
            vals['vat'] = abn or False
        elif has_vat:
            vals['eh_au_abn'] = vat_abn or False
        return vals
