# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
BAS label definition. One row per ATO field on the form.

The data file ships 33 worksheet rows spanning G, GST, PAYG-W, PAYG-I,
FBT, fuel-credit, WET and LCT labels. G1, 1A and 1B use Odoo's AU tax-grid
classification; W5 sums W2, W3 and W4; deployment-specific automatic rows
may map one or more `account.tax` tags through `tag_ids`; remaining rows are
reviewed manual inputs.

The `aggregation` selection determines whether the label sums:

* `base_credit` -- sum of base amount on credit side (income/sales)
* `base_debit` -- sum of base amount on debit side (expenses/acquisitions)
* `tax_credit` -- sum of tax amount on credit side (GST collected)
* `tax_debit` -- sum of tax amount on debit side (GST paid)
* `payg_withheld_total` -- server-computed W2 + W3 + W4
* `manual` -- reviewer enters the amount through the manual-input action

Base and tax rows are split by `tax_line_id`, then signed balance is summed.
That keeps the populations distinct while refunds reduce, rather than vanish
from, the relevant amount.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


# Tax-type driven modes resolve the company's account.tax records by
# type_tax_use at compute time and need no pre-seeded account tags, so the
# shipped GST labels auto-compute on a fresh install. The tag-driven modes
# below them stay available for bespoke labels mapped to a specific chart tag.
_TAX_TYPE_AGGREGATIONS = frozenset({
    'gst_on_sales', 'gst_on_purchases', 'total_sales_incl',
    'payg_withheld_total',
})

_AGGREGATION_CHOICES = [
    ('total_sales_incl', "Total sales incl GST (G1)"),
    ('gst_on_sales', "GST on sales (1A)"),
    ('gst_on_purchases', "GST on purchases (1B)"),
    ('base_credit', "Base on credit (sales)"),
    ('base_debit', "Base on debit (acquisitions)"),
    ('tax_credit', "Tax on credit (GST collected)"),
    ('tax_debit', "Tax on debit (GST paid)"),
    ('payg_withheld_total', "Total PAYG withheld (W2 + W3 + W4)"),
    ('manual', "Manual entry"),
]


class EhBasLabel(models.Model):
    _name = 'eh.bas.label'
    _description = "Australian BAS label"
    _order = 'sequence, code'

    code = fields.Char(required=True, index=True)
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    aggregation = fields.Selection(
        _AGGREGATION_CHOICES, required=True, default='base_credit',
    )
    tag_ids = fields.Many2many(
        'account.account.tag',
        string="Tax tags",
        domain="[('applicability', '=', 'taxes')]",
        help=(
            "Tax tags whose movements feed this BAS label. The compute "
            "pass intersects journal_item.tax_tag_ids with this set."
        ),
    )
    description = fields.Text(translate=True)
    active = fields.Boolean(default=True)

    _unique_code = models.Constraint(
        'unique(code)',
        'BAS label code must be unique.',
    )

    @api.model
    def _eh_lock_definitions(self):
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ('eh_account_l10n_au_bas:labels',),
        )

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_lock_definitions()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_lock_definitions()
        return super().write(vals)

    def unlink(self):
        self._eh_lock_definitions()
        return super().unlink()

    @api.constrains('aggregation', 'tag_ids')
    def _check_aggregation_requires_tags(self):
        for rec in self:
            if rec.aggregation == 'manual':
                continue
            # Tax-type driven modes resolve their taxes at compute time; they
            # do not use tag_ids, so they are exempt from the tag requirement.
            if rec.aggregation in _TAX_TYPE_AGGREGATIONS:
                continue
            if not rec.tag_ids:
                raise ValidationError(_(
                    "BAS label %(code)s uses aggregation %(agg)s but has "
                    "no tax tags configured; the compute would silently "
                    "return zero. Configure at least one tag, or switch "
                    "the label to manual.",
                    code=rec.code, agg=rec.aggregation,
                ))
