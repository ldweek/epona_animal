# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.impairment: IAS 36 impairment charge or reversal on a fixed asset.

IAS 36 requires entities to assess at each reporting date whether an
asset's carrying amount exceeds its recoverable amount (the higher of
fair value less costs of disposal and value in use). When it does, the
entity must write the asset down to recoverable amount and recognise an
impairment loss in the P&L.

Reversals are permitted (and required) when conditions reverse, except
for goodwill. The reversal is capped at what the carrying amount would
have been had the original impairment not been recognised (after
continued depreciation).

This model holds one row per impairment event (charge or reversal) on
an asset. The asset's net_book_value compute subtracts the running
balance (charges minus reversals) from the depreciated cost so the
NBV displayed on the asset form, the balance sheet, and downstream
reports is consistently impairment-aware.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .accounting_integrity import _eh_validate_accounting_company


class EhAssetImpairment(models.Model):
    _name = 'eh.asset.impairment'
    _description = "Asset impairment / reversal"
    _order = 'asset_id, impairment_date, id'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
        'eh.gl.reversal',
    ]

    # Workflow, journal-link, and audit fields are server-owned. They may only
    # be stamped through action_post / action_cancel, never forged over RPC.
    _eh_guarded_fields = (
        'state', 'move_id', 'posted_at', 'posted_by_id',
        'reversal_move_id', 'cancelled_at', 'cancelled_by_id',
        'revaluation_surplus_used',
    )

    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='restrict', index=True,
        check_company=True,
        help="Asset whose carrying amount is being adjusted.",
    )
    cgu_id = fields.Many2one(
        'eh.asset.cgu', ondelete='set null', index=True, copy=False,
        check_company=True,
        help=(
            "Cash-generating unit whose IAS 36 impairment test derived "
            "and allocated this charge. Blank for a hand-keyed "
            "impairment entered directly on the asset."
        ),
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )

    impairment_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help=(
            "Date of the impairment event. Drives the journal entry "
            "date and the period in which the loss / reversal hits "
            "the P&L."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Absolute (positive) amount of the impairment charge or "
            "reversal. Sign is implied by is_reversal."
        ),
    )
    recoverable_amount = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help=(
            "Recoverable amount supporting this impairment event (the "
            "higher of fair value less costs of disposal and value in "
            "use, IAS 36.18). Optional for hand-keyed events; when "
            "stated, posting stamps it onto the asset as its latest "
            "recoverable-amount measurement, which the revaluation "
            "wizard uses to cap upward revaluations."
        ),
    )
    is_reversal = fields.Boolean(
        default=False, tracking=True,
        help=(
            "When False, this row is an impairment charge that "
            "reduces the asset's NBV. When True, it is a reversal "
            "that restores carrying amount up to the cap permitted "
            "by IAS 36 (the depreciated cost the asset would have "
            "carried had the original impairment not been "
            "recognised). The asset's NBV compute subtracts charges "
            "and adds back reversals."
        ),
    )
    reason = fields.Text(
        required=True,
        help=(
            "Documented basis for the impairment: indicator of "
            "impairment, recoverable-amount calculation, valuation "
            "method, key assumptions. Lands in the audit trail and "
            "in the close run's working papers."
        ),
    )

    impairment_account_id = fields.Many2one(
        'account.account',
        check_company=True,
        string="Impairment Loss Account",
        help=(
            "P&L account for the impairment loss (typically an "
            "expense account named 'Impairment Loss' or similar). "
            "Falls back to the asset's disposal loss account when blank."
        ),
    )
    revaluation_reserve_account_id = fields.Many2one(
        'account.account', string="Revaluation Reserve Account",
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
        help=(
            "Equity reserve debited before P&L when this charge relates to a "
            "revalued asset (IAS 36.60). Falls back to the reserve account "
            "stored on the asset."
        ),
    )
    revaluation_surplus_used = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help=(
            "Portion of this impairment charge recognised against the asset's "
            "revaluation surplus instead of P&L under IAS 36.60."
        ),
    )
    accumulated_account_id = fields.Many2one(
        'account.account',
        check_company=True,
        string="Accumulated Impairment Account",
        help=(
            "Balance-sheet contra account against which the "
            "impairment is booked. Falls back to the asset's "
            "accumulated_depreciation_account_id when blank."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal',
        check_company=True,
        help=(
            "Journal used to post the impairment entry. Defaults to "
            "the asset's depreciation journal when blank."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True,
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help="Journal entry posted for this impairment.",
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        check_company=True,
        help="Sealed counter-entry posted when this impairment was cancelled.",
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    cancelled_at = fields.Datetime(readonly=True, tracking=True)
    cancelled_by_id = fields.Many2one('res.users', readonly=True)

    _check_amount_positive = models.Constraint(
        'CHECK (amount > 0)',
        'Impairment amount must be positive (sign is implied by is_reversal).',
    )

    @api.constrains(
        'amount', 'is_reversal', 'asset_id', 'impairment_date',
    )
    def _check_reversal_cap(self):
        """A reversal cannot push the running impairment balance below zero.

        Reversing more than has been charged would imply the asset
        gained carrying amount above what it had before any
        impairment, which IAS 36 forbids. We guard at write time so
        the violation is loud.
        """
        for rec in self:
            if not rec.is_reversal:
                continue
            # IAS 36.124: an impairment loss recognised for goodwill shall
            # not be reversed in a subsequent period. This is absolute; it
            # sits ahead of the cumulative-balance and ceiling tests below,
            # which apply only to reversible (non-goodwill) assets.
            if rec.asset_id.is_goodwill:
                raise ValidationError(_(
                    "Goodwill impairment cannot be reversed on %(asset)s. "
                    "IAS 36.124 prohibits reversing an impairment loss "
                    "recognised for goodwill in any later period.",
                    asset=rec.asset_id.display_name,
                ))
            other_charges = sum(
                rec.asset_id.impairment_ids
                .filtered(lambda i: (
                    i.state == 'posted'
                    and not i.is_reversal
                    and i.id != rec.id
                    and i.impairment_date <= rec.impairment_date
                ))
                .mapped('amount'),
            )
            other_reversals = sum(
                rec.asset_id.impairment_ids
                .filtered(lambda i: (
                    i.state == 'posted'
                    and i.is_reversal
                    and i.id != rec.id
                    and i.impairment_date <= rec.impairment_date
                ))
                .mapped('amount'),
            )
            running = other_charges - other_reversals
            if rec.amount > running:
                raise ValidationError(_(
                    "Reversal of %(amt).2f exceeds the available "
                    "impairment balance of %(bal).2f on %(asset)s. "
                    "IAS 36 caps reversals at the cumulative "
                    "impairment previously recognised; reduce the "
                    "amount or split into multiple events.",
                    amt=rec.amount, bal=running,
                    asset=rec.asset_id.display_name,
                ))
            # IAS 36.117: the post-reversal carrying amount must not exceed
            # the depreciated historical cost (the carrying amount the asset
            # would have had if no impairment had ever been recognised).
            # Because depreciation after an impairment is re-amortised on the
            # lower base, reversing the full charge later can lift the asset
            # above that ceiling even when the cumulative-charge check above
            # passes; this guard blocks it.
            asset = rec.asset_id
            posted_dep = sum(
                asset.depreciation_line_ids
                .filtered(lambda l: (
                    l.is_posted
                    and l.depreciation_date <= rec.impairment_date
                ))
                .mapped('amount'),
            )
            nbv_before_reversal = (
                asset.acquisition_cost + asset.revaluation_adjustment
                - posted_dep - running
            )
            ceiling = asset._ias36_depreciated_cost(
                as_of_date=rec.impairment_date,
            )
            max_reversal = asset.currency_id.round(
                ceiling - nbv_before_reversal,
            )
            if rec.amount > max_reversal:
                raise ValidationError(_(
                    "Reversal of %(amt).2f would lift the carrying amount "
                    "of %(asset)s above its depreciated historical cost of "
                    "%(ceiling).2f. IAS 36.117 caps a reversal at the "
                    "carrying amount that would have been determined, net "
                    "of depreciation, had no impairment been recognised. "
                    "The maximum reversal permitted here is %(max).2f.",
                    amt=rec.amount, asset=asset.display_name,
                    ceiling=ceiling, max=max(0.0, max_reversal),
                ))

    @api.constrains('recoverable_amount')
    def _check_recoverable_amount_nonnegative(self):
        for rec in self:
            if rec.recoverable_amount < 0:
                raise ValidationError(_(
                    "Recoverable amount cannot be negative on %(impairment)s.",
                    impairment=rec.display_name,
                ))

    @api.constrains('cgu_id', 'asset_id')
    def _check_cgu_currency_integrity(self):
        self.mapped('cgu_id')._eh_validate_company_currency()
        for rec in self.filtered('cgu_id'):
            if (rec.cgu_id.company_id != rec.asset_id.company_id
                    or rec.cgu_id.currency_id != rec.asset_id.currency_id
                    or rec.asset_id.cgu_id != rec.cgu_id):
                raise ValidationError(_(
                    "CGU impairment %(impairment)s must point to an asset "
                    "currently assigned to the same CGU, company, and currency.",
                    impairment=rec.display_name,
                ))

    @api.constrains(
        'asset_id', 'impairment_account_id', 'accumulated_account_id',
        'revaluation_reserve_account_id', 'journal_id',
    )
    def _check_accounting_company(self):
        _eh_validate_accounting_company(self, (
            'impairment_account_id', 'accumulated_account_id',
            'revaluation_reserve_account_id', 'journal_id',
        ))

    # ---- freeze (IAS 16.39 / SoD) ----

    # Measurement fields frozen once the impairment is posted. Re-basing the
    # amount or flipping is_reversal on a posted row would desync the asset's
    # net_book_value (which counts posted charges minus posted reversals) from
    # the ledger entry the row already produced. A correction must be a further
    # impairment event (a reversal row) or a cancel/reset, never an in-place
    # edit of the posted row.
    _FROZEN_AFTER_POST = (
        'asset_id', 'cgu_id', 'impairment_date', 'amount',
        'recoverable_amount', 'is_reversal', 'reason',
        'impairment_account_id', 'accumulated_account_id',
        'revaluation_reserve_account_id', 'journal_id',
    )

    def write(self, vals):
        protected = set(vals) & (
            set(self._eh_guarded_fields) | set(self._FROZEN_AFTER_POST)
        )
        if protected:
            # CGU -> asset -> impairment is the same deterministic order used
            # by posting and test allocation. Re-read state after waiting so
            # an editor cannot race a poster and rewrite booked evidence.
            self._eh_lock_for_action()
        if protected and not self.env.su:
            self._eh_check_access('write')
        frozen = [f for f in self._FROZEN_AFTER_POST if f in vals]
        if frozen:
            posted = self.filtered(
                lambda r: r.state in ('posted', 'cancelled') or r.move_id,
            )
            if posted:
                raise UserError(_(
                    "Measurement fields (%(fields)s) are frozen once the "
                    "impairment is posted; the amount must equal the journal "
                    "entry it produced. Cancel the impairment (which reverses "
                    "the entry) or record a further impairment / reversal to "
                    "correct it.",
                    fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_action()
        posted = self.filtered(
            lambda r: r.state in ('posted', 'cancelled') or r.move_id,
        )
        if posted:
            raise UserError(_(
                "A booked or cancelled impairment cannot be deleted; its "
                "journal entry and cancellation are permanent audit "
                "evidence. Record a further correction instead.",
            ))
        return super().unlink()

    @api.model_create_multi
    def create(self, vals_list):
        assets = self.env['eh.asset'].browse({
            vals.get('asset_id') for vals in vals_list if vals.get('asset_id')
        })
        cgus = self.env['eh.asset.cgu'].browse({
            vals.get('cgu_id') for vals in vals_list if vals.get('cgu_id')
        })
        if not self.env.su:
            assets._eh_check_access('write')
            cgus._eh_check_access('read')
        assets._eh_validate_company_currency()
        cgus._eh_validate_company_currency()
        for vals in vals_list:
            if vals.get('state') not in (None, 'draft'):
                raise UserError(_(
                    "An impairment cannot be created directly in a booked or "
                    "cancelled state; that would bypass its journal-entry "
                    "workflow. Create it in draft and use the provided "
                    "actions.",
                ))
        return super().create(vals_list)

    def _eh_lock_for_action(self):
        if not self.ids:
            return self
        self.mapped('cgu_id')._eh_lock_for_test()
        self.mapped('asset_id')._eh_lock_for_transition()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_impairment WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'state', 'move_id', 'reversal_move_id', 'amount', 'is_reversal',
            'asset_id', 'cgu_id',
        ])
        return self

    # ---- actions ----

    def action_post(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post an impairment charge "
                "or reversal to the general ledger. This posting is a "
                "segregation-of-duties control point.",
            ))
        self._eh_check_access('write')
        self.mapped('asset_id')._eh_check_access('write')
        self.mapped('cgu_id')._eh_check_access('write')
        self._eh_lock_for_action()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only draft impairment events can be posted "
                    "(state is %s).",
                ) % rec.state)
            if rec.move_id or rec.reversal_move_id:
                raise UserError(_(
                    "Draft impairment %(impairment)s already carries a journal "
                    "link. Repair the inconsistent legacy record before "
                    "posting.", impairment=rec.display_name,
                ))
            asset = rec.asset_id
            asset._eh_validate_company_currency()
            if asset.state not in ('running', 'paused'):
                raise UserError(_(
                    "Impairment can only post for a running or paused asset; "
                    "%(asset)s is %(state)s.",
                    asset=asset.display_name, state=asset.state,
                ))
            if asset.lvp_pool_id:
                raise UserError(_(
                    "Asset %(asset)s belongs to low-value pool %(pool)s. "
                    "Its carrying amount is measured and depreciated at pool "
                    "level, so an individual impairment would count the same "
                    "value twice.",
                    asset=asset.display_name,
                    pool=asset.lvp_pool_id.display_name,
                ))
            latest_accounting_date = (
                asset._eh_latest_accounting_evidence_date()
            )
            if rec.impairment_date < latest_accounting_date:
                raise UserError(_(
                    "Impairment date %(date)s precedes existing accounting "
                    "evidence dated %(latest)s on %(asset)s. Backdating the "
                    "event would rebuild the schedule underneath later sealed "
                    "entries; post a current-period correction instead.",
                    date=rec.impairment_date, latest=latest_accounting_date,
                    asset=asset.display_name,
                ))
            rec._check_reversal_cap()
            asset.invalidate_recordset(['net_book_value'])
            if (not rec.is_reversal
                    and asset.currency_id.compare_amounts(
                        rec.amount, asset.net_book_value,
                    ) > 0):
                raise UserError(_(
                    "Impairment of %(amount).2f exceeds %(carrying).2f carrying "
                    "amount on %(asset)s.",
                    amount=rec.amount, carrying=asset.net_book_value,
                    asset=asset.display_name,
                ))
            journal = rec.journal_id or asset.journal_id
            if not journal:
                raise UserError(_(
                    "No journal configured for impairment on %s.",
                ) % asset.display_name)
            expense_acc = (
                rec.impairment_account_id
                or asset.disposal_loss_account_id
            )
            contra_acc = (
                rec.accumulated_account_id
                or asset.accumulated_depreciation_account_id
            )
            if not expense_acc or not contra_acc:
                raise UserError(_(
                    "Configure the impairment loss and accumulated "
                    "impairment accounts on the impairment record or "
                    "fall back to the asset's accumulated depreciation "
                    "and disposal loss accounts before posting.",
                ))
            reserve_used = 0.0
            reserve_acc = (
                rec.revaluation_reserve_account_id
                or asset.revaluation_reserve_account_id
            )
            if not rec.is_reversal:
                reserve_used = asset.currency_id.round(min(
                    rec.amount, asset.revaluation_surplus or 0.0,
                ))
                if reserve_used and not reserve_acc:
                    raise UserError(_(
                        "%(asset)s carries a revaluation surplus of %(surplus).2f. "
                        "IAS 36.60 requires this impairment to debit that "
                        "reserve before P&L; configure a Revaluation Reserve "
                        "account on the impairment or asset.",
                        asset=asset.display_name,
                        surplus=asset.revaluation_surplus,
                    ))
                if (reserve_used
                        and reserve_acc.account_type != 'equity'):
                    raise UserError(_(
                        "Revaluation reserve %(account)s must be an equity "
                        "account before IAS 36.60 can use it.",
                        account=reserve_acc.display_name,
                    ))
            label = _("Impairment %s on %s") % (
                _("reversal") if rec.is_reversal else _("charge"),
                asset.display_name,
            )
            move_lines = []
            if rec.is_reversal:
                move_lines.extend([
                    (0, 0, {
                        'name': label,
                        'account_id': contra_acc.id,
                        'debit': rec.amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': expense_acc.id,
                        'debit': 0.0,
                        'credit': rec.amount,
                    }),
                ])
            else:
                expense_amount = asset.currency_id.round(
                    rec.amount - reserve_used,
                )
                if reserve_used:
                    move_lines.append((0, 0, {
                        'name': _(
                            "Impairment against revaluation surplus %s",
                            asset.display_name,
                        ),
                        'account_id': reserve_acc.id,
                        'debit': reserve_used,
                        'credit': 0.0,
                    }))
                if expense_amount:
                    move_lines.append((0, 0, {
                        'name': label,
                        'account_id': expense_acc.id,
                        'debit': expense_amount,
                        'credit': 0.0,
                    }))
                move_lines.append((0, 0, {
                    'name': label,
                    'account_id': contra_acc.id,
                    'debit': 0.0,
                    'credit': rec.amount,
                }))
            move_vals = {
                'move_type': 'entry',
                'eh_sealed': True,
                'journal_id': journal.id,
                'date': rec.impairment_date,
                'ref': "%s / %s" % (asset.name or '', rec.id),
                'line_ids': move_lines,
            }
            move = self.env['account.move']._eh_create_sealed(move_vals)
            move.action_post()
            rec._eh_workflow_write({
                'state': 'posted',
                'move_id': move.id,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'revaluation_surplus_used': reserve_used,
            })
            if reserve_used:
                asset._eh_workflow_write({
                    'revaluation_surplus': asset.currency_id.round(
                        asset.revaluation_surplus - reserve_used,
                    ),
                })
            # IAS 36.63: after an impairment (or its reversal) is
            # recognised, re-amortise the revised carrying amount, less
            # residual, over the remaining useful life so future
            # depreciation does not keep running off the pre-impairment
            # base (which would over-depreciate the asset).
            asset._eh_rebuild_after_impairment()
            # Latest recoverable-amount measurement: when the event
            # states its recoverable amount, stamp it onto the asset so
            # the revaluation wizard can cap uplifts against it.
            asset_vals = {}
            if rec.recoverable_amount:
                asset_vals.update({
                    'recoverable_amount_latest': rec.recoverable_amount,
                    'recoverable_amount_date': rec.impairment_date,
                })
            # A posted impairment event is test evidence for the IAS 36
            # annual-test mandate; clear the overdue flag immediately
            # (the cron re-evaluates on its next pass).
            if asset.annual_test_overdue:
                asset_vals['annual_test_overdue'] = False
            if asset_vals:
                asset.write(asset_vals)
        return True

    def action_cancel(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can cancel an impairment charge "
                "or reversal, because cancelling reverses its journal entry "
                "and moves the asset's carrying amount. This is a "
                "segregation-of-duties control point.",
            ))
        self._eh_check_access('write')
        self.mapped('asset_id')._eh_check_access('write')
        self.mapped('cgu_id')._eh_check_access('write')
        self._eh_lock_for_action()
        self = self._eh_workflow_action()
        for rec in self:
            if rec.state == 'cancelled':
                continue
            if rec.asset_id.state not in ('running', 'paused'):
                raise UserError(_(
                    "Impairment cancellation requires a running or paused "
                    "asset; %(asset)s is %(state)s.",
                    asset=rec.asset_id.display_name,
                    state=rec.asset_id.state,
                ))
            if rec.state == 'posted':
                later_event = self.search([
                    ('asset_id', '=', rec.asset_id.id),
                    ('state', '=', 'posted'),
                    ('id', '!=', rec.id),
                    '|',
                    ('impairment_date', '>', rec.impairment_date),
                    '&',
                    ('impairment_date', '=', rec.impairment_date),
                    ('id', '>', rec.id),
                ], order='impairment_date, id', limit=1)
                if later_event:
                    raise UserError(_(
                        "Impairment %(event)s cannot be cancelled while later "
                        "posted event %(later)s (%(date)s) remains live. "
                        "Cancelling out of order would recompute carrying "
                        "amount and future depreciation beneath later sealed "
                        "evidence, and can make net impairment negative. "
                        "Cancel impairment events in reverse chronological "
                        "order.",
                        event=rec.display_name,
                        later=later_event.display_name,
                        date=later_event.impairment_date,
                    ))
                if not rec.move_id or rec.move_id.state != 'posted':
                    raise UserError(_(
                        "Posted impairment %(impairment)s has no live posted "
                        "source move. Repair the inconsistent ledger link "
                        "before cancelling.", impairment=rec.display_name,
                    ))
                if rec.reversal_move_id:
                    raise UserError(_(
                        "Impairment %(impairment)s already has a reversal move.",
                        impairment=rec.display_name,
                    ))
                reversal_date = max(
                    fields.Date.context_today(rec), rec.move_id.date,
                )
                reversal = rec.move_id._eh_reverse_with_verified_capability([{
                    'date': reversal_date,
                    'journal_id': (rec.journal_id
                                   or rec.asset_id.journal_id).id,
                    'ref': _(
                        "Cancellation of impairment %(impairment)s",
                        impairment=rec.display_name,
                    ),
                }], cancel=False)
                reversal._eh_post_verified_reversal()
                rec._eh_seal_reversal(reversal)
                rec._eh_workflow_write({
                    'state': 'cancelled',
                    'reversal_move_id': reversal.id,
                    'cancelled_at': fields.Datetime.now(),
                    'cancelled_by_id': self.env.user.id,
                })
                if rec.revaluation_surplus_used:
                    rec.asset_id._eh_workflow_write({
                        'revaluation_surplus': rec.asset_id.currency_id.round(
                            rec.asset_id.revaluation_surplus
                            + rec.revaluation_surplus_used,
                        ),
                    })
                if (rec.recoverable_amount
                        and rec.asset_id.recoverable_amount_date
                        == rec.impairment_date
                        and rec.asset_id.currency_id.compare_amounts(
                            rec.asset_id.recoverable_amount_latest,
                            rec.recoverable_amount,
                        ) == 0):
                    rec.asset_id._eh_workflow_write({
                        'recoverable_amount_latest': 0.0,
                        'recoverable_amount_date': False,
                    })
                rec.asset_id._eh_rebuild_after_impairment()
                continue
            if rec.move_id or rec.reversal_move_id:
                raise UserError(_(
                    "Draft impairment %(impairment)s carries a journal link; "
                    "repair it before cancelling.",
                    impairment=rec.display_name,
                ))
            rec._eh_workflow_write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })
        return True
