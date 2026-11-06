# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Structured consolidation bases and durable review evidence.

The engine deliberately keeps these inputs separate from generated run lines:

* acquisition-equity components preserve the account-by-account IFRS 3 basis;
* equity-method rollforwards hold period-specific IAS 28 distributions/OCI;
* disposal events survive reset/recompute and reconstruct IAS 21.48 lines; and
* the SQL trial-balance view gives reviewers one row per run/account.
"""

import math

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from .consol_run_line import CONSOL_ENGINE_CAPABILITY, CONSOL_ENGINE_CTX


_EQUITY_TYPES = ('equity', 'equity_unaffected')
_INCOME_TYPES = ('income', 'income_other')
_OCI_TYPES = _EQUITY_TYPES


def _account_company_ids(account):
    if 'company_ids' in account._fields:
        return set(account.company_ids.ids)
    return set(account.company_id.ids)


class EhConsolAcquisitionEquityComponent(models.Model):
    _name = 'eh.consol.acquisition.equity.component'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Acquisition-date equity component"
    _order = 'member_id, sequence, id'

    member_id = fields.Many2one(
        'eh.consol.member', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    presentation_currency_id = fields.Many2one(
        related='member_id.entity_id.presentation_currency_id', store=True,
        readonly=True,
    )
    source_account_id = fields.Many2one(
        'account.account', required=True, ondelete='restrict',
        string="Subsidiary Equity Account",
        help=(
            "Subsidiary share-capital, reserve or retained-earnings account "
            "whose acquisition-date balance is removed."
        ),
    )
    target_account_id = fields.Many2one(
        'account.account', ondelete='restrict',
        string="Consolidated Equity Account",
        help=(
            "Optional group-chart account for the component. When empty, the "
            "member account map is applied; an unmapped component remains on "
            "its source equity account."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='presentation_currency_id',
        help=(
            "Positive acquisition-date equity component. It follows the "
            "member's acquisition-equity denomination: presentation currency "
            "unless a historical rate is configured."
        ),
    )
    notes = fields.Char()

    _unique_source_component = models.Constraint(
        'unique(member_id, source_account_id)',
        "Each subsidiary equity account can appear once per acquisition basis.",
    )

    @api.model
    def _eh_validate_values(self, vals, current=None):
        member = self.env['eh.consol.member'].browse(vals.get(
            'member_id', current.member_id.id if current else False,
        ))
        if not member:
            raise ValidationError(_("An acquisition component needs a member."))
        member.entity_id._eh_assert_company_closure('write')
        amount = vals.get('amount', current.amount if current else 0.0)
        try:
            amount = float(amount or 0.0)
        except (TypeError, ValueError, OverflowError):
            amount = float('nan')
        if not math.isfinite(amount) or amount <= 0.0:
            raise ValidationError(_(
                "An acquisition-equity component must be a finite positive "
                "amount."
            ))
        source = self.env['account.account'].browse(vals.get(
            'source_account_id',
            current.source_account_id.id if current else False,
        ))
        target = self.env['account.account'].browse(vals.get(
            'target_account_id',
            current.target_account_id.id if current else False,
        ))
        if not source or source.account_type not in _EQUITY_TYPES:
            raise ValidationError(_(
                "An acquisition component source must be an equity account."
            ))
        source_scope = set(member.entity_id._eh_member_source_companies(
            member,
        ).ids)
        if not (_account_company_ids(source) & source_scope):
            raise ValidationError(_(
                "An acquisition component source must belong to that member's "
                "exact company/branch scope."
            ))
        if target:
            target_scope = set((
                member.entity_id.parent_company_id
                | member.entity_id.consolidation_company_id
            ).ids)
            if (
                target.account_type not in _EQUITY_TYPES
                or not (_account_company_ids(target) & target_scope)
            ):
                raise ValidationError(_(
                    "A consolidated acquisition component target must be a "
                    "parent/consolidation-ledger equity account."
                ))
        return member

    def _eh_lock_and_assert_mutable(self, extra_member_ids=()):
        members = self.mapped('member_id') | self.env['eh.consol.member'].browse(
            [member_id for member_id in extra_member_ids if member_id]
        )
        entities = members._eh_lock_configuration()
        entities._eh_assert_scope_mutable()
        return members

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_validate_values(vals)
        members = self._eh_lock_and_assert_mutable(
            {vals.get('member_id') for vals in vals_list},
        )
        records = super().create(vals_list)
        records.mapped('member_id')._eh_validate_references()
        members.mapped('entity_id')._eh_invalidate_company_scope_cache()
        return records

    def write(self, vals):
        for record in self:
            self._eh_validate_values(vals, current=record)
        self._eh_lock_and_assert_mutable((vals.get('member_id'),))
        result = super().write(vals)
        self.mapped('member_id')._eh_validate_references()
        return result

    def unlink(self):
        self._eh_lock_and_assert_mutable()
        return super().unlink()


class EhConsolEquityRollforward(models.Model):
    _name = 'eh.consol.equity.rollforward'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Equity-method period rollforward"
    _order = 'run_id, member_id, id'

    run_id = fields.Many2one(
        'eh.consol.run', required=True, ondelete='cascade', index=True,
    )
    member_id = fields.Many2one(
        'eh.consol.member', required=True, ondelete='restrict', index=True,
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id', store=True, readonly=True,
    )
    dividends_received = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Dividends received from the associate during this run period.",
    )
    dividend_income_account_id = fields.Many2one(
        'account.account', ondelete='restrict',
        help="Parent dividend-income account debited to eliminate distributions.",
    )
    oci_share = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Parent's signed share of associate OCI for this run period.",
    )
    oci_account_id = fields.Many2one(
        'account.account', ondelete='restrict',
        help="Equity/OCI account carrying the associate OCI share.",
    )
    foreign_exchange_adjustment = fields.Monetary(
        currency_field='presentation_currency_id',
        help=(
            "Signed IAS 21 exchange adjustment on the foreign associate's "
            "carrying amount for this run period."
        ),
    )
    foreign_exchange_confirmed = fields.Boolean(
        help=(
            "Confirms that the foreign-associate carrying translation was "
            "measured, including an explicit zero adjustment where applicable."
        ),
    )
    basis = fields.Text(
        help="Evidence supporting dividends, OCI and exchange adjustments.",
    )

    _unique_run_member_rollforward = models.Constraint(
        'unique(run_id, member_id)',
        "A run can contain one equity-method rollforward per member.",
    )

    @api.model
    def _eh_validate_values(self, vals, current=None):
        run = self.env['eh.consol.run'].browse(vals.get(
            'run_id', current.run_id.id if current else False,
        ))
        member = self.env['eh.consol.member'].browse(vals.get(
            'member_id', current.member_id.id if current else False,
        ))
        if not run or not member or member.entity_id != run.entity_id:
            raise ValidationError(_(
                "An equity-method rollforward member must belong to its run."
            ))
        if member.method != 'equity' or member.fv_option:
            raise ValidationError(_(
                "Period rollforwards apply only to equity-method members that "
                "have not elected the fair value option."
            ))
        for field_name in (
            'dividends_received', 'oci_share', 'foreign_exchange_adjustment',
        ):
            try:
                value = float(vals.get(
                    field_name, current[field_name] if current else 0.0,
                ) or 0.0)
            except (TypeError, ValueError, OverflowError):
                value = float('nan')
            if not math.isfinite(value):
                raise ValidationError(_("IAS 28 rollforward amounts must be finite."))
            if field_name == 'dividends_received' and value < 0.0:
                raise ValidationError(_("Dividends received cannot be negative."))
        dividend = float(vals.get(
            'dividends_received', current.dividends_received if current else 0.0,
        ) or 0.0)
        dividend_account = self.env['account.account'].browse(vals.get(
            'dividend_income_account_id',
            current.dividend_income_account_id.id if current else False,
        ))
        if dividend and (
            not dividend_account
            or dividend_account.account_type not in _INCOME_TYPES
        ):
            raise ValidationError(_(
                "A non-zero associate dividend needs an explicit dividend "
                "income account."
            ))
        oci = float(vals.get(
            'oci_share', current.oci_share if current else 0.0,
        ) or 0.0)
        oci_account = self.env['account.account'].browse(vals.get(
            'oci_account_id', current.oci_account_id.id if current else False,
        ))
        if oci and (not oci_account or oci_account.account_type not in _OCI_TYPES):
            raise ValidationError(_(
                "A non-zero associate OCI share needs an explicit equity/OCI "
                "account."
            ))
        foreign = member.functional_currency_id != run.presentation_currency_id
        confirmed = vals.get(
            'foreign_exchange_confirmed',
            current.foreign_exchange_confirmed if current else False,
        )
        basis = vals.get('basis', current.basis if current else False)
        if foreign and (not confirmed or not (basis or '').strip()):
            raise ValidationError(_(
                "A foreign equity-method associate needs a confirmed IAS 21 "
                "carrying-translation adjustment and written basis."
            ))
        return run

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            run = self._eh_validate_values(vals)
            run._eh_assert_company_closure()
            if run.state != 'draft':
                raise UserError(_("Equity rollforwards are editable only in draft."))
        return super().create(vals_list)

    def write(self, vals):
        for record in self:
            record.run_id._eh_lock_for_transition()
            if record.run_id.state != 'draft':
                raise UserError(_("Equity rollforwards are editable only in draft."))
            self._eh_validate_values(vals, current=record)
        return super().write(vals)

    def unlink(self):
        for record in self:
            record.run_id._eh_lock_for_transition()
            if record.run_id.state != 'draft':
                raise UserError(_("Equity rollforwards are editable only in draft."))
        return super().unlink()


class EhConsolDisposalEvent(models.Model):
    _name = 'eh.consol.disposal.event'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidation disposal event"
    _order = 'event_at, id'

    run_id = fields.Many2one(
        'eh.consol.run', required=True, ondelete='cascade', index=True,
    )
    member_id = fields.Many2one(
        'eh.consol.member', required=True, ondelete='restrict', index=True,
    )
    event_at = fields.Datetime(required=True, readonly=True)
    event_by_id = fields.Many2one('res.users', required=True, readonly=True)
    disposal_pct = fields.Float(required=True, digits=(7, 4), readonly=True)
    effective_pct = fields.Float(required=True, digits=(7, 4), readonly=True)
    loses_control = fields.Boolean(readonly=True)
    treatment = fields.Selection([
        ('profit_or_loss', "Recycled to profit or loss"),
        ('nci', "Reattributed to NCI"),
    ], required=True, readonly=True)
    reserve_account_id = fields.Many2one(
        'account.account', required=True, ondelete='restrict', readonly=True,
    )
    counterpart_account_id = fields.Many2one(
        'account.account', required=True, ondelete='restrict', readonly=True,
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', ondelete='restrict', readonly=True,
    )
    reclass_amount = fields.Monetary(
        required=True, currency_field='presentation_currency_id', readonly=True,
        help=(
            "Signed original CTA reserve slice. The reserve line is its "
            "negation and the counterpart line uses this amount."
        ),
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id', store=True, readonly=True,
    )
    notes = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        ):
            raise AccessError(_("Disposal events are immutable engine evidence."))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("Disposal events are immutable engine evidence."))

    def unlink(self):
        if not (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        ):
            raise AccessError(_("Disposal events are immutable engine evidence."))
        return super().unlink()


class EhConsolTrialBalance(models.Model):
    _name = 'eh.consol.trial.balance'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidated trial balance"
    _auto = False
    _order = 'account_code, account_id'

    run_id = fields.Many2one('eh.consol.run', readonly=True, index=True)
    account_id = fields.Many2one('account.account', readonly=True)
    account_code = fields.Char(readonly=True)
    account_name = fields.Char(readonly=True)
    account_type = fields.Selection(
        selection=lambda self: self.env['account.account']._fields[
            'account_type'
        ]._description_selection(self.env),
        readonly=True,
    )
    presentation_currency_id = fields.Many2one('res.currency', readonly=True)
    parent_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    subsidiary_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    elimination_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    nci_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    cta_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    other_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )
    total_amount = fields.Monetary(
        currency_field='presentation_currency_id', readonly=True,
    )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW eh_consol_trial_balance AS (
                SELECT MIN(line.id) AS id,
                       line.run_id,
                       line.account_id,
                       MIN(line.account_code) AS account_code,
                       MIN(line.account_name) AS account_name,
                       MIN(line.account_type) AS account_type,
                       MIN(line.presentation_currency_id)
                           AS presentation_currency_id,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind = 'parent_balance'), 0.0)
                           AS parent_amount,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind = 'subsidiary_balance'), 0.0)
                           AS subsidiary_amount,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind = 'elimination'), 0.0)
                           AS elimination_amount,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind = 'nci'), 0.0)
                           AS nci_amount,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind IN ('cta', 'cta_recycle')), 0.0)
                           AS cta_amount,
                       COALESCE(SUM(line.amount) FILTER (
                           WHERE line.kind NOT IN (
                               'parent_balance', 'subsidiary_balance',
                               'elimination', 'nci', 'cta', 'cta_recycle'
                           )), 0.0) AS other_amount,
                       COALESCE(SUM(line.amount), 0.0) AS total_amount
                  FROM eh_consol_run_line AS line
                 WHERE line.account_id IS NOT NULL
                 GROUP BY line.run_id, line.account_id
            )
        """)

    def action_drilldown(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Consolidation trial-balance detail"),
            'res_model': 'eh.consol.run.line',
            'view_mode': 'list,form',
            'domain': [
                ('run_id', '=', self.run_id.id),
                ('account_id', '=', self.account_id.id),
            ],
            'context': {'default_run_id': self.run_id.id},
        }
