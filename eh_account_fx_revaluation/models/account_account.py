# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inherit account.account to flag accounts subject to FX revaluation.

Accounts marked eh_fx_revalue have their open foreign currency balances
translated at the period end closing rate by an eh.fx.revaluation.run.
By default, the flag is on for typical monetary accounts (receivable,
payable, bank, credit card, current asset / liability) and off for
non monetary accounts (inventory, fixed assets, equity).
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


_DEFAULT_REVALUE_TYPES = (
    'asset_cash',
    'asset_receivable',
    'liability_payable',
    'liability_credit_card',
)

# Account types that are non monetary under IAS 21 and therefore must
# not be retranslated at the closing rate. Monetary items (cash,
# receivables, payables, loans held in current / non current asset and
# liability accounts) remain eligible; the types below are carried at
# historical rate and are rejected if flagged for revaluation.
_NON_MONETARY_TYPES = (
    'asset_prepayments',
    'asset_fixed',
    'equity',
    'equity_unaffected',
    'income',
    'income_other',
    'expense',
    'expense_other',
    'expense_depreciation',
    'expense_direct_cost',
    'off_balance',
)


class AccountAccount(models.Model):
    _inherit = 'account.account'

    eh_fx_revalue = fields.Boolean(
        string="FX Revaluation",
        default=False,
        help="If set, open foreign currency balances on this account are "
             "translated at the closing rate during a period end FX "
             "revaluation run.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # Explicitly flagged accounts are already source-visible when the
        # create returns.  Accounts auto-flagged below go through write(),
        # which performs the same invalidation without a duplicate bump.
        initially_revalued = records.filtered('eh_fx_revalue')
        auto_revalue = records.browse()
        for rec, vals in zip(records, vals_list):
            if (
                'eh_fx_revalue' not in vals
                and not rec.eh_fx_revalue
                and rec.account_type in _DEFAULT_REVALUE_TYPES
            ):
                auto_revalue |= rec
        if auto_revalue:
            auto_revalue.write({'eh_fx_revalue': True})
        if initially_revalued:
            self.env['res.company'].sudo()._eh_bump_move_version(
                initially_revalued._eh_owning_company_ids(),
            )
        return records

    def write(self, vals):
        source_company_ids = (
            self._eh_owning_company_ids()
            if 'eh_fx_revalue' in vals else set()
        )
        res = super().write(vals)
        if self and 'eh_fx_revalue' in vals:
            # An account may be rehomed in the same write.  Include both the
            # old and new owners; Base expands each owner to its descendants
            # for Odoo 18/19 shared charts and resolves company_id on older
            # series where account.account is single-company.
            source_company_ids.update(self._eh_owning_company_ids())
            self.env['res.company'].sudo()._eh_bump_move_version(
                source_company_ids,
            )
        return res

    @api.constrains('eh_fx_revalue', 'account_type')
    def _check_fx_revalue_monetary(self):
        for rec in self:
            if rec.eh_fx_revalue and rec.account_type in _NON_MONETARY_TYPES:
                raise ValidationError(_(
                    "Account %(code)s is non monetary (%(type)s) and cannot "
                    "be flagged for FX revaluation. IAS 21 restricts period "
                    "end retranslation to monetary items (cash, receivables, "
                    "payables, loans); non monetary items such as fixed "
                    "assets, prepayments and equity are carried at historical "
                    "rate.",
                    code=rec.code or rec.name or rec.id,
                    type=rec.account_type,
                ))

    @api.model
    def _eh_seed_existing_monetary_accounts(self):
        """Flag pre-existing core monetary accounts on first installation.

        ``default`` only affects records created after field installation.
        Installation therefore calls this idempotent helper after registry
        setup so existing receivable, payable, bank, and card accounts get
        same safe default as newly-created accounts. Explicit choices made
        after installation remain untouched because hook runs once.
        """
        accounts = self.sudo().search([
            ('account_type', 'in', _DEFAULT_REVALUE_TYPES),
            ('eh_fx_revalue', '=', False),
        ])
        if accounts:
            accounts.write({'eh_fx_revalue': True})
        return accounts
