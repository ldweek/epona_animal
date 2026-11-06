# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.budget.commitment: encumbrance accounting on a budget line.

Government and not-for-profit accounting recognise three positions
against a budget at any point in time:

  available = budgeted - actual - committed

`actual` is what has already been posted to the GL (the existing
budget-line compute).
`committed` is what has been promised but not yet posted: an
approved purchase order, an open contract, etc. The amount is
released back to availability when the underlying document either
becomes a posted bill (it shifts from committed to actual) or is
cancelled.

This model holds one row per source document (typically a
purchase.order line) per budget line. The two computed fields on
eh.budget.line (`committed_amount` and `available_amount`) sum the
matching commitment rows so reports and the PO confirm gate see a
consistent picture.

The lifecycle is deliberately simple:

  draft -> reserved -> released

draft: created as a placeholder while the source document is in
quote / approval state; does not deduct availability.
reserved: source document is confirmed; deducts availability.
released: source document was cancelled OR became a posted bill;
restores availability.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


# This value is deliberately an object, not a truthy context flag.  RPC
# clients can choose arbitrary context keys/values, but cannot reproduce this
# process-local identity.  Source hooks import the object and carry it only
# around the narrow create/update that owns a commitment row.
EH_BUDGET_COMMITMENT_CAPABILITY = object()
_CAPABILITY_CONTEXT_KEY = 'eh_budget_commitment_capability'


class EhBudgetCommitment(models.Model):
    _name = 'eh.budget.commitment'
    _description = "Budget commitment (encumbrance)"
    _order = 'budget_line_id, create_date desc, id desc'
    _inherit = ['mail.thread', 'eh.workflow.guard']

    # The reserve/release lifecycle drives budget availability. It advances
    # only through action_reserve / action_release and the PO source hooks
    # (which run as su); a direct non-superuser write to state is refused by
    # eh.workflow.guard so availability cannot be moved by a raw RPC write.
    _eh_guarded_fields = (
        'state', 'budget_id', 'company_id', 'currency_id',
        'original_amount', 'source_model', 'source_id', 'source_line_id',
        'source_ref',
    )
    _eh_create_guarded_fields = ('state',)

    _ENGINE_FIELDS = frozenset({
        'state', 'budget_line_id', 'budget_id', 'company_id', 'currency_id',
        'amount', 'original_amount', 'source_model', 'source_id',
        'source_line_id', 'source_ref', 'active',
        'eh_integrity_review_reason',
    })
    _RELATED_OUTPUT_FIELDS = frozenset({
        'budget_id', 'company_id', 'currency_id',
    })
    _SOURCE_FIELDS = frozenset({
        'source_model', 'source_id', 'source_line_id', 'source_ref',
    })

    budget_line_id = fields.Many2one(
        'eh.budget.line', required=True,
        ondelete='cascade', index=True, check_company=True,
        help=(
            "Budget line this commitment reserves against. Resolved "
            "from the source document's account / analytic / date."
        ),
    )
    budget_id = fields.Many2one(
        related='budget_line_id.budget_id', store=True, readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related='budget_line_id.budget_id.company_id',
        store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='budget_line_id.currency_id', store=True, readonly=True,
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('reserved', "Reserved"),
            ('released', "Released"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "Lifecycle. draft: source document is still in quote / "
            "approval; does not deduct availability. reserved: "
            "source document is confirmed; deducts availability. "
            "released: source document was cancelled or became a "
            "posted bill; restored availability."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Commitment amount in budget currency. Always entered "
            "positive: a confirmed purchase order for AUD 5,000 "
            "creates a 5,000 commitment regardless of the account "
            "sign. This is the CURRENT reserved amount: partial "
            "releases (partial billing) reduce it toward zero while "
            "leaving original_amount untouched."
        ),
    )
    original_amount = fields.Monetary(
        currency_field='currency_id',
        help=(
            "The commitment amount as first reserved, before any "
            "partial release. Partial billing computes the release "
            "against this stable original so sequential partial bills "
            "add up to the full release and never leave a phantom "
            "residual. Populated on create and refreshed whenever "
            "amount is written above the recorded original."
        ),
    )

    # ---- source document ----
    source_model = fields.Char(
        index=True,
        help=(
            "Model name of the source document (typically "
            "'purchase.order' or 'purchase.order.line'). Sites that "
            "want to commit against sales orders or contracts add "
            "their own hook and write the relevant model name here."
        ),
    )
    source_id = fields.Integer(
        index=True,
        help="Database id of the source document.",
    )
    source_line_id = fields.Integer(
        index=True,
        help=(
            "Database id of the source document line (e.g. the "
            "purchase.order.line) when the commitment is line-level. "
            "Together with source_model and source_id this forms the "
            "idempotency key: one commitment per source line per "
            "budget line. Two purchase order lines that resolve to the "
            "same budget line therefore keep separate commitments "
            "instead of one overwriting the other. Zero when the "
            "source document has no line granularity."
        ),
    )
    source_ref = fields.Char(
        help=(
            "Display reference of the source document (e.g. PO "
            "number). Captured at creation so the row stays "
            "informative even if the source document is deleted."
        ),
    )

    notes = fields.Text()
    active = fields.Boolean(default=True, index=True)
    eh_integrity_review_reason = fields.Text(
        readonly=True,
        help=(
            "Upgrade quarantine reason. Rows remain preserved but inactive "
            "until duplicate source provenance is explicitly reviewed."
        ),
    )

    _check_amount_positive = models.Constraint(
        'CHECK (amount >= 0)',
        'Commitment amount must be zero or positive.',
    )

    def init(self):
        """Database idempotency for active source-backed commitments.

        Manual rows intentionally share the zero source key, so uniqueness is
        partial. Upgrade migration quarantines ambiguous legacy duplicates
        before this index is installed.
        """
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "eh_budget_commitment_source_uniq "
            "ON eh_budget_commitment "
            "(source_model, source_id, source_line_id, budget_line_id) "
            "WHERE active AND source_model IS NOT NULL "
            "AND source_model NOT IN ('', 'manual')"
        )

    # ---- original-amount bookkeeping ----
    #
    # original_amount tracks the amount as first reserved so partial
    # releases can be computed against a stable baseline. It is set on
    # create (defaulting to amount) and refreshed whenever amount is
    # written to a value at or above the current original (a fresh
    # reserve or a PO re-confirm that raises the line amount). Writes
    # that only lower amount (partial release) leave it untouched.

    @api.model
    def _eh_has_engine_capability(self):
        return (
            self.env.context.get(_CAPABILITY_CONTEXT_KEY)
            is EH_BUDGET_COMMITMENT_CAPABILITY
        )

    @api.model
    def _eh_check_manual_manager(self):
        if self.env.su:
            return
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an ERP Heritage Accounting Manager may reserve or "
                "release a manual budget commitment."
            ))

    @api.model
    def _eh_validate_budget_line(self, budget_line_id, operation='read'):
        line = self.env['eh.budget.line'].browse(budget_line_id).exists()
        if not line:
            raise UserError(_("The selected budget line no longer exists."))
        if not self.env.su:
            line._eh_check_access(operation)
            if line.budget_id.company_id.id not in self.env.companies.ids:
                raise AccessError(_(
                    "The budget line belongs to a company outside your "
                    "allowed companies."
                ))
        return line

    def _eh_lock_rows(self):
        if not self:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_budget_commitment WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(self.ids),),
        )
        self.invalidate_recordset()

    @api.model_create_multi
    def create(self, vals_list):
        internal = self._eh_has_engine_capability()
        clean_vals_list = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            related = self._RELATED_OUTPUT_FIELDS.intersection(vals)
            if related and not internal:
                raise AccessError(_(
                    "Stored commitment outputs are server-owned: %(fields)s.",
                    fields=', '.join(sorted(related)),
                ))
            source_model = vals.get('source_model')
            if source_model not in (None, False, '', 'manual') and not internal:
                raise AccessError(_(
                    "Source-document provenance is assigned only by the "
                    "source accounting engine."
                ))
            if not internal:
                supplied_source = self._SOURCE_FIELDS.intersection(vals)
                non_manual = supplied_source - {'source_model'}
                if non_manual or vals.get('original_amount') not in (None, False):
                    raise AccessError(_(
                        "Commitment provenance and original amount are "
                        "server-owned."
                    ))
                vals.update({
                    'source_model': 'manual',
                    'source_id': 0,
                    'source_line_id': 0,
                    'source_ref': False,
                    'eh_integrity_review_reason': False,
                })
                if vals.get('active') is False:
                    raise AccessError(_(
                        "Only the accounting engine may quarantine a "
                        "commitment."
                    ))
            self._eh_validate_budget_line(vals.get('budget_line_id'))
            vals['original_amount'] = vals.get('amount', 0.0)
            clean_vals_list.append(vals)
        return super().create(clean_vals_list)

    def write(self, vals):
        internal = self._eh_has_engine_capability()
        if 'budget_line_id' in vals:
            self._eh_validate_budget_line(vals['budget_line_id'])
        if not internal:
            if 'active' in vals:
                raise AccessError(_(
                    "Commitment archive/quarantine state is server-owned."
                ))
            related = self._RELATED_OUTPUT_FIELDS.intersection(vals)
            provenance = (
                self._SOURCE_FIELDS | {'original_amount'}
            ).intersection(vals)
            if related or provenance:
                raise AccessError(_(
                    "Commitment outputs and provenance are server-owned: "
                    "%(fields)s.",
                    fields=', '.join(sorted(related | provenance)),
                ))
            engine_rows = self.filtered(
                lambda r: r.source_model not in (False, '', 'manual')
            )
            protected = self._ENGINE_FIELDS.intersection(vals)
            if engine_rows and protected:
                raise AccessError(_(
                    "Source-backed commitments are immutable outside their "
                    "source-document accounting engine."
                ))
            frozen = self.filtered(lambda r: r.state != 'draft')
            manual_anchors = {'budget_line_id', 'amount'}.intersection(vals)
            if frozen and manual_anchors:
                raise AccessError(_(
                    "Reserved or released commitment anchors are frozen."
                ))
        if 'amount' in vals and 'original_amount' not in vals:
            new_amount = vals['amount']
            for rec in self:
                if new_amount >= (rec.original_amount or 0.0):
                    super(EhBudgetCommitment, rec.sudo().with_context(
                        **{
                            _CAPABILITY_CONTEXT_KEY:
                                EH_BUDGET_COMMITMENT_CAPABILITY,
                        }
                    )).write({'original_amount': new_amount})
        return super().write(vals)

    def unlink(self):
        if not self._eh_has_engine_capability():
            engine_rows = self.filtered(
                lambda r: r.source_model not in (False, '', 'manual')
            )
            if engine_rows:
                raise AccessError(_(
                    "Source-backed commitments cannot be deleted."
                ))
            if self.filtered(lambda r: r.state != 'draft'):
                raise UserError(_(
                    "Only draft manual commitments can be deleted."
                ))
        return super().unlink()

    # ---- transitions ----

    def action_reserve(self):
        internal = self._eh_has_engine_capability()
        if not internal:
            self._eh_check_access('write')
            self._eh_check_manual_manager()
            if self.filtered(
                lambda r: r.source_model not in (False, '', 'manual')
            ):
                raise AccessError(_(
                    "Source-backed commitments are reserved only by their "
                    "source document."
                ))
        self._eh_lock_rows()
        self = self.sudo().with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        })
        for rec in self:
            if rec.state == 'reserved':
                continue
            if rec.state == 'released':
                if not internal:
                    raise UserError(_(
                        "Released manual commitments cannot be re-reserved. "
                        "Create a fresh commitment for the document.",
                    ))
            rec.state = 'reserved'

    def action_release(self):
        internal = self._eh_has_engine_capability()
        if not internal:
            self._eh_check_access('write')
            self._eh_check_manual_manager()
            if self.filtered(
                lambda r: r.source_model not in (False, '', 'manual')
            ):
                raise AccessError(_(
                    "Source-backed commitments are released only by their "
                    "source document."
                ))
        self._eh_lock_rows()
        self = self.sudo().with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        })
        for rec in self:
            if rec.state == 'released':
                continue
            rec.state = 'released'

    # ---- lookup helpers used by the source-document hooks ----

    @api.model
    @api.private
    def _resolve_budget_line(
        self, account, date, company, analytic_account=None,
    ):
        """Pick the budget line that should carry the commitment.

        Resolution: find a line on a confirmed budget whose period
        contains date, whose account matches, and whose analytic
        account matches (or is empty when analytic_account is None).
        More than one independently approved match is ambiguous: silently
        choosing one would make commitment enforcement depend on record IDs.
        Fail closed until one controlling budget remains.

        Returns an empty recordset when no line matches; the caller
        decides whether to skip silently or raise.
        """
        if not account or not date or not company:
            return self.env['eh.budget.line']
        if not self.env.su:
            account._eh_check_access('read')
            company._eh_check_access('read')
            if company.id not in self.env.companies.ids:
                raise AccessError(_(
                    "Budget resolution is limited to an allowed company."
                ))
            if analytic_account:
                analytic_account._eh_check_access('read')
        Line = self.env['eh.budget.line'].sudo()
        domain = [
            ('budget_id.state', '=', 'confirmed'),
            ('budget_id.company_id', '=', company.id),
            ('budget_id.overrun_policy', '!=', 'off'),
            ('account_id', '=', account.id),
            ('period_from', '<=', date),
            ('period_to', '>=', date),
        ]
        if analytic_account:
            domain.append((
                'analytic_account_id', '=', analytic_account.id,
            ))
        else:
            domain.append((
                'analytic_account_id', '=', False,
            ))
        quarantined = self.env['eh.budget.budget'].sudo().search_count([
            ('state', 'in', ('confirmed', 'revised')),
            ('company_id', '=', company.id),
            ('date_from', '<=', date),
            ('date_to', '>=', date),
            ('overrun_policy', '!=', 'off'),
            ('eh_basis_integrity_review_required', '=', True),
        ])
        if quarantined:
            raise UserError(_(
                "A legacy approved budget basis covering this company and "
                "date is quarantined. Create and approve an independent "
                "reviewed replacement before recording commitments."
            ))
        domain.append((
            'budget_id.eh_basis_integrity_review_required', '=', False,
        ))
        matches = Line.search(domain, order='id', limit=2)
        if len(matches) > 1:
            raise UserError(_(
                "More than one confirmed budget line matches this account, "
                "date, company, and analytic scope. Close or revise the "
                "overlapping budget before recording commitments."
            ))
        return matches
