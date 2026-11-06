# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move trigger: mirror posted invoices into the counterparty
company when the partner's company_id is set and that company has
inter-company mirroring enabled.

Mirror direction map:

    out_invoice -> in_invoice   (sale on A becomes purchase on B)
    out_refund  -> in_refund
    in_invoice  -> out_invoice  (purchase on A becomes sale on B)
    in_refund   -> out_refund

The mirror move is created with eh_intercompany_origin_id pointing
back at the source so re-posting (or duplicate triggers) cannot
produce duplicate mirrors. The check is a simple existence query on
the destination company before creating the new draft.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


_DIRECTION_MIRROR = {
    'out_invoice': 'in_invoice',
    'out_refund': 'in_refund',
    'in_invoice': 'out_invoice',
    'in_refund': 'out_refund',
}

# Process-local capability. RPC clients can forge context keys and primitive
# values, but can never reproduce this object identity. Only mirror/elimination
# engine paths below attach it to recordsets that create or link evidence.
_EH_IC_LINK_CONTEXT = 'eh_ic_link_capability'
_EH_IC_LINK_CAPABILITY = object()


def _eh_ic_capable(recordset):
    return recordset.with_context(**{
        _EH_IC_LINK_CONTEXT: _EH_IC_LINK_CAPABILITY,
    })


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_intercompany_origin_id = fields.Many2one(
        'account.move',
        string="Inter-company origin",
        index=True,
        ondelete='set null',
        copy=False,
        readonly=True,
        help=(
            "When this move is the auto-generated mirror of an inter-"
            "company source invoice, this field points at that source. "
            "Used to prevent duplicate mirrors when the source is "
            "re-posted."
        ),
    )
    eh_intercompany_mirror_id = fields.Many2one(
        'account.move',
        string="Inter-company mirror",
        index=True,
        ondelete='set null',
        copy=False,
        readonly=True,
        help=(
            "When this move is an inter-company source, this field "
            "points at the auto-generated mirror in the counterparty "
            "company."
        ),
    )
    eh_intercompany_state = fields.Selection(
        [
            ('na',                "Not applicable"),
            ('unmatched',         "No mirror"),
            ('mirror_draft',      "Mirror draft"),
            ('amount_mismatch',   "Amount mismatch"),
            ('orphan_mirror',     "Posted mirror for cancelled source"),
            ('matched',           "Matched"),
        ],
        compute='_compute_eh_intercompany_state',
        store=True,
        index=True,
        help=(
            "Lifecycle of the inter-company mirror for this move. "
            "Operations teams use this to spot pairs where the "
            "destination accountant has not yet posted the mirror, "
            "or where the totals diverge after a manual edit on "
            "either side. Stored so the review-queue search filter "
            "can use a plain domain on the field."
        ),
    )

    # One mirror per source move per destination company. PostgreSQL
    # treats NULLs as distinct in a unique constraint, so ordinary moves
    # (origin id NULL) are unaffected; this only binds the mirror rows and
    # closes the race where two concurrent posts both pass the search-
    # before-create guard and create duplicate mirrors.
    _unique_intercompany_mirror = models.Constraint(
        'unique(eh_intercompany_origin_id, company_id)',
        "An inter-company mirror already exists for this source move in "
        "this company.",
    )

    @api.depends(
        'move_type', 'state', 'amount_total',
        'eh_intercompany_origin_id', 'eh_intercompany_mirror_id',
        'eh_intercompany_mirror_id.state',
        'eh_intercompany_mirror_id.amount_total',
    )
    def _compute_eh_intercompany_state(self):
        # Compute the lifecycle bucket per move. Treat the source side
        # as authoritative: a move that is itself a mirror does not get
        # a "mirror" state (its origin carries the pair status).
        for move in self:
            if move.eh_intercompany_origin_id:
                move.eh_intercompany_state = 'na'
                continue
            if move.move_type not in _DIRECTION_MIRROR:
                move.eh_intercompany_state = 'na'
                continue
            mirror = move.eh_intercompany_mirror_id
            if not mirror:
                # Posted source without a mirror = real gap; draft
                # source is not yet expected to have one.
                move.eh_intercompany_state = (
                    'unmatched' if move.state == 'posted' else 'na'
                )
                continue
            if move.state != 'posted' and mirror.state == 'posted':
                move.eh_intercompany_state = 'orphan_mirror'
                continue
            if mirror.state != 'posted':
                move.eh_intercompany_state = 'mirror_draft'
                continue
            # Both sides posted: tolerate a 1c rounding gap so currency
            # conversion or per-line tax rounding does not flag every
            # pair. Anything bigger is a real mismatch and needs human
            # review (manual edit, missing line, FX revaluation drift).
            if abs((move.amount_total or 0.0) - (mirror.amount_total or 0.0)) > 0.01:
                move.eh_intercompany_state = 'amount_mismatch'
            else:
                move.eh_intercompany_state = 'matched'

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            'eh_intercompany_origin_id', 'eh_intercompany_mirror_id',
        }
        if (
            any(protected.intersection(vals or {}) for vals in vals_list)
            and self.env.context.get(_EH_IC_LINK_CONTEXT)
            is not _EH_IC_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Inter-company source/mirror links are server-owned "
                "evidence and cannot be supplied through create."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if (
            {'eh_intercompany_origin_id', 'eh_intercompany_mirror_id'}
            .intersection(vals)
            and self.env.context.get(_EH_IC_LINK_CONTEXT)
            is not _EH_IC_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Inter-company source/mirror links are server-owned "
                "evidence and cannot be changed directly."
            ))
        return super().write(vals)

    def action_eh_view_intercompany_mirror(self):
        """Open the inter-company counterpart from the move form's
        stat button. Source moves jump to their mirror; mirror moves
        jump back to their origin. The action carries the target's
        company in allowed_company_ids so a user logged into the
        source company sees the destination move without an explicit
        company switch.
        """
        self.ensure_one()
        target = self.eh_intercompany_mirror_id or self.eh_intercompany_origin_id
        if not target:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': 'Inter-company counterpart',
            'res_model': 'account.move',
            'res_id': target.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'context': {'allowed_company_ids': [target.company_id.id]},
        }

    def _post(self, soft=True):
        """Hook the post path so a successful post triggers the mirror.

        Called by Odoo's standard action_post() pipeline. We let the
        upstream post complete normally (so validation, sequence
        numbering, and journal entries all happen as expected) and only
        then queue the mirror creation. A mirror failure does not roll
        back the source post; instead we surface a chatter warning on
        the source so the accountant can fix the destination side.

        Before the upstream post, the restrict_ic_partners guard runs:
        when the posting company's configuration carries the flag, an
        invoice or bill towards the raw partner record of another group
        company (not flagged with a Represented Company) is refused, so
        a mis-keyed inter-company transaction cannot bypass mirroring
        and the elimination engine. The guard raises BEFORE posting, so
        nothing is booked. Off by default; existing flows unchanged.
        """
        for move in self:
            move._eh_check_ic_partner_restriction()
        result = super()._post(soft=soft)
        for move in self:
            if move.state != 'posted':
                continue
            try:
                # Mirror automation is secondary to source posting. A
                # PostgreSQL failure must roll back its partial destination
                # work and restore the cursor before chatter records the
                # operational failure.
                with self.env.cr.savepoint():
                    move._eh_create_intercompany_mirror()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Inter-company mirror failed for move %s: %s",
                    move.display_name, exc,
                )
                move.message_post(body=_(
                    "Inter-company mirror failed: %s. The source post "
                    "succeeded; create the destination invoice "
                    "manually or fix the configuration and retry.",
                    str(exc),
                ))
        return result

    def _eh_check_ic_partner_restriction(self):
        """Refuse posting a mis-keyed inter-company document.

        Fires only when the POSTING company's inter-company config
        carries restrict_ic_partners. A document towards a partner
        whose commercial partner is the partner record of another
        res.company, without the Represented Company flag (and without
        the legacy company link), is a group transaction keyed outside
        the mirroring engine: it would never mirror and never be
        matched by the elimination batch, so it is blocked with a clear
        instruction. Mirrors themselves (origin set) are the sanctioned
        counterparty leg and always pass.
        """
        self.ensure_one()
        if self.move_type not in _DIRECTION_MIRROR:
            return
        if self.eh_intercompany_origin_id:
            return
        commercial, represented_company_id, legacy_company_id = (
            self._eh_ic_partner_route()
        )
        if not commercial:
            return
        if represented_company_id:
            return  # flagged for mirroring: the sanctioned IC path
        if (
            legacy_company_id
            and legacy_company_id != self.company_id.id
        ):
            return  # legacy company link: the mirror trigger handles it
        config = self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', self.company_id.id),
             ('enabled', '=', True),
             ('restrict_ic_partners', '=', True)],
            limit=1,
        )
        if not config:
            return
        group_company = self.env['res.company'].sudo().search(
            [('partner_id', '=', commercial.id),
             ('id', '!=', self.company_id.id)],
            limit=1,
        )
        if group_company:
            raise UserError(_(
                "Partner %(partner)s is group company %(company)s, but "
                "it is not flagged for inter-company mirroring "
                "(Represented Company). Posting this document would "
                "bypass the mirror and the elimination engine. Set the "
                "Represented Company on the partner (or disable "
                "'Restrict group-company partners' on the inter-company "
                "configuration).",
                partner=commercial.display_name,
                company=group_company.display_name,
            ))

    def _eh_ic_partner_route(self):
        """Return actor-visible commercial partner and server route ids.

        ``eh_represented_company_id`` is intentionally manager-only.  Posting
        accountants still need the routing engine to inspect it.  Prove actor
        access to source move, source company, partner, and commercial root
        first; elevate only the single restricted-field read.  Source posting
        and all source writes stay in actor environment.
        """
        self.ensure_one()
        self._eh_check_access('read')
        self.company_id._eh_check_access('read')
        partner = self.partner_id
        if not partner:
            return partner, False, False
        partner._eh_check_access('read')
        commercial = partner.commercial_partner_id
        if not commercial:
            return commercial, False, False
        commercial._eh_check_access('read')
        represented_company_id = (
            commercial.sudo().eh_represented_company_id.id
        )
        return commercial, represented_company_id, commercial.company_id.id

    def _eh_is_from_intercompany_mirror_po(self):
        """True when this bill is the invoicing of a purchase order that was
        itself drafted as an inter-company order mirror (eh_ic_origin_so_id on
        the PO, set by eh_account_intercompany_so_po). Field-existence guarded
        so this base module stays independent of the purchase / so_po SKUs: if
        those fields are absent the check is simply False.
        """
        self.ensure_one()
        Line = self.env['account.move.line']
        if 'purchase_line_id' not in Line._fields:
            return False
        orders = self.invoice_line_ids.mapped('purchase_line_id').mapped(
            'order_id')
        if not orders or 'eh_ic_origin_so_id' not in orders._fields:
            return False
        return any(o.eh_ic_origin_so_id for o in orders)

    def _eh_create_intercompany_mirror(self):
        """Create the mirror invoice in the partner's company.

        Skips when:
        * This move already has an inter-company origin (it IS a mirror).
        * The partner has no company_id (not an inter-company partner).
        * The destination company has no enabled config.
        * A mirror already exists for this source.
        * The move type is not in the mirror direction map.
        """
        self.ensure_one()
        if self.eh_intercompany_origin_id:
            return  # this is itself a mirror; do not chain.
        if self.move_type not in _DIRECTION_MIRROR:
            return
        if self._eh_is_from_intercompany_mirror_po():
            # This bill is the invoicing of an inter-company MIRROR purchase
            # order that eh_account_intercompany_so_po drafted from a confirmed
            # sale order in the other company. That sale order already produces
            # its own customer invoice, which this engine mirrors into a bill;
            # letting the mirror-PO bill ALSO mirror would spawn a second,
            # reverse-direction invoice back in the origin company (a duplicate
            # / loop). Suppress it.
            return
        commercial, represented_company_id, legacy_company_id = (
            self._eh_ic_partner_route()
        )
        if not commercial:
            return
        # Resolve the destination company. Prefer the dedicated
        # eh_represented_company_id field (added on res.partner by
        # this module) so the partner can stay global (company_id=False)
        # and remain usable across companies. Fall back to the legacy
        # commercial_partner_id.company_id path so existing data
        # continues to mirror.
        dest_company_id = represented_company_id or legacy_company_id
        if not dest_company_id or dest_company_id == self.company_id.id:
            return
        # Destination resolution belongs to configured server automation;
        # source posting above remains bound to actor ACLs and record rules.
        dest_company = self.env['res.company'].sudo().browse(
            dest_company_id,
        ).exists()
        if not dest_company:
            return

        config = self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', dest_company.id), ('enabled', '=', True)],
            limit=1,
        )
        if not config:
            return

        existing = self.env['account.move'].sudo().search(
            [('eh_intercompany_origin_id', '=', self.id),
             ('company_id', '=', dest_company.id)],
            limit=1,
        )
        if existing:
            # Source actor already passed source move/company/partner ACLs in
            # _eh_ic_partner_route. Linking destination-company evidence is a
            # server-owned cross-company write, not actor master-data access.
            _eh_ic_capable(self.sudo()).write({
                'eh_intercompany_mirror_id': existing.id,
            })
            return

        mirror = self._eh_build_mirror(dest_company, config)
        if not mirror:
            return
        _eh_ic_capable(self.sudo()).write({
            'eh_intercompany_mirror_id': mirror.id,
        })
        self.message_post(body=_(
            "Inter-company mirror created in %(company)s: %(name)s.",
            company=dest_company.display_name,
            name=mirror.display_name,
        ))
        mirror.sudo().message_post(body=_(
            "Auto-generated from %(company)s source %(name)s.",
            company=self.company_id.display_name,
            name=self.display_name,
        ))
        if config.auto_post_mirror:
            try:
                # Auto-post is a second optional stage. Keep a valid draft
                # mirror when posting fails, and never leave the transaction
                # cursor aborted before the outer source-post path resumes.
                with self.env.cr.savepoint():
                    self._eh_apply_mirror_user(mirror, config).with_company(
                        dest_company).action_post()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Inter-company mirror auto-post failed for %s: %s",
                    mirror.display_name, exc,
                )

    @api.model
    def _eh_apply_mirror_user(self, recordset, config):
        """Bind mirror work to one accountable target-company identity.

        Never retain the source actor's company context here.  Downstream
        accounting guards deliberately drop ``sudo`` to re-check their real
        actor; a sudoed source-only user carrying the target company would
        then either fail as an unauthorized company or become a confused
        deputy.  A configured service user is therefore rebound to target
        company only.  Legacy sudo automation remains available only when
        caller already owns target scope (or is itself superuser).
        """
        config = config.sudo()
        target_company = config.company_id
        user = config.intercompany_user_id
        if user:
            if not user.active:
                raise UserError(_(
                    "Inter-company user %(user)s is archived.",
                    user=user.display_name,
                ))
            if target_company not in user.company_ids:
                raise UserError(_(
                    "Inter-company user %(user)s is not allowed in target "
                    "company %(company)s.",
                    user=user.display_name,
                    company=target_company.display_name,
                ))
            return recordset.with_user(user).with_context(
                allowed_company_ids=[target_company.id],
            ).with_company(target_company)
        if (
            not self.env.su
            and target_company.id not in self.env.companies.ids
        ):
            raise UserError(_(
                "Configure an inter-company service user for target company "
                "%(company)s before source-only users can run this route.",
                company=target_company.display_name,
            ))
        return recordset.sudo().with_context(
            allowed_company_ids=[target_company.id],
        ).with_company(target_company)

    def _eh_build_mirror(self, dest_company, config):
        """Build and create the mirror move on dest_company.

        Returns the created account.move or None when journals are not
        configured for this direction. The build runs sudo() with the
        destination company so cross-company write permissions resolve
        through the standard Odoo pattern.
        """
        self.ensure_one()
        target_type = _DIRECTION_MIRROR.get(self.move_type)
        if target_type in ('out_invoice', 'out_refund'):
            journal = config.sale_journal_id
        elif target_type in ('in_invoice', 'in_refund'):
            journal = config.purchase_journal_id
        else:
            return None
        if not journal:
            raise UserError(_(
                "Inter-company config for %(company)s has no journal "
                "configured for mirror type %(type)s. Configure one "
                "under Configuration > Inter-company Configurations.",
                company=dest_company.display_name,
                type=target_type,
            ))

        # Resolve the counterparty partner: it is the company on the
        # source's side from the destination's perspective. We use the
        # source company's partner record.
        counterparty = self.company_id.partner_id

        line_vals = []
        for line in self.invoice_line_ids:
            line_vals.append((0, 0, self._eh_build_mirror_line_vals(
                line, dest_company, target_type, config,
            )))

        move_model = _eh_ic_capable(self._eh_apply_mirror_user(
            self.env['account.move'], config))
        return move_model.with_company(
            dest_company,
        ).create({
            'move_type': target_type,
            'partner_id': counterparty.id,
            'journal_id': journal.id,
            'company_id': dest_company.id,
            'invoice_date': self.invoice_date or self.date,
            'invoice_date_due': self.invoice_date_due,
            'currency_id': self.currency_id.id,
            'ref': self.name or self.ref,
            'invoice_line_ids': line_vals,
            'eh_intercompany_origin_id': self.id,
        })

    def _eh_build_mirror_line_vals(self, source_line, dest_company,
                                   target_type, config):
        """Build vals for one mirror invoice line.

        account_id resolution priority:
          1. Destination-company product accounts (Option D): if the
             source line carries a product, look up its expense
             (for bill mirrors) or income (for invoice mirrors)
             account in the destination company's chart, with the
             destination company's fiscal position applied.
          2. Config fallback (Option C): use config.fallback_expense_
             account_id for bill mirrors, config.fallback_revenue_
             account_id for invoice mirrors.
          3. Hard fail with UserError. No silent default, no null
             account_id ever reaches account.move.line._create.

        Taxes propagate by mapping each source tax to the destination
        company's equivalent on rate and direction (see
        _eh_resolve_mirror_taxes), never by name. Stripping taxes
        entirely breaks GST/VAT balance.
        """
        # Structural invoice rows are presentation metadata, not accountable
        # product rows.  Asking the account/tax resolver to treat a section,
        # subsection or note as a product line either creates a bogus zero
        # amount expense row or makes the whole mirror fail for want of an
        # account.  Preserve the structure and deliberately omit accounting
        # fields, exactly as account.move expects for display-only rows.
        if source_line.display_type in (
                'line_section', 'line_subsection', 'line_note'):
            return {
                'display_type': source_line.display_type,
                'name': source_line.name,
                'sequence': source_line.sequence,
            }

        is_bill_mirror = target_type in ('in_invoice', 'in_refund')

        # ---- account resolution ----
        account = self.env['account.account']
        product = source_line.product_id
        product_company = (
            product.company_id if product and 'company_id' in product._fields
            else self.env['res.company']
        )
        product_usable = bool(
            product and (not product_company or product_company == dest_company)
        )
        if product_usable:
            # Option D: destination-company product accounts.
            try:
                accounts_map = product.with_company(
                    dest_company,
                )._get_product_accounts()
            except Exception:  # noqa: BLE001
                accounts_map = {}
            if is_bill_mirror:
                account = accounts_map.get('expense') or account
            else:
                account = accounts_map.get('income') or account
        if not account:
            # Option C: config-level fallback.
            if is_bill_mirror:
                account = config.fallback_expense_account_id
            else:
                account = config.fallback_revenue_account_id
        if not account:
            # Hard fail. No null account_id ever.
            field_name = (
                'fallback_expense_account_id' if is_bill_mirror
                else 'fallback_revenue_account_id'
            )
            raise UserError(_(
                "Inter-company mirror cannot resolve an account for "
                "line '%(name)s'. Either configure %(kind)s accounts "
                "on the product in company %(company)s, or set "
                "config.%(field)s on the inter-company configuration "
                "for that company.",
                name=source_line.name or '?',
                kind='expense' if is_bill_mirror else 'income',
                company=dest_company.display_name,
                field=field_name,
            ))

        vals = {
            'name': source_line.name,
            'quantity': source_line.quantity,
            'price_unit': source_line.price_unit,
            'discount': source_line.discount,
            'account_id': account.id,
        }
        if source_line.product_uom_id:
            vals['product_uom_id'] = source_line.product_uom_id.id
        if product_usable:
            vals['product_id'] = product.id
        if product:
            source_uom = source_line.product_uom_id or product.uom_id
            base_qty = source_uom._compute_quantity(
                source_line.quantity, product.uom_id,
            )
            cost_company = (
                self.company_id
                if self.move_type in ('out_invoice', 'out_refund')
                else dest_company
            )
            source_cost = product.with_company(
                cost_company,
            ).standard_price * base_qty
            vals.update({
                'eh_ic_source_move_line_id': source_line.id,
                'eh_ic_source_cost_amount': source_cost,
                'eh_ic_source_cost_date': self.date,
                'eh_ic_source_cost_company_id': cost_company.id,
            })

        # ---- analytic distribution ----
        # Carry the analytic distribution to the mirror, keeping only
        # analytic accounts usable in the destination company. Preserve
        # group-wide allocations while refusing source-company-only leakage.
        mirror_dist = self._eh_mirror_analytic_distribution(
            source_line, dest_company,
        )
        if mirror_dist:
            vals['analytic_distribution'] = mirror_dist

        # ---- tax propagation ----
        if source_line.tax_ids:
            dest_taxes = self._eh_resolve_mirror_taxes(
                source_line.tax_ids, dest_company, target_type,
            )
            if dest_taxes:
                vals['tax_ids'] = [(6, 0, dest_taxes.ids)]
        return vals

    def _eh_resolve_mirror_taxes(self, source_taxes, dest_company,
                                 target_type):
        """Map each source tax to its destination-company equivalent.

        The mirror flips the document direction, so a sale tax on a
        customer invoice maps to a PURCHASE tax on the vendor-bill
        mirror, and a purchase tax maps to a SALE tax on the invoice
        mirror. Matching is on stable, language-independent attributes
        (rate, computation type, price-include flag, and the direction-
        appropriate type_tax_use), never on the tax name, which differs
        across translations and renames. The name is used only to break
        a tie when several destination taxes share the same rate and
        direction.

        A source tax with no destination equivalent is skipped, not
        guessed: it is better to drop it (and log) than to apply a tax
        of the wrong rate or wrong direction, which the previous
        name-or-amount matching could do. The skip is logged so the
        gap is visible rather than silent, since a missing tax
        understates the mirror's GST/VAT.

        Returns an account.tax recordset with one equivalent per source
        tax that resolved.
        """
        Tax = self.env['account.tax'].sudo()
        wanted_use = (
            'purchase' if target_type in ('in_invoice', 'in_refund')
            else 'sale'
        )
        resolved = Tax
        for src in source_taxes:
            candidates = Tax.search([
                ('company_id', '=', dest_company.id),
                ('type_tax_use', '=', wanted_use),
                ('amount_type', '=', src.amount_type),
                ('amount', '=', src.amount),
                ('price_include', '=', src.price_include),
            ])
            match = (
                candidates.filtered(lambda t: t.name == src.name)[:1]
                or candidates.sorted('id')[:1]
            )
            if not match:
                _logger.warning(
                    "Inter-company mirror found no %s tax in company %s "
                    "matching source tax '%s' (rate %s) from company %s; "
                    "the mirror line carries no equivalent tax for it.",
                    wanted_use, dest_company.display_name, src.name,
                    src.amount, src.company_id.display_name,
                )
                continue
            resolved |= match
        return resolved

    def _eh_mirror_analytic_distribution(self, source_line, dest_company):
        """Return the source line's analytic distribution restricted to
        analytic accounts usable in the destination company.

        Analytic distribution keys are comma-joined analytic account ids
        (one per plan). A key is kept only when every account it
        references is group-wide (no company) or belongs to the
        destination company, so a source-company-only analytic account is
        not written onto a destination-company line. Returns False when
        nothing survives the filter.
        """
        dist = source_line.analytic_distribution or {}
        if not dist:
            return False
        Analytic = self.env['account.analytic.account'].sudo()
        all_ids = set()
        for key in dist:
            for part in str(key).split(','):
                part = part.strip()
                if part.isdigit():
                    all_ids.add(int(part))
        if not all_ids:
            return False
        usable = set(
            Analytic.browse(list(all_ids)).filtered(
                lambda a: not a.company_id or a.company_id == dest_company,
            ).ids
        )
        kept = {}
        for key, pct in dist.items():
            ids = [
                int(p) for p in str(key).split(',') if p.strip().isdigit()
            ]
            if ids and all(i in usable for i in ids):
                kept[key] = pct
        return kept or False


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    eh_ic_source_move_line_id = fields.Many2one(
        'account.move.line', readonly=True, copy=False, index=True,
        ondelete='restrict', string="Inter-company source invoice line",
        help=(
            "Exact selling-company invoice line used to build this mirror "
            "line and its immutable sale-date cost snapshot."
        ),
    )
    eh_ic_source_cost_amount = fields.Float(
        readonly=True, copy=False, digits=(16, 6),
        help=(
            "Selling-company functional-currency standard cost captured "
            "when the source posted; later product-cost changes cannot "
            "rewrite elimination margin."
        ),
    )
    eh_ic_source_cost_date = fields.Date(
        readonly=True, copy=False,
        help="Accounting date of the immutable inter-company cost snapshot.",
    )
    eh_ic_source_cost_company_id = fields.Many2one(
        'res.company', readonly=True, copy=False,
        help=(
            "Functional-currency company owning the immutable cost "
            "snapshot."
        ),
    )
    eh_ic_elimination_role = fields.Selection(
        [
            ('unrealised_inventory', "Unrealised-profit inventory"),
            ('unrealised_cogs', "Unrealised-profit cost of sales"),
        ],
        readonly=True, copy=False, index=True,
        help=(
            "Server-owned role identifying point-in-time unrealised-profit "
            "elimination legs for subsequent-period delta calculation."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            'eh_ic_source_move_line_id', 'eh_ic_source_cost_amount',
            'eh_ic_source_cost_date', 'eh_ic_source_cost_company_id',
            'eh_ic_elimination_role',
        }
        if (
            any(protected.intersection(vals or {}) for vals in vals_list)
            and self.env.context.get(_EH_IC_LINK_CONTEXT)
            is not _EH_IC_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Inter-company line provenance is server-owned and cannot "
                "be supplied through create."
            ))
        return super().create(vals_list)

    def write(self, vals):
        protected = {
            'eh_ic_source_move_line_id', 'eh_ic_source_cost_amount',
            'eh_ic_source_cost_date', 'eh_ic_source_cost_company_id',
            'eh_ic_elimination_role',
        }
        if (
            protected.intersection(vals)
            and self.env.context.get(_EH_IC_LINK_CONTEXT)
            is not _EH_IC_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Inter-company line provenance is server-owned and cannot "
                "be changed directly."
            ))
        return super().write(vals)
