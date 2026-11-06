# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Per-company inter-company configuration.

A config row enables auto-mirroring on a specific company. When a sale
invoice posts in this company and its partner.company_id matches some
other company, the trigger fires and the mirror move is created in
that other company.

Direction is implicit: out_invoice -> in_invoice (mirror), in_invoice ->
out_invoice (mirror). out_refund -> in_refund and the symmetric pair are
also handled.

Auto-post: when set, the mirror posts immediately. Otherwise it stays
draft for the receiving accountant to review and post manually.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class EhIntercompanyConfig(models.Model):
    _name = 'eh.intercompany.config'
    _description = "Inter-company configuration"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True, tracking=True,
        help=(
            "The company this config governs. When an invoice posts in "
            "this company, the trigger checks the partner's company_id "
            "to decide whether to mirror."
        ),
    )
    enabled = fields.Boolean(
        default=True, tracking=True,
        help=(
            "Master switch. Disable to pause mirroring without deleting "
            "the journal selection or auto-post setting."
        ),
    )
    sale_journal_id = fields.Many2one(
        'account.journal',
        tracking=True,
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
        help=(
            "Journal used when this company is the destination of a "
            "mirror that originated as a vendor bill (so the mirror "
            "here is a customer invoice). Required when in_invoice "
            "and in_refund mirrors should land here."
        ),
    )
    purchase_journal_id = fields.Many2one(
        'account.journal',
        tracking=True,
        domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
        help=(
            "Journal used when this company is the destination of a "
            "mirror that originated as a customer invoice (so the "
            "mirror here is a vendor bill). Required when out_invoice "
            "and out_refund mirrors should land here."
        ),
    )
    auto_post_mirror = fields.Boolean(
        default=False, tracking=True,
        help=(
            "When set, mirrors created on this company are posted "
            "automatically. Default off so the receiving accountant "
            "reviews each mirror in draft state."
        ),
    )
    fallback_expense_account_id = fields.Many2one(
        'account.account',
        tracking=True,
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost')), "
               "('company_ids', '=', company_id)]",
        help=(
            "Used on mirror BILL lines (in_invoice/in_refund) when the "
            "source line's product has no expense account configured "
            "in this company. Hard fail at post time if neither this "
            "fallback nor the product account is set."
        ),
    )
    fallback_revenue_account_id = fields.Many2one(
        'account.account',
        tracking=True,
        domain="[('account_type', 'in', ('income', 'income_other')), "
               "('company_ids', '=', company_id)]",
        help=(
            "Used on mirror INVOICE lines (out_invoice/out_refund) when "
            "the source line's product has no income account configured "
            "in this company. Hard fail at post time if neither this "
            "fallback nor the product account is set."
        ),
    )
    intercompany_user_id = fields.Many2one(
        'res.users',
        string="Mirror created by",
        tracking=True,
        help=(
            "When set, mirror documents in this company are created and "
            "posted as this user (instead of the elevated system "
            "context), so ownership and the audit trail name a real "
            "responsible user. The user needs access to this company "
            "and to accounting. Leave empty to create with elevated "
            "rights."
        ),
    )
    restrict_ic_partners = fields.Boolean(
        string="Restrict group-company partners",
        default=False, tracking=True,
        help=(
            "When set, posting an invoice or bill IN THIS COMPANY whose "
            "partner is another company of the group (the partner "
            "record of a res.company) without the Represented Company "
            "flag is refused. This catches inter-company transactions "
            "keyed against the raw company partner, which would bypass "
            "mirroring and the elimination engine. Off by default so "
            "existing flows are unchanged."
        ),
    )
    elimination_company_id = fields.Many2one(
        'res.company',
        string="Elimination Company",
        tracking=True,
        domain="[('eh_ic_consolidation_only', '=', True)]",
        help=(
            "Dedicated non-trading consolidation entity where elimination "
            "batches book. It must be marked Consolidation-only; a parent or "
            "subsidiary legal entity is not a valid elimination workspace."
        ),
    )
    elimination_journal_id = fields.Many2one(
        'account.journal',
        string="Elimination Journal",
        tracking=True,
        domain="[('type', '=', 'general'), "
               "('company_id', '=', elimination_company_id)]",
        help=(
            "Dedicated journal in the elimination company the "
            "elimination entries are booked to. Auto-created on first "
            "use (code ICEL) when left empty on the elimination "
            "company's configuration."
        ),
    )
    unrealised_inventory_account_id = fields.Many2one(
        'account.account',
        string="Unrealised-profit inventory account",
        tracking=True,
        domain="[('account_type', 'in', ('asset_current', 'asset_non_current')), "
               "('company_ids', '=', elimination_company_id)]",
        help=(
            "Inventory account credited for profit remaining in ending "
            "inventory. Must belong to the consolidation-only elimination "
            "company."
        ),
    )
    unrealised_cogs_account_id = fields.Many2one(
        'account.account',
        string="Unrealised-profit cost-of-sales account",
        tracking=True,
        domain="[('account_type', 'in', ('expense', 'expense_direct_cost')), "
               "('company_ids', '=', elimination_company_id)]",
        help=(
            "Cost-of-sales account debited for profit remaining in ending "
            "inventory. Must belong to the consolidation-only elimination "
            "company."
        ),
    )
    notes = fields.Text(tracking=True)

    _unique_per_company = models.Constraint(
        'unique(company_id)',
        'Only one inter-company config per company.',
    )

    @api.constrains('sale_journal_id', 'purchase_journal_id', 'company_id')
    def _check_journal_companies(self):
        for rec in self:
            if rec.sale_journal_id and rec.sale_journal_id.company_id != rec.company_id:
                raise ValidationError(_(
                    "Sale journal must belong to the same company as "
                    "this configuration.",
                ))
            if rec.purchase_journal_id and rec.purchase_journal_id.company_id != rec.company_id:
                raise ValidationError(_(
                    "Purchase journal must belong to the same company "
                    "as this configuration.",
                ))

    @api.constrains(
        'elimination_journal_id', 'elimination_company_id',
        'unrealised_inventory_account_id', 'unrealised_cogs_account_id',
    )
    def _check_elimination_journal(self):
        for rec in self:
            if (rec.elimination_company_id
                    and not rec.elimination_company_id
                    .eh_ic_consolidation_only):
                raise ValidationError(_(
                    "The elimination company must be explicitly marked as a "
                    "consolidation-only elimination entity."))
            journal = rec.elimination_journal_id
            if (
                (journal or rec.unrealised_inventory_account_id
                 or rec.unrealised_cogs_account_id)
                and not rec.elimination_company_id
            ):
                raise ValidationError(_(
                    "Set the Elimination Company before selecting "
                    "elimination journals or accounts.",
                ))
            if journal and journal.company_id != rec.elimination_company_id:
                raise ValidationError(_(
                    "The elimination journal must belong to the "
                    "elimination company.",
                ))
            if journal and journal.type != 'general':
                raise ValidationError(_(
                    "The elimination journal must be a miscellaneous "
                    "(general) journal.",
                ))
            for account, label, allowed_types in (
                (
                    rec.unrealised_inventory_account_id,
                    _("Unrealised-profit inventory account"),
                    ('asset_current', 'asset_non_current'),
                ),
                (
                    rec.unrealised_cogs_account_id,
                    _("Unrealised-profit cost-of-sales account"),
                    ('expense', 'expense_direct_cost'),
                ),
            ):
                if not account:
                    continue
                account_companies = (
                    account.company_ids
                    if 'company_ids' in account._fields
                    else account.company_id
                )
                if rec.elimination_company_id not in account_companies:
                    raise ValidationError(_(
                        "%(label)s must belong to the elimination company.",
                        label=label,
                    ))
                if account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(label)s has an incompatible account type.",
                        label=label,
                    ))
            if (
                rec.unrealised_inventory_account_id
                and rec.unrealised_inventory_account_id
                == rec.unrealised_cogs_account_id
            ):
                raise ValidationError(_(
                    "Unrealised-profit inventory and cost-of-sales accounts "
                    "must be different."
                ))
