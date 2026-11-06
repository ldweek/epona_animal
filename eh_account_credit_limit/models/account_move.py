# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move integration: gate _post() on customer credit limit.

The gate fires for out_invoice and out_refund only. Vendor moves and
manual journal entries are out of scope; their credit considerations
belong to the AP and approval workflow modules respectively.

Override flow:

1. User attempts to post a customer invoice that breaches the partner's
   limit. _post raises a UserError with a clear message naming the
   exposure, limit, and excess amount.
2. A manager (member of the policy's override_group) writes
   eh_credit_override_reason on the move and posts again. The gate
   sees the reason, records an immutable audit log row, and allows the
   post.
3. The override is per-move; a manager who blesses one invoice does
   not implicitly bless the next one.
"""

import hashlib
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat
from odoo.tools.misc import format_amount


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_credit_override_reason = fields.Char(
        copy=False,
        help=(
            "Reason for overriding the customer credit limit on this "
            "specific invoice. Setting this field is a positive act: "
            "the gate sees it as 'manager has explicitly accepted "
            "this excess' and records the override before posting."
        ),
    )
    eh_credit_override_reason_author_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help="Authorised user who last recorded the override reason.",
    )
    eh_credit_override_reason_at = fields.Datetime(
        readonly=True, copy=False,
        help="Time the authorised override reason was last recorded.",
    )
    eh_credit_override_basis = fields.Char(
        readonly=True,
        copy=False,
        help=(
            "Server-owned fingerprint of the customer, company, currency, "
            "dates, amount and invoice lines that the authorised user "
            "reviewed. Posting refuses a stale reason after the invoice is "
            "changed."
        ),
    )
    eh_credit_override_log_id = fields.Many2one(
        'eh.credit.override.log',
        readonly=True, copy=False,
        help=(
            "Audit row recorded when this move's post bypassed the "
            "credit gate. Empty when no override was needed."
        ),
    )
    eh_credit_warning = fields.Char(
        compute='_compute_eh_credit_warning',
        groups=(
            'account.group_account_invoice,'
            'eh_account_base.group_eh_user'
        ),
        help=(
            "Live, non-blocking warning shown on a draft customer "
            "invoice when posting it would take the partner over its "
            "credit limit. The hard gate still runs at post time."
        ),
    )
    eh_credit_can_override = fields.Boolean(
        compute='_compute_eh_credit_can_override',
        groups=(
            'account.group_account_invoice,'
            'eh_account_base.group_eh_user'
        ),
        help="Whether the current user may record this invoice's override.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            'eh_credit_override_log_id',
            'eh_credit_override_reason_author_id',
            'eh_credit_override_reason_at',
            'eh_credit_override_basis',
        }
        if not self.env.su and any(
                any(vals.get(field_name) for field_name in protected)
                for vals in vals_list):
            raise AccessError(_(
                "Credit override provenance is assigned only by the "
                "posting gate."
            ))
        moves = super().create(vals_list)
        reason_moves = moves.filtered('eh_credit_override_reason')
        if reason_moves:
            reason_moves._eh_stamp_credit_override_reason()
        return moves

    def write(self, vals):
        if not self.env.su:
            if {
                'eh_credit_override_log_id',
                'eh_credit_override_reason_author_id',
                'eh_credit_override_reason_at',
                'eh_credit_override_basis',
            }.intersection(vals):
                raise AccessError(_(
                    "Credit override provenance is server-owned."
                ))
            if 'eh_credit_override_reason' in vals and any(
                    move.state == 'posted' for move in self):
                raise AccessError(_(
                    "A posted credit-override reason is frozen with its "
                    "audit evidence."
                ))
            if 'eh_credit_override_reason' in vals:
                self._eh_check_credit_override_actor(prospective_vals=vals)
        result = super().write(vals)
        if 'eh_credit_override_reason' in vals:
            # A legacy/reset draft can still point to its earlier immutable
            # log. Detach only the move-side pointer when a manager records a
            # fresh decision; the old log continues to retain move_id and all
            # original evidence.
            self.filtered('eh_credit_override_log_id').sudo().write({
                'eh_credit_override_log_id': False,
            })
            if vals.get('eh_credit_override_reason'):
                self._eh_stamp_credit_override_reason()
            else:
                self.sudo().write({
                    'eh_credit_override_reason_author_id': False,
                    'eh_credit_override_reason_at': False,
                    'eh_credit_override_basis': False,
                })
        return result

    def button_draft(self):
        """Reset any consumed override without erasing its audit row.

        A reason authorises one posting attempt against one reviewed invoice
        basis. Core reset-to-draft makes the document editable again, so the
        consumed reason must not silently authorise a second posting. The
        append-only log remains linked back to the move through ``move_id``.
        """
        result = super().button_draft()
        reset = self.filtered(
            lambda move: move.state == 'draft' and (
                move.eh_credit_override_reason
                or move.eh_credit_override_reason_author_id
                or move.eh_credit_override_reason_at
                or move.eh_credit_override_basis
                or move.eh_credit_override_log_id
            )
        )
        if reset:
            reset.sudo().write({
                'eh_credit_override_reason': False,
                'eh_credit_override_reason_author_id': False,
                'eh_credit_override_reason_at': False,
                'eh_credit_override_basis': False,
                'eh_credit_override_log_id': False,
            })
        return result

    def unlink(self):
        self._eh_check_access('unlink')
        retained = self.env['eh.credit.override.log'].sudo().search([
            ('move_id', 'in', self.ids),
        ], limit=1)
        if retained:
            raise UserError(_(
                "This journal entry has retained credit-override evidence "
                "and cannot be deleted. Keep or archive its audit trail."
            ))
        return super().unlink()

    def _eh_credit_override_group(
            self, policy=None, partner=None, company=None):
        self.ensure_one()
        partner = partner or self.partner_id.commercial_partner_id
        company = company or self.company_id
        policy = policy or (
            partner.with_company(company)._eh_resolve_credit_policy()
            if partner else self.env['eh.credit.policy']
        )
        return policy.override_group_id or self.env.ref(
            'eh_account_base.group_eh_manager', raise_if_not_found=False,
        )

    def _eh_check_credit_override_actor(
            self, policy=None, prospective_vals=None):
        prospective_vals = prospective_vals or {}
        for move in self:
            company = self.env['res.company'].browse(
                prospective_vals.get('company_id') or move.company_id.id,
            )
            partner = self.env['res.partner'].browse(
                prospective_vals.get('partner_id') or move.partner_id.id,
            ).commercial_partner_id
            target_policy = policy or (
                partner.with_company(company)._eh_resolve_credit_policy()
                if partner else self.env['eh.credit.policy']
            )
            override_group = move._eh_credit_override_group(
                policy=target_policy,
                partner=partner,
                company=company,
            )
            if (not override_group
                    or override_group not in self.env.user.all_group_ids):
                raise AccessError(_(
                    "Only a member of the configured credit-override group "
                    "may record or clear an override reason."
                ))

    @api.depends('partner_id', 'company_id', 'move_type', 'state')
    @api.depends_context('uid', 'company')
    def _compute_eh_credit_can_override(self):
        for move in self:
            allowed = False
            if (
                move.state == 'draft'
                and move.move_type in ('out_invoice', 'out_refund')
                and move.partner_id
            ):
                group = move._eh_credit_override_group()
                allowed = bool(
                    group and group in self.env.user.all_group_ids
                )
            move.eh_credit_can_override = allowed

    def _eh_stamp_credit_override_reason(self):
        if not self:
            return self
        if not self.env.su:
            self._eh_check_credit_override_actor()
        actor_id = self.env.user.id
        authored_at = fields.Datetime.now()
        for move in self:
            move.sudo().write({
                'eh_credit_override_reason_author_id': actor_id,
                'eh_credit_override_reason_at': authored_at,
                'eh_credit_override_basis': (
                    move.sudo()._eh_credit_override_basis_digest()
                ),
            })
        return self

    def _eh_credit_override_basis_digest(self):
        """Fingerprint every move-owned input that can affect the gate.

        ``amount_total_signed`` binds the company-currency amount. Line ids
        and source-sale links additionally catch a same-total rewrite that
        changes how confirmed-order exposure is replaced. External partner
        exposure remains live by design and is captured in the final log.
        """
        self.ensure_one()
        line_payload = []
        for line in self.invoice_line_ids.sorted('id'):
            item = {
                'id': line.id,
                'display_type': line.display_type or False,
                'account_id': line.account_id.id or False,
                'quantity': float(line.quantity or 0.0),
                'price_unit': float(line.price_unit or 0.0),
                'discount': float(line.discount or 0.0),
                'price_subtotal': float(line.price_subtotal or 0.0),
                'price_total': float(line.price_total or 0.0),
                'tax_ids': sorted(line.tax_ids.ids),
            }
            if 'sale_line_ids' in line._fields:
                item['sale_line_ids'] = sorted(line.sale_line_ids.ids)
            line_payload.append(item)
        payload = {
            'partner_id': self.partner_id.id or False,
            'commercial_partner_id': (
                self.partner_id.commercial_partner_id.id or False
            ),
            'company_id': self.company_id.id or False,
            'currency_id': self.currency_id.id or False,
            'move_type': self.move_type or False,
            'invoice_date': (
                self.invoice_date.isoformat() if self.invoice_date else False
            ),
            'date': self.date.isoformat() if self.date else False,
            'amount_total_signed': float(self.amount_total_signed or 0.0),
            'invoice_lines': line_payload,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    @api.depends('partner_id', 'amount_total', 'move_type', 'state',
                 'company_id')
    def _compute_eh_credit_warning(self):
        for move in self:
            warning = False
            if (
                move.move_type in ('out_invoice', 'out_refund')
                and move.partner_id
                and move.state == 'draft'
            ):
                partner, policy, limit = move._eh_credit_gate_inputs()
                if policy and limit:
                    exposure = move._eh_partner_exposure_excluding_self(
                        partner, policy)
                    projected = exposure + move._eh_credit_relevant_amount()
                    currency = move.company_id.currency_id
                    if currency.compare_amounts(projected, limit) > 0:
                        warning = _(
                            "Posting brings %(partner)s to %(new)s, over the "
                            "%(limit)s credit limit (excess %(excess)s).",
                            partner=partner.display_name,
                            new=format_amount(self.env, projected, currency),
                            limit=format_amount(self.env, limit, currency),
                            excess=format_amount(
                                self.env, projected - limit, currency,
                            ),
                        )
            move.eh_credit_warning = warning

    def _eh_credit_gate_inputs(self):
        """Return commercial partner, policy, and rounded-currency limit."""
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        if not partner or not self.company_id:
            return partner, self.env['eh.credit.policy'], 0.0
        scoped_partner = partner.with_company(self.company_id)
        policy = scoped_partner._eh_resolve_credit_policy()
        limit = (
            scoped_partner._eh_credit_limit_for_company(self.company_id)
            or (policy.default_credit_limit if policy else 0.0)
        )
        return partner, policy, float(limit or 0.0)

    def _eh_credit_gate_enabled(self):
        self.ensure_one()
        _partner, policy, limit = self._eh_credit_gate_inputs()
        return bool(policy and limit)

    def _post(self, soft=True):
        """Hook the post pipeline so the credit check runs first.

        For out_invoice / out_refund moves, computes the partner's
        exposure plus this move's amount and compares against the
        effective limit. Block-mode policies raise unless the move
        carries an override reason. Warn-mode policies log to chatter
        but do not block.

        The check runs in order alongside the move's own validation,
        so a malformed move still fails on its own validation; the
        credit gate only fires when validation would otherwise pass.
        """
        # Keep override evidence and core posting in one action-owned
        # savepoint. Outer RPC transactions normally roll back on failure,
        # but server callers may catch a downstream posting exception and
        # continue; without this boundary those callers could commit a log
        # and chatter message for a move which never posted.
        with self.env.cr.savepoint():
            pending = self.filtered(lambda move: move.state != 'posted')
            if pending:
                pending._eh_check_access('write')
            candidates = pending.filtered(
                lambda move: (
                    move.state != 'posted'
                    and move.move_type in ('out_invoice', 'out_refund')
                )
            ).sorted(
                key=lambda move: (
                    move.partner_id.commercial_partner_id.id,
                    move.id,
                )
            )
            gated = candidates.filtered(
                lambda move: move._eh_credit_gate_enabled()
            )
            if gated:
                gated._eh_lock_moves_for_credit_gate()
            for move in candidates:
                move._eh_check_credit_gate()
            return self._eh_post_after_credit_gate(soft=soft)

    def _eh_post_after_credit_gate(self, soft=True):
        """Narrow downstream boundary kept patchable for rollback tests."""
        return super()._post(soft=soft)

    def _eh_lock_moves_for_credit_gate(self):
        """Lock and refresh posting rows before reading override evidence."""
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id IN %s ORDER BY id "
            "FOR UPDATE",
            (ids,),
        )
        self.invalidate_recordset()
        return self

    def _eh_check_credit_gate(self):
        self.ensure_one()
        partner, policy, limit = self._eh_credit_gate_inputs()
        if not partner:
            return
        if not policy:
            return  # no policy configured; gate is disabled.
        if not limit:
            return  # zero limit means no enforcement.
        # Block mode is a hard check-then-act: read exposure, compare
        # against the limit, then post. Serialise concurrent block-mode
        # gates for the same customer on the partner row so two posts
        # cannot both read the pre-post exposure, both pass, and both
        # post over the limit with no override recorded. Warn mode is
        # advisory only, so it stays lock-free. The lock is taken before
        # reading exposure so the queued second post re-reads the
        # freshly-committed first invoice.
        if policy.enforcement_mode == 'block':
            self._eh_lock_partner_for_gate(partner)
        # We exclude this move from exposure to avoid double counting:
        # if drafts are included in the policy, the current move was
        # already counted as a draft. Compute exposure WITHOUT this
        # move, then add this move's amount once.
        exposure = self._eh_partner_exposure_excluding_self(partner, policy)
        move_amount = self._eh_credit_relevant_amount()
        new_exposure = exposure + move_amount
        currency = self.company_id.currency_id
        if currency.compare_amounts(new_exposure, limit) <= 0:
            return  # within limit; no further action needed.

        excess = new_exposure - limit
        if policy.enforcement_mode == 'warn':
            self.message_post(body=_(
                "Credit warning: posting this invoice brings %(partner)s "
                "to %(new)s, exceeding the %(limit)s limit by %(excess)s. "
                "Posting allowed (warn-only policy).",
                partner=partner.display_name,
                new=format_amount(self.env, new_exposure, currency),
                limit=format_amount(self.env, limit, currency),
                excess=format_amount(self.env, excess, currency),
            ))
            return

        # Block mode: require an override reason.
        if not self.eh_credit_override_reason:
            raise UserError(_(
                "Credit limit exceeded for %(partner)s. Posting this "
                "invoice would bring exposure to %(new)s against a limit "
                "of %(limit)s (excess %(excess)s). To proceed, "
                "a manager must record the override reason on this "
                "invoice before posting again.",
                partner=partner.display_name,
                new=format_amount(self.env, new_exposure, currency),
                limit=format_amount(self.env, limit, currency),
                excess=format_amount(self.env, excess, currency),
            ))

        # Override path: verify the user is authorised. The override group
        # is configurable per policy and defaults to the suite manager
        # group. When a deployment explicitly clears it, fall back to that
        # manager group rather than letting any invoice writer override:
        # the previous "override_group and ..." short-circuit allowed an
        # unrestricted bypass whenever the group was empty.
        override_group = self._eh_credit_override_group(policy=policy)
        # all_group_ids includes implied/parent groups (group_ids holds only
        # explicitly assigned ones), matching how has_group resolves access.
        if (not override_group
                or override_group not in self.env.user.all_group_ids):
            raise UserError(_(
                "Posting %(partner)s over its credit limit requires an "
                "override by a member of %(group)s. Configure the override "
                "group on the credit policy, or have an authorised user "
                "post this invoice.",
                partner=partner.display_name,
                group=(
                    override_group.name if override_group
                    else _("the credit-override group")
                ),
            ))

        reason_author = self.eh_credit_override_reason_author_id
        if (
            not reason_author
            or not self.eh_credit_override_reason_at
            or override_group not in reason_author.all_group_ids
        ):
            raise UserError(_(
                "An authorised credit-override user must save the reason "
                "before this invoice can be posted. Re-enter the reason "
                "to capture its author and timestamp."
            ))

        current_basis = self._eh_credit_override_basis_digest()
        if (
            not self.eh_credit_override_basis
            or self.eh_credit_override_basis != current_basis
        ):
            raise UserError(_(
                "This invoice changed after its credit override was "
                "authorised. An authorised user must review the current "
                "customer, dates, currency and lines, then re-enter and "
                "save the override reason before posting."
            ))

        # Record the audit row, link it to the move.
        actor_id = self.env.user.id
        log = self.env['eh.credit.override.log'].sudo().create({
            'move_id': self.id,
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'exposure_at_override': exposure,
            'limit_at_override': limit,
            'move_amount': move_amount,
            'reason': self.eh_credit_override_reason,
            'user_id': actor_id,
            'reason_author_id': reason_author.id,
            'reason_authored_at': self.eh_credit_override_reason_at,
        })
        self._eh_check_access('write')
        self.sudo().write({'eh_credit_override_log_id': log.id})
        self.message_post(body=_(
            "Credit override recorded. Exposure %(exp)s, limit %(lim)s, "
            "excess %(exc)s. Reason: %(reason)s.",
            exp=format_amount(self.env, exposure, currency),
            lim=format_amount(self.env, limit, currency),
            exc=format_amount(self.env, excess, currency),
            reason=self.eh_credit_override_reason,
        ))

    def _eh_lock_partner_for_gate(self, partner):
        """Serialise concurrent block-mode credit gates for one customer.

        The block guarantee is a check-then-act: read the partner's
        exposure, compare against the limit, then post. Without a lock
        two posts for the same commercial partner both read the pre-post
        exposure (each other's draft is invisible under READ COMMITTED),
        both pass, and their combined exposure silently breaches the
        limit with no override recorded in the audit log.

        A row lock on the commercial partner forces the second gate to
        queue behind the first; once the first commits, the second
        re-reads exposure (now including the freshly-posted invoice via a
        fresh read_group) and correctly blocks or forces the override
        path. flush + invalidate around the lock so the re-read sees
        committed state rather than a stale snapshot. Mirrors the FOR
        UPDATE discipline in eh.cheque.book and the run-model posting
        producers.
        """
        partner.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM res_partner WHERE id = %s FOR UPDATE",
            (partner.id,),
        )
        partner.invalidate_recordset()

    def _eh_partner_exposure_excluding_self(self, partner, policy):
        """Compute partner exposure excluding this move's contribution.

        Mirrors res.partner._eh_compute_exposure but adds an exclusion
        on this move's id so a draft customer invoice is not double-
        counted when the policy includes drafts and we then add the
        move amount on top of the exposure.
        """
        self.ensure_one()
        AML = self.env['account.move.line']
        company_ids = partner._eh_credit_ledger_company_ids(self.company_id)
        persisted_id = self.id if type(self.id) is int else False
        domain = [
            ('partner_id.commercial_partner_id', '=', partner.id),
            ('account_id.account_type', '=', 'asset_receivable'),
            ('parent_state', '=', 'posted'),
            ('amount_residual', '!=', 0),
            ('company_id', 'in', company_ids),
        ]
        if persisted_id:
            domain.append(('move_id', '!=', persisted_id))
        # One SQL pass instead of materialising every AR line per post.
        posted_rows = read_group_compat(AML, domain, [], ['amount_residual:sum'])
        total = posted_rows[0][0] if posted_rows else 0.0

        # Current move is added explicitly below, so any part sourced from a
        # sale order must replace (not duplicate) that order commitment.
        represented_drafts = self.env['account.move'].browse(persisted_id)
        if policy.include_drafts:
            Move = self.env['account.move']
            draft_domain = [
                ('commercial_partner_id', '=', partner.id),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
                ('state', '=', 'draft'),
                ('company_id', 'in', company_ids),
            ]
            if persisted_id:
                draft_domain.append(('id', '!=', persisted_id))
            draft_rows = read_group_compat(Move,
                draft_domain,
                [],
                # amount_total_signed is in company currency and signs
                # refunds negative, matching the partner-form exposure
                # (res.partner._eh_compute_exposure). amount_total would
                # use each draft's own currency and inflate refunds.
                ['amount_total_signed:sum'],
            )
            total += draft_rows[0][0] if draft_rows else 0.0
            if (
                policy.include_sale_orders
                and 'sale.order' in self.env
                and 'sale_line_ids' in self.env[
                    'account.move.line'
                ]._fields
            ):
                represented_drafts |= Move.search(draft_domain)

        # Mirror partner-side SO exposure. Sale-derived invoice lines replace
        # their source commitment: projected exposure contains remaining SO
        # value plus invoice value exactly once.
        if policy.include_sale_orders and 'sale.order' in self.env:
            SaleOrder = self.env['sale.order']
            company = self.company_id or self.env.company
            company_currency = company.currency_id
            so_domain = [
                ('partner_id.commercial_partner_id', '=', partner.id),
                ('state', 'in', ('sale', 'done')),
                ('company_id', 'in', company_ids),
                ('invoice_status', '!=', 'invoiced'),
                ('amount_total', '!=', 0.0),
            ]
            # Convert each open order from its own currency to company
            # currency at the order's effective date, mirroring
            # res.partner._eh_compute_exposure. A raw sum of
            # amount_to_invoice would compare a foreign-currency order
            # against the limit at its unconverted figure and
            # mis-enforce the block.
            orders = SaleOrder.search(so_domain)
            for order in orders:
                raw = partner._eh_sale_order_projected_uninvoiced_amount(
                    order,
                    represented_moves=represented_drafts,
                )
                if not raw:
                    continue
                order_currency = order.currency_id or company_currency
                if order_currency == company_currency:
                    total += raw
                else:
                    rate_date = (
                        getattr(order, 'date_order', False)
                        or fields.Date.context_today(self)
                    )
                    total += order_currency._convert(
                        raw, company_currency, company, rate_date,
                    )

        return total

    def _eh_credit_relevant_amount(self):
        """Signed amount this move adds to partner exposure.

        out_invoice adds to exposure (customer owes more), out_refund
        subtracts (customer owes less). The value must be in COMPANY
        currency, because both the stored exposure and the credit limit
        are held in company currency: amount_total lives in the move's
        own (possibly foreign) currency, so returning it raw would
        compare a foreign-currency invoice against the limit at its
        unconverted figure and mis-enforce the gate.

        amount_total_signed is already company-currency and signs
        out_refund negative / out_invoice positive, matching the
        partner-form exposure (res.partner._eh_compute_exposure, which
        sums amount_total_signed for drafts). Use it directly so the
        gate compares like-for-like.
        """
        self.ensure_one()
        return float(self.amount_total_signed or 0.0)
