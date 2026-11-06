# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tolerance profile.

Captures the qty, price and amount tolerances applied to a 3 way match
between a vendor bill line, a purchase order line and the goods
receipt. Each partner can carry an override; otherwise the default
profile (is_default=True) applies.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_EH_AP_PROFILE_COMPANY_INTERNAL = 'eh_ap_profile_company_internal'

_EH_AP_DEFAULT_PROFILE_VALUES = {
    'name': 'Default',
    'is_default': True,
    'qty_tolerance_pct': 2.0,
    'price_tolerance_pct': 1.0,
    'amount_tolerance': 5.0,
    'over_receipt_pct': 0.0,
    'active': True,
}


class EhApToleranceProfile(models.Model):
    _name = 'eh.ap.tolerance.profile'
    _description = "AP Tolerance Profile"
    _order = 'sequence, name'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    is_default = fields.Boolean(
        default=False,
        help="If set, this profile applies to partners that do not "
             "carry an explicit override. Only one profile per company "
             "can be flagged as default.",
    )
    active = fields.Boolean(default=True)

    qty_tolerance_pct = fields.Float(
        string="Qty Tolerance (%)",
        default=0.0,
        help="Allowed delta between billed qty and received qty as a "
             "percentage of received qty.",
    )
    price_tolerance_pct = fields.Float(
        string="Price Tolerance (%)",
        default=0.0,
        help="Allowed delta between billed unit price and PO unit price "
             "as a percentage of PO unit price.",
    )
    amount_tolerance = fields.Monetary(
        string="Absolute Amount Tolerance",
        currency_field='amount_tolerance_currency_id',
        default=0.0,
        help="Allowed absolute delta on the line subtotal in addition "
             "to the percentage tolerances. Entered in the profile "
             "company currency and converted to the bill currency at the "
             "bill date.",
    )
    over_receipt_pct = fields.Float(
        string="Over Receipt Tolerance (%)",
        default=0.0,
        help="Allowed over receipt of qty beyond ordered qty as a "
             "percentage of ordered qty.",
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    amount_tolerance_currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', store=True,
        readonly=True, string="Amount Tolerance Currency",
    )

    _check_qty_pct_non_negative = models.Constraint(
        'CHECK (qty_tolerance_pct >= 0)',
        'Qty tolerance must be non negative.',
    )
    _check_price_pct_non_negative = models.Constraint(
        'CHECK (price_tolerance_pct >= 0)',
        'Price tolerance must be non negative.',
    )
    _check_amount_non_negative = models.Constraint(
        'CHECK (amount_tolerance >= 0)',
        'Amount tolerance must be non negative.',
    )
    _check_over_receipt_non_negative = models.Constraint(
        'CHECK (over_receipt_pct >= 0)',
        'Over-receipt tolerance must be non negative.',
    )

    def init(self):
        """Cross-series partial uniqueness for the one live default."""
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "eh_ap_tolerance_profile_default_company_uniq "
            "ON eh_ap_tolerance_profile (company_id) "
            "WHERE is_default IS TRUE"
        )

    @api.model_create_multi
    def create(self, vals_list):
        default_company_ids = [
            int(vals.get('company_id') or self.env.company.id)
            for vals in vals_list if vals.get('is_default')
        ]
        if not self.env.su:
            for vals in vals_list:
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id,
                ).exists()
                if not company or company not in self.env.companies:
                    raise AccessError(_(
                        "AP tolerance profiles can only be created in an "
                        "active company."
                    ))
                company._eh_check_access('read')
        if default_company_ids:
            if len(default_company_ids) != len(set(default_company_ids)):
                raise ValidationError(_(
                    "Only one default AP tolerance profile is allowed per "
                    "company."
                ))
            self._eh_lock_default_companies(default_company_ids)
            existing = self.search([
                ('company_id', 'in', default_company_ids),
                ('is_default', '=', True),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "Company %(company)s already has a default tolerance "
                    "profile (%(profile)s). Unset that one first.",
                    company=existing.company_id.display_name,
                    profile=existing.name,
                ))
        return super().create(vals_list)

    def write(self, vals):
        if 'company_id' in vals:
            target_id = vals.get('company_id')
            moving = self.filtered(
                lambda profile: profile.company_id.id != target_id,
            )
            if moving and not (
                    self.env.su and self.env.context.get(
                        _EH_AP_PROFILE_COMPANY_INTERNAL)):
                raise AccessError(_(
                    "An AP tolerance profile company is immutable. Create a "
                    "profile in the intended active company instead."
                ))
        if vals.get('is_default'):
            company_ids = self.mapped('company_id').ids
            self._eh_lock_default_companies(company_ids)
            if len(self.filtered(
                    lambda profile: profile.company_id.id in company_ids
            )) != len(set(company_ids)):
                raise ValidationError(_(
                    "Only one default AP tolerance profile is allowed per "
                    "company."
                ))
            existing = self.search([
                ('company_id', 'in', company_ids),
                ('is_default', '=', True),
                ('id', 'not in', self.ids),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "Company %(company)s already has a default tolerance "
                    "profile (%(profile)s). Unset that one first.",
                    company=existing.company_id.display_name,
                    profile=existing.name,
                ))
        return super().write(vals)

    def _eh_lock_default_companies(self, company_ids):
        company_ids = tuple(sorted(set(int(company_id) for company_id in company_ids)))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s ORDER BY id FOR UPDATE",
                (company_ids,),
            )

    @api.model
    def _eh_ensure_company_defaults(self, companies):
        """Create the shipped default for every genuinely unconfigured company.

        The XML record can only belong to the company active while the module
        is installed.  Installation/upgrade hooks and ``res.company.create``
        share this idempotent corridor so existing and future companies both
        receive a usable fallback.  Company-row locks serialize concurrent
        seeders; the partial unique index remains the final invariant.

        Archived defaults count as configured.  An administrator who
        deliberately archived a profile must decide whether to reactivate or
        replace it; automatic seeding must not silently undo that choice.
        """
        companies = companies.sudo().exists()
        if not companies:
            return self.browse()

        Profile = self.sudo().with_context(active_test=False)
        Profile._eh_lock_default_companies(companies.ids)
        configured_company_ids = set(Profile.search([
            ('company_id', 'in', companies.ids),
            ('is_default', '=', True),
        ]).mapped('company_id').ids)
        missing_companies = companies.filtered(
            lambda company: company.id not in configured_company_ids,
        )
        if not missing_companies:
            return Profile.browse()

        return Profile.create([
            dict(_EH_AP_DEFAULT_PROFILE_VALUES, company_id=company.id)
            for company in missing_companies
        ])

    @api.constrains(
        'qty_tolerance_pct', 'price_tolerance_pct', 'amount_tolerance',
        'over_receipt_pct',
    )
    def _check_finite_tolerances(self):
        for profile in self:
            values = (
                profile.qty_tolerance_pct, profile.price_tolerance_pct,
                profile.amount_tolerance, profile.over_receipt_pct,
            )
            if not all(math.isfinite(value) and value >= 0 for value in values):
                raise ValidationError(_(
                    "AP tolerances must be finite, non-negative numbers."
                ))

    @api.constrains('is_default', 'company_id')
    def _check_one_default_per_company(self):
        for prof in self:
            if not prof.is_default:
                continue
            existing = self.search([
                ('is_default', '=', True),
                ('company_id', '=', prof.company_id.id),
                ('id', '!=', prof.id),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "Company %(company)s already has a default tolerance "
                    "profile (%(profile)s). Unset that one first.",
                    company=prof.company_id.display_name,
                    profile=existing.name,
                ))

    @api.model
    def get_default(self, company=None):
        company = company or self.env.company
        if not self.env.su:
            self._eh_check_access('read')
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise UserError(_(
                    "Company %s is outside the active companies.",
                    company.display_name,
                ))
        prof = self.search([
            ('is_default', '=', True),
            ('company_id', '=', company.id),
        ], limit=1)
        if not prof:
            raise UserError(_(
                "No default AP tolerance profile configured for company "
                "%s.", company.display_name,
            ))
        return prof

    @api.model
    def resolve_for_partner(self, partner, company=None):
        """Return the tolerance profile to use for `partner`.

        Falls back to the company default if the partner does not
        carry an explicit override.
        """
        company = company or self.env.company
        if not self.env.su:
            company._eh_check_access('read')
            if company not in self.env.companies:
                raise UserError(_(
                    "Company %s is outside the active companies.",
                    company.display_name,
                ))
            if partner:
                partner._eh_check_access('read')
        profile = (
            partner.with_company(company).eh_ap_tolerance_profile_id
            if partner else self
        )
        if profile and profile.active and profile.company_id == company:
            if not self.env.su:
                profile._eh_check_access('read')
            return profile
        return self.get_default(company=company)
