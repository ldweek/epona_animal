# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Asset disposal wizard.

Posts the disposal entry (IAS 16.67-72 derecognition):

  Dr Cash / Receivable        proceeds
  Dr Accumulated depreciation total_depreciated
  Dr Accumulated impairment   net posted impairment (per contra account)
  Dr Loss (or Cr Gain)        balancing
  Cr Asset                    acquisition_cost

Both contra balances (accumulated depreciation AND accumulated impairment)
are removed so no impairment is stranded on the balance sheet after the
asset is gone, and the gain / loss is measured against the true carrying
amount (cost less depreciation less impairment), which is exactly the
figure shown on the wizard before posting.

On disposal, any remaining revaluation surplus (IAS 16.41) is recycled
directly to retained earnings, never through P&L, as an extra pair of
equity legs that net to zero within the same balanced disposal move:

  Dr Revaluation Reserve   revaluation_surplus
  Cr Retained Earnings     revaluation_surplus

The asset's revaluation_surplus is zeroed after posting.

Marks remaining (unposted) schedule lines as cancelled by removing them,
and moves the asset to the disposed state.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.accounting_integrity import _eh_validate_accounting_company


class EhAssetDisposeWizard(models.TransientModel):
    _name = 'eh.asset.dispose.wizard'
    _description = "Asset Disposal Wizard"

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade',
    )
    disposal_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    proceeds = fields.Monetary(default=0.0)
    sale_invoice_line_id = fields.Many2one(
        'account.move.line', string="Posted Asset Sale Invoice Line",
        check_company=True,
        domain="[('move_id.move_type', '=', 'out_invoice'), "
               "('move_id.state', '=', 'posted'), "
               "('display_type', '=', 'product'), "
               "('company_id', '=', company_id)]",
        help=(
            "Optional posted customer-invoice product line for this asset "
            "sale. Its company-currency net amount becomes proceeds; the "
            "disposal entry debits that line's revenue/clearing account. The "
            "invoice remains the sole receivable and output-tax source."
        ),
    )
    partner_id = fields.Many2one('res.partner', string="Buyer / Counterparty")
    cash_account_id = fields.Many2one(
        'account.account', string="Proceeds Account",
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]",
        help="Where to debit the proceeds. Required if proceeds > 0.",
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account', string="Revaluation Reserve Account",
        help="Equity revaluation reserve to debit when recycling any "
             "remaining revaluation surplus on disposal (IAS 16.41). "
             "Required only when the asset still carries a surplus.",
    )
    retained_earnings_account_id = fields.Many2one(
        'account.account', string="Retained Earnings Account",
        help="Equity retained-earnings account to credit with the recycled "
             "revaluation surplus on disposal (IAS 16.41). The transfer is "
             "made directly within equity, never through P&L. Required only "
             "when the asset still carries a surplus.",
    )
    notes = fields.Text()
    processed = fields.Boolean(readonly=True, copy=False)

    revaluation_surplus = fields.Monetary(
        related='asset_id.revaluation_surplus', readonly=True,
        help="Remaining revaluation surplus that will be recycled to "
             "retained earnings on disposal.",
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', readonly=True,
    )
    nbv = fields.Monetary(
        compute='_compute_nbv', readonly=True,
        help="Net book value of the asset right now.",
    )
    expected_gain_loss = fields.Monetary(
        compute='_compute_gain_loss', readonly=True,
    )

    @api.depends('asset_id')
    def _compute_nbv(self):
        for w in self:
            w.nbv = w._eh_carrying_amount() if w.asset_id else 0.0

    @api.depends('asset_id', 'proceeds', 'sale_invoice_line_id',
                 'sale_invoice_line_id.balance')
    def _compute_gain_loss(self):
        for w in self:
            w.expected_gain_loss = (
                w._eh_effective_proceeds() - (w.nbv or 0.0)
            )

    def _eh_effective_proceeds(self):
        self.ensure_one()
        if self.sale_invoice_line_id:
            return self.asset_id.currency_id.round(
                -self.sale_invoice_line_id.balance,
            )
        return self.proceeds or 0.0

    def _eh_validate_sale_invoice_line(self):
        self.ensure_one()
        line = self.sale_invoice_line_id
        if not line:
            return line
        move = line.move_id
        asset = self.asset_id
        if (move.move_type != 'out_invoice' or move.state != 'posted'
                or line.display_type != 'product'
                or move.company_id != asset.company_id):
            raise UserError(_(
                "Asset sale evidence must be a posted same-company customer-"
                "invoice product line."
            ))
        proceeds = asset.currency_id.round(-line.balance)
        if proceeds <= 0:
            raise UserError(_(
                "The selected sale invoice line must carry a positive net "
                "credit amount in company currency."
            ))
        duplicate = self.env['eh.asset'].search([
            ('disposal_invoice_line_id', '=', line.id),
            ('id', '!=', asset.id),
        ], limit=1)
        if duplicate:
            raise UserError(_(
                "Invoice line %(line)s already supports disposal of %(asset)s.",
                line=line.display_name, asset=duplicate.display_name,
            ))
        if (self.proceeds or 0.0) or self.cash_account_id:
            raise UserError(_(
                "Use either a posted sale invoice line or manual proceeds, "
                "not both. Clear Proceeds and Proceeds Account for the "
                "invoice-backed path."
            ))
        return line

    def _eh_posted_impairment_by_account(self):
        """Net posted impairment (charges minus reversals) grouped by the
        contra account each event was booked to.

        Impairment is credited to accumulated_account_id when set, otherwise
        to the asset's accumulated_depreciation_account_id (the same fallback
        the impairment posting uses). Grouping by account lets disposal debit
        back exactly what is sitting in each contra account, so nothing is
        stranded regardless of how the charges were configured. Only posted
        events are considered: they are the ones actually in the ledger.
        """
        self.ensure_one()
        asset = self.asset_id
        by_account = {}
        for imp in asset.impairment_ids.filtered(lambda i: i.state == 'posted'):
            contra = (imp.accumulated_account_id
                      or asset.accumulated_depreciation_account_id)
            signed = -imp.amount if imp.is_reversal else imp.amount
            by_account[contra] = by_account.get(contra, 0.0) + signed
        return by_account

    def _eh_carrying_amount(self):
        """True carrying amount: cost less accumulated depreciation less
        net posted impairment. This is the basis for the disposal gain/loss
        and is the figure the wizard displays, so the posted result matches
        what the approving manager was shown."""
        self.ensure_one()
        asset = self.asset_id
        net_impairment = sum(self._eh_posted_impairment_by_account().values())
        carrying = (
            asset.acquisition_cost
            + asset.revaluation_adjustment
            - asset.total_depreciated
            - net_impairment
        )
        return asset.currency_id.round(carrying) if asset.currency_id else carrying

    def action_dispose(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_dispose_wizard WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        self.invalidate_recordset(['processed'])
        if self.processed:
            raise UserError(_("This disposal wizard has already been applied."))
        asset = self.asset_id
        # Prove the caller may mutate this exact asset before reading its
        # state or creating any accounting side effect.  The wizard itself
        # has no company rule, so relying on its related company_id would let
        # a guessed foreign asset id reach this public method.
        asset._eh_check_access('write')
        asset._eh_lock_for_transition()
        self.invalidate_recordset(['nbv', 'expected_gain_loss'])
        if asset.state == 'disposed':
            raise UserError(_("Asset is already disposed."))
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only EH accounting managers can dispose of assets.",
            ))
        if asset.deferred_type != 'asset':
            raise UserError(_(
                "%(record)s is a %(kind)s recognition schedule, not a fixed "
                "asset. IAS 16 disposal would derecognise its holding account "
                "with fixed-asset gain/loss legs and produce a structurally "
                "wrong journal entry. Correct, finish, or explicitly reverse "
                "the recognition schedule instead.",
                record=asset.display_name,
                kind=asset.deferred_type,
            ))
        if self.proceeds < 0:
            raise UserError(_("Disposal proceeds cannot be negative."))
        sale_line = self._eh_validate_sale_invoice_line()
        if asset.is_under_construction:
            raise UserError(_(
                "An asset under construction must be capitalised or corrected "
                "through its AUC account before disposal."
            ))
        if asset.lvp_pool_id:
            raise UserError(_(
                "%(asset)s belongs to low-value pool %(pool)s. Individual "
                "disposal would double-derecognise carrying value already "
                "moved into the pool; pool disposal adjustments are not "
                "implemented.",
                asset=asset.display_name,
                pool=asset.lvp_pool_id.display_name,
            ))
        live_book_postings = asset.book_ids.mapped('line_ids').filtered(
            lambda line: line.is_posted and not line.reversal_move_id,
        )
        if live_book_postings:
            raise UserError(_(
                "Reverse all live historical parallel-book journal entries "
                "before disposing this asset; otherwise their accumulated "
                "depreciation would remain stranded."
            ))
        latest_evidence_date = asset._eh_latest_accounting_evidence_date()
        if latest_evidence_date and self.disposal_date < latest_evidence_date:
            raise UserError(_(
                "Disposal date cannot precede the latest fixed-asset "
                "accounting evidence (%s).",
                latest_evidence_date,
            ))
        _eh_validate_accounting_company(self, (
            'cash_account_id', 'revaluation_reserve_account_id',
            'retained_earnings_account_id',
        ))
        if (self.proceeds or 0.0) > 0 and not self.cash_account_id:
            raise UserError(_(
                "Provide a proceeds account when proceeds is greater than zero.",
            ))
        asset._validate_posting_setup()

        # IAS 16.55: recognise full due rows and the earned fraction of the
        # current period before measuring the carrying amount derecognised.
        asset._eh_post_depreciation_through(self.disposal_date)
        asset.invalidate_recordset(['total_depreciated', 'net_book_value'])
        self.invalidate_recordset(['nbv', 'expected_gain_loss'])

        accumulated = asset.total_depreciated
        cost = asset.acquisition_cost
        proceeds = self._eh_effective_proceeds()
        impairment_by_account = self._eh_posted_impairment_by_account()
        nbv = self._eh_carrying_amount()
        currency = asset.currency_id
        gain_loss = currency.round(proceeds - nbv) if currency else proceeds - nbv

        lines = []
        if proceeds > 0:
            lines.append((0, 0, {
                'name': (
                    _("Clear invoiced asset-sale proceeds %s", asset.display_name)
                    if sale_line else
                    _("Disposal proceeds %s", asset.display_name)
                ),
                'account_id': (
                    sale_line.account_id.id if sale_line
                    else self.cash_account_id.id
                ),
                'partner_id': (
                    sale_line.move_id.partner_id.id if sale_line
                    else self.partner_id.id if self.partner_id else False
                ),
                'debit': proceeds,
                'credit': 0.0,
            }))
        if accumulated > 0:
            lines.append((0, 0, {
                'name': _("Accumulated depreciation reversal %s", asset.display_name),
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': accumulated,
                'credit': 0.0,
            }))
        # Derecognise accumulated impairment against the same contra
        # account(s) it was charged to, so no impairment lingers on the
        # balance sheet after the asset leaves. Normal case is a debit
        # (net charge); a net reversal balance flips to a credit.
        for contra_account, net in impairment_by_account.items():
            net = currency.round(net) if currency else net
            if not net:
                continue
            lines.append((0, 0, {
                'name': _("Accumulated impairment reversal %s", asset.display_name),
                'account_id': contra_account.id,
                'debit': net if net > 0 else 0.0,
                'credit': -net if net < 0 else 0.0,
            }))
        if gain_loss > 0:
            if not asset.disposal_gain_account_id:
                raise UserError(_(
                    "Asset gain on disposal: configure a Disposal Gain "
                    "account on the asset or its category.",
                ))
            lines.append((0, 0, {
                'name': _("Gain on disposal %s", asset.display_name),
                'account_id': asset.disposal_gain_account_id.id,
                'debit': 0.0,
                'credit': gain_loss,
            }))
        elif gain_loss < 0:
            if not asset.disposal_loss_account_id:
                raise UserError(_(
                    "Asset loss on disposal: configure a Disposal Loss "
                    "account on the asset or its category.",
                ))
            lines.append((0, 0, {
                'name': _("Loss on disposal %s", asset.display_name),
                'account_id': asset.disposal_loss_account_id.id,
                'debit': abs(gain_loss),
                'credit': 0.0,
            }))
        if asset.asset_account_id:
            # Derecognise the full gross carrying on the asset account: the
            # original cost plus any revaluation adjustment that a prior
            # uplift/downward revaluation posted to this account. Crediting
            # only cost would strand the revaluation on the balance sheet.
            gross_asset = currency.round(cost + asset.revaluation_adjustment) \
                if currency else cost + asset.revaluation_adjustment
            lines.append((0, 0, {
                'name': _("Asset cost reversal %s", asset.display_name),
                'account_id': asset.asset_account_id.id,
                'debit': 0.0 if gross_asset >= 0 else -gross_asset,
                'credit': gross_asset if gross_asset >= 0 else 0.0,
            }))
        else:
            raise UserError(_(
                "Asset is missing the Asset Account; cannot reverse the "
                "capitalised cost on disposal.",
            ))

        # IAS 16.41: recycle any remaining revaluation surplus directly to
        # retained earnings, NOT through P&L. These two legs net to zero
        # within equity, so they leave the disposal move balanced and do not
        # disturb the gain/loss measurement above. Assets that never carried
        # a surplus (revaluation_surplus == 0) get no extra legs and behave
        # exactly as before.
        surplus = currency.round(asset.revaluation_surplus) if currency \
            else asset.revaluation_surplus
        if surplus:
            if not self.revaluation_reserve_account_id \
                    or not self.retained_earnings_account_id:
                raise UserError(_(
                    "Asset %(asset)s carries a revaluation surplus of "
                    "%(amt).2f that must be recycled to retained earnings on "
                    "disposal (IAS 16.41). Provide both a Revaluation Reserve "
                    "account and a Retained Earnings account.",
                    asset=asset.display_name, amt=surplus,
                ))
            non_equity = (
                self.revaluation_reserve_account_id
                | self.retained_earnings_account_id
            ).filtered(lambda account: account.account_type != 'equity')
            if non_equity:
                raise UserError(_(
                    "Revaluation Reserve and Retained Earnings must both be "
                    "equity accounts; correct %(accounts)s.",
                    accounts=', '.join(non_equity.mapped('display_name')),
                ))
            lines.append((0, 0, {
                'name': _("Revaluation surplus recycle %s", asset.display_name),
                'account_id': self.revaluation_reserve_account_id.id,
                'debit': surplus if surplus > 0 else 0.0,
                'credit': -surplus if surplus < 0 else 0.0,
            }))
            lines.append((0, 0, {
                'name': _(
                    "Retained earnings from surplus %s", asset.display_name,
                ),
                'account_id': self.retained_earnings_account_id.id,
                'debit': -surplus if surplus < 0 else 0.0,
                'credit': surplus if surplus > 0 else 0.0,
            }))

        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'date': self.disposal_date,
            'journal_id': asset.journal_id.id,
            'ref': _("Disposal %s", asset.display_name),
            'line_ids': lines,
        })
        move.action_post()

        # Drop unposted schedule lines.
        unposted = asset.depreciation_line_ids.filtered(lambda l: not l.is_posted)
        unposted.sudo().unlink()

        disposal_vals = {
            'state': 'disposed',
            'disposed_at': fields.Datetime.now(),
            'disposed_by_id': self.env.user.id,
            'disposal_date': self.disposal_date,
            'disposal_proceeds': proceeds,
            'disposal_partner_id': self.partner_id.id if self.partner_id else False,
            'disposal_move_id': move.id,
        }
        if sale_line:
            disposal_vals.update({
                'disposal_partner_id': sale_line.move_id.partner_id.id,
                'disposal_invoice_id': sale_line.move_id.id,
                'disposal_invoice_line_id': sale_line.id,
            })
        # Surplus has been recycled to retained earnings; zero it so no
        # revaluation reserve is stranded against a disposed asset.
        if surplus:
            disposal_vals['revaluation_surplus'] = 0.0
        asset._eh_workflow_write(disposal_vals)
        if self.notes:
            asset.message_post(body=_("Disposal notes: %s", self.notes))
        self.sudo().write({'processed': True})
        return {'type': 'ir.actions.act_window_close'}

    def write(self, vals):
        if 'processed' in vals and not self.env.su:
            raise UserError(_("Processed status is server-owned."))
        return super().write(vals)
