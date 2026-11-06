# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.credit.policy: per-company credit limit policy.

A policy carries the default credit limit for partners that do not
define their own override, plus the enforcement mode (warn or block),
and the choice of whether draft customer invoices contribute to a
partner's exposure. The override group sets which users are allowed
to bypass the gate.

The default policy is per company. A partner with a non-zero
eh_credit_policy_id field uses that policy; otherwise the partner's
company default applies.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class EhCreditPolicy(models.Model):
    _name = 'eh.credit.policy'
    _description = "Customer credit policy"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'company_id, sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    is_company_default = fields.Boolean(
        default=False,
        help=(
            "When set, this policy is the company-level default for "
            "partners that do not define their own override. Only one "
            "company-default policy may exist per company; the "
            "constraint enforces this."
        ),
    )

    default_credit_limit = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Default credit limit applied when a partner has no "
            "override. Zero means no enforcement; the gate skips."
        ),
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )

    enforcement_mode = fields.Selection(
        [
            ('warn', "Warn only"),
            ('block', "Block on post (override allowed)"),
        ],
        default='block', required=True,
        help=(
            "Warn-only logs the breach to the move's chatter but does "
            "not block the post. Block raises a UserError unless an "
            "override has been recorded on the move."
        ),
    )

    include_drafts = fields.Boolean(
        default=False,
        help=(
            "When set, draft customer invoices count toward a partner's "
            "exposure alongside posted-and-unpaid invoices. Useful in "
            "shops where sales and accounting are different cohorts and "
            "drafts are a real commitment. Default off so the gate only "
            "considers what is posted."
        ),
    )
    include_sale_orders = fields.Boolean(
        default=False,
        help=(
            "When set, the not-yet-invoiced portion of confirmed sales "
            "orders counts toward a partner's exposure. Catches credit "
            "risk earlier than waiting for the invoice: a customer with "
            "a 100k confirmed order but only 20k invoiced has 80k of "
            "real future receivables, and a strict gate should refuse a "
            "fresh order that pushes total commitments past the limit. "
            "Has no effect when the sale module is not installed."
        ),
    )

    override_group_id = fields.Many2one(
        'res.groups',
        default=lambda self: self.env.ref(
            'eh_account_base.group_eh_manager',
            raise_if_not_found=False,
        ),
        help=(
            "Group whose members can record an override on a move. "
            "Defaults to the EH Accounting Manager group; deployments "
            "that need a separate Credit Officer cohort point this at "
            "their own group."
        ),
    )

    notes = fields.Text()

    _check_default_limit_non_negative = models.Constraint(
        'CHECK (default_credit_limit >= 0)',
        'Default credit limit cannot be negative.',
    )

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "Credit policies may only use a company enabled in the "
                "caller's current context."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            for vals in vals_list:
                if 'currency_id' in vals:
                    raise AccessError(_(
                        "Credit-policy currency is derived from its company."
                    ))
                self._eh_assert_company_allowed(
                    vals.get('company_id') or self.env.company.id,
                )
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
            if 'currency_id' in vals:
                raise AccessError(_(
                    "Credit-policy currency is derived from its company."
                ))
            if 'company_id' in vals:
                self._eh_assert_company_allowed(vals['company_id'])
                if any(rec.company_id.id != int(vals['company_id'])
                       for rec in self):
                    raise AccessError(_(
                        "A credit policy's company is immutable. Create a "
                        "new policy in the target company instead."
                    ))
        return super().write(vals)

    @api.constrains('is_company_default', 'company_id', 'active')
    def _check_one_default_per_company(self):
        # 'active' is a trigger so unarchiving a stale default re-runs the
        # check. Archiving a default drops it from search (active_test=True
        # excludes it), so a second default can be created while the first
        # is archived; restoring the first via the standard Unarchive action
        # writes only {'active': True}, which would not otherwise re-validate
        # and would leave two active defaults that find_for_company resolves
        # non-deterministically.
        for rec in self:
            if not rec.is_company_default or not rec.active:
                continue
            existing = self.search([
                ('is_company_default', '=', True),
                ('company_id', '=', rec.company_id.id),
                ('id', '!=', rec.id),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "Company %(company)s already has a default credit "
                    "policy (%(name)s). Unset that one first.",
                    company=rec.company_id.display_name,
                    name=existing.name,
                ))

    @api.onchange('company_id')
    def _onchange_company_id_currency(self):
        """Refresh currency display when company switches.

        currency_id is a related field, so it tracks automatically on
        save, but the form needs an onchange to reflect the new
        currency before save while the user is still entering values.
        """
        for rec in self:
            if rec.company_id:
                rec.currency_id = rec.company_id.currency_id

    @api.onchange('is_company_default', 'company_id')
    def _onchange_is_company_default(self):
        """Warn before saving a duplicate company-default policy.

        The constrains check raises on save; the onchange offers an
        earlier signal so users do not lose their other field input
        when the validator rejects the record.
        """
        for rec in self:
            if not rec.is_company_default or not rec.company_id:
                continue
            origin = rec._origin
            existing = self.search([
                ('is_company_default', '=', True),
                ('company_id', '=', rec.company_id.id),
                ('id', '!=', origin.id if origin else 0),
            ], limit=1)
            if existing:
                return {
                    'warning': {
                        'title': _("Existing default"),
                        'message': _(
                            "Company %(company)s already has a default "
                            "policy (%(name)s). Saving this record will "
                            "fail unless you clear the existing default "
                            "first.",
                            company=rec.company_id.display_name,
                            name=existing.name,
                        ),
                    },
                }

    @api.model
    def _eh_authorize_policy_lookup(self, company):
        """Validate the actor before any policy read is elevated.

        Credit policy is server configuration used by the posting gate.  A
        standard Odoo Accounting user must be governed by it even when that
        user was deliberately not granted the EH configuration groups.  Keep
        the configuration ACL closed, but permit the engine lookup after
        proving an accounting role and an allowed company.
        """
        company = self.env['res.company'].browse(company.id)
        company.ensure_one()
        if self.env.su:
            return company
        # Credit status is intentionally present on the base partner form for
        # sales and purchasing operators too.  Internal-user membership plus
        # the record/company checks below is the least role that matches that
        # UI; portal/public callers remain excluded.
        if not self.env.user.has_group('base.group_user'):
            raise AccessError(_(
                "Only an internal user may resolve a credit policy."
            ))
        company._eh_check_access('read')
        if company.id not in self.env.companies.ids:
            raise AccessError(_(
                "Credit policies may only be resolved for a company enabled "
                "in the caller's current context."
            ))
        return company

    @api.model
    def find_for_company(self, company):
        """Return the company default policy or empty recordset.

        Search runs as the server only after the caller's accounting role,
        company access and allowed-company context have been validated.  The
        returned elevated record is for gate internals; configuration menus
        and ordinary model reads still enforce their existing ACLs.
        """
        company = self._eh_authorize_policy_lookup(company)
        policy_companies = company
        if 'root_id' in company._fields and company.root_id != company:
            policy_companies |= company.root_id
        policies = self.sudo().search(
            [
                ('company_id', 'in', policy_companies.ids),
                ('is_company_default', '=', True),
                ('active', '=', True),
            ],
        )
        return policies.sorted(
            lambda policy: (
                0 if policy.company_id == company else 1,
                policy.sequence,
                policy.id,
            ),
        )[:1]
