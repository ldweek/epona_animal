# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.book: a parallel depreciation book for a fixed asset.

Most jurisdictions require at least two books on the same asset:
the statutory book that posts to the General Ledger (governed by the
local accounting standard) and one or more tax books (governed by the
tax-deduction rules of each jurisdiction). Australia adds the
prime-cost vs diminishing-value choice for tax purposes; IFRS adds a
revaluation model that can diverge from the statutory book; large
groups add a management book for internal reporting.

This model holds one such book per asset, INDEPENDENT of the asset's
own primary depreciation parameters (which represent the statutory
book that posts to the GL). Books defined here are reporting-only:
they generate a parallel schedule that the user can extract for the
tax return or the management pack, but they never post journal entries.
Odoo has one General Ledger; charging a second full depreciation book
would duplicate expense and accumulated depreciation in that ledger.

Schedule generation reuses the same per-method helpers as the asset's
primary schedule, parameterised on the book's own fields. This means
adding a new method (e.g. units of production at a different rate per
book) only needs the helper to be parameter-driven, which it already
is.
"""

import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_BOOK_TYPES = [
    ('statutory', "Statutory (parallel)"),
    ('tax', "Tax"),
    ('ifrs', "IFRS"),
    ('mgmt', "Management"),
]

_METHODS = [
    ('straight_line', "Straight Line"),
    ('reducing_balance', "Reducing Balance"),
    ('prime_cost', "Prime Cost (AU tax)"),
    ('diminishing_value', "Diminishing Value (AU tax)"),
    ('manual', "Manual"),
]


class EhAssetBook(models.Model):
    _name = 'eh.asset.book'
    _description = "Asset depreciation book"
    _order = 'asset_id, book_type, id'
    _rec_name = 'name'

    name = fields.Char(
        required=True,
        help=(
            "Display label for the book, e.g. 'AU Tax Book' or "
            "'IFRS book'. Shown on the asset form and on every "
            "schedule line so reports can disambiguate parallel "
            "schedules."
        ),
    )
    asset_id = fields.Many2one(
        'eh.asset', required=True, ondelete='cascade', index=True,
        check_company=True,
        help="Parent asset this book belongs to.",
    )
    book_type = fields.Selection(
        _BOOK_TYPES, required=True, default='tax',
        help=(
            "Functional category of the book. Statutory parallel books "
            "duplicate the primary book under a different accounting "
            "standard. Tax books drive tax-return numbers. IFRS / "
            "management books drive secondary reporting. The category "
            "is informational; every additional book is reporting-only."
        ),
    )
    posts_to_gl = fields.Boolean(
        default=False, readonly=True,
        help=(
            "Legacy audit flag. Additional books are reporting-only and "
            "cannot post in current versions: the primary schedule on the "
            "asset is the sole GL source of truth. A True value is retained "
            "only when an upgraded database already contains historical "
            "parallel-book journal evidence, which remains reversible."
        ),
    )

    method = fields.Selection(
        _METHODS, required=True, default='straight_line',
        help=(
            "Depreciation method for this book. Prime Cost is the "
            "AU tax straight-line over the effective life. "
            "Diminishing Value is the AU tax reducing-balance with the "
            "200% factor applied to the prime-cost rate by default."
        ),
    )
    useful_life_months = fields.Integer(
        string="Useful Life (months)",
        required=True, default=60,
        help=(
            "Effective life used for this book's schedule. Tax-book "
            "lives are dictated by ATO ruling TR 2025/1 (and "
            "successors); statutory book lives reflect the entity's "
            "accounting policy."
        ),
    )
    salvage_value = fields.Monetary(
        default=0.0,
        currency_field='currency_id',
        help=(
            "Estimated residual value at end of useful life. Salvage "
            "for tax-book purposes is typically zero (write down to "
            "zero); statutory salvage may be non-zero for assets with "
            "expected resale value."
        ),
    )
    declining_factor = fields.Float(
        default=2.0,
        help=(
            "Multiplier on the straight-line rate when method is "
            "Reducing Balance or Diminishing Value. Australia uses "
            "200% (factor 2.0) for assets acquired on or after "
            "10 May 2006; legacy assets use 150% (factor 1.5)."
        ),
    )
    prorate_first_period = fields.Boolean(
        default=True,
        help=(
            "When set, the first period's depreciation is prorated "
            "based on days in service that month. AU tax usually "
            "prorates by days held; statutory accounting may use "
            "either convention."
        ),
    )

    currency_id = fields.Many2one(
        related='asset_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='asset_id.company_id', store=True, readonly=True,
    )

    line_ids = fields.One2many(
        'eh.asset.book.line', 'book_id', copy=False,
        help="Generated schedule lines for this book.",
    )
    line_count = fields.Integer(compute='_compute_totals', store=False)
    total_depreciation = fields.Monetary(
        compute='_compute_totals', store=False,
        currency_field='currency_id',
        help="Sum of every scheduled depreciation line on this book.",
    )
    final_book_value = fields.Monetary(
        compute='_compute_totals', store=False,
        currency_field='currency_id',
        help=(
            "Net book value at the end of the schedule. Should equal "
            "the salvage value; non-zero divergence is rounding."
        ),
    )

    notes = fields.Text(
        help=(
            "Notes on the book's purpose, regulatory reference, or "
            "differences from the primary book. Visible to the tax "
            "agent and the auditor."
        ),
    )

    _check_useful_life = models.Constraint(
        'CHECK (useful_life_months > 0)',
        'Useful life must be greater than zero on every book.',
    )
    _check_salvage = models.Constraint(
        'CHECK (salvage_value >= 0)',
        'Salvage value cannot be negative.',
    )

    # Once this book has produced ledger evidence, its source identity and
    # measurement policy must continue to explain those entries. Corrections
    # use the line reversal action and a new book, never an in-place rewrite.
    _FROZEN_AFTER_BOOKING = (
        'name', 'asset_id', 'book_type', 'posts_to_gl', 'method',
        'useful_life_months', 'salvage_value', 'declining_factor',
        'prorate_first_period',
    )

    def _eh_lock_for_change(self):
        if not self.ids:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        self.mapped('asset_id')._eh_lock_for_transition()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_book WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'asset_id', 'posts_to_gl', 'method', 'salvage_value', 'line_ids',
        ])
        return self

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('posts_to_gl') for vals in vals_list):
            raise ValidationError(_(
                "Additional depreciation books are reporting-only. The "
                "asset's primary schedule is the sole General Ledger source; "
                "enabling a second full posting book would duplicate "
                "depreciation expense and accumulated depreciation."
            ))
        assets = self.env['eh.asset'].browse({
            vals.get('asset_id') for vals in vals_list if vals.get('asset_id')
        })
        if not self.env.su:
            assets._eh_lock_for_transition()
        assets._eh_validate_company_currency()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('posts_to_gl'):
            raise UserError(_(
                "Additional depreciation books cannot be enabled for General "
                "Ledger posting. Use the primary asset schedule for GL and "
                "keep this book for parallel tax or management reporting."
            ))
        frozen = [
            field_name for field_name in self._FROZEN_AFTER_BOOKING
            if field_name in vals
        ]
        if frozen and not self.env.su:
            self._eh_check_access('write')
        if frozen:
            self._eh_lock_for_change()
            if vals.get('asset_id'):
                target = self.env['eh.asset'].browse(vals['asset_id'])
                if not self.env.su:
                    target._eh_lock_for_transition()
                target._eh_validate_company_currency()
            booked = self.filtered(lambda book: any(
                line.is_posted or line.move_id or line.reversal_move_id
                for line in book.line_ids
            ))
            if booked:
                raise UserError(_(
                    "Book policy fields (%(fields)s) are frozen after a "
                    "book line has posted. Reverse the booked lines and "
                    "create a new book for a changed accounting policy.",
                    fields=', '.join(frozen),
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        booked = self.filtered(lambda book: any(
            line.is_posted or line.move_id or line.reversal_move_id
            for line in book.line_ids
        ))
        if booked:
            raise UserError(_(
                "A depreciation book with ledger entries cannot be deleted. "
                "Its schedule and move links are permanent audit evidence.",
            ))
        return super().unlink()

    @api.depends('line_ids.amount')
    def _compute_totals(self):
        for book in self:
            book.line_count = len(book.line_ids)
            book.total_depreciation = sum(
                book.line_ids.mapped('amount'),
            )
            if book.line_ids:
                book.final_book_value = (
                    book.asset_id.acquisition_cost
                    - book.total_depreciation
                )
            else:
                book.final_book_value = book.asset_id.acquisition_cost

    @api.constrains('salvage_value', 'asset_id')
    def _check_salvage_le_cost(self):
        for book in self:
            if (book.asset_id and book.salvage_value
                    > book.asset_id.acquisition_cost):
                raise ValidationError(_(
                    "Salvage value on book %(book)s exceeds the asset's "
                    "acquisition cost. Lower the salvage or revise the "
                    "asset cost first.",
                    book=book.display_name,
                ))

    # ---- actions ----

    def action_compute_schedule(self):
        """Generate (or regenerate) the schedule for this book.

        Refuses to overwrite any historical line carrying ledger evidence;
        otherwise wipes existing lines and rebuilds from current parameters.
        """
        self._eh_check_access('write')
        self._eh_lock_for_change()
        for book in self:
            book.asset_id._eh_validate_company_currency()
            posted = book.line_ids.filtered(
                lambda line: (
                    line.is_posted or line.move_id or line.reversal_move_id
                ),
            )
            if posted:
                raise UserError(_(
                    "Book %(book)s has %(n)d posted line(s); recompute "
                    "would corrupt the GL. Mark the asset as paused, "
                    "reverse the postings, or create a new book.",
                    book=book.display_name, n=len(posted),
                ))
            book.line_ids.sudo().unlink()
            book._build_schedule()
        return True

    def action_post_due_lines(self):
        """Fail closed: only the asset's primary schedule may post to GL."""
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can post a depreciation "
                "book to the general ledger.",
            ))
        if self:
            raise UserError(_(
                "Additional depreciation books are reporting-only and cannot "
                "post to the General Ledger. The primary asset schedule is "
                "the sole GL source of truth. Historical parallel-book moves "
                "remain available through their source and reversal links."
            ))
        return True

    def _build_schedule(self):
        self.ensure_one()
        Line = self.env['eh.asset.book.line'].sudo()
        rows = self._generate_rows()
        for row in rows:
            Line.create({
                'book_id': self.id,
                'sequence': row['sequence'],
                'depreciation_date': row['date'],
                'amount': row['amount'],
                'accumulated': row['accumulated'],
                'remaining_value': row['remaining'],
            })

    def _generate_rows(self):
        """Pure function: return the schedule rows for this book.

        Mirrors the asset's _schedule_* helpers but reads parameters
        from the book record so each book can carry an independent
        method / life / factor.
        """
        self.ensure_one()
        if self.method == 'manual':
            return []
        depreciable = (
            self.asset_id.acquisition_cost - self.salvage_value
        )
        if depreciable <= 0:
            return []
        if self.method in ('straight_line', 'prime_cost'):
            return self._rows_straight_line(depreciable)
        if self.method in ('reducing_balance', 'diminishing_value'):
            return self._rows_reducing_balance(depreciable)
        return []

    def _rows_straight_line(self, depreciable):
        """Straight-line / prime-cost rows.

        AU prime-cost = depreciable / effective_life expressed as a
        per-month figure. Identical maths to the existing straight-
        line helper; the label difference signals intent in tax
        reporting.
        """
        self.ensure_one()
        rows = []
        months = self.useful_life_months
        per_period = depreciable / months
        period_fractions = self._schedule_period_fractions(months)
        accumulated = 0.0
        period_date = self._first_period_date()
        for n, period_fraction in enumerate(period_fractions, start=1):
            if n == len(period_fractions):
                amount = depreciable - accumulated
            else:
                amount = per_period * period_fraction
            amount = self.currency_id.round(amount)
            accumulated = self.currency_id.round(accumulated + amount)
            remaining = self.currency_id.round(
                self.asset_id.acquisition_cost - accumulated,
            )
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': remaining,
            })
            period_date = self._next_period_end(period_date)
        return rows

    def _rows_reducing_balance(self, depreciable):
        """Reducing-balance / diminishing-value rows.

        AU diminishing-value rate = (declining_factor / effective_life)
        applied to the opening NBV each period. Switches to straight-
        line when the SL on the remaining balance exceeds the DV
        amount, which is the AU pattern: 200% DV in early years,
        flatten to SL when the DV would otherwise drag the asset
        below zero before end of life.
        """
        self.ensure_one()
        rows = []
        months = self.useful_life_months
        years = max(1, months / 12.0)
        sl_rate_per_year = 1.0 / years
        rate_per_year = self.declining_factor * sl_rate_per_year
        rate_per_period = rate_per_year / 12.0
        period_fractions = self._schedule_period_fractions(months)
        accumulated = 0.0
        nbv = self.asset_id.acquisition_cost
        period_date = self._first_period_date()
        for n, period_fraction in enumerate(period_fractions, start=1):
            remaining_periods = sum(period_fractions[n - 1:])
            sl_amount = max(
                0.0, (nbv - self.salvage_value) / remaining_periods,
            )
            rb_amount = max(
                0.0, (nbv - self.salvage_value),
            ) * rate_per_period
            amount = max(rb_amount, sl_amount) * period_fraction
            if n == len(period_fractions):
                amount = depreciable - accumulated
            amount = max(0.0, self.currency_id.round(amount))
            if accumulated + amount > depreciable:
                amount = self.currency_id.round(depreciable - accumulated)
            accumulated = self.currency_id.round(accumulated + amount)
            nbv = self.asset_id.acquisition_cost - accumulated
            rows.append({
                'sequence': n,
                'date': period_date,
                'amount': amount,
                'accumulated': accumulated,
                'remaining': self.currency_id.round(nbv),
            })
            period_date = self._next_period_end(period_date)
            if accumulated >= depreciable:
                break
        return rows

    def _first_period_date(self):
        d = self.asset_id.in_service_date
        return self._month_end(d)

    @staticmethod
    def _month_end(d):
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, last)

    def _next_period_end(self, d):
        nxt = d + relativedelta(months=1)
        return self._month_end(nxt)

    def _first_period_prorated_amount(self, full_period_amount):
        d = self.asset_id.in_service_date
        last = calendar.monthrange(d.year, d.month)[1]
        days_in_service = last - d.day + 1
        if last <= 0:
            return full_period_amount
        return full_period_amount * (days_in_service / float(last))

    def _schedule_period_fractions(self, months):
        """Span exact useful life with first and final calendar stubs."""
        self.ensure_one()
        first_fraction = 1.0
        if self.prorate_first_period:
            first_fraction = self._first_period_prorated_amount(1.0)
        first_fraction = max(0.0, min(1.0, first_fraction))
        remaining = max(0.0, float(months) - first_fraction)
        full_periods = int(remaining)
        final_fraction = remaining - full_periods
        fractions = [first_fraction] + [1.0] * full_periods
        if final_fraction > 1e-12:
            fractions.append(final_fraction)
        return fractions


class EhAssetBookLine(models.Model):
    _name = 'eh.asset.book.line'
    _inherit = ['eh.workflow.guard', 'eh.gl.reversal']
    _description = "Asset book schedule line"
    _order = 'book_id, sequence, depreciation_date'

    _eh_guarded_fields = (
        'is_posted', 'move_id', 'posted_at', 'posted_by_id',
        'reversal_move_id', 'reversed_at', 'reversed_by_id',
    )

    book_id = fields.Many2one(
        'eh.asset.book', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    asset_id = fields.Many2one(
        related='book_id.asset_id', store=True, index=True,
    )
    sequence = fields.Integer(default=1)
    depreciation_date = fields.Date(
        required=True,
        help="Period-end date the depreciation amount falls on.",
    )
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Period depreciation amount per the book's method.",
    )
    accumulated = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative depreciation through the end of this period. "
            "Independent of any other book's accumulator."
        ),
    )
    remaining_value = fields.Monetary(
        currency_field='currency_id',
        help="Net book value after this period's depreciation.",
    )
    is_posted = fields.Boolean(
        default=False, readonly=True, copy=False,
        help=(
            "Legacy audit marker. Current additional books are reporting-only "
            "and leave this False. True is retained only for a historical "
            "parallel-book journal entry created by an earlier release."
        ),
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help=(
            "Historical journal entry linked to this line. Current additional "
            "books cannot create new moves; legacy links remain immutable."
        ),
    )
    posted_at = fields.Datetime(readonly=True, copy=False)
    posted_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help=(
            "Sealed counter-entry produced by the book-line reversal "
            "action. The original posting remains linked and immutable."
        ),
    )
    reversed_at = fields.Datetime(readonly=True, copy=False)
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )

    currency_id = fields.Many2one(
        related='book_id.currency_id', store=True, readonly=True,
    )

    _uniq_book_sequence = models.Constraint(
        'unique(book_id, sequence)',
        'Sequence must be unique within a depreciation book.',
    )
    _check_amount_non_negative = models.Constraint(
        'CHECK (amount >= 0)',
        'Book depreciation amount cannot be negative.',
    )

    _FROZEN_AFTER_POST = (
        'book_id', 'sequence', 'depreciation_date', 'amount',
        'accumulated', 'remaining_value',
    )

    @api.model_create_multi
    def create(self, vals_list):
        books = self.env['eh.asset.book'].browse({
            vals.get('book_id') for vals in vals_list if vals.get('book_id')
        })
        if not self.env.su:
            books._eh_lock_for_change()
            if books.filtered(lambda book: book.method != 'manual'):
                raise UserError(_(
                    "Schedule rows for non-manual depreciation books are "
                    "engine-generated. Use Compute Schedule instead."
                ))
        books.mapped('asset_id')._eh_validate_company_currency()
        return super().create(vals_list)

    def write(self, vals):
        # Preserve the workflow guard's AccessError for attempts to forge
        # posting/reversal identity before applying the broader row policy.
        if not self.env.su and set(vals) & set(self._eh_guarded_fields):
            return super().write(vals)
        protected = set(vals) & (
            set(self._eh_guarded_fields) | set(self._FROZEN_AFTER_POST)
        )
        if protected and not self.env.su:
            self._eh_check_access('write')
            books = self.mapped('book_id')
            if vals.get('book_id'):
                books |= self.env['eh.asset.book'].browse(vals['book_id'])
            books._eh_lock_for_change()
            if books.filtered(lambda book: book.method != 'manual'):
                raise UserError(_(
                    "Engine-generated depreciation-book rows cannot be edited "
                    "directly. Recompute the draft schedule instead."
                ))
        frozen = [
            field_name for field_name in self._FROZEN_AFTER_POST
            if field_name in vals
        ]
        if frozen:
            booked = self.filtered(
                lambda line: (
                    line.is_posted or line.move_id or line.reversal_move_id
                ),
            )
            if booked:
                raise UserError(_(
                    "Book schedule fields (%(fields)s) are frozen once the "
                    "line has posted. Reverse the entry and create a new "
                    "book for a corrected schedule.",
                    fields=', '.join(frozen),
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            if self.mapped('book_id').filtered(
                    lambda book: book.method != 'manual'):
                raise UserError(_(
                    "Engine-generated depreciation-book rows cannot be deleted "
                    "directly. Recompute the schedule instead."
                ))
        booked = self.filtered(
            lambda line: (
                line.is_posted or line.move_id or line.reversal_move_id
            ),
        )
        if booked:
            raise UserError(_(
                "A booked depreciation-book line cannot be deleted; its "
                "source identity and journal links are permanent evidence.",
            ))
        return super().unlink()

    def _eh_lock_for_post(self):
        if not self.ids:
            return
        self.mapped('book_id')._eh_lock_for_change()
        self.env.cr.execute(
            'SELECT id FROM eh_asset_book_line WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'is_posted', 'move_id', 'reversal_move_id',
        ])

    def action_post(self):
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can post depreciation-book "
                "lines to the general ledger.",
            ))
        self._eh_lock_for_post()
        for line in self:
            # Both posting and reversal are post-once operations. A repeat
            # click or concurrent waiter is a no-op after the row lock.
            if line.reversal_move_id:
                continue
            if line.is_posted and not line.move_id:
                raise UserError(_(
                    "%(line)s is marked posted but has no journal-entry "
                    "link. Repair that inconsistent legacy record before "
                    "continuing.",
                    line=line.display_name,
                ))
            if line.is_posted:
                continue
            if line.move_id:
                raise UserError(_(
                    "%(line)s already has a journal-entry link but is not "
                    "marked posted. Repair that inconsistent legacy record "
                    "before attempting another posting.",
                    line=line.display_name,
                ))
            raise UserError(_(
                "Additional depreciation-book rows cannot post to the General "
                "Ledger. Posting the same asset measurement through a second "
                "full book would duplicate expense and accumulated "
                "depreciation. Use the primary schedule; retain this row for "
                "parallel reporting only."
            ))
        return True

    def action_reverse(self):
        """Post and seal one counter-entry; never erase the source move."""
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH accounting manager can reverse a booked "
                "depreciation-book line.",
            ))
        self._eh_lock_for_post()
        for line in self:
            if line.reversal_move_id:
                continue
            if not line.is_posted or not line.move_id:
                raise UserError(_(
                    "Only a posted book line with its source move can be "
                    "reversed.",
                ))
            if line.move_id.state != 'posted':
                raise UserError(_(
                    "The source move is not posted; repair the inconsistent "
                    "ledger link before reversing the book line.",
                ))
            reversal = line.move_id._eh_reverse_with_verified_capability([{
                'date': fields.Date.context_today(line),
                'journal_id': line.book_id.asset_id.journal_id.id,
                'ref': _(
                    "Reversal of book depreciation %(asset)s / %(book)s "
                    "#%(seq)s",
                    asset=line.asset_id.display_name,
                    book=line.book_id.display_name,
                    seq=line.sequence,
                ),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            line._eh_seal_reversal(reversal)
            line._eh_workflow_write({
                'reversal_move_id': reversal.id,
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
            })
        return True

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No book journal entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }
