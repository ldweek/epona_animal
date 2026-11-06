# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared helpers for inter-company order mirroring.

Both sale.order and purchase.order resolve the destination company and
its inter-company configuration the same way, and create the mirror as
the configured inter-company user (or with elevated rights). This
abstract mixin holds that logic so both sides stay consistent with the
invoice-mirroring engine in eh_account_intercompany.
"""

import math
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.float_utils import float_compare


IC_MIRROR_CONTEXT = 'eh_ic_order_mirror_capability'
# High-entropy process capabilities stay JSON serialisable while remaining
# impossible for an RPC caller to manufacture.  A boolean/string flag is not
# sufficient here: these contexts authorize otherwise forbidden provenance
# and sealed-line mutations.
IC_MIRROR_CAPABILITY = secrets.token_urlsafe(48)
IC_PAIR_TRANSITION_CONTEXT = 'eh_ic_order_pair_transition_capability'
IC_PAIR_TRANSITION_CAPABILITY = secrets.token_urlsafe(48)

# One transaction-scoped advisory-lock namespace for each possible source
# model.  The source order exists before its mirror, so both confirmation and
# every later transition can serialize on the same stable key even while the
# mirror FK is being created.
IC_SALE_SOURCE_LOCK = 719401000000000000
IC_PURCHASE_SOURCE_LOCK = 719402000000000000


class EhIcOrderMixin(models.AbstractModel):
    _name = 'eh.ic.order.mixin'
    _description = "Inter-company order mirroring helpers"

    @api.private
    def _eh_ic_assert_actor_order_access(self):
        """Prove actor ownership of current side before route elevation."""
        self.ensure_one()
        self._eh_check_access('write')
        if (
            not self.env.su
            and self.company_id.id not in self.env.companies.ids
        ):
            raise AccessError(_(
                "The source order company is outside your allowed-company "
                "scope."
            ))
        self.company_id._eh_check_access('read')
        partner = self.partner_id
        if not partner:
            return partner
        partner._eh_check_access('read')
        commercial = partner.commercial_partner_id
        if commercial:
            commercial._eh_check_access('read')
        self.order_line._eh_check_access('read')
        return commercial

    @api.private
    def _eh_ic_dest_company(self):
        """Resolve manager-owned target after proving source-side access."""
        self.ensure_one()
        commercial = self._eh_ic_assert_actor_order_access()
        empty = self.env['res.company'].browse()
        if not commercial:
            return empty
        # Only the manager-controlled controller field is authoritative.
        # Falling back to res.partner.company_id lets an ordinary contact
        # writer manufacture a privileged destination route.
        # Controller is manager-only.  Source accountants may still trigger a
        # preconfigured route without selecting or gaining access to target.
        # Only this restricted field read is elevated here; target writes are
        # separately bound to configured service user below.
        dest = commercial.sudo().eh_represented_company_id.exists()
        if not dest or dest == self.company_id:
            return empty
        return dest

    @api.private
    def _eh_ic_config(self, dest_company):
        return self.env['eh.intercompany.config'].sudo().search(
            [('company_id', '=', dest_company.id), ('enabled', '=', True)],
            limit=1,
        )

    @api.model
    @api.private
    def _eh_ic_has_server_capability(self, context_key, capability):
        supplied = self.env.context.get(context_key)
        return (
            isinstance(supplied, str)
            and secrets.compare_digest(supplied, capability)
        )

    @api.model
    @api.private
    def _eh_ic_apply_user(self, recordset, config):
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
            # Discard caller company context.  Even a broadly privileged
            # configured user operates through this corridor in target only.
            recordset = recordset.with_user(user).with_context(
                allowed_company_ids=[target_company.id],
            ).with_company(target_company)
        else:
            # Preserve legacy system automation only when actor already owns
            # target scope (or call itself is superuser).  A source-only actor
            # must have explicit accountable target service user configured.
            if (
                not self.env.su
                and target_company.id not in self.env.companies.ids
            ):
                raise UserError(_(
                    "Configure an inter-company service user for target "
                    "company %(company)s before source-only users can run "
                    "this route.",
                    company=target_company.display_name,
                ))
            recordset = recordset.sudo().with_context(
                allowed_company_ids=[target_company.id],
            ).with_company(target_company)
        # Object identity, not a truthy context flag, proves the following
        # origin/mirror writes are part of this in-process engine call.
        return recordset.with_context(**{
            IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
        })

    @api.model
    @api.private
    def _eh_ic_is_mirror_engine(self):
        return self._eh_ic_has_server_capability(
            IC_MIRROR_CONTEXT, IC_MIRROR_CAPABILITY,
        )

    @api.model
    @api.private
    def _eh_ic_filter_target_taxes(self, taxes, company):
        """Return taxes valid for target company on every supported series."""
        native_filter = getattr(taxes, '_filter_taxes_by_company', None)
        if native_filter:
            return native_filter(company)
        # Odoo 16/17 use one exact company on account.tax and have no branch-
        # aware helper. Companyless taxes remain shared by core convention.
        if 'company_id' in taxes._fields:
            return taxes.filtered(
                lambda tax: not tax.company_id or tax.company_id == company,
            )
        return taxes

    @api.model
    @api.private
    def _eh_ic_line_taxes(self, line):
        """Return the tax recordset behind either order-line API shape."""
        for field_name in ('tax_ids', 'tax_id', 'taxes_id'):
            if field_name in line._fields:
                return getattr(line, field_name)
        return self.env['account.tax']

    @api.model
    @api.private
    def _eh_ic_tax_summary(self, taxes):
        if not taxes:
            return _("No source taxes")
        parts = []
        for tax in taxes.sorted(lambda item: (item.sequence, item.id)):
            inclusion = _("price included") if tax.price_include else _(
                "price excluded"
            )
            parts.append(_(
                "%(name)s (%(type)s %(amount)s; %(inclusion)s)",
                name=tax.display_name,
                type=tax.amount_type,
                amount=tax.amount,
                inclusion=inclusion,
            ))
        return '; '.join(parts)

    @api.model
    @api.private
    def _eh_ic_target_product(self, source_product, dest_company):
        """Resolve a shared or target-company counterpart product safely.

        Shared products retain identity.  A company-restricted product must
        have exactly one active shared/target-company product with the same
        internal reference and UoM category.  Silent first-match behaviour is
        deliberately rejected because it can post trade to the wrong SKU.
        """
        source_product.ensure_one()
        source_product._eh_check_access('read')
        source = source_product.sudo()
        Product = self.env['product.product'].with_context(active_test=True)
        source_company = source.company_id
        if not source_company or source_company == dest_company:
            target = Product.browse(source.id).exists()
            if not target:
                raise UserError(_(
                    "Product %(product)s is not available to target company "
                    "%(company)s.",
                    product=source.display_name,
                    company=dest_company.display_name,
                ))
            target._eh_check_access('read')
            return target

        internal_reference = source.default_code
        if not internal_reference:
            raise UserError(_(
                "Company-restricted product %(product)s needs an internal "
                "reference and one matching shared or %(company)s product "
                "before this inter-company order can be confirmed.",
                product=source.display_name,
                company=dest_company.display_name,
            ))
        candidates = Product.search([
            ('default_code', '=', internal_reference),
            ('product_tmpl_id.company_id', 'in', [False, dest_company.id]),
        ]).filtered(
            lambda product: self._eh_ic_uoms_compatible(
                product.uom_id, source.uom_id,
            )
        )
        if len(candidates) != 1:
            raise UserError(_(
                "Internal reference %(reference)s for source product "
                "%(product)s resolves to %(count)s eligible products in "
                "target company %(company)s; configure exactly one shared "
                "or target-company counterpart with a compatible UoM.",
                reference=internal_reference,
                product=source.display_name,
                count=len(candidates),
                company=dest_company.display_name,
            ))
        candidates._eh_check_access('read')
        return candidates

    @api.model
    @api.private
    def _eh_ic_uoms_compatible(self, left_uom, right_uom):
        """Compare UoM families across the 16.0--19.0 data models."""
        left_uom.ensure_one()
        right_uom.ensure_one()
        common_reference = getattr(left_uom, '_has_common_reference', None)
        if common_reference:
            return common_reference(right_uom)
        return left_uom.category_id == right_uom.category_id

    @api.model
    @api.private
    def _eh_ic_assert_product_counterpart(
        self, source_product, target_product, dest_company,
    ):
        if source_product == target_product:
            return
        # Pair validation follows server-owned FKs after actor access has
        # already been proved.  Resolve with sudo only for this comparison;
        # mirror creation itself still uses the configured target identity.
        expected = self.sudo()._eh_ic_target_product(
            source_product.sudo(), dest_company.sudo(),
        )
        if expected != target_product:
            raise UserError(_(
                "Inter-company source and mirror products are not the "
                "configured shared/SKU counterpart."
            ))

    @api.model
    @api.private
    def _eh_ic_map_no_variant_values(
        self, source_values, target_product,
    ):
        if not source_values:
            return []
        target_template = target_product.product_tmpl_id
        if source_values.product_tmpl_id == target_template:
            return source_values.ids
        target_values = (
            target_template.attribute_line_ids.product_template_value_ids
        )
        mapped = self.env['product.template.attribute.value']
        for source_value in source_values:
            matches = target_values.filtered(
                lambda target_value: (
                    target_value.product_attribute_value_id
                    == source_value.product_attribute_value_id
                )
            )
            if len(matches) != 1:
                raise UserError(_(
                    "No unique no-variant attribute counterpart exists for "
                    "%(attribute)s on target product %(product)s.",
                    attribute=source_value.display_name,
                    product=target_product.display_name,
                ))
            mapped |= matches
        return mapped.ids

    @api.model
    @api.private
    def _eh_ic_tax_excluded_unit(
        self, taxes, price_unit, currency, product, partner,
    ):
        if not taxes:
            return price_unit
        return taxes.with_context(round=False, round_base=False).compute_all(
            price_unit,
            currency=currency,
            quantity=1.0,
            product=product,
            partner=partner,
        )['total_excluded']

    @api.model
    @api.private
    def _eh_ic_price_unit_for_target_base(
        self, untaxed_unit, taxes, currency, product, partner,
    ):
        """Build the target price from an unrounded tax-excluded base.

        ``handle_price_include=False`` asks every supported tax engine to
        compute from the supplied base.  Add back only taxes which the target
        line says are included in ``price_unit``; excluded taxes remain on top
        of that price.  This handles fixed, division, group and mixed tax sets
        without a rounding-sensitive one-unit numerical probe.
        """
        if not taxes:
            return untaxed_unit
        computed = taxes.with_context(
            round=False, round_base=False,
        ).compute_all(
            untaxed_unit,
            currency=currency,
            quantity=1.0,
            product=product,
            partner=partner,
            handle_price_include=False,
        )
        included_amount = sum(
            item['amount'] for item in computed['taxes']
            if item.get('price_include')
        )
        return untaxed_unit + included_amount

    @api.model
    @api.private
    def _eh_ic_mirror_line_economics(
        self, source_line, target_product, target_taxes, target_currency,
        dest_company, rate_date, target_partner, target_has_discount,
    ):
        source_taxes = self._eh_ic_line_taxes(source_line)
        source_discount = (
            source_line.discount if 'discount' in source_line._fields else 0.0
        )
        discount_factor = 1.0 - (source_discount / 100.0)
        source_effective_price = source_line.price_unit * discount_factor
        source_untaxed = self._eh_ic_tax_excluded_unit(
            source_taxes,
            source_effective_price,
            source_line.currency_id,
            source_line.product_id,
            source_line.order_id.partner_id,
        )
        source_currency = self.env['res.currency'].browse(
            source_line.currency_id.id,
        )
        target_untaxed = source_currency._convert(
            source_untaxed,
            target_currency,
            dest_company,
            rate_date,
            round=False,
        )
        target_effective_price = self._eh_ic_price_unit_for_target_base(
            target_untaxed,
            target_taxes,
            target_currency,
            target_product,
            target_partner,
        )
        if target_has_discount and not math.isclose(
            discount_factor, 0.0, abs_tol=1e-12,
        ):
            target_price = target_effective_price / discount_factor
        else:
            target_price = target_effective_price
            if not target_has_discount:
                source_discount = 0.0
        return (
            target_price,
            source_discount,
            self._eh_ic_tax_summary(source_taxes),
        )

    @api.model
    @api.private
    def _eh_ic_commercial_lines(self, order):
        return order.order_line.filtered(
            lambda line: (
                line.product_id
                and not line.display_type
                and not getattr(line, 'eh_ic_derived_line_kind', False)
            )
        )

    @api.private
    def _eh_ic_is_presentation_state_write(self, vals):
        """Allow only draft-to-sent presentation marking on a bound order.

        Core Print/Send actions persist ``state='sent'``.  It changes neither
        the commercial seal nor legal lifecycle, so the immutable-pair guard
        permits this one idempotent presentation transition after actor ACLs
        and row locks.  Confirmation, cancellation, reset and locking still
        require their controlled pair-wide actions.
        """
        if set(vals) != {'state'} or vals.get('state') != 'sent':
            return False
        self._eh_check_access('write')
        self._eh_ic_lock_commercial_graph()
        for order in self:
            bound = (
                getattr(order, 'eh_ic_mirror_po_id', False)
                or getattr(order, 'eh_ic_origin_po_id', False)
                or getattr(order, 'eh_ic_mirror_so_id', False)
                or getattr(order, 'eh_ic_origin_so_id', False)
            )
            if bound and order.state not in ('draft', 'sent'):
                return False
        return True

    @api.model
    @api.private
    def _eh_ic_transition_context(self):
        return {
            IC_PAIR_TRANSITION_CONTEXT: IC_PAIR_TRANSITION_CAPABILITY,
        }

    @api.model
    @api.private
    def _eh_ic_assert_transition_capability(self, sale, purchase):
        if not (
            sale._eh_ic_has_server_capability(
                IC_PAIR_TRANSITION_CONTEXT, IC_PAIR_TRANSITION_CAPABILITY,
            )
            and purchase._eh_ic_has_server_capability(
                IC_PAIR_TRANSITION_CONTEXT, IC_PAIR_TRANSITION_CAPABILITY,
            )
        ):
            raise AccessError(_(
                "Inter-company pair mutation requires a verified server "
                "transition capability."
            ))

    @api.private
    def _eh_ic_candidate_source_lock_keys(self):
        """Return stable source-order advisory keys for this singleton.

        Provenance fields are server-owned from 1.0.3 onward.  A normal pair
        therefore has exactly one key.  Returning every candidate for a
        malformed row lets us acquire them in global order before rejecting
        the graph, rather than introducing a deadlock while handling corrupt
        legacy data.
        """
        self.ensure_one()
        order = self.sudo()
        keys = set()
        if self._name == 'sale.order':
            if order.eh_ic_origin_po_id:
                keys.add(IC_PURCHASE_SOURCE_LOCK + order.eh_ic_origin_po_id.id)
            if order.eh_ic_mirror_po_id or not order.eh_ic_origin_po_id:
                keys.add(IC_SALE_SOURCE_LOCK + self.id)
        elif self._name == 'purchase.order':
            if order.eh_ic_origin_so_id:
                keys.add(IC_SALE_SOURCE_LOCK + order.eh_ic_origin_so_id.id)
            if order.eh_ic_mirror_so_id or not order.eh_ic_origin_so_id:
                keys.add(IC_PURCHASE_SOURCE_LOCK + self.id)
        else:  # pragma: no cover - the abstract mixin has only two consumers
            raise UserError(_("Unsupported inter-company order model."))
        return sorted(keys)

    @api.private
    def _eh_ic_advisory_lock_source(self):
        """Serialize mirror creation and lifecycle on the stable source."""
        self.ensure_one()
        # Refresh first: a concurrent confirm may have linked the mirror while
        # this request was waiting for actor-level access checks.
        self.invalidate_recordset([
            field_name
            for field_name in (
                'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
                'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            )
            if field_name in self._fields
        ])
        for key in self._eh_ic_candidate_source_lock_keys():
            self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        self.invalidate_recordset([
            field_name
            for field_name in (
                'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
                'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            )
            if field_name in self._fields
        ])

    @api.private
    def _eh_ic_pair_candidates(self):
        """Return server-owned pair references for locked validation."""
        self.ensure_one()
        order = self.sudo()
        if self._name == 'sale.order':
            sale_orders = order
            purchase_orders = (
                order.eh_ic_mirror_po_id | order.eh_ic_origin_po_id
            )
        else:
            sale_orders = order.eh_ic_mirror_so_id | order.eh_ic_origin_so_id
            purchase_orders = order
        return sale_orders, purchase_orders

    @api.model
    @api.private
    def _eh_ic_lock_pair_graph(self, sale_orders, purchase_orders):
        """Lock a pair in one global order: SO header/lines, then PO.

        Actor access to current order is proved before this helper.  Pair FKs
        and raw locks are server-controlled, then reciprocal provenance is
        validated before any result can become a transition capability.
        """
        if sale_orders:
            self.env.cr.execute(
                "SELECT id FROM sale_order WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (sorted(sale_orders.ids),),
            )
            self.env.cr.execute(
                "SELECT id FROM sale_order_line WHERE order_id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (sorted(sale_orders.ids),),
            )
        if purchase_orders:
            self.env.cr.execute(
                "SELECT id FROM purchase_order WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (sorted(purchase_orders.ids),),
            )
            self.env.cr.execute(
                "SELECT id FROM purchase_order_line WHERE order_id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                (sorted(purchase_orders.ids),),
            )

        sale_fields = [
            'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
            'eh_ic_provenance_verified', 'eh_ic_integrity_review_reason',
            'state', 'order_line', 'partner_id', 'company_id',
        ]
        if 'locked' in sale_orders._fields:
            sale_fields.append('locked')
        sale_orders.invalidate_recordset(sale_fields)
        purchase_fields = [
            'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            'eh_ic_provenance_verified', 'eh_ic_integrity_review_reason',
            'state', 'order_line', 'partner_id', 'company_id',
        ]
        if 'locked' in purchase_orders._fields:
            purchase_fields.append('locked')
        purchase_orders.invalidate_recordset(purchase_fields)
        sale_line_fields = set(
            sale_orders.order_line._eh_ic_commercial_fields()
        ) | {'eh_ic_derived_line_kind', 'eh_ic_source_tax_summary'}
        purchase_line_fields = set(
            purchase_orders.order_line._eh_ic_commercial_fields()
        ) | {'eh_ic_derived_line_kind', 'eh_ic_source_tax_summary'}
        sale_orders.order_line.invalidate_recordset([
            name for name in sale_line_fields
            if name in sale_orders.order_line._fields
        ])
        purchase_orders.order_line.invalidate_recordset([
            name for name in purchase_line_fields
            if name in purchase_orders.order_line._fields
        ])

    @api.model
    @api.private
    def _eh_ic_assert_pair_provenance(
        self, sale, purchase, require_verified=True,
    ):
        """Validate reciprocal engine provenance and immutable line scope."""
        sale.ensure_one()
        purchase.ensure_one()
        if require_verified and not (
            sale.eh_ic_provenance_verified
            and purchase.eh_ic_provenance_verified
            and not sale.eh_ic_integrity_review_reason
            and not purchase.eh_ic_integrity_review_reason
        ):
            raise UserError(_(
                "Legacy or quarantined inter-company provenance on %(sale)s "
                "and %(purchase)s requires controlled review before any "
                "further lifecycle or accounting action.",
                sale=sale.display_name,
                purchase=purchase.display_name,
            ))

        sale_source = (
            sale.eh_ic_mirror_po_id == purchase
            and not sale.eh_ic_origin_po_id
            and purchase.eh_ic_origin_so_id == sale
            and not purchase.eh_ic_mirror_so_id
        )
        purchase_source = (
            purchase.eh_ic_mirror_so_id == sale
            and not purchase.eh_ic_origin_so_id
            and sale.eh_ic_origin_po_id == purchase
            and not sale.eh_ic_mirror_po_id
        )
        if sale_source == purchase_source:
            raise UserError(_(
                "Inter-company order provenance is not exactly reciprocal; "
                "the pair is blocked for controlled review."
            ))
        if sale.company_id == purchase.company_id:
            raise UserError(_(
                "An inter-company SO/PO pair cannot belong to one company."
            ))

        # Read manager-owned routing only after current-order actor access and
        # reciprocal server provenance have been established.
        if sale_source:
            represented = (
                sale.partner_id.commercial_partner_id.sudo()
                .eh_represented_company_id
            )
            target_partner = purchase.partner_id.commercial_partner_id
            source_partner = sale.company_id.partner_id.commercial_partner_id
            expected_company = purchase.company_id
        else:
            represented = (
                purchase.partner_id.commercial_partner_id.sudo()
                .eh_represented_company_id
            )
            target_partner = sale.partner_id.commercial_partner_id
            source_partner = purchase.company_id.partner_id.commercial_partner_id
            expected_company = sale.company_id
        if represented != expected_company or target_partner != source_partner:
            raise UserError(_(
                "Inter-company partner routing no longer matches the sealed "
                "source/mirror company provenance."
            ))

        sale_lines = self._eh_ic_commercial_lines(sale)
        purchase_lines = self._eh_ic_commercial_lines(purchase)
        if len(sale_lines) != len(purchase_lines):
            raise UserError(_(
                "Inter-company source and mirror line scopes differ; the "
                "pair is blocked for controlled review."
            ))
        for sale_line, purchase_line in zip(sale_lines, purchase_lines):
            if sale.eh_ic_mirror_po_id == purchase:
                source_product = sale_line.product_id
                target_product = purchase_line.product_id
                target_company = purchase.company_id
            else:
                source_product = purchase_line.product_id
                target_product = sale_line.product_id
                target_company = sale.company_id
            self._eh_ic_assert_product_counterpart(
                source_product, target_product, target_company,
            )
            converted_qty = sale_line.product_uom_id._compute_quantity(
                sale_line.product_uom_qty,
                purchase_line.product_uom_id,
                round=False,
            )
            if (
                float_compare(
                    converted_qty,
                    purchase_line.product_qty,
                    precision_rounding=purchase_line.product_uom_id.rounding,
                )
            ):
                raise UserError(_(
                    "Inter-company source and mirror product/quantity scope "
                    "differs; the pair is blocked for controlled review."
                ))

    @api.model
    @api.private
    def _eh_ic_assert_pair_lifecycle_shape(self, sale, purchase):
        """Reject a legacy pair whose lifecycle cannot be safely attested."""
        sale_states = {'draft', 'sent', 'sale', 'done', 'cancel'}
        purchase_states = {
            'draft', 'sent', 'to approve', 'purchase', 'done', 'cancel',
        }
        if sale.state not in sale_states or purchase.state not in purchase_states:
            raise UserError(_(
                "The inter-company pair has an unsupported lifecycle state and "
                "cannot be released from quarantine."
            ))
        if (sale.state == 'cancel') != (purchase.state == 'cancel'):
            raise UserError(_(
                "A partly cancelled inter-company pair cannot be released from "
                "quarantine. Reconstruct and attest the pair outside this "
                "automatic workflow."
            ))
        if (
            self._eh_ic_pair_fully_confirmed(sale, purchase)
            and self._eh_ic_order_locked(sale)
            != self._eh_ic_order_locked(purchase)
        ):
            raise UserError(_(
                "A confirmed inter-company pair with divergent lock states "
                "cannot be released from quarantine."
            ))

    @api.model
    @api.private
    def _eh_ic_assert_integrity_reviewer(self):
        user = self.env.user
        if not self.env.su and not (
            user.has_group('eh_account_base.group_eh_manager')
            and user.has_group('sales_team.group_sale_manager')
            and user.has_group('purchase.group_purchase_manager')
        ):
            raise AccessError(_(
                "Inter-company integrity review requires EH, Sales, and "
                "Purchase manager roles."
            ))

    def action_eh_ic_open_integrity_review(self):
        """Open the explicit attestation step for a quarantined legacy pair."""
        self.ensure_one()
        self._eh_ic_assert_integrity_reviewer()
        self._eh_ic_assert_actor_order_access()
        if not (
            self.eh_ic_integrity_review_reason
            or not self.eh_ic_provenance_verified
        ):
            raise UserError(_(
                "This inter-company pair is already verified."
            ))
        context = dict(self.env.context)
        if self._name == 'sale.order':
            context['default_sale_order_id'] = self.id
        else:
            context['default_purchase_order_id'] = self.id
        return {
            'type': 'ir.actions.act_window',
            'name': _("Review inter-company pair"),
            'res_model': 'eh.ic.integrity.review.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': context,
        }

    @api.private
    def _eh_ic_review_integrity_pair(self, review_note):
        """Attest one structurally valid quarantined pair, never repair it.

        A reviewer must independently own both companies and both functional
        documents.  The workflow only removes quarantine after deterministic
        locks and the same reciprocal route/product/quantity checks used by
        live lifecycle and accounting actions; ambiguous or damaged links are
        deliberately left frozen.
        """
        self.ensure_one()
        self._eh_ic_assert_integrity_reviewer()
        note = (review_note or '').strip()
        if not note:
            raise ValidationError(_(
                "Record the evidence and conclusion for this integrity review."
            ))
        self._eh_ic_assert_actor_order_access()
        self._eh_ic_advisory_lock_source()
        sale_orders, purchase_orders = self._eh_ic_pair_candidates()
        self._eh_ic_lock_pair_graph(sale_orders, purchase_orders)
        self.invalidate_recordset([
            field_name
            for field_name in (
                'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
                'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            )
            if field_name in self._fields
        ])
        sale_orders, purchase_orders = self._eh_ic_pair_candidates()
        if len(sale_orders) != 1 or len(purchase_orders) != 1:
            raise UserError(_(
                "Only one complete, unambiguous SO/PO pair can be released "
                "from quarantine."
            ))
        sale = sale_orders.ensure_one()
        purchase = purchase_orders.ensure_one()
        actor_sale, actor_purchase = self._eh_ic_bind_actor_pair(
            sale, purchase,
        )
        self._eh_ic_assert_pair_provenance(
            sale, purchase, require_verified=False,
        )
        self._eh_ic_assert_pair_lifecycle_shape(sale, purchase)
        reviewed_at = fields.Datetime.now()
        review_vals = {
            'eh_ic_provenance_verified': True,
            'eh_ic_integrity_review_reason': False,
            'eh_ic_integrity_review_note': note,
            'eh_ic_integrity_reviewed_by_id': self.env.user.id,
            'eh_ic_integrity_reviewed_at': reviewed_at,
        }
        engine_context = {IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        actor_sale.with_company(sale.company_id).with_context(
            **engine_context
        ).write(review_vals)
        actor_purchase.with_company(purchase.company_id).with_context(
            **engine_context
        ).write(review_vals)
        body = _(
            "Inter-company pair integrity attested by %(reviewer)s. "
            "Evidence: %(note)s",
            reviewer=self.env.user.display_name,
            note=note,
        )
        actor_sale.message_post(body=body)
        actor_purchase.message_post(body=body)
        return True

    @api.private
    def _eh_ic_lifecycle_pair(self):
        """Lock and validate pair for read-only lifecycle decisions.

        Returned records use server read context.  Call
        ``_eh_ic_transition_pair`` for any operation that writes both sides.
        """
        self.ensure_one()
        self._eh_ic_assert_actor_order_access()
        self._eh_ic_advisory_lock_source()

        sale_orders, purchase_orders = self._eh_ic_pair_candidates()
        self._eh_ic_lock_pair_graph(sale_orders, purchase_orders)

        # Re-resolve after the deterministic locks.  This is essential for a
        # cancellation that queued behind the source confirmation which
        # created the mirror FK.
        self.invalidate_recordset([
            field_name
            for field_name in (
                'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
                'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
            )
            if field_name in self._fields
        ])
        sale_orders, purchase_orders = self._eh_ic_pair_candidates()
        if not sale_orders or not purchase_orders:
            return self.env['sale.order'], self.env['purchase.order']
        if len(sale_orders) != 1 or len(purchase_orders) != 1:
            raise UserError(_(
                "Inter-company order provenance references an incomplete or "
                "ambiguous pair; controlled review is required."
            ))
        sale = sale_orders.ensure_one()
        purchase = purchase_orders.ensure_one()
        self._eh_ic_assert_pair_provenance(sale, purchase)
        return sale, purchase

    @api.model
    @api.private
    def _eh_ic_bind_actor_pair(self, sale, purchase):
        """Bind both sides to actor after full dual-company access checks."""
        if not self.env.su:
            company_ids = {sale.company_id.id, purchase.company_id.id}
            if company_ids - set(self.env.companies.ids):
                raise AccessError(_(
                    "Every company in an inter-company order pair must be "
                    "in your allowed-company scope."
                ))
        actor_sale = self.env['sale.order'].browse(sale.id)
        actor_purchase = self.env['purchase.order'].browse(purchase.id)
        actor_sale._eh_check_access('write')
        actor_purchase._eh_check_access('write')
        actor_sale.company_id._eh_check_access('read')
        actor_purchase.company_id._eh_check_access('read')
        actor_sale.partner_id._eh_check_access('read')
        actor_purchase.partner_id._eh_check_access('read')
        actor_sale.order_line._eh_check_access('read')
        actor_purchase.order_line._eh_check_access('read')
        transition_context = self._eh_ic_transition_context()
        return (
            actor_sale.with_context(**transition_context),
            actor_purchase.with_context(**transition_context),
        )

    @api.model
    @api.private
    def _eh_ic_pair_source_target(self, sale, purchase):
        """Return canonical actor source and configured service target."""
        if (
            sale.eh_ic_mirror_po_id == purchase
            and not sale.eh_ic_origin_po_id
        ):
            return sale, purchase
        return purchase, sale

    @api.private
    def _eh_ic_transition_pair(self, require_full_actor=False):
        """Authorize pair write through dual actor or source/service split.

        Full dual-company actors keep normal access.  Otherwise only actor on
        canonical source may enter corridor, and configured service user is
        bound to target company only.  Target-side actors cannot use service
        identity to write canonical source.
        """
        self.ensure_one()
        sale, purchase = self._eh_ic_lifecycle_pair()
        if not sale or not purchase:
            return self.env['sale.order'], self.env['purchase.order']
        try:
            return self._eh_ic_bind_actor_pair(sale, purchase)
        except AccessError:
            if require_full_actor:
                raise

        source, target = self._eh_ic_pair_source_target(sale, purchase)
        if self._name != source._name or self.id != source.id:
            raise AccessError(_(
                "A target-side user cannot mutate canonical inter-company "
                "source. Run pair-wide transition from source order or use "
                "an actor with access to both companies."
            ))

        source_actor = self.env[source._name].browse(source.id)
        source_actor._eh_ic_assert_actor_order_access()
        config = self._eh_ic_config(target.company_id)
        if not config:
            raise UserError(_(
                "Inter-company route for target company %(company)s is "
                "disabled or missing; pair-wide transition is blocked.",
                company=target.company_id.display_name,
            ))
        if not config.intercompany_user_id:
            raise UserError(_(
                "Configure an inter-company service user for target company "
                "%(company)s before a source-only actor can change both sides.",
                company=target.company_id.display_name,
            ))

        target_service = self._eh_ic_apply_user(
            self.env[target._name], config,
        ).browse(target.id)
        target_service._eh_check_access('write')
        target_service.company_id._eh_check_access('read')
        target_service.partner_id._eh_check_access('read')
        target_service.order_line._eh_check_access('read')
        transition_context = self._eh_ic_transition_context()
        source_actor = source_actor.with_context(**transition_context)
        target_service = target_service.with_context(**transition_context)
        if source._name == 'sale.order':
            return source_actor, target_service
        return target_service, source_actor

    @api.model
    @api.private
    def _eh_ic_assert_not_cancelled(self, sale, purchase):
        if sale.state == 'cancel' or purchase.state == 'cancel':
            raise UserError(_(
                "A cancelled inter-company order cannot leave or create a "
                "live counterpart. Reset the whole pair to draft first."
            ))

    @api.model
    @api.private
    def _eh_ic_order_locked(self, order):
        """Map series-specific lock evidence to one boolean."""
        order.ensure_one()
        if 'locked' in order._fields:
            return bool(order.locked)
        return order.state == 'done'

    @api.model
    @api.private
    def _eh_ic_pair_fully_confirmed(self, sale, purchase):
        return (
            sale.state in ('sale', 'done')
            and purchase.state in ('purchase', 'done')
        )

    @api.model
    @api.private
    def _eh_ic_cancel_pair(self, sale, purchase):
        self._eh_ic_assert_transition_capability(sale, purchase)
        if (
            self._eh_ic_order_locked(sale)
            or self._eh_ic_order_locked(purchase)
        ):
            raise UserError(_(
                "Unlock the complete inter-company pair before cancelling it."
            ))
        if sale.state != 'cancel':
            sale.with_company(sale.company_id).with_context(
                **{
                    IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY,
                    # Odoo <=18 otherwise opens a sale-cancellation warning
                    # wizard instead of synchronous pair transition.
                    'disable_cancel_warning': True,
                }
            ).action_cancel()
        if purchase.state != 'cancel':
            purchase.with_company(purchase.company_id).with_context(
                **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
            ).button_cancel()
        sale.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in sale._fields
        ])
        purchase.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in purchase._fields
        ])
        if sale.state != 'cancel' or purchase.state != 'cancel':
            raise UserError(_(
                "The complete inter-company pair could not be cancelled "
                "atomically; no lifecycle change was kept."
            ))

    @api.model
    @api.private
    def _eh_ic_reset_pair(self, sale, purchase):
        self._eh_ic_assert_transition_capability(sale, purchase)
        if sale.state != 'cancel' or purchase.state != 'cancel':
            raise UserError(_(
                "Only a completely cancelled inter-company pair can be "
                "reset to draft."
            ))
        sale.with_company(sale.company_id).with_context(
            **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        ).action_draft()
        purchase.with_company(purchase.company_id).with_context(
            **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        ).button_draft()
        sale.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in sale._fields
        ])
        purchase.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in purchase._fields
        ])
        if sale.state != 'draft' or purchase.state != 'draft':
            raise UserError(_(
                "The complete inter-company pair could not be reset "
                "atomically; no lifecycle change was kept."
            ))

    @api.model
    @api.private
    def _eh_ic_set_pair_locked(self, sale, purchase, locked):
        self._eh_ic_assert_transition_capability(sale, purchase)
        self._eh_ic_assert_not_cancelled(sale, purchase)
        if locked and not self._eh_ic_pair_fully_confirmed(sale, purchase):
            raise UserError(_(
                "Both sides of an inter-company pair must be confirmed "
                "before it can be locked. A pending pair may only be "
                "unlocked or cancelled after unlocking."
            ))
        sale_action = (
            ('action_lock' if 'locked' in sale._fields else 'action_done')
            if locked else 'action_unlock'
        )
        purchase_action = (
            ('button_lock' if 'locked' in purchase._fields else 'button_done')
            if locked else 'button_unlock'
        )
        getattr(
            sale.with_company(sale.company_id).with_context(
                **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
            ),
            sale_action,
        )()
        getattr(
            purchase.with_company(purchase.company_id).with_context(
                **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
            ),
            purchase_action,
        )()
        sale.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in sale._fields
        ])
        purchase.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in purchase._fields
        ])
        if (
            self._eh_ic_order_locked(sale) != locked
            or self._eh_ic_order_locked(purchase) != locked
        ):
            raise UserError(_(
                "The complete inter-company pair could not be %(action)s "
                "atomically; no lifecycle change was kept.",
                action=_("locked") if locked else _("unlocked"),
            ))

    @api.model
    @api.private
    def _eh_ic_sync_pair_lock_after_confirmation(self, sale, purchase):
        """Once both sides are active, preserve the stricter lock state."""
        sale.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in sale._fields
        ])
        purchase.invalidate_recordset([
            field_name for field_name in ('state', 'locked')
            if field_name in purchase._fields
        ])
        locked = (
            self._eh_ic_order_locked(sale)
            or self._eh_ic_order_locked(purchase)
        )
        # Confirming current side needs no counterpart write when both sides
        # remain unlocked.  This keeps ordinary target reviewer path local.
        if not self._eh_ic_pair_fully_confirmed(sale, purchase) or not locked:
            return
        transition_sale, transition_purchase = self._eh_ic_transition_pair()
        self._eh_ic_set_pair_locked(
            transition_sale, transition_purchase, True,
        )

    @api.model
    @api.private
    def _eh_ic_manual_fulfillment_lines(self, line):
        """Resolve one bound line to its reciprocal line under pair locks."""
        line.ensure_one()
        line._eh_check_access('write')
        order = line.order_id
        sale, purchase = order._eh_ic_transition_pair(
            require_full_actor=True,
        )
        if not sale or not purchase:
            raise UserError(_(
                "Manual inter-company fulfilment is available only on a "
                "durably bound SO/PO pair."
            ))
        order._eh_ic_assert_not_cancelled(sale, purchase)
        if not order._eh_ic_pair_fully_confirmed(sale, purchase):
            raise UserError(_(
                "Both sides of the inter-company pair must be confirmed "
                "before manual fulfilment can be recorded."
            ))
        if (
            self._eh_ic_order_locked(sale)
            or self._eh_ic_order_locked(purchase)
        ):
            raise UserError(_(
                "Unlock the complete inter-company pair before recording "
                "manual fulfilment."
            ))

        sale_lines = sale.order_line.filtered(
            lambda candidate: candidate.product_id
            and not candidate.display_type
        )
        purchase_lines = purchase.order_line.filtered(
            lambda candidate: candidate.product_id
            and not candidate.display_type
        )
        source_lines = sale_lines if line._name == 'sale.order.line' \
            else purchase_lines
        try:
            position = source_lines.ids.index(line.id)
        except ValueError as exc:  # pragma: no cover - guarded by provenance
            raise UserError(_(
                "The requested line is outside the sealed pair scope."
            )) from exc
        return sale_lines[position], purchase_lines[position]

    @api.model
    @api.private
    def _eh_ic_set_manual_fulfillment(self, line, quantity):
        """Atomically attest matching manual delivery and receipt anchors.

        Direct writes remain forbidden.  This narrow server path is callable
        only through the public line action after proving dual functional
        management, full pair access, durable reciprocal provenance, active
        unlocked states, manual methods, range, and monotonicity.
        """
        user = self.env.user
        if not self.env.su and not (
            user.has_group('eh_account_base.group_eh_manager')
            and user.has_group('sales_team.group_sale_manager')
            and user.has_group('purchase.group_purchase_manager')
        ):
            raise AccessError(_(
                "Manual inter-company fulfilment requires EH, Sales, and "
                "Purchase manager roles."
            ))
        try:
            requested_qty = float(quantity)
        except (TypeError, ValueError) as exc:
            raise ValidationError(_(
                "Manual fulfilment quantity must be a finite number."
            )) from exc
        if not math.isfinite(requested_qty):
            raise ValidationError(_(
                "Manual fulfilment quantity must be a finite number."
            ))

        sale_line, purchase_line = self._eh_ic_manual_fulfillment_lines(line)
        if (
            sale_line.qty_delivered_method != 'manual'
            or purchase_line.qty_received_method != 'manual'
        ):
            raise UserError(_(
                "This pair is fulfilled by a stock, analytic, timesheet, or "
                "other computed engine; manual quantities are forbidden."
            ))

        if line._name == 'sale.order.line':
            sale_qty = requested_qty
            purchase_qty = sale_line.product_uom_id._compute_quantity(
                requested_qty,
                purchase_line.product_uom_id,
                round=False,
            )
        else:
            purchase_qty = requested_qty
            sale_qty = purchase_line.product_uom_id._compute_quantity(
                requested_qty,
                sale_line.product_uom_id,
                round=False,
            )
        if (
            float_compare(
                sale_qty, 0.0,
                precision_rounding=sale_line.product_uom_id.rounding,
            ) < 0
            or float_compare(
                sale_qty, sale_line.product_uom_qty,
                precision_rounding=sale_line.product_uom_id.rounding,
            ) > 0
            or float_compare(
                purchase_qty, 0.0,
                precision_rounding=purchase_line.product_uom_id.rounding,
            ) < 0
            or float_compare(
                purchase_qty, purchase_line.product_qty,
                precision_rounding=purchase_line.product_uom_id.rounding,
            ) > 0
        ):
            raise ValidationError(_(
                "Manual fulfilment must stay between zero and the sealed "
                "ordered quantity on both sides."
            ))
        if (
            float_compare(
                sale_qty, sale_line.qty_delivered,
                precision_rounding=sale_line.product_uom_id.rounding,
            ) < 0
            or float_compare(
                purchase_qty,
                max(
                    purchase_line.qty_received,
                    purchase_line.qty_received_manual,
                ),
                precision_rounding=purchase_line.product_uom_id.rounding,
            ) < 0
        ):
            raise ValidationError(_(
                "Manual inter-company fulfilment is monotonic; use a "
                "controlled return/correction workflow instead of reducing "
                "the attested quantity."
            ))

        sale_line.with_context(
            **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        ).write({
            'qty_delivered': sale_qty,
        })
        purchase_line.with_context(
            **{IC_MIRROR_CONTEXT: IC_MIRROR_CAPABILITY}
        ).write({
            'qty_received': purchase_qty,
            'qty_received_manual': purchase_qty,
        })
        return True
