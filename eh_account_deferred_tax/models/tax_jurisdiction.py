# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.tax.jurisdiction / eh.tax.rate: the enacted-rate table behind IAS 12.47.

Deferred tax is measured at the rates expected to apply when the asset is
realised or the liability settled, based on rates enacted or substantively
enacted by the end of the reporting period (IAS 12.47). A jurisdiction is
one taxation authority for one company; its rate rows carry the enacted
date and the date the rate takes effect, so a run resolves each line's
rate from the table at the run's reporting date instead of freezing one
scalar per run.

Resolution rule (eh.tax.jurisdiction.rate_at): the row with the latest
effective_from on or before the reporting date, ignoring rows whose
enacted_date is after the reporting date (a rate not yet enacted or
substantively enacted at the reporting date may not be used, IAS 12.48).

The company default jurisdiction is auto-created (sudo, system-controlled)
the first time a temporary-difference line is created without an explicit
jurisdiction, so existing databases keep working with zero setup: a
jurisdiction with no rate rows resolves nothing and the line falls back to
the run's statutory rate, the pre-table behaviour.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhTaxJurisdiction(models.Model):
    _name = 'eh.tax.jurisdiction'
    _description = "Tax jurisdiction (IAS 12)"
    _order = 'company_id, name, id'

    name = fields.Char(required=True)
    country_id = fields.Many2one(
        'res.country', string="Country",
        help="Optional country of the taxation authority; informational.",
    )
    company_id = fields.Many2one(
        'res.company', required=True, index=True,
        default=lambda self: self.env.company,
    )
    is_company_default = fields.Boolean(
        string="Company Default",
        help="The jurisdiction a temporary-difference line falls into when "
             "none is chosen. Auto-created on first use.",
    )
    rate_ids = fields.One2many(
        'eh.tax.rate', 'jurisdiction_id', string="Enacted Rates",
    )

    _unique_name_company = models.Constraint(
        'unique(name, company_id)',
        'A jurisdiction name must be unique per company.',
    )

    def init(self):
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                eh_tax_jurisdiction_one_default_company_uniq
            ON eh_tax_jurisdiction (company_id)
            WHERE is_company_default IS TRUE
        """)

    @api.constrains('company_id', 'is_company_default')
    def _check_one_company_default(self):
        for jurisdiction in self.filtered('is_company_default'):
            duplicate = self.search_count([
                ('company_id', '=', jurisdiction.company_id.id),
                ('is_company_default', '=', True),
                ('id', '!=', jurisdiction.id),
            ])
            if duplicate:
                raise ValidationError(_(
                    "Only one default tax jurisdiction is allowed per "
                    "company."
                ))

    @api.model
    def _lock_default_companies(self, company_ids):
        for company_id in sorted({int(value) for value in company_ids if value}):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("eh.tax.jurisdiction.default:%s" % company_id,),
            )

    @api.model_create_multi
    def create(self, vals_list):
        defaults = [
            vals.get('company_id') or self.env.company.id
            for vals in vals_list if vals.get('is_company_default')
        ]
        self._lock_default_companies(defaults)
        if len(defaults) != len(set(defaults)):
            raise ValidationError(_(
                "Only one default tax jurisdiction is allowed per company."
            ))
        for company_id in defaults:
            if self.search_count([
                ('company_id', '=', company_id),
                ('is_company_default', '=', True),
            ]):
                raise ValidationError(_(
                    "Only one default tax jurisdiction is allowed per "
                    "company."
                ))
        return super().create(vals_list)

    def write(self, vals):
        candidates = []
        for jurisdiction in self:
            is_default = vals.get(
                'is_company_default', jurisdiction.is_company_default,
            )
            if is_default:
                candidates.append(
                    vals.get('company_id', jurisdiction.company_id.id)
                )
        self._lock_default_companies(candidates)
        if len(candidates) != len(set(candidates)):
            raise ValidationError(_(
                "Only one default tax jurisdiction is allowed per company."
            ))
        for jurisdiction, company_id in zip(
            self.filtered(lambda row: vals.get(
                'is_company_default', row.is_company_default,
            )),
            candidates,
        ):
            if self.search_count([
                ('company_id', '=', company_id),
                ('is_company_default', '=', True),
                ('id', 'not in', self.ids),
            ]):
                raise ValidationError(_(
                    "Only one default tax jurisdiction is allowed per "
                    "company."
                ))
        return super().write(vals)

    @api.model
    def _get_company_default(self, company):
        """Return (creating if needed) the company's default jurisdiction.

        Runs as sudo: the row is system-controlled reference data and a
        plain accounting user adding a temporary-difference line must not
        need create rights on the jurisdiction table.
        """
        Jurisdiction = self.sudo()
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("eh.tax.jurisdiction.default:%s" % company.id,),
        )
        jur = Jurisdiction.search([
            ('company_id', '=', company.id),
            ('is_company_default', '=', True),
        ], limit=1)
        if not jur:
            # A same-named row may pre-exist without the flag; adopt it
            # rather than tripping the unique-name constraint.
            jur = Jurisdiction.search([
                ('company_id', '=', company.id),
                ('name', '=', company.name),
            ], limit=1)
            if jur:
                jur.is_company_default = True
        if not jur:
            jur = Jurisdiction.create({
                'name': company.name,
                'company_id': company.id,
                'country_id': company.country_id.id,
                'is_company_default': True,
            })
        return self.browse(jur.id)

    def rate_at(self, day):
        """Enacted rate (percentage) applicable at ``day``, or None.

        Latest effective_from on or before ``day``; rows enacted after
        ``day`` are ignored (IAS 12.47-48: only rates enacted or
        substantively enacted by the reporting date may measure the
        position). A missing enacted_date is treated as enacted.
        """
        self.ensure_one()
        if not day:
            return None
        row = self.env['eh.tax.rate'].search([
            ('jurisdiction_id', '=', self.id),
            ('effective_from', '<=', day),
            '|', ('enacted_date', '=', False), ('enacted_date', '<=', day),
        ], order='effective_from desc, id desc', limit=1)
        return row.rate if row else None


class EhTaxRate(models.Model):
    _name = 'eh.tax.rate'
    _description = "Enacted tax rate (IAS 12.47)"
    _order = 'jurisdiction_id, effective_from desc, id desc'

    jurisdiction_id = fields.Many2one(
        'eh.tax.jurisdiction', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='jurisdiction_id.company_id', store=True, readonly=True,
    )
    rate = fields.Float(
        digits=(6, 3), required=True,
        help="Enacted / substantively enacted tax rate, as a percentage.",
    )
    enacted_date = fields.Date(
        help="Date the rate was enacted or substantively enacted. A rate "
             "enacted after a run's reporting date is ignored by that run "
             "(IAS 12.47-48). Leave empty to treat the rate as enacted.",
    )
    effective_from = fields.Date(
        required=True,
        help="First day the rate applies. A run resolves the row with the "
             "latest effective date on or before its reporting date.",
    )

    _check_rate_range = models.Constraint(
        'CHECK (rate >= 0 AND rate <= 100)',  # noqa: gcclog-hardcode statutory percentage bounds
        'Tax rate must be between 0 and 100.',
    )
    _unique_jurisdiction_from = models.Constraint(
        'unique(jurisdiction_id, effective_from)',
        'Only one rate row per jurisdiction per effective date.',
    )
