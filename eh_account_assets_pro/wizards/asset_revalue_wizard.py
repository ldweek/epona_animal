# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset revaluation wizard.

Two flavours:

* Uplift  Dr Asset, Cr Revaluation Reserve / Income (counterpart_account_id).
          The uplift also increments the asset's revaluation_surplus, the
          equity reserve balance tracked per IAS 16.39.
* Impair  IAS 16.40 downward revaluation. The decrease first reverses any
          existing revaluation surplus (Dr Revaluation Reserve up to the
          surplus balance) and only the excess is recognised in P&L
          (Dr Impairment / Expense via counterpart_account_id):

            dr_to_reserve = min(amount, revaluation_surplus)
            dr_to_pl      = amount - dr_to_reserve
            Dr Revaluation Reserve  dr_to_reserve
            Dr Impairment / Expense dr_to_pl
            Cr Asset                amount

          The surplus is decremented by dr_to_reserve.

After posting, the remaining (unposted) schedule lines are wiped and the
selected consumption method is rebuilt over the remaining useful life on
the new net book value.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.accounting_integrity import _eh_validate_accounting_company
from ..models.asset import (
    _ASSET_REVALUATION_CAPABILITY,
    _ASSET_REVALUATION_CONTEXT_KEY,
)


class EhAssetRevalueWizard(models.TransientModel):
    _name = 'eh.asset.revalue.wizard'
    _description = "Asset Revaluation Wizard"

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade',
    )
    revalue_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    direction = fields.Selection([
        ('uplift', "Uplift (Posts Live Entry)"),
        ('impairment', "Impairment"),
    ], required=True, default='impairment')
    amount = fields.Monetary(required=True)
    counterpart_account_id = fields.Many2one(
        'account.account', required=True,
        help="Counterpart account: revaluation reserve / income for "
             "uplift, impairment loss / expense (the P&L leg) for a "
             "downward revaluation.",
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account',
        string="Revaluation Reserve Account",
        help="Equity revaluation reserve. On a downward revaluation the "
             "existing revaluation surplus is reversed against this account "
             "before any excess is charged to P&L (IAS 16.40). Leave blank "
             "to reuse the uplift counterpart account. Only consulted when "
             "the asset carries a revaluation surplus.",
    )
    revaluation_income_account_id = fields.Many2one(
        'account.account',
        string="Revaluation Income (P&L) Account",
        help="P&L income account used on an uplift to reverse a revaluation "
             "decrease previously recognised in P&L (IAS 16.39): the uplift "
             "credits income up to the prior decrease before any remainder is "
             "credited to the revaluation surplus. Leave blank to reuse the "
             "uplift counterpart account. Only consulted when the asset "
             "carries a prior P&L revaluation decrease.",
    )
    notes = fields.Text()
    processed = fields.Boolean(readonly=True, copy=False)

    # ---- IAS 36 uplift cap (recoverable amount) ----
    recoverable_latest = fields.Monetary(
        related='asset_id.recoverable_amount_latest', readonly=True,
        string="Latest Recoverable Amount",
    )
    recoverable_date = fields.Date(
        related='asset_id.recoverable_amount_date', readonly=True,
        string="Recoverable Amount Date",
    )
    override_recoverable_cap = fields.Boolean(
        string="Override recoverable-amount cap",
        default=False,
        help=(
            "IAS 36 does not permit carrying an asset above its "
            "recoverable amount, so an uplift beyond the latest "
            "recoverable-amount measurement is blocked. A manager may "
            "override when a NEW recoverable-amount assessment "
            "supports the higher value (e.g. a fresh valuation not yet "
            "recorded as a test); the override and its reason are "
            "logged on the asset's audit trail."
        ),
    )
    override_reason = fields.Text(
        help=(
            "Documented basis for revaluing above the latest recorded "
            "recoverable amount. Required when the override is ticked; "
            "logged on the asset."
        ),
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', readonly=True,
    )
    nbv = fields.Monetary(compute='_compute_nbv', readonly=True)
    new_nbv = fields.Monetary(compute='_compute_new_nbv', readonly=True)

    @api.depends('asset_id')
    def _compute_nbv(self):
        for w in self:
            w.nbv = w.asset_id.net_book_value if w.asset_id else 0.0

    @api.depends('asset_id', 'amount', 'direction')
    def _compute_new_nbv(self):
        for w in self:
            sign = 1 if w.direction == 'uplift' else -1
            w.new_nbv = (w.nbv or 0.0) + sign * (w.amount or 0.0)

    def action_revalue(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_revalue_wizard WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset(['processed'])
        if self.processed:
            raise UserError(_(
                "This revaluation wizard has already been applied."
            ))
        asset = self.asset_id
        # The transient wizard has no company rule of its own.  Check the
        # source record before any state/configuration read or journal entry,
        # otherwise a caller could invoke the public method with a guessed
        # foreign-company asset id.
        asset._eh_check_access('write')
        asset._eh_lock_for_transition()
        self.invalidate_recordset(['nbv', 'new_nbv'])
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only EH accounting managers can revalue assets.",
            ))
        if asset.state not in ('running', 'paused'):
            raise UserError(_(
                "Revaluation requires a running or paused asset. A fully "
                "depreciated asset has no remaining useful-life periods; "
                "record a supported useful-life reassessment before any "
                "future revaluation workflow.",
            ))
        if asset.is_under_construction or asset.lvp_pool_id:
            raise UserError(_(
                "Assets under construction or already transferred to a "
                "low-value pool cannot use the individual revaluation "
                "workflow."
            ))
        latest_evidence_date = asset._eh_latest_accounting_evidence_date()
        if latest_evidence_date and self.revalue_date < latest_evidence_date:
            raise UserError(_(
                "Revaluation date cannot precede the latest fixed-asset "
                "accounting evidence (%s).",
                latest_evidence_date,
            ))
        _eh_validate_accounting_company(self, (
            'counterpart_account_id', 'revaluation_reserve_account_id',
            'revaluation_income_account_id',
        ))
        if (self.amount or 0.0) <= 0:
            raise UserError(_("Revaluation amount must be positive."))
        if self.direction == 'uplift' and asset.accumulated_impairment > 0:
            raise UserError(_(
                "%(asset)s carries a prior impairment of %(imp).2f. Recovering "
                "it must be recognised as an impairment reversal, routed "
                "through P&L and capped at depreciated historical cost per "
                "IAS 36.117, not credited to a revaluation surplus. Use the "
                "Record Impairment action with 'reversal' set. A revaluation "
                "uplift is only appropriate once no impairment remains.",
                asset=asset.display_name, imp=asset.accumulated_impairment,
            ))
        asset._validate_posting_setup()
        if not asset.asset_account_id:
            raise UserError(_(
                "Asset is missing the Asset Account; cannot revalue.",
            ))
        sign = 1 if self.direction == 'uplift' else -1
        new_carrying = asset.net_book_value + sign * self.amount
        if new_carrying <= asset.salvage_value:
            raise UserError(_(
                "Revaluation would reduce the carrying amount below salvage "
                "value.",
            ))
        unposted = asset.depreciation_line_ids.filtered(
            lambda line: not line.is_posted,
        )
        remaining_periods = len(unposted)
        if not remaining_periods:
            raise UserError(_(
                "No remaining periods to absorb the revaluation.",
            ))
        # IAS 36: an asset must not be carried above its recoverable
        # amount. When a recoverable-amount measurement exists (from a
        # CGU test or an impairment event that stated one), an uplift
        # that would carry the asset beyond it is blocked unless a
        # manager overrides with a documented reason.
        if self.direction == 'uplift' and asset.recoverable_amount_latest:
            currency0 = asset.currency_id
            target_carrying = currency0.round(
                asset.net_book_value + self.amount,
            )
            cap = asset.recoverable_amount_latest
            if currency0.compare_amounts(target_carrying, cap) > 0:
                if not self.override_recoverable_cap:
                    raise UserError(_(
                        "The uplift would carry %(asset)s at %(new).2f, "
                        "above its latest recoverable amount of %(cap).2f "
                        "(measured %(date)s). IAS 36 does not permit "
                        "carrying an asset above its recoverable amount. "
                        "Reduce the uplift, record a new recoverable-"
                        "amount measurement, or tick the manager "
                        "override with a documented reason.",
                        asset=asset.display_name, new=target_carrying,
                        cap=cap, date=asset.recoverable_amount_date,
                    ))
                if not (self.override_reason or '').strip():
                    raise UserError(_(
                        "Overriding the recoverable-amount cap requires "
                        "a documented reason.",
                    ))
                asset.message_post(body=_(
                    "Recoverable-amount cap OVERRIDDEN by %(user)s: "
                    "uplift to %(new).2f exceeds the latest recoverable "
                    "amount %(cap).2f (measured %(date)s). Reason: "
                    "%(reason)s",
                    user=self.env.user.display_name, new=target_carrying,
                    cap=cap, date=asset.recoverable_amount_date,
                    reason=self.override_reason.strip(),
                ))

        currency = asset.currency_id
        # dr_to_reserve is the slice of a downward revaluation that reverses
        # a previously-recognised revaluation surplus (IAS 16.40). It is set
        # in the impairment branch and consumed after posting to decrement
        # revaluation_surplus.
        dr_to_reserve = 0.0
        # cr_to_pl is the slice of an uplift that reverses a revaluation
        # decrease previously recognised in P&L (IAS 16.39). It is set in the
        # uplift branch and consumed after posting to decrement
        # revaluation_pl_decrease. When no prior decrease exists it is zero and
        # the uplift collapses to the original Dr Asset / Cr Reserve entry, so
        # legacy behaviour is unchanged.
        cr_to_pl = 0.0
        if self.direction == 'uplift':
            # IAS 16.39: an increase is recognised in P&L to the extent it
            # reverses a revaluation decrease of the same asset previously
            # recognised in P&L; only the remainder is credited to the
            # revaluation surplus.
            prior_decrease = asset.revaluation_pl_decrease or 0.0
            cr_to_pl = currency.round(min(self.amount, prior_decrease))
            cr_to_surplus = currency.round(self.amount - cr_to_pl)
            income_account = (
                self.revaluation_income_account_id
                or self.counterpart_account_id
            )
            lines = [
                (0, 0, {
                    'name': _("Asset uplift %s", asset.display_name),
                    'account_id': asset.asset_account_id.id,
                    'debit': self.amount,
                    'credit': 0.0,
                }),
            ]
            if cr_to_pl > 0:
                lines.append((0, 0, {
                    'name': _(
                        "Revaluation decrease reversal %s",
                        asset.display_name,
                    ),
                    'account_id': income_account.id,
                    'debit': 0.0,
                    'credit': cr_to_pl,
                }))
            if cr_to_surplus > 0:
                lines.append((0, 0, {
                    'name': _("Revaluation reserve %s", asset.display_name),
                    'account_id': self.counterpart_account_id.id,
                    'debit': 0.0,
                    'credit': cr_to_surplus,
                }))
        else:
            # IAS 16.40: a decrease first reverses any existing revaluation
            # surplus, and only the excess is recognised in P&L. When no
            # surplus is present (the pre-Wave-1 case) dr_to_reserve is zero
            # and this collapses to the original Dr P&L / Cr Asset entry, so
            # legacy behaviour is unchanged.
            surplus = asset.revaluation_surplus or 0.0
            dr_to_reserve = currency.round(min(self.amount, surplus))
            dr_to_pl = currency.round(self.amount - dr_to_reserve)
            reserve_account = (
                self.revaluation_reserve_account_id
                or self.counterpart_account_id
            )
            lines = []
            if dr_to_reserve > 0:
                lines.append((0, 0, {
                    'name': _(
                        "Revaluation surplus reversal %s",
                        asset.display_name,
                    ),
                    'account_id': reserve_account.id,
                    'debit': dr_to_reserve,
                    'credit': 0.0,
                }))
            if dr_to_pl > 0:
                lines.append((0, 0, {
                    'name': _("Asset impairment %s", asset.display_name),
                    'account_id': self.counterpart_account_id.id,
                    'debit': dr_to_pl,
                    'credit': 0.0,
                }))
            lines.append((0, 0, {
                'name': _("Asset reduction %s", asset.display_name),
                'account_id': asset.asset_account_id.id,
                'debit': 0.0,
                'credit': self.amount,
            }))
        reserve_account_used = False
        if self.direction == 'uplift' and (self.amount - cr_to_pl) > 0:
            reserve_account_used = self.counterpart_account_id
        elif self.direction == 'impairment' and dr_to_reserve > 0:
            reserve_account_used = (
                self.revaluation_reserve_account_id
                or self.counterpart_account_id
            )
        if (reserve_account_used
                and asset.revaluation_reserve_account_id
                and asset.revaluation_reserve_account_id
                != reserve_account_used):
            raise UserError(_(
                "This asset's revaluation surplus is already tied to reserve "
                "account %(account)s; use that same account.",
                account=asset.revaluation_reserve_account_id.display_name,
            ))
        if (reserve_account_used
                and reserve_account_used.account_type != 'equity'):
            raise UserError(_(
                "Revaluation reserve %(account)s must be an equity account.",
                account=reserve_account_used.display_name,
            ))
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.revalue_date,
            'journal_id': asset.journal_id.id,
            'ref': _("Revaluation %s", asset.display_name),
            'line_ids': lines,
        })
        move.action_post()

        # Record the revaluation as a carrying-amount adjustment, then
        # rebuild the remaining schedule. acquisition_cost is deliberately
        # left untouched: historical cost, and the IAS 36.117 depreciated-
        # cost ceiling derived from it, must stay intact. net_book_value
        # includes revaluation_adjustment and _build_remaining_schedule
        # bases depreciation on net_book_value, so future charges run off
        # the revalued base.
        unposted.sudo().unlink()
        # Track the equity revaluation surplus and the P&L revaluation-decrease
        # balance alongside the carrying-amount adjustment.
        #  * An uplift first reverses any prior P&L decrease (cr_to_pl, which
        #    reduces revaluation_pl_decrease) and only the remainder
        #    (amount - cr_to_pl) is added to the surplus.
        #  * A downward revaluation removes the slice that reversed the surplus
        #    (dr_to_reserve, keeping the surplus non-negative) and records the
        #    excess routed to P&L (dr_to_pl, increasing revaluation_pl_decrease)
        #    so a later uplift knows how much to reverse through P&L first.
        # Assets that never carried a surplus or a P&L decrease keep both
        # balances at 0.0, so legacy behaviour is byte-identical.
        currency = asset.currency_id
        if self.direction == 'uplift':
            new_surplus = (
                (asset.revaluation_surplus or 0.0) + (self.amount - cr_to_pl)
            )
            new_pl_decrease = (
                (asset.revaluation_pl_decrease or 0.0) - cr_to_pl
            )
        else:
            new_surplus = (asset.revaluation_surplus or 0.0) - dr_to_reserve
            new_pl_decrease = (
                (asset.revaluation_pl_decrease or 0.0) + dr_to_pl
            )
        asset_vals = {
            'revaluation_adjustment': (
                asset.revaluation_adjustment + sign * self.amount
            ),
            'revaluation_surplus': currency.round(new_surplus),
            'revaluation_pl_decrease': currency.round(new_pl_decrease),
            'revaluation_move_ids': [(4, move.id)],
        }
        if reserve_account_used:
            asset_vals['revaluation_reserve_account_id'] = (
                reserve_account_used.id
            )
        asset.with_context({
            _ASSET_REVALUATION_CONTEXT_KEY: _ASSET_REVALUATION_CAPABILITY,
        })._eh_workflow_write(asset_vals)
        asset._build_remaining_schedule(
            remaining_periods, start_date=self.revalue_date,
        )
        asset.message_post(
            body=_("%(dir)s of %(amt)s posted: %(notes)s",
                   dir=dict(self._fields['direction'].selection)[self.direction],
                   amt=self.amount,
                   notes=self.notes or '/'),
        )
        self.sudo().write({'processed': True})
        return {'type': 'ir.actions.act_window_close'}

    def write(self, vals):
        if 'processed' in vals and not self.env.su:
            raise UserError(_("Processed status is server-owned."))
        return super().write(vals)
