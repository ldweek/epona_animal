# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.lvp.pool: Australian low-value asset pool.

Australian tax law allows depreciable assets under the low-value
threshold (currently AUD 1,000 cost or AUD 1,000 opening adjustable
value when transferred from individual depreciation) to be grouped
into a single low-value pool. The pool depreciates as a unit at
fixed ATO rates:

  * 18.75% in the first year for assets transferred during the year
    (the "half-year rule" applied by the ATO via the lower rate).
  * 37.5% per year for assets that have been in the pool for at least
    one full year and for the opening pool balance each year.

This implementation models one pool per company (sites can override to
allow multiple pools by year of allocation if their workflow needs
that). Assets transferred into the pool freeze their own schedule;
the pool's annual depreciation cron posts a single JE per year per
pool.

Out of scope (queued for follow-ups):
  * Software development pool (separate ATO regime, similar mechanics).
  * Disposal proceeds adjustment (proceeds reduce the pool balance
    rather than triggering a per-asset gain/loss).
"""

from datetime import date
import json
import math

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .accounting_integrity import (
    _eh_require_exact_posting_date,
    _eh_validate_accounting_company,
)


_FIRST_YEAR_RATE = 18.75
_SUBSEQUENT_YEAR_RATE = 37.5


class EhAssetLvpPool(models.Model):
    _name = 'eh.asset.lvp.pool'
    _description = "AU low-value asset pool"
    _order = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        required=True,
        help=(
            "Display label for the pool. Convention: 'Low-Value Pool "
            "<FY>'. Reused across years; the per-year balance lives "
            "in line_ids."
        ),
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )
    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive pools are hidden from the "
            "transfer-into picker but existing data stays readable."
        ),
    )

    threshold = fields.Monetary(
        currency_field='currency_id',
        default=1000.0,
        help=(
            "Maximum cost of an asset eligible for transfer into this "
            "pool. AU tax sets this at AUD 1,000 for general low-"
            "value assets; the field is configurable so the same "
            "model can support future threshold changes."
        ),
    )
    first_year_rate = fields.Float(
        default=_FIRST_YEAR_RATE, digits=(5, 2),
        help=(
            "Depreciation rate (%) applied to assets transferred "
            "during the year. ATO default: 18.75%."
        ),
    )
    subsequent_year_rate = fields.Float(
        default=_SUBSEQUENT_YEAR_RATE, digits=(5, 2),
        help=(
            "Depreciation rate (%) applied to the opening pool "
            "balance each year. ATO default: 37.5%."
        ),
    )

    asset_ids = fields.One2many(
        'eh.asset', 'lvp_pool_id',
        help="Assets transferred into this pool.",
    )
    asset_count = fields.Integer(
        compute='_compute_pool_totals', store=False,
    )
    pool_balance = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help=(
            "Sum of opening adjustable values of every asset "
            "transferred in, less cumulative pool depreciation. The "
            "balance approaches zero as the pool depreciates each "
            "year and grows as new assets transfer in."
        ),
    )
    transferred_in_total = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help="Lifetime total of opening adjustable values transferred in.",
    )
    accumulated_depreciation = fields.Monetary(
        compute='_compute_pool_totals', store=False,
        currency_field='currency_id',
        help="Lifetime depreciation posted from the pool's annual runs.",
    )

    line_ids = fields.One2many(
        'eh.asset.lvp.pool.line', 'pool_id', copy=False,
        help=(
            "Annual depreciation lines for the pool. One row per year "
            "per pool; computed by action_compute_year."
        ),
    )

    pool_account_id = fields.Many2one(
        'account.account', string="Pool Asset Account",
        check_company=True,
        help=(
            "Balance-sheet account that carries the pool's gross "
            "value. Assets transferred in shift their NBV here."
        ),
    )
    accumulated_account_id = fields.Many2one(
        'account.account', string="Accumulated Pool Depreciation",
        check_company=True,
        help="Balance-sheet contra account for pool depreciation.",
    )
    expense_account_id = fields.Many2one(
        'account.account', string="Pool Depreciation Expense",
        check_company=True,
    )
    journal_id = fields.Many2one(
        'account.journal', string="Pool Journal",
        check_company=True,
    )

    notes = fields.Text()

    @api.constrains(
        'company_id', 'pool_account_id', 'accumulated_account_id',
        'expense_account_id', 'journal_id',
    )
    def _check_accounting_company(self):
        _eh_validate_accounting_company(self, (
            'pool_account_id', 'accumulated_account_id',
            'expense_account_id', 'journal_id',
        ))
        for pool in self:
            if pool.asset_ids.filtered(
                    lambda asset: asset.company_id != pool.company_id):
                raise ValidationError(_(
                    "Every asset in low-value pool %(pool)s must belong to "
                    "the pool company.", pool=pool.display_name,
                ))

    @api.constrains('threshold', 'first_year_rate', 'subsequent_year_rate')
    def _check_rates(self):
        for pool in self:
            if not math.isfinite(pool.threshold) or pool.threshold <= 0:
                raise ValidationError(_(
                    "Low-value-pool threshold must be positive."
                ))
            if not math.isfinite(pool.first_year_rate) \
                    or not (0 <= pool.first_year_rate <= 100):
                raise ValidationError(_(
                    "Low-value-pool first-year rate must be between 0 and 100."
                ))
            if not math.isfinite(pool.subsequent_year_rate) \
                    or not (0 <= pool.subsequent_year_rate <= 100):
                raise ValidationError(_(
                    "Low-value-pool subsequent-year rate must be between 0 "
                    "and 100."
                ))

    def _eh_lock_for_change(self):
        if not self.ids:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        self.env.cr.execute(
            'SELECT id FROM eh_asset_lvp_pool WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'company_id', 'currency_id', 'asset_ids', 'line_ids',
            'threshold', 'first_year_rate', 'subsequent_year_rate',
        ])
        return self

    def write(self, vals):
        if vals:
            if not self.env.su:
                self._eh_check_access('write')
            # Compute, post, transfer and policy edits all serialize on the
            # pool row. Each annual line snapshots the policy that won.
            self._eh_lock_for_change()
        return super().write(vals)

    @api.depends(
        'asset_ids', 'asset_ids.lvp_opening_value',
        'asset_ids.net_book_value', 'line_ids.amount',
    )
    def _compute_pool_totals(self):
        for pool in self:
            pool.asset_count = len(pool.asset_ids)
            transferred = sum(
                self._lvp_asset_base(asset) for asset in pool.asset_ids
            )
            depreciation = sum(pool.line_ids.mapped('amount'))
            pool.transferred_in_total = transferred
            pool.accumulated_depreciation = depreciation
            pool.pool_balance = transferred - depreciation

    @api.model
    def _lvp_asset_base(self, asset):
        """Depreciable base a pooled asset contributes: the opening
        adjustable value captured when it was transferred in (its net book
        value at that moment), which is exactly what was reclassified into
        the pool asset account. Falls back to the live net book value for any
        asset linked to the pool without a captured value (defensive; the
        transfer flow always stamps it)."""
        return asset.lvp_opening_value or asset.net_book_value

    # ---- transfer flow ----

    def action_open_transfer_wizard(self):
        self.ensure_one()
        self._eh_check_access('read')
        if not self.active:
            raise UserError(_(
                "Archived low-value pool %s cannot receive new assets.",
                self.display_name,
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Transfer Asset to Low-Value Pool"),
            'res_model': 'eh.asset.lvp.transfer.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {
                'default_company_id': self.company_id.id,
                'default_pool_id': self.id,
            },
        }

    def action_transfer_asset(self, asset, transfer_date=None):
        """Move an eligible asset into the pool.

        Validates the asset meets the threshold, freezes its primary
        schedule, links lvp_pool_id, and adds the asset's current
        net book value to the pool's transferred_in total.

        GL handling: when the pool carries its own asset account and the
        asset carries its own gross-asset and accumulated-depreciation
        accounts (and the pool has a journal), a balanced reclassification
        move is posted that removes the asset's gross cost from its asset
        account, clears its accumulated depreciation, and lands the net
        book value in the pool asset account:

          CR asset gross-asset account         (acquisition cost)
          DR asset accumulated-depreciation    (accumulated depreciation)
          DR pool asset account                (net book value)

        The two debits sum to the single credit by construction
        (accumulated_depreciation + net_book_value == acquisition_cost),
        so the entry balances. When the pool has no GL accounts configured
        (a tax-only pool that carries no carrying value in the ledger), no
        journal entry is posted and only the operational link is recorded.

        :param asset: eh.asset record to transfer.
        :param transfer_date: optional date for the JE. Defaults to today.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can transfer an asset into a "
                "low-value pool. The transfer freezes its individual schedule "
                "and may reclassify its carrying value in the General Ledger."
            ))
        # Validate both sides before reading figures, posting the reclass, or
        # elevating the guarded asset write. This prevents a guessed foreign-
        # company pool/asset id from crossing the workflow sudo boundary.
        self._eh_check_access('write')
        asset._eh_check_access('write')
        self._eh_lock_for_change()
        asset._eh_lock_for_transition()
        asset._eh_validate_company_currency()
        self._check_accounting_company()
        if not self.active:
            raise UserError(_(
                "Archived low-value pool %s cannot receive new assets.",
                self.display_name,
            ))
        if asset.company_id != self.company_id:
            raise UserError(_(
                "Asset %(asset)s and pool %(pool)s must belong to the same "
                "company.",
                asset=asset.display_name, pool=self.display_name,
            ))
        owner_check = getattr(asset, '_eh_has_exclusive_accounting_owner', None)
        if owner_check and owner_check():
            raise UserError(_(
                "Asset %s is controlled by an active held-for-sale or "
                "investment-property workflow and cannot enter a low-value "
                "pool.", asset.display_name,
            ))
        if asset.lvp_pool_id:
            raise UserError(_(
                "Asset %(asset)s is already in pool %(pool)s; a second "
                "transfer would duplicate its reclassification.",
                asset=asset.display_name,
                pool=asset.lvp_pool_id.display_name,
            ))
        if asset.state not in ('draft', 'running', 'paused'):
            raise UserError(_(
                "Only a draft, running, or paused asset can enter a low-value "
                "pool; %(asset)s is %(state)s.",
                asset=asset.display_name, state=asset.state,
            ))
        if asset.deferred_type != 'asset':
            raise UserError(_(
                "Deferred revenue and deferred expense schedules cannot enter "
                "a low-value asset pool. %(asset)s is %(kind)s.",
                asset=asset.display_name,
                kind=asset.deferred_type,
            ))
        if asset.is_under_construction:
            raise UserError(_(
                "Asset %(asset)s is under construction. Capitalise it before "
                "assessing eligibility for a low-value pool.",
                asset=asset.display_name,
            ))
        if any(
            line.is_posted or line.move_id or line.reversal_move_id
            for book in asset.book_ids
            for line in book.line_ids
        ):
            raise UserError(_(
                "Asset %(asset)s has general-ledger entries in an additional "
                "depreciation book. The pool reclassification only clears the "
                "primary carrying-value accounts, so transferring it would "
                "orphan the parallel-book balance.",
                asset=asset.display_name,
            ))
        transfer_date = fields.Date.to_date(
            transfer_date or fields.Date.context_today(self),
        )
        latest_accounting_date = asset._eh_latest_accounting_evidence_date()
        if transfer_date < latest_accounting_date:
            raise UserError(_(
                "Pool transfer date %(date)s precedes existing asset "
                "accounting evidence dated %(latest)s on %(asset)s. Use a "
                "date on or after the latest sealed entry.",
                date=transfer_date, latest=latest_accounting_date,
                asset=asset.display_name,
            ))
        opening_value = self.currency_id.round(asset.net_book_value or 0.0)
        if min(asset.acquisition_cost, opening_value) > self.threshold:
            raise UserError(_(
                "Asset %(asset)s cost %(cost).2f and opening adjustable value "
                "%(opening).2f both exceed pool threshold %(thr).2f.",
                asset=asset.display_name,
                cost=asset.acquisition_cost, opening=opening_value,
                thr=self.threshold,
            ))
        if opening_value <= 0:
            raise UserError(_(
                "Asset %(asset)s has no positive opening adjustable value to "
                "transfer into the pool.", asset=asset.display_name,
            ))
        if (asset.revaluation_adjustment
                or asset.accumulated_impairment):
            raise UserError(_(
                "Asset %(asset)s has revaluation or impairment balances. "
                "Assets Pro cannot reclassify those balances into a low-value "
                "pool without their dedicated contra/equity accounts; reverse "
                "them before transfer.", asset=asset.display_name,
            ))
        transfer_move = self._transfer_asset_reclass_move(asset, transfer_date)
        # Persist the opening adjustable value (net book value at transfer)
        # and the allocation date so the pool depreciates and reports on the
        # value actually reclassified into the pool GL account, and rates the
        # asset by the year it was allocated rather than its in-service year.
        asset_vals = {
            'lvp_pool_id': self.id,
            'lvp_opening_value': opening_value,
            'lvp_allocation_date': transfer_date,
            'lvp_transfer_move_id': transfer_move.id if transfer_move else False,
            'lvp_transferred_at': fields.Datetime.now(),
            'lvp_transferred_by_id': self.env.user.id,
        }
        if asset.state == 'running':
            asset_vals['state'] = 'paused'
        asset._eh_workflow_write(asset_vals)
        asset.message_post(body=_(
            "Transferred to low-value pool %(pool)s on %(date)s. "
            "Individual depreciation paused; pool will depreciate as a unit.",
            pool=self.display_name,
            date=transfer_date,
        ))
        return True

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_change()
        booked = self.filtered(
            lambda pool: any(
                line.is_posted or line.move_id for line in pool.line_ids
            )
        )
        if booked:
            raise UserError(_(
                "A low-value pool with booked annual depreciation cannot be "
                "deleted; its lines and journal-entry links are permanent "
                "audit evidence. Archive the pool instead.",
            ))
        return super().unlink()

    def _transfer_asset_reclass_move(self, asset, transfer_date):
        """Post the balanced GL reclassification for a pool transfer.

        Returns the posted account.move, or False when the pool is a
        tax-only pool with no GL accounts configured (no move is posted).
        Posting is a segregation-of-duties control point: only a manager
        may move carrying value between GL accounts.
        """
        self.ensure_one()
        # Tax-only pool: no ledger carrying value to reclassify.
        if not (self.pool_account_id and self.journal_id):
            return False
        if not (asset.asset_account_id
                and asset.accumulated_depreciation_account_id):
            raise UserError(_(
                "Asset %(asset)s has no gross-asset and accumulated-"
                "depreciation accounts configured, so its carrying value "
                "cannot be reclassified into pool %(pool)s. Configure the "
                "asset accounts or clear the pool's GL accounts to run a "
                "tax-only pool.",
                asset=asset.display_name, pool=self.display_name,
            ))
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can reclassify an asset's "
                "carrying value into a low-value pool. This posting is a "
                "segregation-of-duties control point.",
            ))
        _eh_require_exact_posting_date(
            self.company_id,
            transfer_date,
            self.journal_id,
            _(
                "Low-value-pool transfer of %(asset)s",
                asset=asset.display_name,
            ),
        )
        currency = self.currency_id or asset.currency_id
        gross = currency.round(asset.acquisition_cost or 0.0)
        nbv = currency.round(asset.net_book_value or 0.0)
        accumulated = currency.round(gross - nbv)
        label = _("LVP transfer %(asset)s to %(pool)s",
                  asset=asset.display_name, pool=self.display_name)
        line_ids = [
            (0, 0, {
                'name': label,
                'account_id': asset.asset_account_id.id,
                'debit': 0.0,
                'credit': gross,
            }),
        ]
        if accumulated:
            line_ids.append((0, 0, {
                'name': label,
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': accumulated,
                'credit': 0.0,
            }))
        line_ids.append((0, 0, {
            'name': label,
            'account_id': self.pool_account_id.id,
            'debit': nbv,
            'credit': 0.0,
        }))
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'eh_sealed': True,
            'journal_id': self.journal_id.id,
            'date': transfer_date,
            'ref': label,
            'line_ids': line_ids,
        })
        move.action_post()
        return move

    def action_compute_year(self, year=None):
        """Compute and persist a depreciation line for the given year.

        Uses the simplified ATO formula:
          depreciation = subsequent_rate% * opening_pool_balance
                         + first_year_rate% * additions_during_year

        Returns the created line. Idempotent per-year: re-running for
        an existing year raises rather than producing duplicate lines.
        """
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can compute a low-value-pool "
                "annual depreciation row."
            ))
        self._eh_check_access('write')
        self._eh_lock_for_change()
        year = year or self._fiscal_year_label(
            fields.Date.context_today(self),
        )
        existing = self.line_ids.filtered(lambda l: l.year == year)
        if existing:
            raise UserError(_(
                "A line already exists for pool %(pool)s in year "
                "%(year)s. Review that row before recomputing.",
                pool=self.display_name, year=year,
            ))
        annual_values = self._lvp_year_values(year)
        line = self.env['eh.asset.lvp.pool.line'].sudo().create({
            'pool_id': self.id,
            'year': year,
            **annual_values,
            'first_year_rate': self.first_year_rate,
            'subsequent_year_rate': self.subsequent_year_rate,
            'pool_account_id': self.pool_account_id.id,
            'accumulated_account_id': self.accumulated_account_id.id,
            'expense_account_id': self.expense_account_id.id,
            'journal_id': self.journal_id.id,
        })
        return line

    def _lvp_year_values(self, year, first_year_rate=None,
                         subsequent_year_rate=None):
        """Derive one fiscal-year row from current pool source evidence."""
        self.ensure_one()
        assets = self.env['eh.asset'].sudo().search([
            ('lvp_pool_id', '=', self.id),
        ], order='id')
        assets.sudo(False)._eh_check_access('read')
        assets._eh_lock_for_transition()
        assets._eh_validate_company_currency()
        if assets.filtered(lambda asset: asset.company_id != self.company_id):
            raise UserError(_(
                "Every low-value-pool asset must remain in the pool company."
            ))
        # Opening balance = transferred-in lifetime - depreciation
        # already booked in prior years.
        prior_lines = self.line_ids.filtered(lambda l: l.year < year)
        prior_dep = sum(prior_lines.mapped('amount'))
        # Classify each asset by the FINANCIAL year it was allocated into the
        # pool (its transfer date), not the calendar year or in-service year:
        # the ATO first-year 18.75% rate applies in the financial year of
        # allocation, the 37.5% rate to the opening pool balance thereafter.
        # For a 30-June company, a September 2025 allocation belongs to
        # FY2026 and must not be charged the subsequent-year rate in FY2026.
        # Depreciate on the opening adjustable value reclassified into the
        # pool (net book value at transfer), never the gross acquisition cost.
        additions = 0.0
        opening_balance = 0.0
        for asset in assets:
            alloc_date = asset.lvp_allocation_date or asset.in_service_date
            if not alloc_date:
                continue
            allocation_year = self._fiscal_year_label(alloc_date)
            if allocation_year > year:
                # Not yet allocated to the pool in this year: no charge.
                continue
            base = self._lvp_asset_base(asset)
            if allocation_year == year:
                additions += base
            else:
                opening_balance += base
        opening_balance = opening_balance - prior_dep
        first_year_rate = (
            self.first_year_rate
            if first_year_rate is None else first_year_rate
        )
        subsequent_year_rate = (
            self.subsequent_year_rate
            if subsequent_year_rate is None else subsequent_year_rate
        )
        amount = (
            (opening_balance * subsequent_year_rate / 100.0)
            + (additions * first_year_rate / 100.0)
        )
        return {
            'opening_balance': self.currency_id.round(opening_balance),
            'additions': self.currency_id.round(additions),
            'amount': self.currency_id.round(amount),
        }

    def _lvp_recompute_source_issues(self):
        """Explain why a legacy row cannot be safely re-derived.

        A normal prospective calculation can retain the historical fallback
        for old tax-only records. Upgrade remediation is deliberately
        stricter: a manager may replace a legacy result only when every pool
        member retains the server-owned allocation date, transferred value,
        actor and timestamp written by the transfer workflow. Otherwise the
        quarantine stays in place and no amount is guessed.
        """
        self.ensure_one()
        assets = self.env['eh.asset'].sudo().search([
            ('lvp_pool_id', '=', self.id),
        ], order='id')
        assets.sudo(False)._eh_check_access('read')
        issues = []
        for asset in assets:
            missing = []
            if not asset.lvp_allocation_date:
                missing.append(_('allocation date'))
            if not asset.lvp_opening_value or asset.lvp_opening_value <= 0:
                missing.append(_('opening adjustable value'))
            if not asset.lvp_transferred_at:
                missing.append(_('transfer timestamp'))
            if not asset.lvp_transferred_by_id:
                missing.append(_('transfer actor'))
            if missing:
                issues.append(_(
                    "%(asset)s lacks %(evidence)s",
                    asset=asset.display_name,
                    evidence=', '.join(missing),
                ))
        return issues

    def action_compute_current_year(self):
        """UI wrapper: compute then reload the pool form."""
        self.ensure_one()
        self.action_compute_year()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _fiscal_year_label(self, value):
        """End-year label of the company fiscal year containing ``value``."""
        self.ensure_one()
        value = fields.Date.to_date(value)
        fiscal_year = self.company_id.compute_fiscalyear_dates(value)
        date_to = fiscal_year.get('date_to')
        if not date_to:
            raise UserError(_(
                "Cannot resolve the company fiscal year containing %s.",
                value,
            ))
        return date_to.year


class EhAssetLvpPoolLine(models.Model):
    _name = 'eh.asset.lvp.pool.line'
    _inherit = ['eh.workflow.guard']
    _description = "AU LVP annual depreciation line"
    _order = 'pool_id, year desc'

    # is_posted / move_id may only change through the record's own posting
    # action (which runs as su). A plain RPC write (or the old editable
    # boolean toggle) cannot flip is_posted True->False to re-arm the poster
    # and book a second pool move, nor repoint move_id.
    _eh_guarded_fields = (
        'pool_id', 'year', 'opening_balance', 'additions', 'amount',
        'first_year_rate', 'subsequent_year_rate',
        'pool_account_id', 'accumulated_account_id', 'expense_account_id',
        'journal_id',
        'policy_quarantined', 'policy_quarantine_note',
        'allocation_basis_quarantined',
        'allocation_basis_quarantine_note',
        'allocation_basis_original_values',
        'allocation_basis_reviewed_at', 'allocation_basis_reviewed_by_id',
        'is_posted', 'move_id',
    )

    pool_id = fields.Many2one(
        'eh.asset.lvp.pool', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    currency_id = fields.Many2one(
        related='pool_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='pool_id.company_id', store=True, readonly=True, index=True,
    )
    year = fields.Integer(
        required=True,
        help="AU financial year for this depreciation line.",
    )
    opening_balance = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Pool opening balance at the start of the year (after "
            "prior-year depreciation, before this year's additions)."
        ),
    )
    additions = fields.Monetary(
        currency_field='currency_id',
        help="Acquisition cost of assets transferred in during the year.",
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id',
        help=(
            "Annual depreciation: subsequent_rate% * opening_balance "
            "+ first_year_rate% * additions."
        ),
    )
    first_year_rate = fields.Float(readonly=True)
    subsequent_year_rate = fields.Float(readonly=True)
    pool_account_id = fields.Many2one(
        'account.account', readonly=True, ondelete='restrict',
        check_company=True,
    )
    accumulated_account_id = fields.Many2one(
        'account.account', readonly=True, ondelete='restrict',
        check_company=True,
    )
    expense_account_id = fields.Many2one(
        'account.account', readonly=True, ondelete='restrict',
        check_company=True,
    )
    journal_id = fields.Many2one(
        'account.journal', readonly=True, ondelete='restrict',
        check_company=True,
    )
    policy_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Upgrade audit could not prove the policy in force when this "
            "legacy annual entry posted. The historical entry is retained "
            "without inventing a rate/account snapshot."
        ),
    )
    policy_quarantine_note = fields.Text(readonly=True, copy=False)
    allocation_basis_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Upgrade audit found that this legacy annual row predates the "
            "financial-year allocation fix. Posted evidence is retained for "
            "review; a wholly unposted row must be manager-recomputed before "
            "it can post."
        ),
    )
    allocation_basis_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )
    allocation_basis_original_values = fields.Text(
        readonly=True, copy=False,
        help=(
            "Immutable JSON snapshot of the legacy row before a manager "
            "recomputes its financial-year allocation basis."
        ),
    )
    allocation_basis_reviewed_at = fields.Datetime(readonly=True, copy=False)
    allocation_basis_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )

    is_posted = fields.Boolean(default=False, copy=False, readonly=True)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )

    _FROZEN_AFTER_POST = (
        'pool_id', 'year', 'opening_balance', 'additions', 'amount',
        'first_year_rate', 'subsequent_year_rate',
        'pool_account_id', 'accumulated_account_id', 'expense_account_id',
        'journal_id',
    )

    _uniq_pool_year = models.Constraint(
        'unique(pool_id, year)',
        'Only one annual depreciation row is allowed per pool and year.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        pools = self.env['eh.asset.lvp.pool'].browse({
            vals.get('pool_id') for vals in vals_list if vals.get('pool_id')
        })
        if not self.env.su:
            raise UserError(_(
                "Low-value-pool annual rows are engine-generated. Use Compute "
                "Year instead of creating them directly."
            ))
        pools._check_accounting_company()
        lines = super().create(vals_list)
        _eh_validate_accounting_company(lines, (
            'pool_account_id', 'accumulated_account_id',
            'expense_account_id', 'journal_id',
        ))
        return lines

    def write(self, vals):
        protected = set(vals) & (
            set(self._eh_guarded_fields) | set(self._FROZEN_AFTER_POST)
        )
        if protected:
            self._eh_lock_for_post()
        if protected and not self.env.su:
            self._eh_check_access('write')
        frozen = [field for field in self._FROZEN_AFTER_POST if field in vals]
        if frozen:
            booked = self.filtered(lambda line: line.is_posted or line.move_id)
            if booked:
                raise UserError(_(
                    "Pool fields (%(fields)s) are frozen once the annual "
                    "line is booked; its source and amount must continue to "
                    "match the journal entry it produced.",
                    fields=', '.join(frozen),
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            raise UserError(_(
                "Low-value-pool annual rows are engine-generated and cannot be "
                "deleted directly. Preserve them as audit evidence."
            ))
        self._eh_lock_for_post()
        booked = self.filtered(lambda line: line.is_posted or line.move_id)
        if booked:
            raise UserError(_(
                "A booked low-value-pool line cannot be deleted; its source "
                "identity and journal-entry link are permanent audit "
                "evidence.",
            ))
        return super().unlink()

    def _posting_date(self):
        """Fiscal year-end of the pool year this line depreciates.

        A pool run for year Y is routinely posted during the following
        year's close (e.g. a FY2025 run posted in Feb 2026). Dating the move
        on the day the button is clicked would push the whole charge into the
        wrong reporting period. Book it at the close of the year it relates
        to instead, honouring the company's configured fiscal year-end
        (defaults to an AU 30-June year for a low-value pool).
        """
        self.ensure_one()
        company = self.pool_id.company_id or self.env.company
        # Use the first day of the closing month as the lookup anchor. Odoo
        # explicitly allows a 29-February year end and normalises it to the
        # 28th in non-leap years; constructing the configured last day here
        # directly would crash for those legitimate companies.
        anchor = date(self.year, int(company.fiscalyear_last_month), 1)
        fiscal_year = company.compute_fiscalyear_dates(anchor)
        date_to = fiscal_year.get('date_to')
        if not date_to or date_to.year != self.year:
            raise UserError(_(
                "Cannot resolve fiscal-year end for pool year %s.",
                self.year,
            ))
        return date_to

    def _eh_lock_for_post(self):
        """Serialise concurrent posters so a cron and a manual click (or a
        double-submit) cannot each create a journal entry for the same pool
        line. Take a row lock, then re-read is_posted from the database."""
        if not self.ids:
            return
        self.mapped('pool_id')._eh_lock_for_change()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_lvp_pool_line WHERE id IN %s '
            'FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset(['is_posted', 'move_id'])

    def action_post(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post low-value pool "
                "depreciation to the general ledger. This posting is a "
                "segregation-of-duties control point.",
            ))
        self._eh_lock_for_post()
        for rec in self:
            if rec.allocation_basis_quarantined:
                raise UserError(_(
                    "Pool line %(line)s is quarantined because its legacy "
                    "calendar-year allocation basis is unproved. A manager "
                    "must review and recompute an unposted row; posted "
                    "journal evidence must be corrected separately, never "
                    "rewritten.",
                    line=rec.display_name,
                ))
            if rec.policy_quarantined:
                raise UserError(_(
                    "Pool line %s is quarantined because its historical "
                    "rate/account basis could not be proven. Existing GL "
                    "evidence is retained and cannot be re-posted.",
                    rec.display_name,
                ))
            # Idempotent: never re-book a line that already carries a live
            # posted move (that duplicates the pool's annual charge).
            if rec.is_posted and not rec.move_id:
                raise UserError(_(
                    "Pool line %(line)s is marked posted but has no move link. "
                    "Repair the inconsistent legacy record before continuing.",
                    line=rec.display_name,
                ))
            if rec.is_posted:
                continue
            if rec.move_id:
                raise UserError(_(
                    "Pool line %(line)s has a move link but is not marked "
                    "posted. Repair it before another posting.",
                    line=rec.display_name,
                ))
            pool = rec.pool_id
            pool._check_accounting_company()
            if not (rec.expense_account_id and rec.accumulated_account_id
                    and rec.journal_id):
                raise UserError(_(
                    "Configure the pool's expense, accumulated, and "
                    "journal accounts before posting.",
                ))
            available = pool.currency_id.round(
                (rec.opening_balance or 0.0) + (rec.additions or 0.0),
            )
            if rec.amount <= 0:
                raise UserError(_(
                    "A zero-value low-value-pool line cannot post."
                ))
            if pool.currency_id.compare_amounts(rec.amount, available) > 0:
                raise UserError(_(
                    "Pool depreciation of %(amount).2f exceeds %(available).2f "
                    "available pool base.",
                    amount=rec.amount, available=max(0.0, available),
                ))
            _eh_require_exact_posting_date(
                pool.company_id,
                rec._posting_date(),
                rec.journal_id,
                _("Low-value-pool annual row %(line)s", line=rec.display_name),
            )
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'eh_sealed': True,
                'journal_id': rec.journal_id.id,
                'date': rec._posting_date(),
                'ref': "LVP %s %s" % (pool.name, rec.year),
                'line_ids': [
                    (0, 0, {
                        'name': "LVP depreciation %s" % rec.year,
                        'account_id': rec.expense_account_id.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': "LVP depreciation %s" % rec.year,
                        'account_id': rec.accumulated_account_id.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                    }),
                ],
            })
            move.action_post()
            # is_posted / move_id are guarded; stamp through the sanctioned
            # action path (runs as su) so a real, non-superuser manager can
            # post while a direct write to those fields stays blocked.
            rec._eh_workflow_write({'is_posted': True, 'move_id': move.id})
        return True

    def action_recompute_allocation_basis(self):
        """Manager-reviewed release of an unposted fiscal-basis quarantine.

        Never changes a posted row or linked move. The migration snapshot is
        retained byte-for-byte while the current pool membership is re-read
        and the row's original rate policy is applied on a financial-year
        basis.
        """
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can resolve a low-value-pool "
                "allocation-basis quarantine."
            ))
        reviewer_id = self.env.user.id
        self._eh_lock_for_post()
        for rec in self.sorted(lambda line: (line.pool_id.id, line.year)):
            if not rec.allocation_basis_quarantined:
                raise UserError(_(
                    "Pool line %s is not allocation-basis quarantined.",
                    rec.display_name,
                ))
            if rec.is_posted or rec.move_id:
                raise UserError(_(
                    "Pool line %s already has ledger evidence. Its historical "
                    "values cannot be recomputed; review and correct the "
                    "General Ledger through a separate adjustment.",
                    rec.display_name,
                ))
            if rec.policy_quarantined:
                raise UserError(_(
                    "Pool line %s also lacks a proven rate/account policy; "
                    "that policy quarantine must be resolved independently.",
                    rec.display_name,
                ))
            unresolved_prior = rec.pool_id.line_ids.filtered(
                lambda line: (
                    line.year < rec.year
                    and line.allocation_basis_quarantined
                    and not (line.is_posted or line.move_id)
                ),
            )
            if unresolved_prior:
                raise UserError(_(
                    "Resolve earlier unposted quarantined pool years first: "
                    "%s.",
                    ', '.join(
                        str(year) for year in unresolved_prior.mapped('year')
                    ),
                ))
            source_issues = rec.pool_id._lvp_recompute_source_issues()
            if source_issues:
                raise UserError(_(
                    "Pool line %(line)s cannot be safely recomputed because "
                    "its source provenance is incomplete: %(issues)s. The "
                    "legacy values and quarantine were retained for manual "
                    "review.",
                    line=rec.display_name,
                    issues='; '.join(source_issues),
                ))
            rates = (rec.first_year_rate, rec.subsequent_year_rate)
            if any(
                    rate is None or not math.isfinite(rate)
                    or not (0 <= rate <= 100)
                    for rate in rates):
                raise UserError(_(
                    "Pool line %s has no safe, finite legacy rate policy. "
                    "Its values and quarantine were retained.",
                    rec.display_name,
                ))
            original = rec.allocation_basis_original_values
            if not original:
                original = json.dumps({
                    'year': rec.year,
                    'opening_balance': rec.opening_balance,
                    'additions': rec.additions,
                    'amount': rec.amount,
                    'first_year_rate': rec.first_year_rate,
                    'subsequent_year_rate': rec.subsequent_year_rate,
                    'pool_account_id': rec.pool_account_id.id or False,
                    'accumulated_account_id': (
                        rec.accumulated_account_id.id or False
                    ),
                    'expense_account_id': rec.expense_account_id.id or False,
                    'journal_id': rec.journal_id.id or False,
                }, sort_keys=True)
            annual_values = rec.pool_id._lvp_year_values(
                rec.year,
                first_year_rate=rec.first_year_rate,
                subsequent_year_rate=rec.subsequent_year_rate,
            )
            rec.sudo().write({
                **annual_values,
                'allocation_basis_quarantined': False,
                'allocation_basis_quarantine_note': (
                    "Manager recomputed this unposted row from current "
                    "same-company pool membership using its original locked "
                    "rate policy and the company financial-year calendar."
                ),
                'allocation_basis_original_values': original,
                'allocation_basis_reviewed_at': fields.Datetime.now(),
                'allocation_basis_reviewed_by_id': reviewer_id,
            })
            rec.pool_id.message_post(body=_(
                "Manager %(reviewer)s recomputed quarantined low-value-pool "
                "year %(year)s on the company financial-year basis. The "
                "legacy row snapshot was retained on the annual row.",
                reviewer=self.env.user.display_name,
                year=rec.year,
            ))
        return True

    def action_acknowledge_allocation_basis_review(self):
        """Record manager review of a posted legacy row without releasing it.

        A reviewer cannot make an old calculation provable after the fact and
        must never rewrite its linked journal. This acknowledgement records
        who reviewed it and when, while the audit quarantine, original values
        and every posted financial byte remain intact.
        """
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can acknowledge a legacy "
                "low-value-pool allocation-basis review."
            ))
        reviewer_id = self.env.user.id
        self._eh_lock_for_post()
        for rec in self:
            if not rec.allocation_basis_quarantined:
                raise UserError(_(
                    "Pool line %s is not allocation-basis quarantined.",
                    rec.display_name,
                ))
            if not (rec.is_posted or rec.move_id):
                raise UserError(_(
                    "Unposted pool line %s must be recomputed from proven "
                    "source evidence before it can be released.",
                    rec.display_name,
                ))
            rec._eh_workflow_write({
                'allocation_basis_quarantine_note': (
                    "Manager reviewed this posted legacy row. Its unproved "
                    "allocation-basis quarantine, original values and linked "
                    "journal remain unchanged; record any required correction "
                    "through a separate General Ledger adjustment."
                ),
                'allocation_basis_reviewed_at': fields.Datetime.now(),
                'allocation_basis_reviewed_by_id': reviewer_id,
            })
            rec.pool_id.message_post(body=_(
                "Manager %(reviewer)s reviewed quarantined posted low-value-"
                "pool year %(year)s. No annual-row value or linked journal "
                "entry was changed.",
                reviewer=self.env.user.display_name,
                year=rec.year,
            ))
        return True
