# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Non-cash transaction disclosure register (IAS 7.43).

Investing and financing transactions that do not require the use of cash
(assets acquired under leases, debt converted to equity, assets acquired by
assuming directly related liabilities) are excluded from the Cash Flow
Statement's activity sections but must be disclosed elsewhere in a way that
provides all the relevant information (IAS 7.43). This register is that
"elsewhere": each entry names the period, the transaction, the amount and
the kind, and the Cash Flow Statement renders a dedicated memo section
listing the entries falling inside the reported period. The amounts are
informational and never feed the net-change-in-cash arithmetic.

The model also hosts the IAS 7 auto-tag helper: a server action (and the
module's post_init_hook) that walks the suite's own account configuration
fields (asset depreciation expense, impairment loss, provision expense, FX
revaluation gain/loss, fair value gain/loss) and stamps the matching
"EH Non-Cash ..." account tags so the indirect cash flow method resolves
its add-back lines without manual tagging. The helper only ever adds tags,
never removes them, and every probe is defensive: a module that is not
installed, or a field that does not exist, is silently skipped.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class EhNoncashTransaction(models.Model):
    _name = 'eh.noncash.transaction'
    _description = "Non-Cash Transaction (IAS 7.43)"
    _order = 'date desc, id desc'

    name = fields.Char(
        string="Description",
        required=True,
        help=(
            "What was exchanged without cash, e.g. 'Warehouse racking "
            "acquired under a five-year lease' or 'Convertible note "
            "series A converted to ordinary shares'."
        ),
    )
    date = fields.Date(
        string="Date",
        required=True,
        default=fields.Date.context_today,
        help=(
            "Transaction date. The Cash Flow Statement lists the entry "
            "in every reporting period containing this date."
        ),
    )
    amount = fields.Monetary(
        string="Amount",
        required=True,
        currency_field='currency_id',
        help=(
            "Disclosed amount of the transaction. Informational only: "
            "it never enters the net change in cash."
        ),
    )
    kind = fields.Selection(
        [('lease', "Asset acquired under lease"),
         ('debt_conversion', "Debt converted to equity"),
         ('other', "Other non-cash transaction")],
        string="Kind",
        required=True,
        default='other',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string="Company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        related='company_id.currency_id',
        string="Currency",
        readonly=True,
    )

    def _eh_invalidate_report_cache(self, company_ids):
        self.env['res.company'].sudo()._eh_bump_move_version(company_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._eh_invalidate_report_cache(records.mapped('company_id.id'))
        return records

    def write(self, vals):
        company_ids = set(self.mapped('company_id.id'))
        result = super().write(vals)
        if {'name', 'date', 'amount', 'kind', 'company_id'}.intersection(vals):
            company_ids.update(self.mapped('company_id.id'))
            self._eh_invalidate_report_cache(company_ids)
        return result

    def unlink(self):
        company_ids = set(self.mapped('company_id.id'))
        result = super().unlink()
        if company_ids:
            self._eh_invalidate_report_cache(company_ids)
        return result

    # ------------------------------------------------------------------
    # IAS 7 auto-tag helper
    # ------------------------------------------------------------------

    # (model, field, tag key) probes over the suite's own account
    # configuration. Each is resolved only when the model is installed and
    # the field exists, so this module keeps zero hard dependencies on the
    # rest of the suite. The tag keys map to the account_tags.xml records.
    _EH_IAS7_TAG_PROBES = (
        ('eh.asset', 'depreciation_account_id', 'depreciation'),
        ('eh.asset.impairment', 'impairment_account_id', 'impairment'),
        ('eh.provision', 'expense_account_id', 'provisions'),
        ('eh.provision', 'finance_cost_account_id', 'provisions'),
        ('eh.provision', 'income_account_id', 'provisions'),
        ('eh.fx.revaluation.run', 'gain_account_id', 'fx'),
        ('eh.fx.revaluation.run', 'loss_account_id', 'fx'),
        ('eh.fair.value.item', 'gain_loss_account_id', 'fair_value'),
        ('eh.fair.value.item', 'oci_account_id', 'fair_value'),
    )

    _EH_IAS7_TAG_XMLIDS = {
        'depreciation':
            'eh_account_dynamic_reports.account_tag_noncash_depreciation',
        'impairment':
            'eh_account_dynamic_reports.account_tag_noncash_impairment',
        'provisions':
            'eh_account_dynamic_reports.account_tag_noncash_provisions',
        'fx': 'eh_account_dynamic_reports.account_tag_noncash_fx',
        'fair_value':
            'eh_account_dynamic_reports.account_tag_noncash_fair_value',
    }

    @api.model
    def action_eh_ias7_auto_tag(self):
        """Stamp the EH Non-Cash tags on the suite's own known accounts.

        Sources, in order:

        1. Accounts of account_type `expense_depreciation` receive the
           depreciation tag (the indirect method also treats that type as
           implicitly tagged, so this is documentation more than mechanics).
        2. The company exchange gain/loss accounts
           (income/expense_currency_exchange_account_id) receive the FX tag:
           unrealised revaluation differences post there.
        3. Every (model, field) probe in _EH_IAS7_TAG_PROBES whose model is
           installed contributes the accounts configured on its records.

        Additive and idempotent: existing tags are never removed, an
        already-tagged account is left untouched, and any individual probe
        failure is logged and skipped so neither the server action nor the
        install hook can break on a partial suite.
        """
        if (
            not self.env.su
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'
            )
        ):
            raise AccessError(_(
                "Only an ERP Heritage Accounting Manager can apply "
                "IAS 7 account tags."
            ))
        company_ids = (
            self.env['res.company'].sudo().search([]).ids
            if self.env.su
            else self.env.companies.ids
        )
        companies = self.env['res.company'].sudo().browse(company_ids)
        Account = self.env['account.account'].sudo()
        account_company_field = (
            'company_ids' if 'company_ids' in Account._fields
            else 'company_id'
        )
        check_company_domain = getattr(
            Account, '_check_company_domain', None)
        account_company_domain = (
            list(check_company_domain(companies))
            if callable(check_company_domain)
            else [(account_company_field, 'in', company_ids)]
        )
        tagged = 0

        def _apply(accounts, tag_key):
            nonlocal tagged
            tag = self.env.ref(
                self._EH_IAS7_TAG_XMLIDS[tag_key], raise_if_not_found=False)
            if not tag or not accounts:
                return
            safe_accounts = Account.search(
                [('id', 'in', accounts.ids)] + account_company_domain
            )
            for account in safe_accounts:
                if tag not in account.tag_ids:
                    account.sudo().write({'tag_ids': [(4, tag.id)]})
                    tagged += 1

        # 1. Depreciation expense accounts by type.
        try:
            _apply(
                Account.search(
                    [('account_type', '=', 'expense_depreciation')]
                    + account_company_domain
                ),
                'depreciation',
            )
        except Exception:  # pragma: no cover - defensive
            _logger.exception("IAS 7 auto-tag: depreciation probe failed")

        # 2. Company exchange difference accounts.
        try:
            fx_accounts = Account.browse()
            for fname in ('income_currency_exchange_account_id',
                          'expense_currency_exchange_account_id'):
                if fname in companies._fields:
                    fx_accounts |= companies.mapped(fname)
            _apply(fx_accounts, 'fx')
        except Exception:  # pragma: no cover - defensive
            _logger.exception("IAS 7 auto-tag: company FX probe failed")

        # 3. Suite configuration probes.
        for model_name, field_name, tag_key in self._EH_IAS7_TAG_PROBES:
            try:
                if model_name not in self.env:
                    continue
                Model = self.env[model_name].sudo()
                if field_name not in Model._fields:
                    continue
                company_field = next(
                    (field for field in ('company_id', 'company_ids')
                     if field in Model._fields),
                    None,
                )
                domain = (
                    [(company_field, 'in', company_ids)]
                    if company_field else []
                )
                records = Model.search(domain)
                _apply(records.mapped(field_name), tag_key)
            except Exception:  # pragma: no cover - defensive
                _logger.exception(
                    "IAS 7 auto-tag: probe %s.%s failed",
                    model_name, field_name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("IAS 7 account tags"),
                'message': _(
                    "%(count)s account(s) newly tagged for the Cash Flow "
                    "Statement.", count=tagged),
                'type': 'success',
                'sticky': False,
            },
        }
