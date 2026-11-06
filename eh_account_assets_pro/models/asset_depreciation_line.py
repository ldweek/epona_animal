# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Single line of an asset's depreciation schedule.

Each line is a real record so it can be queried, exported and audited.
A line stores the planned amount; on action_post it produces a balanced
account.move (debit Depreciation Expense, credit Accumulated
Depreciation) and stamps the user/timestamp.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .accounting_integrity import _eh_require_exact_posting_date


class EhAssetDepreciationLine(models.Model):
    _name = 'eh.asset.depreciation.line'
    _inherit = ['eh.workflow.guard']
    _description = "Asset Depreciation Schedule Line"
    _order = 'asset_id, sequence, depreciation_date'

    # Posting identity and audit fields may only be stamped by action_post.
    # Source identity and measurement fields remain editable before booking,
    # then become immutable through _FROZEN_AFTER_POST below.
    _eh_guarded_fields = (
        'is_posted', 'move_id', 'posted_at', 'posted_by_id',
        'is_event_accrual',
    )

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade', index=True,
    )
    sequence = fields.Integer(required=True, default=10)
    depreciation_date = fields.Date(required=True)
    amount = fields.Monetary(required=True)
    accumulated = fields.Monetary(
        help="Accumulated depreciation up to and including this line.",
    )
    remaining_value = fields.Monetary(
        help="Net book value after this line is posted.",
    )
    is_event_accrual = fields.Boolean(
        string="Event-date Stub", readonly=True, copy=False,
        help=(
            "Engine-generated prorated depreciation through a disposal or "
            "other derecognition date that falls inside a scheduled period."
        ),
    )

    is_posted = fields.Boolean(default=False, copy=False, readonly=True)
    posted_at = fields.Datetime(readonly=True, copy=False)
    posted_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )

    _uniq_asset_sequence = models.Constraint(
        'unique(asset_id, sequence)',
        'Sequence must be unique within an asset.',
    )
    _check_amount_non_negative = models.Constraint(
        'CHECK (amount >= 0)',
        'Depreciation amount cannot be negative.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        assets = self.env['eh.asset'].browse({
            vals.get('asset_id') for vals in vals_list if vals.get('asset_id')
        })
        if not self.env.su:
            assets._eh_check_access('write')
            non_manual = assets.filtered(lambda asset: asset.method != 'manual')
            if non_manual:
                raise UserError(_(
                    "Depreciation schedules for non-manual assets are "
                    "engine-generated. Use Compute Schedule instead of "
                    "creating rows directly."
                ))
        assets._eh_validate_company_currency()
        lines = super().create(vals_list)
        lines.env['res.company'].sudo()._eh_bump_move_version(
            lines.mapped('company_id.id'),
        )
        return lines

    @api.constrains('asset_id')
    def _check_ias38_indefinite_life(self):
        """IAS 38.107: an indefinite-life intangible is not amortised.

        Guard on the child line (not only the parent form) so every
        creation path - manual schedule keying, imports, code - is
        blocked, mirroring the parent-side constraint on eh.asset.
        """
        for line in self:
            asset = line.asset_id
            if asset.asset_class == 'intangible' and asset.is_indefinite_life:
                raise ValidationError(_(
                    "%(asset)s has an indefinite useful life; IAS 38.107 "
                    "prohibits amortisation, so no depreciation line can "
                    "be recorded on it. It is subject to the annual "
                    "impairment test instead (IAS 36.10).",
                    asset=asset.display_name,
                ))

    # Schedule measurement fields frozen once the line has produced its
    # journal entry. Re-basing amount or date on a posted line would move the
    # charge away from the ledger it already booked; a correction must be a
    # further posting, not an in-place edit.
    _FROZEN_AFTER_POST = (
        'asset_id', 'sequence', 'amount', 'depreciation_date',
        'accumulated', 'remaining_value', 'is_event_accrual',
    )

    def write(self, vals):
        # Let eh.workflow.guard own its fields. Besides keeping one canonical
        # security decision, this preserves AccessError semantics for a direct
        # RPC attempt to forge posting identity or audit stamps.
        if not self.env.su and set(vals) & set(self._eh_guarded_fields):
            return super().write(vals)
        protected = set(vals) & (
            set(self._eh_guarded_fields) | set(self._FROZEN_AFTER_POST)
        )
        if protected:
            self._eh_lock_for_mutation(vals.get('asset_id'))
        if protected and not self.env.su:
            self._eh_check_access('write')
            target_assets = self.mapped('asset_id')
            if vals.get('asset_id'):
                target_assets |= self.env['eh.asset'].browse(vals['asset_id'])
            target_assets._eh_check_access('write')
            if target_assets.filtered(lambda asset: asset.method != 'manual'):
                raise UserError(_(
                    "Engine-generated depreciation schedule rows cannot be "
                    "edited directly. Recompute the draft schedule instead."
                ))
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(
                lambda line: line.is_posted or line.move_id,
            )
            if posted:
                raise UserError(_(
                    "Schedule fields (%(fields)s) are frozen once the "
                    "depreciation line is posted; the charge must equal the "
                    "journal entry it produced. Reverse the entry to correct "
                    "it.",
                    fields=', '.join(frozen)))
        company_ids = set(self.mapped('company_id.id'))
        result = super().write(vals)
        if vals:
            company_ids.update(self.mapped('company_id.id'))
            self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            if self.mapped('asset_id').filtered(
                    lambda asset: asset.method != 'manual'):
                raise UserError(_(
                    "Engine-generated depreciation rows cannot be deleted "
                    "directly. Recompute the draft schedule instead."
                ))
        self._eh_lock_for_mutation()
        booked = self.filtered(lambda line: line.is_posted or line.move_id)
        if booked:
            raise UserError(_(
                "A booked depreciation line cannot be deleted; its source "
                "identity and journal-entry link are permanent audit "
                "evidence. Reverse the entry to correct it.",
            ))
        company_ids = set(self.mapped('company_id.id'))
        result = super().unlink()
        if company_ids:
            self.env['res.company'].sudo()._eh_bump_move_version(company_ids)
        return result

    def _eh_lock_for_mutation(self, target_asset_id=None):
        """Canonical asset-then-line lock for edits, deletes and posting."""
        assets = self.mapped('asset_id')
        if target_asset_id:
            assets |= self.env['eh.asset'].browse(target_asset_id)
        assets._eh_lock_for_transition()
        if self.ids:
            self.env.cr.execute(
                'SELECT id FROM eh_asset_depreciation_line WHERE id IN %s '
                'ORDER BY id FOR UPDATE',
                (tuple(self.ids),),
            )
            self.invalidate_recordset([
                'asset_id', 'is_posted', 'move_id', 'amount',
                'depreciation_date',
            ])
        return self

    def _eh_lock_for_post(self):
        """Serialise concurrent posters on the schedule lines.

        The daily cron and the manual 'Post Due Lines' button (and a plain
        double-click / browser retry) both read-then-post the same unposted
        line. Under READ COMMITTED both would read is_posted=False and each
        create a posted move, silently doubling the GL charge. Take a row
        lock and re-read is_posted from the database so the loser blocks on
        the winner and then sees the committed True and skips.
        """
        if not self.ids:
            return
        self._eh_lock_for_mutation()

    def action_post(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post depreciation to the "
                "general ledger. This posting is a segregation-of-duties "
                "control point.",
            ))
        self._eh_lock_for_post()
        for line in self:
            # Idempotent: a line that already carries a live posted move must
            # never be re-booked (that duplicates the period's depreciation
            # and orphans the first entry). Skip silently so a concurrent
            # cron/manual race, a double-submit, or a re-run is a no-op.
            if line.is_posted and not line.move_id:
                raise UserError(_(
                    "%(line)s is marked posted but has no journal-entry link. "
                    "Repair the inconsistent legacy record before continuing.",
                    line=line.display_name,
                ))
            if line.is_posted:
                continue
            if line.move_id:
                raise UserError(_(
                    "%(line)s already has a journal-entry link but is not "
                    "marked posted. Repair the inconsistent legacy record "
                    "before attempting another posting.",
                    line=line.display_name,
                ))
            asset = line.asset_id
            if asset.state not in ('running', 'paused'):
                raise UserError(_(
                    "Depreciation can only post for a running or paused "
                    "asset; %(asset)s is %(state)s.",
                    asset=asset.display_name, state=asset.state,
                ))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "%(asset)s belongs to low-value pool %(pool)s; its "
                    "individual depreciation schedule is permanently frozen.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name,
                ))
            if line.amount <= 0:
                raise UserError(_(
                    "A zero-value depreciation line cannot post."
                ))
            asset._validate_posting_setup()
            asset.invalidate_recordset(['net_book_value'])
            available = asset.currency_id.round(
                (asset.net_book_value or 0.0) - (asset.salvage_value or 0.0),
            )
            if asset.currency_id.compare_amounts(line.amount, available) > 0:
                raise UserError(_(
                    "Depreciation of %(amount).2f exceeds %(available).2f of "
                    "remaining depreciable carrying amount on %(asset)s.",
                    amount=line.amount, available=max(0.0, available),
                    asset=asset.display_name,
                ))
            _eh_require_exact_posting_date(
                asset.company_id,
                line.depreciation_date,
                asset.journal_id,
                _("Depreciation schedule row %(line)s", line=line.display_name),
            )
            move_lines = line._build_move_lines_for_type(asset)
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'eh_sealed': True,
                'date': line.depreciation_date,
                'journal_id': asset.journal_id.id,
                'ref': line._move_ref_for_type(asset),
                'line_ids': move_lines,
            })
            move.action_post()
            # is_posted / move_id are guarded; stamp them through the
            # sanctioned action path (runs as su) so a real, non-superuser
            # manager can post while a direct RPC write stays blocked.
            line._eh_workflow_write({
                'is_posted': True,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
        return True

    def _build_move_lines_for_type(self, asset):
        """Return the (debit, credit) leg pair appropriate for the parent
        asset's deferred_type.

        * asset (default fixed asset): DR Depreciation Expense,
          CR Accumulated Depreciation. Net book value declines.
        * deferred_revenue: the asset's `asset_account_id` is the
          deferred revenue liability that pre-paid customers credit;
          recognition reduces the liability and credits the
          `depreciation_account_id` (used here as the revenue
          recognition account). Net of the period: DR Deferred Revenue,
          CR Revenue.
        * deferred_expense: the asset's `asset_account_id` is the
          prepaid expense asset; recognition reduces it and debits the
          `depreciation_account_id` (used here as the expense account).
          Net of the period: DR Expense, CR Prepaid Asset.
        """
        self.ensure_one()
        amount = self.amount
        if asset.deferred_type == 'deferred_revenue':
            return [
                (0, 0, {
                    'name': _("Deferred revenue release %s", asset.display_name),
                    'account_id': asset.asset_account_id.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Revenue recognition %s", asset.display_name),
                    'account_id': asset.depreciation_account_id.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ]
        if asset.deferred_type == 'deferred_expense':
            return [
                (0, 0, {
                    'name': _("Expense recognition %s", asset.display_name),
                    'account_id': asset.depreciation_account_id.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': _("Prepaid asset release %s", asset.display_name),
                    'account_id': asset.asset_account_id.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ]
        return [
            (0, 0, {
                'name': _("Depreciation %s", asset.display_name),
                'account_id': asset.depreciation_account_id.id,
                'debit': amount,
                'credit': 0.0,
            }),
            (0, 0, {
                'name': _("Accumulated depreciation %s", asset.display_name),
                'account_id': asset.accumulated_depreciation_account_id.id,
                'debit': 0.0,
                'credit': amount,
            }),
        ]

    def _move_ref_for_type(self, asset):
        if asset.deferred_type == 'deferred_revenue':
            return _("Deferred revenue release %(asset)s #%(seq)s",
                     asset=asset.display_name, seq=self.sequence)
        if asset.deferred_type == 'deferred_expense':
            return _("Deferred expense release %(asset)s #%(seq)s",
                     asset=asset.display_name, seq=self.sequence)
        return _("Depreciation %(asset)s #%(seq)s",
                 asset=asset.display_name, seq=self.sequence)

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No journal entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
