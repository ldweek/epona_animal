# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.partner extension: per-partner credit limit override and live exposure.

Adds an optional eh_credit_limit override and an optional policy
override on top of the company default. Computed eh_credit_exposure
and eh_credit_status surface the partner's live position so the
sales team can see the gate state on the partner form before the
invoice is even drafted.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


_CREDIT_CONFIG_FIELDS = frozenset({
    'eh_credit_limit', 'eh_credit_policy_id',
})
_CREDIT_QUARANTINE_CONTEXT = 'eh_credit_quarantine_internal'


def _effective_create_value(vals, context, field_name):
    """Mirror ORM create-default precedence for one field."""
    return (
        vals[field_name]
        if field_name in vals
        else context.get(f'default_{field_name}')
    )


def _creates_credit_configuration(vals, context):
    """Return whether create values configure a real partner override.

    Mirror the ORM's explicit-value-over-context-default precedence. Delegated
    ``res.users`` creation can pass explicit false defaults for these fields;
    those values do not configure credit and must not turn ordinary user
    creation into a manager-only operation. Writes remain key-sensitive
    because clearing existing configuration is a privileged change.
    """
    policy = _effective_create_value(
        vals, context, 'eh_credit_policy_id',
    )
    limit = _effective_create_value(vals, context, 'eh_credit_limit')
    return bool(policy or float(limit or 0.0))


def _supplies_credit_quarantine(vals, context):
    """Return whether create explicitly or contextually supplies evidence."""
    return (
        'eh_credit_legacy_config_quarantine' in vals
        or 'default_eh_credit_legacy_config_quarantine' in context
    )


def _credit_root_company(company):
    """Return accounting root when native branch semantics exist."""
    if 'root_id' in company._fields and company.root_id:
        return company.root_id
    return company


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _eh_sale_order_uninvoiced_amount(self, order):
        """Return open SO value on every supported core series.

        ``amount_to_invoice`` is an invoicing-workflow measure, not a credit
        commitment: under delivery-based policies it is zero until delivery
        even though the confirmed order remains fully exposed. Reconstruct
        the gross remainder from posted source-linked invoice lines on every
        series. Line attribution also avoids charging an order for unrelated
        lines on a combined customer invoice.
        """
        invoice_lines = order.order_line.mapped('invoice_lines')
        invoiced = 0.0
        for line in invoice_lines:
            move = line.move_id
            if not (
                move.state == 'posted'
                or move.payment_state == 'invoicing_legacy'
            ):
                continue
            amount = line.currency_id._convert(
                line.price_total,
                order.currency_id,
                order.company_id,
                move.invoice_date or fields.Date.today(),
            )
            if move.move_type == 'out_invoice':
                invoiced += amount
            elif move.move_type == 'out_refund':
                invoiced -= amount
        return max((order.amount_total or 0.0) - invoiced, 0.0)

    def _eh_sale_order_projected_uninvoiced_amount(
            self, order, represented_moves=None):
        """Return SO exposure after moves already counted elsewhere.

        sale.order.amount_to_invoice deliberately ignores draft invoices.
        That is correct for invoicing workflow, but it would charge a
        sale-derived draft twice in credit exposure: once as draft/current
        invoice and again as unchanged order balance.

        Reduce only part of each represented move whose invoice lines link
        to this order. Unlinked invoice lines remain new credit exposure.
        Posted invoices are already reflected by the source-linked invoice
        reconstruction and must never be passed here.
        """
        self.ensure_one()
        open_amount = max(
            float(self._eh_sale_order_uninvoiced_amount(order) or 0.0),
            0.0,
        )
        represented_moves = represented_moves or self.env['account.move']
        if (
            not represented_moves
            or 'sale_line_ids' not in self.env['account.move.line']._fields
        ):
            return open_amount

        represented_amount = 0.0
        for move in represented_moves.filtered(
                lambda item: item.state == 'draft'):
            linked_total = sum(
                move.invoice_line_ids.filtered(
                    lambda line: order in line.sale_line_ids.order_id
                ).mapped('price_total')
            )
            if not linked_total:
                continue
            # Customer invoices consume open order balance; customer refunds
            # restore it. Other move types sit outside credit gate.
            direction = {
                'out_invoice': 1.0,
                'out_refund': -1.0,
            }.get(move.move_type, 0.0)
            if not direction:
                continue
            move_date = (
                move.invoice_date
                or move.date
                or fields.Date.context_today(self)
            )
            represented_amount += move.currency_id._convert(
                linked_total * direction,
                order.currency_id,
                move.company_id,
                move_date,
            )
        return max(open_amount - represented_amount, 0.0)

    # Odoo 19's generic company-dependent serializer converts Monetary
    # values to strings before placing them in JSONB.  PostgreSQL then cannot
    # cast the selected JSON string back to numeric.  Float overrides that
    # serializer and stores a JSON number; the form still renders it with the
    # monetary widget and the explicit company-currency field.
    eh_credit_limit = fields.Float(
        digits=(16, 2),
        company_dependent=True,
        help=(
            "Per-partner credit limit override. Zero means use the "
            "company default policy. Negative values are not allowed."
        ),
    )
    eh_credit_policy_id = fields.Many2one(
        'eh.credit.policy',
        company_dependent=True,
        check_company=True,
        domain="[('company_id', '=', eh_credit_policy_company_id)]",
        help=(
            "Per-partner policy override. When set, takes precedence "
            "over the company default. Useful for known-risk customers "
            "that need stricter enforcement than the default policy "
            "applies."
        ),
    )
    eh_credit_policy_company_id = fields.Many2one(
        'res.company',
        compute='_compute_eh_credit_policy_company',
        help=(
            "Company whose policies are valid in the current partner-form "
            "context. Shared partners use the active company."
        ),
    )
    eh_credit_legacy_config_quarantine = fields.Text(
        string="Legacy Credit Configuration Quarantine",
        readonly=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "Preserved pre-company-scope settings from a shared partner. "
            "They require explicit manager review and are never applied "
            "automatically to a company."
        ),
    )

    eh_credit_exposure_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_eh_credit_exposure_currency',
        help="Company currency exposure and limit are expressed in.",
    )
    eh_credit_exposure = fields.Monetary(
        compute='_compute_eh_credit_exposure',
        currency_field='eh_credit_exposure_currency_id',
        groups=(
            'account.group_account_invoice,'
            'eh_account_base.group_eh_user'
        ),
        help=(
            "Sum of unpaid open AR plus, when the effective policy "
            "says so, draft customer invoices and not-yet-invoiced "
            "sale orders. Multi-currency lines are converted to the "
            "company currency at the rate captured on each document. "
            "Live; recomputes on every form load."
        ),
    )
    eh_effective_credit_limit = fields.Monetary(
        compute='_compute_eh_credit_exposure',
        currency_field='eh_credit_exposure_currency_id',
        groups=(
            'account.group_account_invoice,'
            'eh_account_base.group_eh_user'
        ),
        help=(
            "Per-partner override if set, else the company default "
            "policy's default_credit_limit. Zero means no enforcement. "
            "Expressed in the company currency to match exposure."
        ),
    )
    eh_credit_status = fields.Selection(
        [
            ('ok', "OK"),
            ('warn', "Warning"),
            ('over', "Over limit"),
            ('disabled', "No policy"),
        ],
        compute='_compute_eh_credit_exposure',
        groups=(
            'account.group_account_invoice,'
            'eh_account_base.group_eh_user'
        ),
        help=(
            "OK: exposure under 80 percent of limit. Warning: between "
            "80 and 100 percent. Over: above the limit. Disabled: no "
            "company policy in place."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            limit = _effective_create_value(
                vals, self.env.context, 'eh_credit_limit',
            )
            if float(limit or 0.0) < 0.0:
                raise ValidationError(_(
                    "A customer credit limit cannot be negative."
                ))
            if _creates_credit_configuration(vals, self.env.context):
                parent_id = vals.get('parent_id')
                if parent_id:
                    raise ValidationError(_(
                        "Credit limits and policies belong to the commercial "
                        "customer, not an individual child contact."
                    ))
        supplies_quarantine = any(
            _supplies_credit_quarantine(vals, self.env.context)
            for vals in vals_list
        )
        if not self.env.su:
            if any(_creates_credit_configuration(vals, self.env.context)
                   for vals in vals_list) and not self.env.user.has_group(
                       'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only an EH Accounting Manager may configure customer "
                    "credit limits or policies."
                ))
            if supplies_quarantine:
                raise AccessError(_(
                    "Legacy credit configuration quarantine is server-owned."
                ))
        elif supplies_quarantine and not self.env.context.get(
                _CREDIT_QUARANTINE_CONTEXT):
            raise AccessError(_(
                "Legacy credit configuration quarantine is server-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if (
            'eh_credit_limit' in vals
            and float(vals.get('eh_credit_limit') or 0.0) < 0.0
        ):
            raise ValidationError(_(
                "A customer credit limit cannot be negative."
            ))
        if _CREDIT_CONFIG_FIELDS.intersection(vals) and not (
                self.env.su or self.env.user.has_group(
                    'eh_account_base.group_eh_manager')):
            raise AccessError(_(
                "Only an EH Accounting Manager may configure customer "
                "credit limits or policies."
            ))
        if 'eh_credit_legacy_config_quarantine' in vals and not (
                self.env.su and self.env.context.get(
                    _CREDIT_QUARANTINE_CONTEXT)):
            raise AccessError(_(
                "Legacy credit configuration quarantine is server-owned."
            ))
        if _CREDIT_CONFIG_FIELDS.intersection(vals) and not self.env.su:
            mismatched = self.filtered(
                lambda partner: (
                    partner.company_id
                    and partner.company_id != self.env.company
                )
            )
            if mismatched:
                raise AccessError(_(
                    "A company-owned partner's credit configuration must be "
                    "edited in that partner's company context."
                ))
        if (
            _CREDIT_CONFIG_FIELDS.intersection(vals)
            and any(vals.get(field_name) for field_name in _CREDIT_CONFIG_FIELDS)
            and self.filtered(
                lambda partner: partner.commercial_partner_id != partner
            )
        ):
            raise ValidationError(_(
                "Credit limits and policies belong to the commercial "
                "customer, not an individual child contact."
            ))
        return super().write(vals)

    def unlink(self):
        self._eh_check_access('unlink')
        retained = self.env['eh.credit.override.log'].sudo().search([
            ('partner_id', 'in', self.ids),
        ], limit=1)
        if retained:
            raise ValidationError(_(
                "This customer has retained credit-override evidence and "
                "cannot be deleted. Archive the customer instead."
            ))
        return super().unlink()

    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_eh_credit_exposure_currency(self):
        for partner in self:
            commercial = partner.commercial_partner_id
            company = commercial.company_id or self.env.company
            partner.eh_credit_exposure_currency_id = company.currency_id

    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_eh_credit_policy_company(self):
        for partner in self:
            commercial = partner.commercial_partner_id
            partner.eh_credit_policy_company_id = (
                commercial.company_id or self.env.company
            )

    @api.depends('credit_limit', 'eh_credit_limit', 'eh_credit_policy_id')
    @api.depends_context('company')
    def _compute_eh_credit_exposure(self):
        for partner in self:
            commercial = partner.commercial_partner_id
            company = commercial.company_id or self.env.company
            scoped_partner = commercial.with_company(company)
            policy = scoped_partner._eh_resolve_credit_policy()
            if not policy:
                partner.eh_credit_exposure = 0.0
                partner.eh_effective_credit_limit = 0.0
                partner.eh_credit_status = 'disabled'
                continue
            limit = scoped_partner._eh_credit_limit_for_company(
                company,
            ) or policy.default_credit_limit
            partner.eh_effective_credit_limit = limit
            partner.eh_credit_exposure = scoped_partner._eh_compute_exposure(
                policy,
            )
            if not limit:
                partner.eh_credit_status = 'disabled'
            elif company.currency_id.compare_amounts(
                    partner.eh_credit_exposure, limit) > 0:
                partner.eh_credit_status = 'over'
            elif company.currency_id.compare_amounts(
                    partner.eh_credit_exposure, limit * 0.80) > 0:
                partner.eh_credit_status = 'warn'
            else:
                partner.eh_credit_status = 'ok'

    def _eh_resolve_credit_policy(self):
        """Return the policy that governs this partner.

        Per-partner override takes precedence; otherwise the company's
        default policy. Returns empty recordset when no policy is set
        for the partner's company.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        commercial = self.commercial_partner_id
        company = commercial.company_id or self.env.company
        Policy = self.env['eh.credit.policy']
        company = Policy._eh_authorize_policy_lookup(company)

        # The partner-specific property is policy configuration too.  Reading
        # it in the actor environment fixes only the company-default path and
        # still crashes standard accounting users when an override exists.
        # Elevate this single read after partner + company authorization, then
        # keep the returned policy in the server environment for gate fields.
        scoped_partner = commercial.with_company(company)
        override = scoped_partner.sudo().eh_credit_policy_id.filtered(
            'active',
        )
        root = _credit_root_company(company)
        if not override and root != company:
            override = commercial.with_company(
                root,
            ).sudo().eh_credit_policy_id.filtered('active')
        if override:
            return override
        return Policy.find_for_company(company)

    def _eh_credit_limit_for_company(self, company=None):
        """Return commercial-customer override with branch-root fallback."""
        self.ensure_one()
        commercial = self.commercial_partner_id
        company = company or commercial.company_id or self.env.company
        self.env['eh.credit.policy']._eh_authorize_policy_lookup(company)
        limit = commercial.with_company(company).sudo().eh_credit_limit
        root = _credit_root_company(company)
        if not limit and root != company:
            limit = commercial.with_company(root).sudo().eh_credit_limit
        return float(limit or 0.0)

    def _eh_credit_ledger_company_ids(self, company=None):
        """Return complete actor-authorised legal-entity branch scope."""
        self.ensure_one()
        company = company or self.company_id or self.env.company
        root = _credit_root_company(company)
        if (
            root == company
            and (
                'root_id' not in company._fields
                or not hasattr(root, '_accessible_branches')
            )
        ):
            return company.ids
        if not hasattr(root, '_accessible_branches'):
            return company.ids
        Company = self.env['res.company']
        complete = Company.sudo().search([('id', 'child_of', root.id)])
        if self.env.su:
            return sorted(complete.ids)
        accessible = root._accessible_branches()
        if set(accessible.ids) != set(complete.ids):
            raise AccessError(_(
                "Credit exposure requires access to every accounting branch "
                "of %(company)s. Select the complete branch scope.",
                company=root.display_name,
            ))
        return sorted(complete.ids)

    def _eh_compute_exposure(self, policy):
        """Sum of open AR plus optional drafts and confirmed-but-unbilled
        sales orders for this partner, expressed in the company's
        currency.

        Multi-currency: AR uses amount_residual (already in company
        currency on account.move.line). Draft moves use
        amount_total_signed (company currency, refunds negative). Sale
        orders convert per-record from order currency to company
        currency via res.currency._convert at order_date so a USD
        order on an AUD company contributes its AUD value, not the
        raw USD figure.
        """
        self.ensure_one()
        commercial = self.commercial_partner_id
        AML = self.env['account.move.line']
        company = commercial.company_id or self.env.company
        company_currency = company.currency_id
        company_ids = commercial._eh_credit_ledger_company_ids(company)
        domain = [
            ('partner_id.commercial_partner_id', '=',
             commercial.id),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('amount_residual', '!=', 0),
            ('company_id', 'in', company_ids),
        ]
        # amount_residual on account.move.line is already in the company
        # currency: a EUR receivable on an AUD company sums its AUD
        # equivalent at the exchange rate captured on the move.
        posted_rows = read_group_compat(AML, domain, [], ['amount_residual:sum'])
        total = posted_rows[0][0] if posted_rows else 0.0

        represented_drafts = self.env['account.move']
        if policy.include_drafts:
            Move = self.env['account.move']
            draft_domain = [
                ('commercial_partner_id', '=',
                 commercial.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('state', '=', 'draft'),
                ('company_id', 'in', company_ids),
            ]
            # amount_total_signed is in company currency and signs
            # refunds negative, so a draft refund reduces exposure
            # rather than inflating it.
            draft_rows = read_group_compat(Move,
                draft_domain,
                [],
                ['amount_total_signed:sum'],
            )
            total += draft_rows[0][0] if draft_rows else 0.0
            # Only needed for sale-order overlap removal. Keep normal draft
            # total on read_group so SO-disabled forms stay one-query.
            if (
                policy.include_sale_orders
                and 'sale.order' in self.env
                and 'sale_line_ids' in self.env[
                    'account.move.line'
                ]._fields
            ):
                represented_drafts = Move.search(draft_domain)

        # Sales orders contribute the not-yet-invoiced portion of any
        # confirmed-and-not-cancelled order. Convert each order from its
        # own currency to company currency at the order's effective
        # date so multi-currency exposure is comparable to the AR side.
        if policy.include_sale_orders and 'sale.order' in self.env:
            SaleOrder = self.env['sale.order']
            so_domain = [
                ('partner_id.commercial_partner_id', '=',
                 commercial.id),
                ('state', 'in', ('sale', 'done')),
                ('company_id', 'in', company_ids),
                ('invoice_status', '!=', 'invoiced'),
                ('amount_total', '!=', 0.0),
            ]
            orders = SaleOrder.search(so_domain)
            for order in orders:
                raw = self._eh_sale_order_projected_uninvoiced_amount(
                    order,
                    represented_moves=represented_drafts,
                )
                if not raw:
                    continue
                order_currency = order.currency_id or company_currency
                if order_currency == company_currency:
                    total += raw
                else:
                    rate_date = (
                        getattr(order, 'date_order', False)
                        or fields.Date.context_today(self)
                    )
                    total += order_currency._convert(
                        raw, company_currency, company, rate_date,
                    )

        return total

    def action_view_eh_credit_status(self):
        """Open the override audit log filtered to this partner.

        Surfaced from the credit stat-button so a user clicking
        through gets the historical context (every override that
        bumped this partner past the limit, who authorised it, why)
        rather than just the current-state badge.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Credit overrides',
            'res_model': 'eh.credit.override.log',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('partner_id', '=', self.id)],
            'context': {
                'search_default_partner_id': self.id,
            },
        }
