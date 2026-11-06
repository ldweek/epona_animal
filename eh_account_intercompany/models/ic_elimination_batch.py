# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Inter-company elimination pair engine (IFRS 10.B86).

eh.ic.elimination.batch matches the posted inter-company move pairs of one
company pair for one period (via the eh_intercompany_origin_id linkage the
mirroring engine maintains) and builds the elimination legs the group
accounts need:

* balance elimination: the receivable still outstanding on the selling
  side at the reporting cutoff against the reciprocal payable still
  outstanding on the buying side at that cutoff;
* flow elimination: the revenue recognised on the selling side against
  the expense recognised on the buying side, for the period.

Lifecycle: draft -> computed -> posted. Compute is idempotent: rerunning
it replaces the engine-built lines (a DB unique constraint on
pair + period forbids duplicate batches, including the reversed pair).
Posting books one balanced, sealed journal entry in the designated distinct
consolidation-only elimination company (never a member legal entity) in a dedicated
elimination journal that is auto-created on first use and stored on that
company's inter-company configuration.  Balance-sheet legs are posted as the
delta from earlier active batches for the same company pair.  This leaves the
persistent elimination ledger at the current period-end target instead of
adding the full receivable/payable balance again every month.

Pairs that do not reconcile are never silently eliminated:

* a posted source with no mirror, or with a mirror still in draft, is
  listed on the mismatch tab with the reason and produces no legs;
* a pair whose two totals diverge by more than one cent is listed on the
  mismatch tab AND eliminated only at the common (lower) amount, so the
  unreconciled difference stays visible in the group accounts;
* posting is refused while mismatches exist, unless the manager clears
  the batch's block_on_mismatch flag (the audited override path).

Unrealised profit in inventory (IFRS 10.B86(c)) is computed from the
source documents, never hand-typed: for every matched pair whose selling
side is a customer invoice, margin uses immutable selling-company cost
captured when source posted, so later standard-cost changes cannot rewrite
history. Remaining fraction is approved per exact source invoice line
against dated period-end inventory evidence. Current stock quants are not
used: they cannot prove inter-company provenance or reconstruct a historical
reporting cutoff and can allocate the same on-hand units to several invoices.

Consolidation hook
------------------
eh_ic_elimination_summary(period_from, period_to, company_ids=None) on
this model returns the structured totals (receivable / payable
eliminated, revenue / expense eliminated, unrealised profit, mismatch
count, per-batch rows) so eh_account_consolidation (or any caller) can
consume elimination evidence without coupling both modules. Unrealised
inventory profit is also booked here, once, as a point-in-time inventory /
cost-of-sales delta in the sealed elimination ledger; summary hook reports
same target but never posts it again.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL
from odoo.tools import float_is_zero

from .account_move import _DIRECTION_MIRROR, _eh_ic_capable

# Context key the elimination engine sets on its own writes so the
# engine-built child rows (lines / mismatches / unrealised) let the
# sanctioned rebuild paths through their own edit guards.
IC_ENGINE_CTX = 'eh_ic_elim_engine'

_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = ('expense', 'expense_depreciation', 'expense_direct_cost')

# Identity fields frozen once the batch has been computed (they define
# which pairs were matched); change them by resetting to draft.
_FROZEN_AFTER_COMPUTE = (
    'company_a_id', 'company_b_id', 'period_from', 'period_to',
    'elimination_company_id',
)

# Audit fields are written only by the workflow engine.  They are deliberately
# guarded in addition to being readonly in the views: readonly is a client-side
# hint and does not stop an RPC write.
_ENGINE_OWNED_FIELDS = (
    'move_id', 'reversal_move_id', 'move_history_ids',
    'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
    'reversed_at', 'reversed_by_id',
    'statutory_ledger_quarantined', 'statutory_ledger_quarantine_note',
)

# Tolerance for reciprocal totals after translation into the elimination
# currency.
_PAIR_TOLERANCE = 0.01


def _acc_company_field(env):
    # account.account is multi-company (company_ids) from Odoo 18;
    # single company_id before that.
    Account = env['account.account']
    return 'company_ids' if 'company_ids' in Account._fields else 'company_id'


class EhIcEliminationBatch(models.Model):
    _name = 'eh.ic.elimination.batch'
    _description = "Inter-company elimination batch"
    _order = 'period_to desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.gl.reversal',
                'eh.workflow.guard']

    # The state machine is enforced by eh.workflow.guard: 'state' may only
    # change through this model's own actions (which run as su), never a
    # direct RPC/ORM write, closing the "write({'state': 'posted'}) skips
    # action_post and its sealed journal entry" bypass.
    _eh_guarded_fields = ('state',) + _ENGINE_OWNED_FIELDS

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    company_a_id = fields.Many2one(
        'res.company', required=True, index=True, tracking=True,
        string="Company A",
        help=(
            "First company of the pair whose inter-company move pairs "
            "this batch matches and eliminates. The pair is unordered: "
            "a batch for (A, B) also covers documents that originate "
            "in B towards A."
        ),
    )
    company_b_id = fields.Many2one(
        'res.company', required=True, index=True, tracking=True,
        string="Company B",
        help="Second company of the pair.",
    )
    period_from = fields.Date(
        required=True, tracking=True,
        help="Inclusive start of the elimination period.",
    )
    period_to = fields.Date(
        required=True, tracking=True,
        help=(
            "Inclusive end of the elimination period. The elimination "
            "move is dated on this day."
        ),
    )
    elimination_company_id = fields.Many2one(
        'res.company', required=True, tracking=True,
        default=lambda self: self._default_elimination_company(),
        string="Elimination Company",
        domain="[('eh_ic_consolidation_only', '=', True)]",
        help=(
            "Dedicated non-trading consolidation workspace where the "
            "elimination journal is booked. It must be distinct from both "
            "member legal entities and explicitly marked Consolidation-only. "
            "Every account touched must exist there by code."
        ),
    )
    currency_id = fields.Many2one(
        related='elimination_company_id.currency_id',
        store=True, readonly=True,
    )
    block_on_mismatch = fields.Boolean(
        default=True, tracking=True,
        help=(
            "When set (the default), posting is refused while the batch "
            "carries mismatched or unmatched pairs. A manager can clear "
            "this flag to post the matched eliminations anyway; the "
            "override is tracked in the chatter and the mismatches stay "
            "listed for follow-up."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('computed', "Computed"),
            ('posted', "Posted"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'eh.ic.elimination.batch.line', 'batch_id',
        help=(
            "Engine-built elimination legs, one per account per matched "
            "pair. Rebuilt on every compute; not hand-editable."
        ),
    )
    mismatch_ids = fields.One2many(
        'eh.ic.elimination.mismatch', 'batch_id',
        help=(
            "Pairs that did not reconcile: no mirror, mirror still in "
            "draft, or totals diverging beyond one cent. Each row "
            "carries the reason; posting is blocked while rows exist "
            "unless block_on_mismatch is cleared."
        ),
    )
    unrealised_line_ids = fields.One2many(
        'eh.ic.unrealised.line', 'batch_id',
        help=(
            "Unrealised profit in inventory per product line of the "
            "matched inter-company sale invoices. The margin is always "
            "derived from the invoice against the product standard "
            "cost; only the remaining fraction is manual when the "
            "stock module is not installed."
        ),
    )

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help=(
            "The latest sealed elimination journal entry posted in the "
            "elimination company. Reset to draft reverses it but never "
            "deletes either side of the accounting evidence."
        ),
    )
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, tracking=True,
        ondelete='restrict',
        help="The posted reversal of the latest elimination move, if reset.",
    )
    move_history_ids = fields.Many2many(
        'account.move', 'eh_ic_elimination_batch_move_history_rel',
        'batch_id', 'move_id', readonly=True, copy=False,
        string="Elimination move history",
        help=(
            "Append-only history of every original and reversal journal entry "
            "created for this batch. Entries are never unlinked on reset."
        ),
    )
    move_state = fields.Selection(
        related='move_id.state', string="Move status", readonly=True,
    )

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    reversed_at = fields.Datetime(readonly=True, tracking=True)
    reversed_by_id = fields.Many2one('res.users', readonly=True)
    statutory_ledger_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Upgrade audit flag: legacy eliminations used a member/statutory "
            "ledger or an entity not proven consolidation-only."
        ),
    )
    statutory_ledger_quarantine_note = fields.Text(
        readonly=True, copy=False,
    )

    line_count = fields.Integer(compute='_compute_counts')
    mismatch_count = fields.Integer(compute='_compute_counts')
    unrealised_count = fields.Integer(compute='_compute_counts')

    receivable_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Net receivable outstanding at period end eliminated by this batch.",
    )
    payable_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Net payable outstanding at period end eliminated by this batch.",
    )
    revenue_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Net inter-company revenue flow eliminated by this batch.",
    )
    expense_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help="Net inter-company expense flow eliminated by this batch.",
    )
    unrealised_total = fields.Monetary(
        compute='_compute_totals', currency_field='currency_id',
        help=(
            "Unrealised profit in ending inventory computed from the "
            "matched inter-company sale invoices. Booked once as a "
            "point-in-time inventory/cost-of-sales delta and exposed to "
            "the consolidation summary as the same target."
        ),
    )

    notes = fields.Text()

    _unique_pair_period = models.Constraint(
        'unique(company_a_id, company_b_id, period_from, period_to)',
        "An elimination batch already exists for this company pair and "
        "period.",
    )

    # ------------------------------------------------------------------
    # defaults / constraints / CRUD guards
    # ------------------------------------------------------------------

    @api.model
    def _default_elimination_company(self):
        config = self.env['eh.intercompany.config'].sudo().search(
            [('enabled', '=', True),
             ('company_id', 'in', self.env.companies.ids),
             ('elimination_company_id', 'in', self.env.companies.ids),
             ('elimination_company_id.eh_ic_consolidation_only', '=', True)],
            limit=1,
        )
        return config.elimination_company_id

    def _eh_assert_elimination_workspace(self):
        for batch in self:
            company = batch.elimination_company_id
            if (
                not company
                or company in (batch.company_a_id | batch.company_b_id)
                or not company.eh_ic_consolidation_only
                or batch.statutory_ledger_quarantined
            ):
                raise UserError(_(
                    "Batch %(batch)s must use a distinct company explicitly "
                    "marked as a consolidation-only elimination entity. "
                    "Never post eliminations into a member legal entity's "
                    "statutory general ledger. Legacy quarantined batches "
                    "must be reset/reversed and rebuilt in that workspace.",
                    batch=batch.display_name,
                ))
        return True

    @api.model
    def _eh_assert_company_ids_allowed(self, company_ids):
        """Fail before elevation unless the caller owns the whole closure."""
        if self.env.su:
            return
        requested = {int(company_id) for company_id in company_ids if company_id}
        if not requested.issubset(set(self.env.companies.ids)):
            # Do not identify or browse the forbidden company: the error must
            # not become an existence/name oracle for a guessed id.
            raise AccessError(_(
                "An inter-company elimination may only be created or operated "
                "when all pair and elimination companies are enabled in the "
                "caller's current company context."
            ))

    def _eh_assert_company_closure(self):
        if not self.env.su:
            self._eh_check_access('write')
            for batch in self:
                batch._eh_assert_company_ids_allowed((
                    batch.company_a_id.id,
                    batch.company_b_id.id,
                    batch.elimination_company_id.id,
                ))
        return self

    def _eh_lock_for_transition(self, extra_company_ids=()):
        """Serialize same-pair transitions without locking company rows.

        Posting touches ``res_company`` while holding journal locks. Taking
        those rows here inverted lock order and stalled unrelated posting.
        Transaction advisory locks use one deterministic namespace/key per
        company pair; batch rows remain authoritative state locks. Ledger
        snapshot stability is verified separately with ``eh_move_version``.
        """
        companies = (
            self.mapped('company_a_id')
            | self.mapped('company_b_id')
            | self.mapped('elimination_company_id')
        )
        company_ids = tuple(sorted(
            set(companies.ids) | {
                int(company_id) for company_id in extra_company_ids
                if company_id
            }
        ))
        for company_id in company_ids:
            self.env.cr.execute(
                SQL(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    "eh.ic.elimination:company:%d" % company_id,
                )
            )
        ids = tuple(sorted(self.ids))
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            SQL(
                "SELECT id FROM %s WHERE id IN %s ORDER BY id FOR UPDATE",
                SQL.identifier(self._table),
                ids,
            )
        )
        self.invalidate_recordset()
        return self

    def _eh_ledger_versions(self):
        """Committed ledger-version fingerprint for pair companies."""
        self.ensure_one()
        companies = self.company_a_id | self.company_b_id
        companies.invalidate_recordset(['eh_move_version'])
        return tuple(sorted(
            (company.id, company.eh_move_version) for company in companies
        ))

    @api.constrains('company_a_id', 'company_b_id',
                    'period_from', 'period_to', 'elimination_company_id')
    def _check_pair(self):
        for batch in self:
            if batch.company_a_id == batch.company_b_id:
                raise ValidationError(_(
                    "An elimination batch pairs two different companies.",
                ))
            if batch.period_from and batch.period_to \
                    and batch.period_from > batch.period_to:
                raise ValidationError(_(
                    "The period start must not be after the period end.",
                ))
            if batch.elimination_company_id and (
                    batch.elimination_company_id
                    in (batch.company_a_id | batch.company_b_id)
                    or not batch.elimination_company_id
                    .eh_ic_consolidation_only):
                raise ValidationError(_(
                    "The elimination company must be a distinct entity marked "
                    "Consolidation-only, never either member legal entity."
                ))
            # The DB unique constraint cannot see the pair as unordered,
            # so the reversed duplicate is refused here.
            swapped = self.sudo().search_count([
                ('id', '!=', batch.id),
                ('company_a_id', '=', batch.company_b_id.id),
                ('company_b_id', '=', batch.company_a_id.id),
                ('period_from', '=', batch.period_from),
                ('period_to', '=', batch.period_to),
            ])
            if swapped:
                raise ValidationError(_(
                    "An elimination batch already exists for this company "
                    "pair and period (with the companies in the other "
                    "order).",
                ))

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(['elimination_company_id'])
        company_ids = []
        for vals in vals_list:
            closure = (
                vals.get('company_a_id'),
                vals.get('company_b_id'),
                vals.get('elimination_company_id')
                or defaults.get('elimination_company_id'),
            )
            self._eh_assert_company_ids_allowed(closure)
            company_ids.extend(closure)
            if vals.get('name', '/') == '/':
                a = self.env['res.company'].browse(
                    vals.get('company_a_id')).name or '?'
                b = self.env['res.company'].browse(
                    vals.get('company_b_id')).name or '?'
                vals['name'] = "IC ELIM %s / %s %s" % (
                    a, b, vals.get('period_to') or '')
        # The unordered-pair Python constraint is race-safe only when every
        # creator serializes on the same sorted company rows first.
        self._eh_lock_for_transition(extra_company_ids=company_ids)
        return super().create(vals_list)

    def write(self, vals):
        """Freeze the pair / period identity once the batch leaves draft
        (the matched pairs are defined by them); restate via a reset to
        draft, not a direct edit. This freeze is always on for everyone -
        a settled figure is immutable regardless of privilege. The state
        machine itself is owned by eh.workflow.guard (state changes only
        through this model's own su-run actions). block_on_mismatch stays
        writable: clearing it is the sanctioned, tracked override for
        posting past mismatches.
        """
        if not self.env.su:
            self._eh_assert_company_closure()
            target_company_ids = []
            for batch in self:
                closure = (
                    vals.get('company_a_id', batch.company_a_id.id),
                    vals.get('company_b_id', batch.company_b_id.id),
                    vals.get(
                        'elimination_company_id',
                        batch.elimination_company_id.id,
                    ),
                )
                self._eh_assert_company_ids_allowed(closure)
                target_company_ids.extend(closure)
            self._eh_lock_for_transition(target_company_ids)
            self.invalidate_recordset()
            self._eh_assert_company_closure()
        frozen = [f for f in _FROZEN_AFTER_COMPUTE if f in vals]
        settled = self.filtered(lambda b: b.state != 'draft')
        if frozen and settled:
            raise UserError(_(
                "The pair and period of a computed or posted elimination "
                "batch are frozen (%(fields)s); they define the matched "
                "pairs. Reset the batch to draft to change them.",
                fields=', '.join(frozen)))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_assert_company_closure()
            self._eh_lock_for_transition()
            self.invalidate_recordset()
            self._eh_assert_company_closure()
        evidenced = self.filtered(
            lambda b: b.state == 'posted' or b.move_id or b.move_history_ids)
        if evidenced:
            raise UserError(_(
                "An elimination batch with journal-entry evidence cannot be "
                "deleted. Reverse/reset it if necessary and retain the batch "
                "as the provenance record.",
            ))
        return super().unlink()

    @api.depends('line_ids', 'mismatch_ids', 'unrealised_line_ids')
    def _compute_counts(self):
        for batch in self:
            batch.line_count = len(batch.line_ids)
            batch.mismatch_count = len(batch.mismatch_ids)
            batch.unrealised_count = len(batch.unrealised_line_ids)

    @api.depends('line_ids.kind', 'line_ids.debit', 'line_ids.credit',
                 'unrealised_line_ids.unrealised_amount')
    def _compute_totals(self):
        for batch in self:
            totals = {'receivable': 0.0, 'payable': 0.0,
                      'revenue': 0.0, 'expense': 0.0}
            for line in batch.line_ids:
                # Credits, refunds and allocations across several invoices
                # can produce offsetting legs of one kind.  Published totals
                # are the net eliminated balance/flow, not the gross churn of
                # those legs.
                totals[line.kind] += line.debit - line.credit
            batch.receivable_total = abs(totals['receivable'])
            batch.payable_total = abs(totals['payable'])
            batch.revenue_total = abs(totals['revenue'])
            batch.expense_total = abs(totals['expense'])
            batch.unrealised_total = sum(
                batch.unrealised_line_ids.mapped('unrealised_amount'))

    # ------------------------------------------------------------------
    # compute: pair matching engine
    # ------------------------------------------------------------------

    def _eh_source_move_domain(self):
        self.ensure_one()
        return [
            ('state', 'in', ('posted', 'cancel')),
            ('company_id', 'in', (self.company_a_id | self.company_b_id).ids),
            ('move_type', 'in', list(_DIRECTION_MIRROR)),
            ('eh_intercompany_origin_id', '=', False),
            '|',
            ('date', '<=', self.period_to),
            ('eh_intercompany_mirror_id.date', '<=', self.period_to),
        ]

    def _eh_validate_source_access(self):
        """Prove the actor can read every ledger/input row before elevation."""
        if self.env.su:
            return self
        Move = self.env['account.move']
        MoveLine = self.env['account.move.line']
        Partial = self.env['account.partial.reconcile']
        Move._eh_check_access('read')
        MoveLine._eh_check_access('read')
        Partial._eh_check_access('read')
        self.env['product.product']._eh_check_access('read')
        for batch in self:
            visible = Move.search(batch._eh_source_move_domain())
            privileged = Move.sudo().search(batch._eh_source_move_domain())
            if set(visible.ids) != set(privileged.ids):
                raise AccessError(_(
                    "The elimination source set contains ledger rows the "
                    "caller is not authorised to read."
                ))
            sources = privileged | privileged.mapped(
                'eh_intercompany_mirror_id')
            Move.browse(sources.ids)._eh_check_access('read')
            lines = MoveLine.browse(sources.mapped('line_ids').ids)
            lines._eh_check_access('read')
            partials = (
                lines.mapped('matched_debit_ids')
                | lines.mapped('matched_credit_ids')
            )
            Partial.browse(partials.ids)._eh_check_access('read')
            products = sources.mapped('invoice_line_ids.product_id')
            self.env['product.product'].browse(products.ids)._eh_check_access(
                'read')
        return self

    def _eh_validate_output_references(self):
        """Validate persisted engine evidence as the actor before posting."""
        if self.env.su:
            return self
        for batch in self:
            batch.line_ids._eh_check_access('read')
            batch.mismatch_ids._eh_check_access('read')
            batch.unrealised_line_ids._eh_check_access('read')
            moves = (
                batch.line_ids.mapped('source_move_id')
                | batch.line_ids.mapped('mirror_move_id')
                | batch.mismatch_ids.mapped('source_move_id')
                | batch.mismatch_ids.mapped('mirror_move_id')
                | batch.unrealised_line_ids.mapped('source_move_id')
                | batch.unrealised_line_ids.mapped('mirror_move_id')
            )
            moves._eh_check_access('read')
            batch.line_ids.mapped('account_id')._eh_check_access('read')
            batch.unrealised_line_ids.mapped('product_id')._eh_check_access(
                'read')
            batch.unrealised_line_ids.mapped(
                'source_move_line_id')._eh_check_access('read')
            allowed = {batch.company_a_id.id, batch.company_b_id.id}
            if moves.filtered(lambda move: move.company_id.id not in allowed):
                raise AccessError(_(
                    "An elimination evidence row points outside the authorised "
                    "company pair."
                ))
        return self

    def action_compute(self):
        """Match the posted IC move pairs of this company pair for the
        period and rebuild the elimination legs, mismatch rows and
        unrealised-profit lines. Idempotent: rerunning replaces every
        engine-built record, so two computes leave exactly one set.
        """
        caller = self
        caller._eh_assert_company_closure()
        caller._eh_assert_elimination_workspace()
        caller._eh_validate_source_access()
        caller._eh_lock_for_transition()
        caller._eh_assert_company_closure()
        caller._eh_assert_elimination_workspace()
        caller._eh_validate_source_access()
        ledger_versions = {
            batch.id: batch._eh_ledger_versions() for batch in caller
        }
        self = caller._eh_workflow_action()
        engine_ctx = {IC_ENGINE_CTX: True}
        for batch in self:
            if batch.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available on a draft or computed "
                    "batch. Reset the batch to draft first.",
                ))
            batch.line_ids.sudo().with_context(**engine_ctx).unlink()
            batch.mismatch_ids.sudo().with_context(**engine_ctx).unlink()
            prior_evidence = {}
            for old_line in batch.unrealised_line_ids.filtered(
                    'source_move_line_id'):
                if not old_line.inventory_evidence_reviewed:
                    continue
                prior_evidence[old_line.source_move_line_id.id] = {
                    'derived': (
                        old_line.quantity,
                        old_line.unit_cost,
                        old_line.price_subtotal,
                        old_line.margin,
                    ),
                    'remaining_fraction': old_line.remaining_fraction,
                    'inventory_evidence_date':
                        old_line.inventory_evidence_date,
                    'inventory_evidence_reference':
                        old_line.inventory_evidence_reference,
                    'inventory_evidence_reviewed': True,
                    'notes': old_line.notes,
                }
            batch.unrealised_line_ids.sudo().with_context(
                **engine_ctx,
            ).unlink()
            pairs, mismatch_vals = batch._eh_match_pairs()
            line_vals, amount_mismatches = (
                batch._eh_build_elimination_line_vals(pairs)
            )
            mismatch_vals.extend(amount_mismatches)
            up_vals = batch._eh_build_unrealised_vals(
                pairs, prior_evidence=prior_evidence)
            if batch._eh_ledger_versions() != ledger_versions[batch.id]:
                raise UserError(_(
                    "Member ledgers changed while batch %(batch)s was being "
                    "computed. No evidence was replaced; rerun Compute on "
                    "the new committed snapshot.",
                    batch=batch.display_name,
                ))
            if line_vals:
                self.env['eh.ic.elimination.batch.line'].sudo().with_context(
                    **engine_ctx,
                ).create(line_vals)
            if mismatch_vals:
                self.env['eh.ic.elimination.mismatch'].sudo().with_context(
                    **engine_ctx,
                ).create(mismatch_vals)
            if up_vals:
                self.env['eh.ic.unrealised.line'].sudo().with_context(
                    **engine_ctx,
                ).create(up_vals)
            batch.sudo().write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
            batch.message_post(body=_(
                "Elimination computed: %(pairs)s pair(s) matched, "
                "%(legs)s leg(s) built, %(mismatch)s mismatch(es) "
                "flagged.",
                pairs=len(pairs), legs=len(line_vals),
                mismatch=len(mismatch_vals),
            ))
        return True

    @api.model
    def _eh_move_dest_company(self, move):
        """Destination company a source move mirrors into, resolved the
        same way the mirroring trigger resolves it (represented company
        first, legacy commercial company fallback)."""
        commercial = move.partner_id.commercial_partner_id
        if not commercial:
            return self.env['res.company']
        return (commercial.eh_represented_company_id
                or commercial.company_id)

    def _eh_match_pairs(self):
        """Return (pairs, mismatch_vals) for this batch.

        Pair selection uses accounting dates through ``period_to``.  Older
        invoices remain relevant while their receivable/payable is open at
        the cutoff; revenue and expense are filtered to the reporting period
        later.  Both moves must be posted into their ledgers by the cutoff.
        """
        self.ensure_one()
        Move = self.env['account.move'].sudo()
        pair_companies = self.company_a_id | self.company_b_id
        sources = Move.search(self._eh_source_move_domain(), order='date, id')
        locked_moves = sources | sources.mapped('eh_intercompany_mirror_id')
        if locked_moves:
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(locked_moves.ids)),),
            )
            locked_moves.invalidate_recordset()
            line_ids = tuple(sorted(locked_moves.mapped('line_ids').ids))
            if line_ids:
                # Reconciliation changes residuals without changing the
                # invoice itself.  Lock the journal items so compute observes
                # one cutoff-consistent settlement snapshot.
                self.env.cr.execute(
                    "SELECT id FROM account_move_line WHERE id IN %s "
                    "ORDER BY id FOR UPDATE",
                    (line_ids,),
                )
                locked_moves.mapped('line_ids').invalidate_recordset()
        pairs, mismatch_vals = [], []
        for src in sources:
            other = pair_companies - src.company_id
            mirror = src.eh_intercompany_mirror_id
            if mirror:
                if mirror.company_id != other:
                    continue  # pair belongs to a different company pair
            else:
                dest = self._eh_move_dest_company(src)
                if dest != other:
                    continue  # not an IC document towards the pair company
                source_amount = self._eh_one_sided_amount(src)
                if float_is_zero(
                    source_amount,
                    precision_rounding=self.currency_id.rounding or 0.01,
                ):
                    # A historical source with no current outstanding balance
                    # and no flow in this reporting period cannot distort this
                    # batch.  Do not make every future close inherit a stale,
                    # zero-impact exception forever.
                    continue
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'kind': 'no_mirror',
                    'source_amount': source_amount,
                    'reason': _(
                        "No mirror exists in %(company)s for posted "
                        "source %(name)s as of %(cutoff)s.",
                        company=other.display_name,
                        name=src.display_name,
                        cutoff=self.period_to),
                })
                continue
            src_at_cutoff = self._eh_posted_at_cutoff(src)
            mirror_at_cutoff = self._eh_posted_at_cutoff(mirror)
            if src.state == 'cancel' and mirror_at_cutoff:
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'kind': 'orphan_mirror',
                    'source_amount': 0.0,
                    'mirror_amount': self._eh_one_sided_amount(mirror),
                    'reason': _(
                        "Source %(source)s is cancelled while posted mirror "
                        "%(mirror)s remains in the ledger at reporting cutoff "
                        "%(cutoff)s. Reverse or restore the pair before "
                        "elimination.",
                        source=src.display_name,
                        mirror=mirror.display_name,
                        cutoff=self.period_to,
                    ),
                })
                continue
            if not src_at_cutoff or not mirror_at_cutoff:
                source_amount = (
                    self._eh_one_sided_amount(src) if src_at_cutoff else 0.0
                )
                mirror_amount = (
                    self._eh_one_sided_amount(mirror)
                    if mirror_at_cutoff else 0.0
                )
                if float_is_zero(
                    source_amount, precision_rounding=(
                        self.currency_id.rounding or 0.01
                    ),
                ) and float_is_zero(
                    mirror_amount, precision_rounding=(
                        self.currency_id.rounding or 0.01
                    ),
                ):
                    continue
                mismatch_vals.append({
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'kind': 'mirror_draft',
                    'source_amount': source_amount,
                    'mirror_amount': mirror_amount,
                    'reason': _(
                        "Both sides are not posted in their ledgers by "
                        "reporting cutoff %(cutoff)s (source %(source)s; "
                        "mirror %(mirror)s).",
                        cutoff=self.period_to,
                        source=src.display_name,
                        mirror=mirror.display_name),
                })
                continue
            pairs.append({
                'source': src,
                'mirror': mirror,
            })
        return pairs, mismatch_vals

    def _eh_posted_at_cutoff(self, move):
        self.ensure_one()
        return bool(
            move
            and move.state == 'posted'
            and move.date
            and move.date <= self.period_to
        )

    def _eh_in_flow_period(self, move):
        self.ensure_one()
        return bool(
            self._eh_posted_at_cutoff(move)
            and self.period_from <= move.date <= self.period_to
        )

    @staticmethod
    def _eh_bucket_balances(move, account_types):
        """Sum debit-minus-credit per account for the move's lines whose
        account type is in account_types."""
        out = {}
        for line in move.line_ids:
            if line.account_id.account_type in account_types:
                out[line.account_id] = (
                    out.get(line.account_id, 0.0)
                    + line.balance)
        return out

    @staticmethod
    def _eh_residual_at_date(line, cutoff):
        """Signed company-currency residual of ``line`` at ``cutoff``.

        Stored ``amount_residual`` describes today.  Rebuild from the posted
        journal-item balance and only partial reconciliations whose accounting
        ``max_date`` is on/before the cutoff.  ``max_date`` is the stable
        Odoo 16-19 contract used by aged receivable/payable.
        """
        residual = line.balance
        # ``line`` is the debit side of these partials.
        for partial in line.matched_credit_ids:
            if partial.max_date and partial.max_date <= cutoff:
                residual -= partial.amount
        # ``line`` is the credit side of these partials.
        for partial in line.matched_debit_ids:
            if partial.max_date and partial.max_date <= cutoff:
                residual += partial.amount
        return residual

    def _eh_bucket_residuals(self, move, account_types):
        """Outstanding company-currency balance per account at period end."""
        self.ensure_one()
        out = {}
        if not self._eh_posted_at_cutoff(move):
            return out
        for line in move.line_ids:
            if line.account_id.account_type not in account_types:
                continue
            residual = self._eh_residual_at_date(line, self.period_to)
            out[line.account_id] = out.get(line.account_id, 0.0) + residual
        return out

    def _eh_translate_to_elim(self, amount, from_company, rate_type):
        """Translate a member's functional-currency amount into the
        elimination company's presentation currency (IAS 21.39).

        rate_type 'closing' uses the spot rate at period_to (balance-sheet
        items); 'average' approximates the period-average rate as the mean of
        the opening and closing conversions (IAS 21.40 permits an average
        that approximates the actual rates). Identity when the member already
        reports in the elimination currency, so a single-currency group
        produces byte-identical legs to the pre-FX behaviour.
        """
        self.ensure_one()
        elim = self.elimination_company_id
        elim_currency = elim.currency_id
        src_currency = from_company.currency_id
        if (not amount or not src_currency or not elim_currency
                or src_currency == elim_currency):
            return amount
        if rate_type == 'average':
            at_open = src_currency._convert(
                amount, elim_currency, elim, self.period_from)
            at_close = src_currency._convert(
                amount, elim_currency, elim, self.period_to)
            return (at_open + at_close) / 2.0
        return src_currency._convert(
            amount, elim_currency, elim, self.period_to)

    def _eh_pair_moves(self, pair):
        src, mirror = pair['source'], pair['mirror']
        out_move = (
            src if src.move_type in ('out_invoice', 'out_refund')
            else mirror
        )
        return out_move, (mirror if out_move is src else src)

    def _eh_pair_entries(self, pair, balance_sheet):
        """Translated account entries for one reciprocal document pair."""
        self.ensure_one()
        src, mirror = pair['source'], pair['mirror']
        out_move, in_move = self._eh_pair_moves(pair)
        if balance_sheet:
            specs = (
                ('receivable', out_move, ('asset_receivable',), 'closing'),
                ('payable', in_move, ('liability_payable',), 'closing'),
            )
        else:
            specs = (
                ('revenue', out_move, _INCOME_TYPES, 'average'),
                ('expense', in_move, _EXPENSE_TYPES, 'average'),
            )
        entries = {kind: [] for kind, _move, _types, _rate in specs}
        for kind, move, account_types, rate_type in specs:
            if not balance_sheet and not self._eh_in_flow_period(move):
                continue
            raw = (
                self._eh_bucket_residuals(move, account_types)
                if balance_sheet
                else self._eh_bucket_balances(move, account_types)
            )
            for account, amount in raw.items():
                translated = self._eh_translate_to_elim(
                    amount, move.company_id, rate_type,
                )
                entries[kind].append({
                    'account': account,
                    'amount': translated,
                    'source': src,
                    'mirror': mirror,
                })
        return entries

    def _eh_one_sided_amount(self, move):
        """Representative cutoff/flow magnitude in elimination currency."""
        self.ensure_one()
        if not self._eh_posted_at_cutoff(move):
            return 0.0
        is_out = move.move_type in ('out_invoice', 'out_refund')
        account_types = ('asset_receivable',) if is_out else (
            'liability_payable',
        )
        residual = sum(self._eh_bucket_residuals(
            move, account_types,
        ).values())
        residual = self._eh_translate_to_elim(
            residual, move.company_id, 'closing',
        )
        flow = 0.0
        if self._eh_in_flow_period(move):
            flow_types = _INCOME_TYPES if is_out else _EXPENSE_TYPES
            flow = self._eh_translate_to_elim(
                sum(self._eh_bucket_balances(move, flow_types).values()),
                move.company_id,
                'average',
            )
        return self.currency_id.round(max(abs(residual), abs(flow)))

    def _eh_reciprocal_plan(self, left_entries, right_entries):
        """Return signed totals, reciprocal flag and safe common magnitude."""
        self.ensure_one()
        left = self.currency_id.round(sum(
            entry['amount'] for entry in left_entries
        ))
        right = self.currency_id.round(sum(
            entry['amount'] for entry in right_entries
        ))
        rounding = self.currency_id.rounding or 0.01
        left_zero = float_is_zero(left, precision_rounding=rounding)
        right_zero = float_is_zero(right, precision_rounding=rounding)
        reciprocal_sign = (left_zero and right_zero) or left * right < 0.0
        matched = reciprocal_sign and abs(left + right) <= max(
            rounding, _PAIR_TOLERANCE,
        )
        common = (
            min(abs(left), abs(right))
            if reciprocal_sign and not left_zero and not right_zero
            else 0.0
        )
        return left, right, matched, self.currency_id.round(common)

    def _eh_scaled_line_vals(self, kind, entries, target_magnitude):
        """Negate entries, scaling only when reciprocal totals mismatch."""
        self.ensure_one()
        currency = self.currency_id
        rounding = currency.rounding or 0.01
        total = currency.round(sum(entry['amount'] for entry in entries))
        if float_is_zero(total, precision_rounding=rounding):
            return []
        target_magnitude = currency.round(target_magnitude)
        if float_is_zero(target_magnitude, precision_rounding=rounding):
            return []
        scale = target_magnitude / abs(total)
        prepared = []
        for entry in entries:
            elimination = currency.round(-entry['amount'] * scale)
            prepared.append([entry, elimination])
        target = currency.round(
            -target_magnitude if total > 0.0 else target_magnitude
        )
        correction = currency.round(
            target - sum(item[1] for item in prepared)
        )
        if prepared and not float_is_zero(
                correction, precision_rounding=rounding):
            biggest = max(prepared, key=lambda item: abs(item[1]))
            biggest[1] = currency.round(biggest[1] + correction)
        vals = []
        for entry, elimination in prepared:
            if float_is_zero(elimination, precision_rounding=rounding):
                continue
            vals.append({
                'batch_id': self.id,
                'source_move_id': entry['source'].id,
                'mirror_move_id': entry['mirror'].id,
                'kind': kind,
                'account_id': entry['account'].id,
                'debit': elimination if elimination > 0.0 else 0.0,
                'credit': -elimination if elimination < 0.0 else 0.0,
            })
        return vals

    def _eh_amount_mismatch_vals(self, pair, left, right, scope):
        self.ensure_one()
        src = pair.get('source') if pair else self.env['account.move']
        mirror = pair.get('mirror') if pair else self.env['account.move']
        return {
            'batch_id': self.id,
            'source_move_id': src.id,
            'mirror_move_id': mirror.id,
            'kind': 'amount',
            'source_amount': self.currency_id.round(abs(left)),
            'mirror_amount': self.currency_id.round(abs(right)),
            'reason': _(
                "%(scope)s mismatch in %(currency)s: reciprocal sides "
                "%(left).2f and %(right).2f. Only the common reciprocal "
                "amount is eliminated; the difference remains visible.",
                scope=scope,
                currency=self.currency_id.name,
                left=left,
                right=right,
            ),
        }

    def _eh_build_elimination_line_vals(self, pairs):
        """Build period flows plus aggregate as-of outstanding balances.

        AR/AP is matched at company-pair level, not invoice-pair level.  Cash
        allocations and credit-note reconciliations can legitimately land on
        different individual documents in the two ledgers while reciprocal
        totals still agree.  Aggregation avoids false mismatches and preserves
        every source account/document on the evidence lines.
        """
        self.ensure_one()
        vals = []
        mismatches = []
        balance_entries = {'receivable': [], 'payable': []}
        for pair in pairs:
            pair_balance = self._eh_pair_entries(pair, balance_sheet=True)
            balance_entries['receivable'].extend(pair_balance['receivable'])
            balance_entries['payable'].extend(pair_balance['payable'])

            flow_entries = self._eh_pair_entries(pair, balance_sheet=False)
            revenue, expense, matched, common = self._eh_reciprocal_plan(
                flow_entries['revenue'], flow_entries['expense'],
            )
            target = common
            vals.extend(self._eh_scaled_line_vals(
                'revenue', flow_entries['revenue'], target,
            ))
            vals.extend(self._eh_scaled_line_vals(
                'expense', flow_entries['expense'], target,
            ))
            if not matched:
                mismatches.append(self._eh_amount_mismatch_vals(
                    pair, revenue, expense, _("Period flow"),
                ))

        receivable, payable, matched, common = self._eh_reciprocal_plan(
            balance_entries['receivable'], balance_entries['payable'],
        )
        target = common
        vals.extend(self._eh_scaled_line_vals(
            'receivable', balance_entries['receivable'], target,
        ))
        vals.extend(self._eh_scaled_line_vals(
            'payable', balance_entries['payable'], target,
        ))
        if not matched:
            balance_reason = self._eh_amount_mismatch_vals(
                False, receivable, payable, _("Period-end outstanding balance"),
            )
            # One malformed pair normally creates both a flow and a balance
            # mismatch.  Keep one actionable row while retaining both facts.
            if mismatches:
                mismatches[0]['source_amount'] = balance_reason['source_amount']
                mismatches[0]['mirror_amount'] = balance_reason['mirror_amount']
                mismatches[0]['reason'] = "%s %s" % (
                    mismatches[0]['reason'], balance_reason['reason'],
                )
            else:
                mismatches.append(balance_reason)
        return vals, mismatches

    # ------------------------------------------------------------------
    # unrealised profit from source documents
    # ------------------------------------------------------------------

    @api.model
    def _eh_product_storable(self, product):
        """Cross-series storable test: is_storable from Odoo 17.2+,
        type == 'product' before."""
        if 'is_storable' in product._fields:
            return bool(product.is_storable)
        return product.type == 'product'

    def _eh_build_unrealised_vals(self, pairs, prior_evidence=None):
        """Build the unrealised-profit line vals from the matched pairs.

        For every pair whose OUT side is a customer invoice, one line
        per product invoice line: margin uses source revenue plus immutable
        selling-company cost captured on exact mirror line when source
        posted. Later product ``standard_price`` changes cannot rewrite a
        closed-period margin.
        The remaining fraction starts at zero and must be approved against
        dated, source-line-specific inventory evidence. Aggregate current
        stock quants cannot prove which supplier/invoice produced units still
        on hand at period_to and therefore are never used. Refund pairs are
        out of scope for unrealised profit. A recompute retains prior approval
        only when the exact source line and all derived inputs are unchanged.
        """
        self.ensure_one()
        currency = self.currency_id
        prior_evidence = prior_evidence or {}
        vals = []
        for pair in pairs:
            src, mirror = pair['source'], pair['mirror']
            out_move = (
                src if src.move_type in ('out_invoice', 'out_refund')
                else mirror)
            if out_move.move_type != 'out_invoice':
                continue
            for line in out_move.invoice_line_ids:
                product = line.product_id
                if not product or not line.quantity:
                    continue
                if out_move.eh_intercompany_origin_id:
                    mirror_lines = line
                else:
                    mirror_lines = (src | mirror).filtered(
                        lambda move: move != out_move
                    ).invoice_line_ids.filtered(
                        lambda candidate:
                            candidate.eh_ic_source_move_line_id == line
                    )
                if (
                    len(mirror_lines) != 1
                    or not mirror_lines.eh_ic_source_cost_date
                    or not mirror_lines.eh_ic_source_cost_company_id
                ):
                    raise UserError(_(
                        "Inter-company source line %(line)s has no unique "
                        "immutable sale-date cost snapshot on its mirror. "
                        "Do not estimate historical margin from current "
                        "product cost; repair/recreate mirror evidence first.",
                        line=line.display_name,
                    ))
                mirror_line = mirror_lines
                seller = out_move.company_id
                price_subtotal = self._eh_translate_to_elim(
                    abs(line.balance), seller, 'average',
                )
                total_cost = self._eh_translate_to_elim(
                    mirror_line.eh_ic_source_cost_amount,
                    mirror_line.eh_ic_source_cost_company_id,
                    'average',
                )
                unit_cost = currency.round(total_cost / line.quantity)
                price_subtotal = currency.round(price_subtotal)
                margin = currency.round(price_subtotal - total_cost)
                value = {
                    'batch_id': self.id,
                    'source_move_id': src.id,
                    'mirror_move_id': mirror.id,
                    'source_move_line_id': line.id,
                    'product_id': product.id,
                    'quantity': line.quantity,
                    'unit_cost': unit_cost,
                    'price_subtotal': price_subtotal,
                    'margin': margin,
                    'remaining_fraction': 0.0,
                    'fraction_source': 'manual',
                }
                previous = prior_evidence.get(line.id)
                if (
                    previous
                    and previous['derived'] == (
                        line.quantity, unit_cost,
                        line.price_subtotal, margin,
                    )
                    and previous['inventory_evidence_date'] == self.period_to
                ):
                    value.update({
                        key: previous[key] for key in (
                            'remaining_fraction',
                            'inventory_evidence_date',
                            'inventory_evidence_reference',
                            'inventory_evidence_reviewed',
                            'notes',
                        )
                    })
                vals.append(value)
        return vals

    # ------------------------------------------------------------------
    # post / reset
    # ------------------------------------------------------------------

    def _eh_get_elimination_journal(self):
        """Return the dedicated elimination journal in the elimination
        company, auto-creating it on first use and storing it on that
        company's inter-company configuration (when one exists), so
        every batch books into the same journal from then on.
        """
        self.ensure_one()
        company = self.elimination_company_id
        config = self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', company.id)], limit=1)
        if config and config.elimination_journal_id \
                and config.elimination_journal_id.company_id == company:
            return config.elimination_journal_id
        Journal = self.env['account.journal'].sudo()
        journal = Journal.search([
            ('company_id', '=', company.id),
            ('code', '=', 'ICEL'),
        ], limit=1)
        if not journal:
            journal = Journal.create({
                'name': "IC Eliminations",
                'code': 'ICEL',
                'type': 'general',
                'company_id': company.id,
            })
        if config and not config.elimination_journal_id:
            config.elimination_journal_id = journal.id
        journal.sudo(False)._eh_check_access('read')
        return journal

    def _eh_resolve_elim_account(self, account, cache):
        """Map a source-company account into the elimination company's
        chart by code (the consolidation module's mapping convention).
        Raises a clear error naming the account when no counterpart
        exists, so a missing mapping fails loudly rather than dropping
        a leg.
        """
        self.ensure_one()
        company = self.elimination_company_id
        if account.id in cache:
            return cache[account.id]
        company_field = _acc_company_field(self.env)
        if company_field == 'company_ids':
            owns = company in account.company_ids
        else:
            owns = account.company_id == company
        if owns:
            cache[account.id] = account
            return account
        # account.account.code is company-dependent on Odoo 19, so read
        # it in the source account's own company context, then search in
        # the elimination company's context.
        if company_field == 'company_ids':
            source_company = account.company_ids[:1] or company
        else:
            source_company = account.company_id or company
        code = account.with_company(source_company).code
        Account = self.env['account.account'].sudo().with_company(company)
        resolved = Account.search([
            ('code', '=', code),
            (company_field, 'in', company.ids),
        ], limit=1)
        if not resolved:
            raise UserError(_(
                "Account %(code)s %(name)s is not present in the "
                "elimination company %(company)s chart of accounts. Add "
                "it (by code) before posting the elimination batch.",
                code=code or '?',
                name=account.name or '',
                company=company.display_name,
            ))
        resolved.sudo(False)._eh_check_access('read')
        cache[account.id] = resolved
        return resolved

    def _eh_same_pair_domain(self):
        self.ensure_one()
        return [
            ('id', '!=', self.id),
            ('elimination_company_id', '=', self.elimination_company_id.id),
            '|',
            '&',
            ('company_a_id', '=', self.company_a_id.id),
            ('company_b_id', '=', self.company_b_id.id),
            '&',
            ('company_a_id', '=', self.company_b_id.id),
            ('company_b_id', '=', self.company_a_id.id),
        ]

    def _eh_assert_no_posted_successor(self):
        """Delta chains can only post/reset newest-to-oldest."""
        self.ensure_one()
        successor = self.search(
            self._eh_same_pair_domain() + [
                ('state', '=', 'posted'),
                ('period_to', '>=', self.period_to),
            ],
            order='period_to, id',
            limit=1,
        )
        if successor:
            raise UserError(_(
                "Batch %(name)s cannot be posted or reset while later/same-"
                "cutoff batch %(successor)s remains posted. Reverse/reset "
                "newest batches first so the balance-delta chain stays "
                "auditable.",
                name=self.display_name,
                successor=successor.display_name,
            ))

    def _eh_assert_no_period_overlap(self):
        """Prevent two active flow batches from eliminating same dates."""
        self.ensure_one()
        overlap = self.search(
            self._eh_same_pair_domain() + [
                ('state', '=', 'posted'),
                ('period_from', '<=', self.period_to),
                ('period_to', '>=', self.period_from),
            ],
            order='period_to, id',
            limit=1,
        )
        if overlap:
            raise UserError(_(
                "Batch %(name)s overlaps posted batch %(overlap)s for the "
                "same company pair. Flow eliminations require disjoint "
                "periods; reset the conflicting batch or correct this "
                "draft period.",
                name=self.display_name,
                overlap=overlap.display_name,
            ))

    def _eh_prior_balance_eliminations(self):
        """Actual active AR/AP elimination balance before this batch.

        Read posted journal evidence rather than prior target lines.  This
        makes the first corrected batch self-heal cumulative gross postings
        created by older module versions: its delta brings the persistent GL
        to the newly computed cutoff target.
        """
        self.ensure_one()
        prior_batches = self.search(
            self._eh_same_pair_domain() + [
                ('state', '=', 'posted'),
                ('period_to', '<', self.period_to),
            ],
            order='period_to, id',
        )
        balances = {}
        for prior in prior_batches:
            move = prior.move_id
            if not move:
                # A no-change period has batch evidence but no journal delta.
                continue
            if (
                move.state != 'posted'
                or move.company_id != self.elimination_company_id
                or not move.eh_sealed
                or move not in prior.move_history_ids
            ):
                raise UserError(_(
                    "Prior elimination batch %(batch)s failed its sealed "
                    "journal provenance check; current balance delta was not "
                    "posted.",
                    batch=prior.display_name,
                ))
            for line in move.line_ids:
                if line.account_id.account_type not in (
                    'asset_receivable', 'liability_payable',
                ):
                    continue
                balances[line.account_id.id] = (
                    balances.get(line.account_id.id, 0.0) + line.balance
                )
        return balances

    def _eh_prior_unrealised_inventory_balances(self):
        """Active prior point-in-time inventory eliminations by account."""
        self.ensure_one()
        prior_batches = self.search(
            self._eh_same_pair_domain() + [
                ('state', '=', 'posted'),
                ('period_to', '<', self.period_to),
            ],
            order='period_to, id',
        )
        balances = {}
        for prior in prior_batches:
            move = prior.move_id
            if not move:
                continue
            if (
                move.state != 'posted'
                or move.company_id != self.elimination_company_id
                or not move.eh_sealed
                or move not in prior.move_history_ids
            ):
                raise UserError(_(
                    "Prior elimination batch %(batch)s failed its sealed "
                    "journal provenance check; unrealised-profit delta was "
                    "not posted.",
                    batch=prior.display_name,
                ))
            for line in move.line_ids.filtered(
                lambda item:
                    item.eh_ic_elimination_role == 'unrealised_inventory'
            ):
                balances[line.account_id.id] = (
                    balances.get(line.account_id.id, 0.0) + line.balance
                )
        return balances

    def _eh_unrealised_accounts(self):
        """Return explicit elimination inventory and COGS accounts."""
        self.ensure_one()
        config = self.env['eh.intercompany.config'].sudo().search([
            ('company_id', '=', self.elimination_company_id.id),
        ], limit=1)
        inventory = config.unrealised_inventory_account_id
        cogs = config.unrealised_cogs_account_id
        if not inventory or not cogs:
            raise UserError(_(
                "Configure Unrealised-profit inventory and cost-of-sales "
                "accounts on elimination-company configuration %(company)s "
                "before posting material ending-inventory profit.",
                company=self.elimination_company_id.display_name,
            ))
        inventory.sudo(False)._eh_check_access('read')
        cogs.sudo(False)._eh_check_access('read')
        return inventory, cogs

    def _eh_assert_unrealised_evidence_ready(self):
        """Require source-specific, cutoff-dated evidence before consumption."""
        for batch in self:
            material = batch.unrealised_line_ids.filtered(
                lambda line: not float_is_zero(
                    line.margin,
                    precision_rounding=batch.currency_id.rounding,
                )
            )
            invalid = material.filtered(lambda line: (
                not line.source_move_line_id
                or not line.inventory_evidence_reviewed
                or line.inventory_evidence_date != batch.period_to
                or not (line.inventory_evidence_reference or '').strip()
            ))
            if invalid:
                raise UserError(_(
                    "Batch %(batch)s has %(count)s unrealised-profit line(s) "
                    "without reviewed, period-end, source-invoice-specific "
                    "inventory evidence. Current stock on hand is not valid "
                    "evidence because it can include third-party purchases "
                    "and cannot reconstruct the cutoff.",
                    batch=batch.display_name,
                    count=len(invalid),
                ))
        return True

    def action_post(self):
        """Book the elimination journal entry (manager-gated).

        Refused while mismatches exist and block_on_mismatch is set. The
        move is booked in the elimination company's dedicated journal,
        dated on period_to, sealed (eh_sealed) so its figures cannot be
        edited or unposted outside the sanctioned reset path. Each batch
        leg becomes one journal line; a residual of at most one rounding
        unit (possible when a matched pair's two sides differ by the
        tolerated cent) is absorbed into the largest leg so the move
        always balances.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can post an elimination "
                "batch.",
            ))
        caller = self
        caller._eh_assert_company_closure()
        caller._eh_assert_elimination_workspace()
        caller._eh_validate_output_references()
        caller._eh_lock_for_transition()
        caller._eh_assert_company_closure()
        caller._eh_assert_elimination_workspace()
        caller._eh_validate_output_references()
        self = caller._eh_workflow_action()
        for batch in self:
            batch._eh_assert_no_posted_successor()
            batch._eh_assert_no_period_overlap()
            if batch.state != 'computed':
                raise UserError(_(
                    "Only a computed elimination batch can be posted.",
                ))
            batch._eh_assert_unrealised_evidence_ready()
            if batch.mismatch_ids and batch.block_on_mismatch:
                raise UserError(_(
                    "Batch %(name)s carries %(count)s mismatched or "
                    "unmatched pair(s); resolve them (or clear Block on "
                    "Mismatch to post the matched eliminations anyway).",
                    name=batch.display_name,
                    count=len(batch.mismatch_ids),
                ))
            if not batch.line_ids:
                raise UserError(_(
                    "Batch %(name)s has no elimination lines; nothing "
                    "to post.",
                    name=batch.display_name,
                ))
            move = batch._eh_build_elimination_move()
            if move:
                move.action_post()
            batch_vals = {
                'state': 'posted',
                'move_id': move.id if move else False,
                'reversal_move_id': False,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            }
            if move:
                batch_vals['move_history_ids'] = [(4, move.id)]
            batch.sudo().write(batch_vals)
            if move:
                batch.message_post(body=_(
                    "Elimination move %(move)s posted in %(company)s by "
                    "%(user)s.",
                    move=move.name,
                    company=batch.elimination_company_id.display_name,
                    user=self.env.user.display_name,
                ))
            else:
                batch.message_post(body=_(
                    "Elimination batch posted with no journal delta: the "
                    "prior active balance already equals this cutoff target "
                    "and the period carries no flow elimination."
                ))
        return True

    def _eh_build_elimination_move(self):
        self.ensure_one()
        company = self.elimination_company_id
        currency = company.currency_id
        rounding = currency.rounding or 0.01
        journal = self._eh_get_elimination_journal()
        cache = {}
        line_vals = []
        balance_targets = {}
        for line in self.line_ids:
            account = self._eh_resolve_elim_account(line.account_id, cache)
            amount = currency.round(line.debit - line.credit)
            if float_is_zero(amount, precision_rounding=rounding):
                continue
            if line.kind in ('receivable', 'payable'):
                balance_targets[account.id] = currency.round(
                    balance_targets.get(account.id, 0.0) + amount
                )
                continue
            line_vals.append({
                'account_id': account.id,
                'name': "%s: %s" % (
                    dict(line._fields['kind'].selection).get(
                        line.kind, line.kind),
                    line.source_move_id.name or '/'),
                'debit': amount if amount > 0.0 else 0.0,
                'credit': -amount if amount < 0.0 else 0.0,
            })
        prior_balances = self._eh_prior_balance_eliminations()
        for account_id in sorted(
                set(balance_targets) | set(prior_balances)):
            delta = currency.round(
                balance_targets.get(account_id, 0.0)
                - prior_balances.get(account_id, 0.0)
            )
            if float_is_zero(delta, precision_rounding=rounding):
                continue
            line_vals.append({
                'account_id': account_id,
                'name': _("Period-end IC outstanding balance delta"),
                'debit': delta if delta > 0.0 else 0.0,
                'credit': -delta if delta < 0.0 else 0.0,
            })
        prior_unrealised = self._eh_prior_unrealised_inventory_balances()
        unrealised_target = currency.round(self.unrealised_total)
        if (
            prior_unrealised
            or not float_is_zero(
                unrealised_target, precision_rounding=rounding,
            )
        ):
            inventory_account, cogs_account = self._eh_unrealised_accounts()
            targets = {
                inventory_account.id: currency.round(-unrealised_target),
            }
            for account_id in sorted(set(prior_unrealised) | set(targets)):
                delta = currency.round(
                    targets.get(account_id, 0.0)
                    - prior_unrealised.get(account_id, 0.0)
                )
                if float_is_zero(delta, precision_rounding=rounding):
                    continue
                line_vals.extend([
                    {
                        'account_id': account_id,
                        'name': _(
                            "Period-end unrealised profit in inventory delta"
                        ),
                        'debit': delta if delta > 0.0 else 0.0,
                        'credit': -delta if delta < 0.0 else 0.0,
                        'eh_ic_elimination_role': 'unrealised_inventory',
                    },
                    {
                        'account_id': cogs_account.id,
                        'name': _(
                            "Period-end unrealised profit cost-of-sales delta"
                        ),
                        'debit': -delta if delta < 0.0 else 0.0,
                        'credit': delta if delta > 0.0 else 0.0,
                        'eh_ic_elimination_role': 'unrealised_cogs',
                    },
                ])
        if not line_vals:
            return self.env['account.move']
        # Balance the entry. A residual of at most a couple of rounding units
        # is the tolerated cent from a matched pair diverging within
        # _PAIR_TOLERANCE; absorb it into the largest leg. Every larger
        # difference is an integrity failure. Cross-currency pairs are already
        # translated, compared and scaled to their safe common reciprocal
        # amount during compute; using an FX/CTA account here would disguise a
        # missing expense, stock-input or asset leg as an IAS 21 difference.
        net = currency.round(
            sum(v['debit'] - v['credit'] for v in line_vals))
        if not float_is_zero(net, precision_rounding=rounding):
            if abs(net) <= rounding * 2.0:
                biggest = max(
                    line_vals, key=lambda v: abs(v['debit'] - v['credit']))
                adjusted = currency.round(
                    (biggest['debit'] - biggest['credit']) - net)
                biggest['debit'] = adjusted if adjusted > 0.0 else 0.0
                biggest['credit'] = -adjusted if adjusted < 0.0 else 0.0
            else:
                raise UserError(_(
                    "Elimination entry %(batch)s is structurally unbalanced "
                    "by %(amount).2f %(currency)s. Recompute and resolve its "
                    "source-account mismatch; the difference was not posted "
                    "to an FX/CTA account.",
                    batch=self.display_name,
                    amount=abs(net),
                    currency=currency.name,
                ))
        return _eh_ic_capable(
            self.env['account.move'].sudo()
        )._eh_create_sealed({
            'company_id': company.id,
            'move_type': 'entry',
            'date': self.period_to,
            'journal_id': journal.id,
            'ref': _("IC elimination %(name)s", name=self.name),
            'eh_sealed': True,
            'line_ids': [(0, 0, v) for v in line_vals],
        })

    def action_reset_to_draft(self):
        """Manager-gated reset. A posted batch's sealed move is reversed
        by a retained sealed counter-entry, leaving the elimination company's
        ledger flat while preserving both sides of the GL evidence. The batch
        then returns to draft for recompute."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can reset an elimination "
                "batch.",
            ))
        caller = self
        caller._eh_assert_company_closure()
        caller._eh_lock_for_transition()
        caller._eh_assert_company_closure()
        self = caller._eh_workflow_action()
        for batch in self:
            batch._eh_assert_no_posted_successor()
            batch._eh_reverse_elimination_move()
            batch.sudo().write({
                'state': 'draft',
                'statutory_ledger_quarantined': False,
                'statutory_ledger_quarantine_note': False,
            })
        return True

    def action_release_statutory_ledger_quarantine(self):
        """Release a legacy draft after routing it to a safe workspace."""
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an accounting manager can release an elimination "
                "workspace quarantine."))
        caller = self
        caller._eh_assert_company_closure()
        caller._eh_lock_for_transition()
        caller._eh_assert_company_closure()
        self = caller._eh_workflow_action()
        for batch in self:
            company = batch.elimination_company_id
            if batch.state != 'draft':
                raise UserError(_(
                    "Reset/reverse the legacy batch to draft before changing "
                    "its elimination workspace."))
            if (
                not company
                or company in (batch.company_a_id | batch.company_b_id)
                or not company.eh_ic_consolidation_only
            ):
                raise UserError(_(
                    "Select a distinct consolidation-only elimination entity "
                    "before releasing the quarantine."))
            batch.sudo().write({
                'statutory_ledger_quarantined': False,
                'statutory_ledger_quarantine_note': False,
            })
            batch.message_post(body=_(
                "Legacy statutory-ledger quarantine released after routing "
                "the batch to consolidation-only workspace %s.",
                company.display_name,
            ))
        return True

    def _eh_reverse_elimination_move(self):
        self.ensure_one()
        move = self.move_id
        if not move:
            return
        if (
            move.company_id != self.elimination_company_id
            or move.move_type != 'entry'
            or not move.eh_sealed
            or move not in self.move_history_ids
        ):
            raise UserError(_(
                "The linked elimination move failed its sealed provenance "
                "check and will not be reversed."
            ))
        if self.reversal_move_id:
            reversal = self.reversal_move_id
            if (
                reversal.state == 'posted'
                and reversal.company_id == self.elimination_company_id
                and reversal.eh_sealed
                and reversal in self.move_history_ids
            ):
                return
            raise UserError(_(
                "The linked elimination reversal failed its provenance check."
            ))
        if move.state == 'posted':
            reversal = move._eh_reverse_with_verified_capability(
                default_values_list=[{
                    'date': self.period_to,
                    'ref': _("Reversal of IC elimination %(name)s",
                             name=self.name),
                }],
                # The elimination contains deliberately non-reconcilable P&L
                # legs.  Preserve it with a posted counter-entry rather than
                # asking account.move to auto-reconcile every reversed leg.
                cancel=False,
            )
            reversal._eh_post_verified_reversal()
            self._eh_seal_reversal(reversal)
            self.sudo().write({
                'reversal_move_id': reversal.id,
                'move_history_ids': [(4, move.id), (4, reversal.id)],
                'reversed_at': fields.Datetime.now(),
                'reversed_by_id': self.env.user.id,
            })
            self.message_post(body=_(
                "Elimination move %(move)s reversed (%(rev)s) on batch "
                "reset by %(user)s.",
                move=move.name,
                rev=reversal.name if reversal else '',
                user=self.env.user.display_name,
            ))
        else:
            raise UserError(_(
                "The linked elimination move is not posted. It is retained for "
                "audit and cannot be deleted by the reset workflow."
            ))

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No elimination move has been posted."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Elimination move"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.move_id.id,
            'context': {
                'allowed_company_ids': [self.elimination_company_id.id],
            },
        }

    # ------------------------------------------------------------------
    # consolidation hook
    # ------------------------------------------------------------------

    @api.model
    def eh_ic_elimination_summary(self, period_from, period_to,
                                  company_ids=None):
        """Structured elimination totals for a period, for consumption
        by the consolidation run (or any caller) without that module
        touching this module's models.

        :param period_from: date or ISO string, inclusive.
        :param period_to: date or ISO string, inclusive.
        :param company_ids: optional list of res.company ids; when
            given, only batches whose BOTH pair companies are in the
            list are included.
        :return: dict with keys:
            receivable_eliminated, payable_eliminated,
            revenue_eliminated, expense_eliminated (net magnitudes),
            unrealised_profit, mismatch_count, and batches (one row per batch:
            id, name, state, company_a_id, company_b_id, and the same
            per-batch totals).

        Only computed and posted batches count. A batch is included
        when its period overlaps [period_from, period_to]. Revenue/expense
        flows add across those disjoint periods. Receivable/payable and
        unrealised profit are point-in-time figures, so only the latest
        included batch per company pair contributes them; summing monthly
        ending balances would overstate the reporting-cutoff position.
        """
        if not self.env.su:
            self._eh_check_access('read')
        allowed_company_ids = set(self.env.companies.ids)
        requested_company_ids = set(company_ids or allowed_company_ids)
        unauthorized = requested_company_ids - allowed_company_ids
        if unauthorized and not self.env.su:
            raise AccessError(_(
                "Elimination summaries may only include companies enabled "
                "in the caller's current company context."))
        wanted = requested_company_ids if self.env.su else (
            requested_company_ids & allowed_company_ids)
        if not wanted:
            return {
                'receivable_eliminated': 0.0,
                'payable_eliminated': 0.0,
                'revenue_eliminated': 0.0,
                'expense_eliminated': 0.0,
                'unrealised_profit': 0.0,
                'mismatch_count': 0,
                'batches': [],
            }
        period_from = fields.Date.to_date(period_from)
        period_to = fields.Date.to_date(period_to)
        batches = self.sudo().search([
            ('state', 'in', ('computed', 'posted')),
            ('period_from', '<=', period_to),
            ('period_to', '>=', period_from),
            ('company_a_id', 'in', list(wanted)),
            ('company_b_id', 'in', list(wanted)),
            ('elimination_company_id', 'in', list(allowed_company_ids)),
        ])
        batches._eh_assert_elimination_workspace()
        batches._eh_assert_unrealised_evidence_ready()
        totals = {
            'receivable_eliminated': 0.0,
            'payable_eliminated': 0.0,
            'revenue_eliminated': 0.0,
            'expense_eliminated': 0.0,
            'unrealised_profit': 0.0,
        }
        rows = []
        mismatch_count = 0
        latest_by_pair = {}
        for batch in batches:
            row = {
                'id': batch.id,
                'name': batch.name,
                'state': batch.state,
                'company_a_id': batch.company_a_id.id,
                'company_b_id': batch.company_b_id.id,
                'receivable_eliminated': batch.receivable_total,
                'payable_eliminated': batch.payable_total,
                'revenue_eliminated': batch.revenue_total,
                'expense_eliminated': batch.expense_total,
                'unrealised_profit': batch.unrealised_total,
                'mismatch_count': len(batch.mismatch_ids),
            }
            totals['revenue_eliminated'] += row['revenue_eliminated']
            totals['expense_eliminated'] += row['expense_eliminated']
            mismatch_count += row['mismatch_count']
            rows.append(row)
            pair_key = (
                tuple(sorted((batch.company_a_id.id, batch.company_b_id.id))),
                batch.elimination_company_id.id,
            )
            previous = latest_by_pair.get(pair_key)
            if not previous or (batch.period_to, batch.id) > (
                    previous.period_to, previous.id):
                latest_by_pair[pair_key] = batch
        for latest in latest_by_pair.values():
            totals['receivable_eliminated'] += latest.receivable_total
            totals['payable_eliminated'] += latest.payable_total
            totals['unrealised_profit'] += latest.unrealised_total
        totals['mismatch_count'] = mismatch_count
        totals['batches'] = rows
        return totals


class EhIcEliminationBatchLine(models.Model):
    _name = 'eh.ic.elimination.batch.line'
    _description = "Inter-company elimination leg"
    _order = 'batch_id, id'

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", index=True,
        ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    kind = fields.Selection(
        [
            ('receivable', "Receivable"),
            ('payable', "Payable"),
            ('revenue', "Revenue"),
            ('expense', "Expense"),
        ],
        required=True, index=True,
    )
    account_id = fields.Many2one(
        'account.account', required=True,
        help=(
            "Source-company account the leg eliminates. Resolved by "
            "code into the elimination company's chart at post time."
        ),
    )
    debit = fields.Monetary(currency_field='currency_id')
    credit = fields.Monetary(currency_field='currency_id')

    def _eh_guard_engine_only(self):
        if self.env.su and self.env.context.get(IC_ENGINE_CTX):
            return
        raise UserError(_(
            "Elimination legs are engine-built from the matched pairs "
            "and cannot be edited by hand. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get(IC_ENGINE_CTX)):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_engine_only()
        return super().write(vals)

    def unlink(self):
        self._eh_guard_engine_only()
        return super().unlink()


class EhIcEliminationMismatch(models.Model):
    _name = 'eh.ic.elimination.mismatch'
    _description = "Inter-company elimination mismatch"
    _order = 'batch_id, id'

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    kind = fields.Selection(
        [
            ('no_mirror', "No mirror"),
            ('mirror_draft', "Mirror not posted"),
            ('orphan_mirror', "Posted mirror for cancelled source"),
            ('amount', "Amount mismatch"),
        ],
        required=True, index=True,
    )
    source_amount = fields.Monetary(currency_field='currency_id')
    mirror_amount = fields.Monetary(currency_field='currency_id')
    difference = fields.Monetary(
        compute='_compute_difference', store=True,
        currency_field='currency_id',
    )
    reason = fields.Char()

    @api.depends('source_amount', 'mirror_amount')
    def _compute_difference(self):
        for rec in self:
            rec.difference = abs(
                (rec.source_amount or 0.0) - (rec.mirror_amount or 0.0))

    def _eh_guard_engine_only(self):
        if self.env.su and self.env.context.get(IC_ENGINE_CTX):
            return
        raise UserError(_(
            "Mismatch rows are engine-built from the matched pairs and "
            "cannot be edited by hand. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get(IC_ENGINE_CTX)):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._eh_guard_engine_only()
        return super().write(vals)

    def unlink(self):
        self._eh_guard_engine_only()
        return super().unlink()


class EhIcUnrealisedLine(models.Model):
    _name = 'eh.ic.unrealised.line'
    _description = "Inter-company unrealised profit line"
    _order = 'batch_id, id'

    # Fields a user may edit while the batch is not yet posted. The
    # margin and its inputs are engine-derived from source invoice and
    # immutable posting-date cost snapshot, never hand-typed.
    _EH_USER_FIELDS = (
        'remaining_fraction', 'inventory_evidence_date',
        'inventory_evidence_reference', 'inventory_evidence_reviewed',
        'notes',
    )

    batch_id = fields.Many2one(
        'eh.ic.elimination.batch', required=True,
        ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        related='batch_id.currency_id', store=True, readonly=True,
    )
    source_move_id = fields.Many2one(
        'account.move', string="Source move", ondelete='set null',
    )
    mirror_move_id = fields.Many2one(
        'account.move', string="Mirror move", ondelete='set null',
    )
    source_move_line_id = fields.Many2one(
        'account.move.line', string="Exact source invoice line",
        ondelete='restrict', index=True, readonly=True,
        help=(
            "Exact inter-company sale line whose margin and inventory "
            "evidence this row represents. This identity prevents one "
            "product balance being reused across several invoices."
        ),
    )
    product_id = fields.Many2one('product.product', index=True)
    quantity = fields.Float(digits=(16, 4))
    unit_cost = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Selling-company functional-currency cost captured when source "
            "posted and translated into elimination currency."
        ),
    )
    price_subtotal = fields.Monetary(
        currency_field='currency_id',
        help="Untaxed amount of the source invoice line. Engine-derived.",
    )
    margin = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Source company-currency revenue less immutable sale-date cost. "
            "Always derived from source evidence, never typed."
        ),
    )
    remaining_qty = fields.Float(
        digits=(16, 4), compute='_compute_remaining_qty', store=True,
        help=(
            "Approved quantity of this exact source line remaining at the "
            "batch cutoff (invoiced quantity times approved fraction)."
        ),
    )
    remaining_fraction = fields.Float(
        digits=(12, 4),
        help=(
            "Fraction of this exact invoiced quantity still held by the "
            "buyer at period end (0 to 1), supported by dated inventory "
            "provenance evidence."
        ),
    )
    fraction_source = fields.Selection(
        [
            ('stock', "Stock quants"),
            ('manual', "Manual"),
        ],
        default='manual', required=True,
    )
    inventory_evidence_date = fields.Date(
        help=(
            "Inventory cutoff evidenced by the supporting workpaper. It must "
            "equal the elimination batch period end before posting."
        ),
    )
    inventory_evidence_reference = fields.Char(
        help=(
            "Immutable workpaper/export reference tracing remaining units to "
            "this exact inter-company source invoice line."
        ),
    )
    inventory_evidence_reviewed = fields.Boolean(
        help=(
            "Manager attestation that the dated workpaper proves the fraction "
            "for this exact source line, excluding third-party inventory."
        ),
    )
    unrealised_amount = fields.Monetary(
        compute='_compute_unrealised_amount', store=True,
        currency_field='currency_id',
        help=(
            "Margin still unrealised at period end: margin times the "
            "remaining fraction, rounded to the elimination currency."
        ),
    )
    notes = fields.Char()

    @api.depends('margin', 'remaining_fraction')
    def _compute_unrealised_amount(self):
        for line in self:
            currency = line.currency_id
            amount = (line.margin or 0.0) * (line.remaining_fraction or 0.0)
            line.unrealised_amount = (
                currency.round(amount) if currency else round(amount, 2))

    @api.depends('quantity', 'remaining_fraction')
    def _compute_remaining_qty(self):
        for line in self:
            line.remaining_qty = (
                (line.quantity or 0.0) * (line.remaining_fraction or 0.0)
            )

    @api.constrains('remaining_fraction')
    def _check_fraction(self):
        for line in self:
            if not 0.0 <= (line.remaining_fraction or 0.0) <= 1.0:
                raise ValidationError(_(
                    "The remaining fraction must be between 0 and 1.",
                ))

    @api.constrains(
        'inventory_evidence_reviewed', 'inventory_evidence_date',
        'inventory_evidence_reference', 'batch_id',
    )
    def _check_inventory_evidence(self):
        for line in self.filtered('inventory_evidence_reviewed'):
            if line.inventory_evidence_date != line.batch_id.period_to:
                raise ValidationError(_(
                    "Reviewed inventory evidence must be dated exactly at "
                    "the elimination batch cutoff %s.",
                    line.batch_id.period_to,
                ))
            if not (line.inventory_evidence_reference or '').strip():
                raise ValidationError(_(
                    "Reviewed inventory evidence requires an exact workpaper "
                    "or source-system reference."))

    def _eh_guard_engine_only(self, vals=None):
        if self.env.su and self.env.context.get(IC_ENGINE_CTX):
            return
        if vals is not None:
            touched = set(vals) - set(self._EH_USER_FIELDS)
            if touched:
                raise UserError(_(
                    "The margin figures on an unrealised-profit line are "
                    "engine-derived from source invoice and immutable "
                    "sale-date cost (%(fields)s cannot be edited). Only "
                    "the remaining fraction is manual.",
                    fields=', '.join(sorted(touched))))
            posted = self.filtered(lambda l: l.batch_id.state == 'posted')
            if posted:
                raise UserError(_(
                    "The unrealised-profit lines of a posted elimination "
                    "batch are frozen. Reset the batch to draft to "
                    "change them.",
                ))
            return
        raise UserError(_(
            "Unrealised-profit lines are engine-built from the matched "
            "pairs. Recompute the batch instead.",
        ))

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get(IC_ENGINE_CTX)):
            self._eh_guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get(IC_ENGINE_CTX)):
            self._eh_check_access('write')
            batches = self.mapped('batch_id')
            batches._eh_assert_company_closure()
            batches._eh_lock_for_transition()
            self.invalidate_recordset()
            batches.invalidate_recordset()
            self._eh_check_access('write')
            batches._eh_assert_company_closure()
            if (
                self.filtered('inventory_evidence_reviewed')
                and {'remaining_fraction', 'inventory_evidence_date',
                     'inventory_evidence_reference'}.intersection(vals)
                and 'inventory_evidence_reviewed' not in vals
            ):
                vals = dict(vals, inventory_evidence_reviewed=False)
        self._eh_guard_engine_only(vals=vals)
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get(IC_ENGINE_CTX)):
            self._eh_guard_engine_only()
        return super().unlink()
