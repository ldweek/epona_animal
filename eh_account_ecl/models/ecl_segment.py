# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.segment: a portfolio segment for the provision matrix.

IFRS 9.B5.5.35 lets a provision matrix group trade receivables by shared
credit-risk characteristics (customer geography, customer type, and so on)
with a distinct loss-rate profile per group. A segment carries the partner
match rules; populate-from-receivables splits the ageing matrix per segment
when segments exist for the company.
"""

from odoo import fields, models


class EhEclSegment(models.Model):
    _name = 'eh.ecl.segment'
    _description = "ECL portfolio segment"
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    country_ids = fields.Many2many(
        'res.country', string="Customer Countries",
        help="A receivable whose partner country is in this list falls into "
             "this segment.")
    partner_category_ids = fields.Many2many(
        'res.partner.category', string="Partner Tags",
        help="A receivable whose partner carries any of these tags falls "
             "into this segment.")

    def _matches_partner(self, partner):
        """True when the partner belongs to this segment.

        A segment with neither countries nor tags matches nothing, so an
        empty rule set cannot silently capture the whole book.
        """
        self.ensure_one()
        if not partner:
            return False
        partner = partner.commercial_partner_id
        if self.country_ids and partner.country_id in self.country_ids:
            return True
        if self.partner_category_ids \
                and (partner.category_id & self.partner_category_ids):
            return True
        return False
