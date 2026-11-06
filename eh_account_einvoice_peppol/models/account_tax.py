# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
EN 16931 tax category configuration on account.tax.

Peppol BIS Billing 3.0 (and EN 16931 generally) classify every taxed
line into a UNTDID 5305 category:

* S  -- standard rate
* Z  -- zero rated goods
* E  -- exempt from tax
* AE -- VAT reverse charge
* G  -- free export item, tax not charged
* O  -- services outside scope of tax

These are distinct semantics that a rate alone cannot recover: a
zero-rated supply (Z) and an exempt supply (E) can both carry a 0 rate
but map to different categories, and an exempt or reverse-charge supply
requires an exemption reason (EN 16931 rules BR-E-10, BR-AE-10, BR-G-10,
BR-O-10). This model lets an operator set the category explicitly on the
tax. When left blank it defaults to the historical rate rule (zero rate
-> Z, otherwise S), so existing configurations behave unchanged.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


EN16931_TAX_CATEGORIES = [
    ('S', 'S - Standard rate'),
    ('Z', 'Z - Zero rated goods'),
    ('E', 'E - Exempt from tax'),
    ('AE', 'AE - VAT reverse charge'),
    ('G', 'G - Free export item, tax not charged'),
    ('O', 'O - Services outside scope of tax'),
]

# Categories that EN 16931 requires to carry an exemption reason.
_REASON_REQUIRED_CATEGORIES = ('E', 'AE', 'G', 'O')


class AccountTax(models.Model):
    _inherit = 'account.tax'

    eh_edi_tax_category = fields.Selection(
        EN16931_TAX_CATEGORIES,
        string="EN 16931 tax category",
        help=(
            "UNTDID 5305 tax category emitted on e-invoices for lines that "
            "use this tax. Leave blank to derive it from the rate (zero "
            "rate -> Z, otherwise S). Set it explicitly to distinguish "
            "exempt (E), reverse charge (AE), export (G) or out of scope "
            "(O) supplies, which a rate alone cannot tell apart."
        ),
    )
    eh_edi_tax_exemption_reason = fields.Char(
        string="EN 16931 exemption reason",
        help=(
            "Human readable reason why no tax is charged. Required by "
            "EN 16931 for exempt (E), reverse charge (AE), export (G) and "
            "out of scope (O) categories. Emitted as TaxExemptionReason in "
            "the tax breakdown."
        ),
    )

    def eh_edi_resolve_category(self):
        """Return the EN 16931 category code for this tax.

        Prefers the explicit eh_edi_tax_category, then any localization
        override (the FR e-reporting hook eh_fr_einv_category), and
        finally the historical rate rule.
        """
        self.ensure_one()
        if self.eh_edi_tax_category:
            return self.eh_edi_tax_category
        override = getattr(self, 'eh_fr_einv_category', None)
        if override:
            return override
        return 'Z' if (self.amount or 0.0) == 0.0 else 'S'

    @api.constrains('eh_edi_tax_category', 'eh_edi_tax_exemption_reason')
    def _check_eh_edi_exemption_reason(self):
        """EN 16931 requires an exemption reason for E, AE, G and O."""
        for tax in self:
            if (tax.eh_edi_tax_category in _REASON_REQUIRED_CATEGORIES
                    and not (tax.eh_edi_tax_exemption_reason or '').strip()):
                raise ValidationError(_(
                    "Tax %(name)s uses EN 16931 category %(cat)s, which "
                    "requires an exemption reason. Fill in the EN 16931 "
                    "exemption reason field.",
                    name=tax.display_name,
                    cat=tax.eh_edi_tax_category,
                ))
