# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Automatic asset creation from posted vendor bills.

When a vendor bill is posted, every invoice line whose account carries an
asset category (eh_asset_category_id) with a creation mode other than 'no'
spawns a fixed asset from the category defaults. The created asset is
linked back on the line (eh_asset_id) so a reset-and-repost never
duplicates it.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


# An identity sentinel, rather than a boolean/string context flag, prevents a
# JSON-RPC caller from manufacturing the narrow capability used by this
# module's bill-posting engine.
_ASSET_AUTOCREATE_CONTEXT_KEY = 'eh_asset_autocreate_capability'
_ASSET_AUTOCREATE_CAPABILITY = object()


def _asset_engine_context(recordset):
    return recordset.sudo().with_context(**{
        _ASSET_AUTOCREATE_CONTEXT_KEY: _ASSET_AUTOCREATE_CAPABILITY,
    })


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    eh_asset_id = fields.Many2one(
        'eh.asset', string="Generated Asset", copy=False, readonly=True,
        ondelete='set null', check_company=True,
        help="Asset auto-created from this bill line, if any.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_asset_id') for vals in vals_list) \
                and self.env.context.get(_ASSET_AUTOCREATE_CONTEXT_KEY) \
                is not _ASSET_AUTOCREATE_CAPABILITY:
            raise AccessError(_(
                "Generated Asset is server-owned provenance. It can only be "
                "set by vendor-bill posting."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_asset_id' in vals \
                and self.env.context.get(_ASSET_AUTOCREATE_CONTEXT_KEY) \
                is not _ASSET_AUTOCREATE_CAPABILITY:
            raise AccessError(_(
                "Generated Asset is server-owned provenance. It can only be "
                "set by vendor-bill posting."
            ))
        return super().write(vals)

    def unlink(self):
        generated = self.filtered('eh_asset_id')
        if generated:
            if not self.env.su:
                self._eh_check_access('unlink')
            generated._eh_lock_asset_origin_lines()
            assets = generated.mapped('eh_asset_id')
            assets._eh_lock_for_transition()
            unsafe = assets.filtered(lambda asset: asset._eh_has_ledger_evidence())
            if unsafe:
                raise UserError(_(
                    "A vendor-bill line that generated an asset with ledger "
                    "evidence cannot be deleted. Reverse/correct the asset "
                    "through its accounting workflow first: %s.",
                    ', '.join(unsafe.mapped('display_name')),
                ))
            # Draft generated assets have no independent evidence. Remove the
            # dependent record before the origin line so no orphan survives a
            # draft bill edit.
            _asset_engine_context(assets).unlink()
        return super().unlink()

    def _eh_lock_asset_origin_lines(self):
        if self.ids:
            self.env.cr.execute(
                'SELECT id FROM account_move_line WHERE id IN %s '
                'ORDER BY id FOR UPDATE',
                (tuple(self.ids),),
            )
            self.invalidate_recordset(['eh_asset_id', 'move_id', 'account_id'])
        return self


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        posted._eh_quarantine_reversed_asset_origins()
        posted._eh_autocreate_assets()
        return posted

    def button_draft(self):
        generated_lines = self.mapped('invoice_line_ids').filtered('eh_asset_id')
        if generated_lines:
            if not self.env.su:
                self._eh_check_access('write')
            generated_lines._eh_lock_asset_origin_lines()
            assets = generated_lines.mapped('eh_asset_id')
            assets._eh_lock_for_transition()
            unsafe = assets.filtered(lambda asset: asset._eh_has_ledger_evidence())
            if unsafe:
                raise UserError(_(
                    "This bill generated assets that already have ledger "
                    "evidence and therefore cannot be reset or cancelled in "
                    "place. Post a reversal and correct the asset through an "
                    "explicit accounting workflow: %s.",
                    ', '.join(unsafe.mapped('display_name')),
                ))
        result = super().button_draft()
        if generated_lines:
            # With no asset-side ledger evidence, reverting the dependent
            # schedule is safe. Reposting re-reads the final bill values under
            # the same line+asset locks and never duplicates the asset.
            for asset in generated_lines.mapped('eh_asset_id'):
                asset_engine = _asset_engine_context(asset)
                asset_engine._wipe_unposted_lines()
                asset_engine.write({'state': 'draft'})
        return result

    def _eh_autocreate_assets(self):
        """Create assets for posted vendor-bill lines tagged for it."""
        Asset = _asset_engine_context(self.env['eh.asset'])
        for move in self:
            if move.move_type != 'in_invoice' or move.state != 'posted':
                continue
            actor_move = move.sudo(False)
            actor_move._eh_check_access('read')
            if move.company_id not in actor_move.env.companies:
                raise AccessError(_(
                    "The vendor bill company is outside the posting user's "
                    "active companies."
                ))
            lines = move.invoice_line_ids
            if lines:
                lines.sudo(False)._eh_check_access('read')
                lines._eh_lock_asset_origin_lines()
            for line in lines:
                account = line.account_id
                category = account.eh_asset_category_id
                existing = line.eh_asset_id
                if existing:
                    existing._eh_lock_for_transition()
                    existing.invalidate_recordset([
                        'invoice_id', 'invoice_line_id', 'category_id',
                        'acquisition_cost', 'state',
                    ])
                    if existing.invoice_id != move \
                            or existing.invoice_line_id != line:
                        raise UserError(_(
                            "Generated asset provenance is inconsistent for "
                            "bill line %(line)s; the line and asset must point "
                            "to one another and to the same vendor bill.",
                            line=line.display_name,
                        ))
                if not category or account.eh_asset_auto == 'no':
                    if existing:
                        if existing._eh_has_ledger_evidence():
                            raise UserError(_(
                                "Bill line %(line)s no longer has an asset "
                                "creation policy, but generated asset "
                                "%(asset)s already has ledger evidence. "
                                "Restore the policy or correct it explicitly.",
                                line=line.display_name,
                                asset=existing.display_name,
                            ))
                        Asset.browse(existing.id).unlink()
                    continue
                category.sudo(False)._eh_check_access('read')
                account.sudo(False)._eh_check_access('read')
                # price_subtotal is expressed in invoice currency. Assets Pro
                # is a company-currency subledger, so use the posted balance
                # that actually hit the asset account instead.
                cost = move.company_id.currency_id.round(line.balance)
                if cost <= 0:
                    continue
                in_service = move.invoice_date or move.date
                asset_vals = self._eh_asset_vals_from_line(
                    move, line, category, cost, in_service,
                )
                if existing:
                    material = {
                        key: value for key, value in asset_vals.items()
                        if key not in ('name',)
                    }
                    mismatched = [
                        key for key, value in material.items()
                        if self._eh_asset_field_value(existing, key) != value
                    ]
                    if mismatched and existing._eh_has_ledger_evidence():
                        raise UserError(_(
                            "Posted bill values no longer match the immutable "
                            "basis of generated asset %(asset)s (%(fields)s). "
                            "Use an explicit correction/reversal workflow.",
                            asset=existing.display_name,
                            fields=', '.join(sorted(mismatched)),
                        ))
                    if mismatched:
                        existing_engine = _asset_engine_context(existing)
                        existing_engine._wipe_unposted_lines()
                        existing_engine.write(material)
                    asset = existing
                else:
                    asset = Asset.create(asset_vals)
                    _asset_engine_context(line).write({'eh_asset_id': asset.id})
                if account.eh_asset_auto == 'validate':
                    self._eh_try_validate_asset(asset)

    @staticmethod
    def _eh_asset_field_value(asset, field_name):
        value = asset[field_name]
        return value.id if hasattr(value, 'id') else value

    @staticmethod
    def _eh_asset_vals_from_line(move, line, category, cost, in_service):
        return {
            'name': '/',
            'category_id': category.id,
            'partner_id': move.partner_id.id,
            'invoice_id': move.id,
            'invoice_line_id': line.id,
            'acquisition_date': in_service,
            'in_service_date': in_service,
            'acquisition_cost': cost,
            'method': category.method,
            'useful_life_months': category.useful_life_months,
            'salvage_value': category.salvage_rate * cost,
            'declining_factor': category.declining_factor,
            'prorate_first_period': category.prorate_first_period,
            'prorata_mode': category.prorata_mode,
            'asset_account_id': category.asset_account_id.id,
            'depreciation_account_id': category.depreciation_account_id.id,
            'accumulated_depreciation_account_id': (
                category.accumulated_depreciation_account_id.id),
            'disposal_gain_account_id': category.disposal_gain_account_id.id,
            'disposal_loss_account_id': category.disposal_loss_account_id.id,
            'journal_id': category.journal_id.id,
            'company_id': move.company_id.id,
        }

    def _eh_try_validate_asset(self, asset):
        """Generate the schedule and start the asset, but never let an
        incomplete asset setup roll back the bill posting: on failure the
        asset is left in draft for the user to finish."""
        try:
            with self.env.cr.savepoint():
                asset.action_compute_schedule()
                asset.action_activate()
        except UserError as exc:
            asset.message_post(body=_(
                "Auto-validation skipped; asset left in draft: %s", exc))

    def _eh_quarantine_reversed_asset_origins(self):
        """Retain both bill entries and freeze their dependent assets.

        A posted reversal must never leave the original generated asset live.
        We retain the original bill, its reversal, and the exact linkage as
        immutable evidence; follow-on asset postings fail closed until an
        explicit correction workflow resolves the quarantined source.
        """
        reversals = self.filtered(
            lambda move: move.state == 'posted' and move.reversed_entry_id,
        )
        for reversal in reversals:
            source = reversal.reversed_entry_id
            lines = reversal._eh_reversed_asset_origin_lines(source)
            if not lines:
                continue
            lines._eh_lock_asset_origin_lines()
            assets = lines.mapped('eh_asset_id')
            assets._eh_lock_for_transition()
            for asset in assets:
                if asset.origin_reversal_move_id \
                        and asset.origin_reversal_move_id != reversal:
                    raise UserError(_(
                        "Asset %s is already bound to a different source-bill "
                        "reversal.", asset.display_name,
                    ))
                engine_asset = _asset_engine_context(asset)
                if not asset._eh_has_ledger_evidence():
                    engine_asset._wipe_unposted_lines()
                    engine_asset.write({'state': 'draft'})
                engine_asset.write({
                    'origin_source_quarantined': True,
                    'origin_source_quarantine_note': _(
                        "Origin vendor bill %(bill)s was reversed by "
                        "%(reversal)s. Original and reversal GL evidence is "
                        "retained; resolve the asset through an explicit "
                        "accounting correction workflow.",
                        bill=source.display_name,
                        reversal=reversal.display_name,
                    ),
                    'origin_reversal_move_id': reversal.id,
                    'origin_reversed_at': fields.Datetime.now(),
                    'origin_reversed_by_id': self.env.user.id,
                })

    def _eh_reversed_asset_origin_lines(self, source):
        """Identify exact or conservatively ambiguous credited asset lines.

        ``reversed_entry_id`` is move-level provenance; it does not mean every
        product line was credited. Match the reversal's actual product lines to
        source lines by purchase-line identity when available, otherwise by a
        stable accounting fingerprint (product, account and taxes). If that
        fingerprint represents several asset lines, quarantine that ambiguous
        group only -- never unrelated lines elsewhere on the bill.
        """
        self.ensure_one()
        source_asset_lines = source.invoice_line_ids.filtered('eh_asset_id')
        reversal_lines = self.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product'
            and not line.currency_id.is_zero(line.balance),
        )
        if not source_asset_lines or not reversal_lines:
            return source_asset_lines.browse([])

        matched = source_asset_lines.browse([])
        for credit_line in reversal_lines:
            candidates = source_asset_lines
            purchase_line = getattr(credit_line, 'purchase_line_id', False)
            if purchase_line:
                exact = candidates.filtered(
                    lambda line: getattr(line, 'purchase_line_id', False)
                    == purchase_line,
                )
                if exact:
                    matched |= exact
                    continue
            tax_ids = set(credit_line.tax_ids.ids)
            candidates = candidates.filtered(lambda line: (
                line.account_id == credit_line.account_id
                and line.product_id == credit_line.product_id
                and set(line.tax_ids.ids) == tax_ids
                and line.balance * credit_line.balance < 0
            ))
            if not candidates and not credit_line.product_id:
                # Non-product lines have no SKU identity. Name + account + tax
                # is the narrowest evidence available; retain all candidates
                # when that fingerprint is genuinely ambiguous.
                candidates = source_asset_lines.filtered(lambda line: (
                    line.account_id == credit_line.account_id
                    and not line.product_id
                    and (line.name or '').strip() ==
                    (credit_line.name or '').strip()
                    and set(line.tax_ids.ids) == tax_ids
                    and line.balance * credit_line.balance < 0
                ))
            matched |= candidates
        return matched
