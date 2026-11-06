# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bridge: recurring invoice templates -> IFRS 15 revenue contracts.

A template optionally links a revenue contract. When linked, every invoice
the template generates routes its revenue lines to the contract's contract
liability account instead of the line income account, and posting the
invoice registers the billed amount on the contract, mirroring the
contract's own Record Billing convention:

* the receivable is debited by the invoice itself (core behaviour);
* the credit lands on the contract liability account (deferred revenue);
* if the contract carries a contract asset (revenue recognised ahead of
  billing), a balanced reclassification entry moves the asset-clearing
  portion out of the liability, so the general ledger shows exactly the
  same split Record Billing would have produced (credit the contract asset
  first, then the contract liability);
* contract.amount_billed advances, so the recognition run releases the
  liability as performance obligations are satisfied.

The contract-side bookkeeping is a deterministic, server-owned consequence
of a manager-approved template link.  A posting actor must be allowed to
write the invoice; the immutable bridge provenance is then revalidated and
the exact contract bookkeeping is performed with narrowly scoped elevation.
Unlinked templates are untouched (byte-identical default).
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.eh_account_revenue.models.revenue_contract import (
    EhRevenueContract as RevenueContract,
    _REVENUE_MOVE_LINK_CAPABILITY,
)
from odoo.addons.eh_account_recurring_invoices.models.recurring_template import (
    RECURRING_MOVE_CAPABILITY,
    RECURRING_MOVE_CONTEXT,
)


_RECURRING_REVENUE_BILLING_CONTEXT = (
    'eh_recurring_revenue_billing_capability'
)
_RECURRING_REVENUE_BILLING_CAPABILITY = object()
_RECURRING_REVENUE_CORRECTION_POST_CONTEXT = (
    'eh_recurring_revenue_correction_post_capability'
)
_RECURRING_REVENUE_CORRECTION_POST_CAPABILITY = object()


class EhRecurringInvoiceTemplate(models.Model):
    _inherit = 'eh.recurring.invoice.template'

    revenue_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Revenue Contract", tracking=True,
        check_company=True, ondelete='restrict',
        domain="[('company_id', '=', company_id)]",
        help="Optional IFRS 15 revenue contract behind this recurring "
             "billing. When set, generated invoices credit the contract's "
             "contract liability account (deferred revenue) instead of the "
             "line income accounts, and the billed amount registers on the "
             "contract so the recognition run releases it as performance "
             "obligations are satisfied.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('revenue_contract_id') for vals in vals_list):
            self._eh_authorize_contract_links(vals_list)
        return super().create(vals_list)

    def write(self, vals):
        if 'revenue_contract_id' in vals:
            self._eh_authorize_contract_links([vals])
        return super().write(vals)

    def _eh_history_frozen_fields(self):
        return super()._eh_history_frozen_fields() | {'revenue_contract_id'}

    def action_review_legacy_and_resume_setup(self):
        """Review both halves of a quarantined bridge atomically."""
        self.ensure_one()
        contract = self.revenue_contract_id
        note = self.eh_legacy_review_note
        with self.env.cr.savepoint():
            result = super().action_review_legacy_and_resume_setup()
            if contract and contract.eh_integrity_review_required:
                contract.write({'eh_integrity_review_note': note})
                contract.action_review_legacy_integrity()
        return result

    def _eh_authorize_contract_links(self, vals_list):
        if self.env.su:
            return
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can link a recurring "
                "template to a revenue contract."))
        for vals in vals_list:
            contract_id = vals.get('revenue_contract_id')
            if not contract_id:
                continue
            contract = self.env['eh.revenue.contract'].browse(contract_id)
            contract._eh_check_access('read')
            company_id = vals.get('company_id')
            if not company_id and self:
                company_id = self[:1].company_id.id
            company_id = company_id or self.env.company.id
            if contract.company_id.id != company_id:
                raise ValidationError(_(
                    "The revenue contract and recurring template must "
                    "belong to the same company."))

    def _eh_move_create_context(self):
        values = super()._eh_move_create_context()
        # Keep the revenue provenance capability out of ordinary recurring
        # invoice creates.  It is needed only when this bridge is actually
        # going to stamp an immutable revenue-contract link.
        if self.revenue_contract_id:
            values['eh_revenue_move_link_capability'] = (
                _REVENUE_MOVE_LINK_CAPABILITY
            )
        return values

    @api.constrains('revenue_contract_id', 'company_id')
    def _check_revenue_contract_company(self):
        for tpl in self:
            if (tpl.revenue_contract_id
                    and tpl.revenue_contract_id.company_id != tpl.company_id):
                raise ValidationError(_(
                    "The linked revenue contract must belong to the "
                    "template's company."))

    def _build_invoice_vals(self):
        vals = super()._build_invoice_vals()
        return self._eh_apply_revenue_contract_to_move_vals(vals)

    def _eh_prepare_proration_move_vals(self, vals):
        vals = super()._eh_prepare_proration_move_vals(vals)
        return self._eh_apply_revenue_contract_to_move_vals(vals)

    def _eh_apply_revenue_contract_to_move_vals(self, vals):
        """Route normal and proration billing through one IFRS 15 path."""
        self.ensure_one()
        self._eh_assert_integrity_trusted()
        contract = self.revenue_contract_id
        if not contract:
            return vals
        if contract.state != 'active':
            raise UserError(_(
                "Template %(template)s is linked to revenue contract "
                "%(contract)s, which is not active. Activate the contract "
                "or unlink it before generating.",
                template=self.name, contract=contract.display_name))
        if contract.eh_integrity_review_required:
            raise UserError(_(
                "Revenue contract %s is quarantined for legacy integrity "
                "review and cannot accept recurring billing.",
                contract.display_name,
            ))
        # Reclassification may be required immediately after the invoice
        # posts.  Validate every account and the journal it can need now, so a
        # draft is never created only to fail deep inside ``_post_move``.
        trusted_contract = contract.sudo()
        trusted_contract._eh_validate_references()
        trusted_contract._validate_accounts(billing=True)
        move_currency = self.env['res.currency'].browse(
            vals.get('currency_id') or self.currency_id.id
        ).exists()
        if move_currency != contract.currency_id:
            raise ValidationError(_(
                "A revenue-linked recurring invoice must be generated in "
                "the linked contract's currency."
            ))
        for command in vals.get('invoice_line_ids', []):
            command[2]['account_id'] = (
                contract.contract_liability_account_id.id)
        vals['eh_revenue_contract_id'] = contract.id
        source_id = vals.get('eh_recurring_proration_source_move_id')
        if source_id:
            source = self.env['account.move'].browse(source_id).exists()
            if (
                not source
                or source.state != 'posted'
                or source.eh_recurring_template_id != self
                or source.eh_revenue_contract_id != contract
                or not source.eh_revenue_billing_registered
            ):
                raise ValidationError(_(
                    "A revenue-linked proration must adjust a posted, "
                    "registered invoice from the same template and contract."
                ))
        return vals


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_revenue_billing_registered = fields.Boolean(
        string="Revenue Billing Registered",
        readonly=True, copy=False,
        help="This generated invoice has already registered its billed "
             "amount on the linked revenue contract; kept so a reset and "
             "repost cannot double-count the billing.")
    eh_revenue_billing_correction_of_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        index=True,
        help="Exact registered recurring invoice reversed by this verified "
             "sealed credit note.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            vals.get('eh_revenue_billing_registered')
            or vals.get('eh_revenue_billing_correction_of_id')
            for vals in vals_list
        ) and not self._eh_is_recurring_revenue_engine():
            raise AccessError(_(
                "Recurring revenue registration provenance is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if (
            {
                'eh_revenue_billing_registered',
                'eh_revenue_billing_correction_of_id',
            }.intersection(vals)
            and not self._eh_is_recurring_revenue_engine()
        ):
            raise AccessError(_(
                "Recurring revenue registration provenance is immutable."
            ))
        return super().write(vals)

    def _eh_is_recurring_revenue_engine(self):
        return (
            self.env.context.get(_RECURRING_REVENUE_BILLING_CONTEXT)
            is _RECURRING_REVENUE_BILLING_CAPABILITY
        )

    def _eh_recurring_revenue_template(self):
        """Return the one immutable template anchor for a bridge move.

        Ordinary generations use ``eh_recurring_template_id``.  Proration
        moves intentionally use only their dedicated proration provenance;
        treating them as ordinary generations would inflate generated_count.
        """
        self.ensure_one()
        return (
            self.eh_recurring_proration_template_id
            or self.eh_recurring_template_id
        )

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        # Core's verified-reversal helper temporarily removes the original
        # link while posting to avoid late automatic reconciliation.  Defer a
        # correction's billing registration until that helper has restored
        # and revalidated the immutable link.
        if self.env.context.get(
            _RECURRING_REVENUE_CORRECTION_POST_CONTEXT
        ) is not _RECURRING_REVENUE_CORRECTION_POST_CAPABILITY:
            posted._eh_register_revenue_contract_billing()
        return posted

    def button_draft(self):
        registered = self.filtered(
            lambda move: move.eh_revenue_billing_registered
        )
        if registered:
            raise UserError(_(
                "A recurring invoice already registered against a revenue "
                "contract cannot be reset to draft. Correct it through an "
                "auditable reversal/credit and the contract billing flow."
            ))
        return super().button_draft()

    def action_reverse_revenue_billing(self, accounting_date=None):
        """Post and register one exact credit for a bridged invoice."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can correct registered "
                "recurring revenue billing."
            ))
        source = self
        contract = source.eh_revenue_contract_id
        if (
            source.state != 'posted'
            or source.move_type != 'out_invoice'
            or not source.eh_revenue_billing_registered
            or not source.eh_recurring_template_id
            or source.eh_recurring_proration_template_id
            or not contract
            or not source.eh_sealed
            or source.eh_legacy_unverified_seal
            or contract.state != 'active'
            or contract.eh_integrity_review_required
        ):
            raise UserError(_(
                "Only a current verified, registered, non-proration recurring "
                "invoice on an active contract can be corrected automatically."
            ))
        contract._eh_lock_for_transition()
        source.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE",
            (source.id,),
        )
        source.invalidate_recordset()
        correction_date = fields.Date.to_date(
            accounting_date or fields.Date.context_today(self))
        if correction_date < source.date:
            raise UserError(_(
                "Correction date %(date)s cannot precede invoice date "
                "%(invoice_date)s.",
                date=correction_date, invoice_date=source.date,
            ))
        reversal = source.sudo(False)._eh_reverse_with_verified_capability([{
            'date': correction_date,
            # Core invoices derive their ledger date from invoice_date while
            # posting.  Pin both so the approved correction date cannot drift
            # back to the source invoice's business date.
            'invoice_date': correction_date,
            'ref': _("Recurring revenue billing correction %s", source.name),
        }], cancel=False)
        metadata = {
            'eh_recurring_template_id': source.eh_recurring_template_id.id,
            'eh_recurring_period_start': source.eh_recurring_period_start,
            'eh_recurring_period_end': source.eh_recurring_period_end,
            'eh_revenue_contract_id': contract.id,
            'eh_revenue_billing_correction_of_id': source.id,
        }
        reversal.with_context(**{
            RECURRING_MOVE_CONTEXT: RECURRING_MOVE_CAPABILITY,
            'eh_revenue_move_link_capability': _REVENUE_MOVE_LINK_CAPABILITY,
            _RECURRING_REVENUE_BILLING_CONTEXT:
                _RECURRING_REVENUE_BILLING_CAPABILITY,
        })._eh_write_sealed_metadata(metadata, set(metadata))
        reversal.with_context(**{
            _RECURRING_REVENUE_CORRECTION_POST_CONTEXT:
                _RECURRING_REVENUE_CORRECTION_POST_CAPABILITY,
        })._eh_post_verified_reversal()
        reversal.invalidate_recordset(['state', 'reversed_entry_id'])
        reversal._eh_register_revenue_contract_billing()
        receivable = (source.line_ids | reversal.line_ids).filtered(
            lambda line: line.account_id.reconcile
            and line.account_id.account_type == 'asset_receivable'
            and not line.reconciled
        )
        if receivable:
            receivable.reconcile()
        source._eh_message_post_sealed_metadata(body=_(
            "Registered recurring billing corrected by verified sealed credit "
            "note %(credit)s on %(date)s.",
            credit=reversal.display_name, date=correction_date,
        ))
        return True

    def _eh_register_revenue_contract_billing(self):
        """Register normal invoices and atomic proration pairs from GL.

        Contracts lock first, then moves. Proration credit and charge register
        as one signed billing delta; isolated refunds are refused. Asset versus
        liability presentation is normalized from actual posted balances, so
        significant-financing entries cannot be misclassified by nominal
        ``amount_recognised - amount_billed`` arithmetic.
        """
        actor_moves = self.sudo(False).filtered(lambda move: (
            move.move_type in ('out_invoice', 'out_refund')
            and move.eh_revenue_contract_id
            and move._eh_recurring_revenue_template()
            and not move.eh_revenue_billing_registered
        ))
        if not actor_moves:
            return False
        # Posting the invoice is the user's authorised action.  Contract and
        # template links are immutable, engine-owned provenance created from a
        # manager-approved template, so revalidate them below under narrowly
        # scoped elevation rather than requiring every invoicer to hold EH
        # revenue-contract ACLs.
        if not actor_moves.env.su:
            actor_moves._eh_check_access('write')
        moves = actor_moves.sudo()
        contracts = moves.mapped('eh_revenue_contract_id').sorted('id')
        templates = (
            moves.mapped('eh_recurring_template_id')
            | moves.mapped('eh_recurring_proration_template_id')
        )
        for contract in contracts:
            contract._eh_validate_references()
        contracts._eh_lock_for_transition()
        moves.flush_recordset()
        moves.env.cr.execute(
            "SELECT id FROM account_move WHERE id IN %s ORDER BY id "
            "FOR UPDATE",
            (tuple(sorted(moves.ids)),),
        )
        moves.invalidate_recordset()
        contracts.invalidate_recordset()
        templates.invalidate_recordset()
        moves = moves.filtered(
            lambda move: not move.eh_revenue_billing_registered
        )
        if not moves:
            return False

        units = []
        proration = {}
        for move in moves:
            self._eh_validate_revenue_billing_move(move)
            if move.eh_recurring_proration_template_id:
                template = move._eh_recurring_revenue_template()
                key = (
                    move.eh_revenue_contract_id.id,
                    template.id,
                    move.eh_recurring_proration_date,
                    move.eh_recurring_proration_source_move_id.id,
                )
                proration.setdefault(key, moves.browse())
                proration[key] |= move
            else:
                if move.move_type != 'out_invoice' and not (
                    move.move_type == 'out_refund'
                    and move.eh_revenue_billing_correction_of_id
                ):
                    raise ValidationError(_(
                        "A recurring refund registers only as part of one "
                        "complete proration credit/charge pair."
                    ))
                units.append(move)
        for pair in proration.values():
            if (
                len(pair) != 2
                or set(pair.mapped('eh_recurring_proration_role'))
                != {'credit', 'charge'}
                or len(pair.mapped(
                    'eh_recurring_proration_source_move_id')) != 1
            ):
                raise ValidationError(_(
                    "Revenue-linked proration must post one exact credit and "
                    "one exact charge together."
                ))
            units.append(pair)

        units.sort(key=lambda unit: (
            min(unit.mapped('date')),
            min(unit.ids),
        ))
        registered = False
        for unit in units:
            contract = unit[0].eh_revenue_contract_id
            currency = contract.currency_id
            delta = currency.round(sum(
                currency.round(abs(move.amount_untaxed))
                * (1.0 if move.move_type == 'out_invoice' else -1.0)
                for move in unit
            ))
            new_billed = currency.round(contract.amount_billed + delta)
            if currency.compare_amounts(new_billed, 0.0) < 0:
                raise ValidationError(_(
                    "Recurring credits cannot reduce billed consideration "
                    "below zero on contract %s.", contract.display_name,
                ))
            effective_date = max(unit.mapped('date'))
            self._eh_normalize_contract_position(
                contract,
                effective_date,
                ', '.join(unit.mapped('name')),
                new_billed=new_billed,
            )
            contract.sudo().write({'amount_billed': new_billed})
            unit.with_context(**{
                _RECURRING_REVENUE_BILLING_CONTEXT:
                    _RECURRING_REVENUE_BILLING_CAPABILITY,
            })._eh_write_sealed_metadata(
                {'eh_revenue_billing_registered': True},
                {'eh_revenue_billing_registered'},
            )
            registered = True
        return registered

    def _eh_validate_revenue_billing_move(self, move):
        contract = move.eh_revenue_contract_id
        template = move._eh_recurring_revenue_template()
        commercial_lines = move.invoice_line_ids.filtered('account_id')
        if (
            move.state != 'posted'
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
            or move.eh_recurring_legacy_unverified
            or move.company_id != contract.company_id
            or template.company_id != contract.company_id
            or template.eh_legacy_integrity_unverified
            or template.revenue_contract_id != contract
            or move.currency_id != contract.currency_id
            or contract.state != 'active'
            or contract.eh_integrity_review_required
            or not commercial_lines
            or any(
                line.account_id != contract.contract_liability_account_id
                for line in commercial_lines
            )
        ):
            raise ValidationError(_(
                "Recurring billing must remain posted on the exact active, "
                "non-quarantined contract, company, currency, template, and "
                "contract-liability account."
            ))
        if move.eh_recurring_proration_template_id:
            source = move.eh_recurring_proration_source_move_id
            if (
                not source
                or source.state != 'posted'
                or source.eh_recurring_template_id != template
                or source.eh_revenue_contract_id != contract
                or not source.eh_revenue_billing_registered
                or not source.eh_recurring_period_start
                or not source.eh_recurring_period_end
                or not (
                    source.eh_recurring_period_start
                    <= move.eh_recurring_proration_date
                    <= source.eh_recurring_period_end
                )
            ):
                raise ValidationError(_(
                    "Proration billing lost its exact registered source "
                    "invoice and immutable service-period evidence."
                ))
        correction_source = move.eh_revenue_billing_correction_of_id
        if correction_source and (
            move.move_type != 'out_refund'
            or correction_source.state != 'posted'
            or correction_source.move_type != 'out_invoice'
            or not correction_source.eh_revenue_billing_registered
            or correction_source.eh_revenue_contract_id != contract
            or correction_source.eh_recurring_template_id != template
            or move.reversed_entry_id != correction_source
        ):
            raise ValidationError(_(
                "A revenue billing correction must be the exact verified "
                "sealed reversal of one registered invoice from the same "
                "template and contract."
            ))

    def _eh_latest_contract_position_date(self, contract, effective_date):
        """Never back-date a reclass ahead of evidence it clears."""
        effective_date = fields.Date.to_date(effective_date)
        contract.flush_recordset([
            'contract_asset_account_id', 'contract_liability_account_id',
        ])
        self.env['account.move'].flush_model([
            'date', 'state', 'eh_revenue_contract_id',
        ])
        self.env['account.move.line'].flush_model([
            'move_id', 'account_id',
        ])
        self.env.cr.execute(
            "SELECT MAX(am.date) "
            "FROM account_move_line aml "
            "JOIN account_move am ON am.id = aml.move_id "
            "WHERE am.eh_revenue_contract_id = %s "
            "AND am.company_id = %s AND am.state = 'posted' "
            "AND aml.account_id IN %s",
            (
                contract.id,
                contract.company_id.id,
                tuple(sorted((
                    contract.contract_asset_account_id
                    | contract.contract_liability_account_id
                ).ids)),
            ),
        )
        latest = self.env.cr.fetchone()[0]
        return max(
            date for date in (
                effective_date,
                fields.Date.to_date(latest) if latest else False,
            ) if date
        )

    def _eh_normalize_contract_position(
        self, contract, effective_date, label, new_billed=None,
    ):
        effective_date = self._eh_latest_contract_position_date(
            contract, effective_date,
        )
        currency = contract.currency_id
        asset_balance, liability_balance = (
            contract._eh_ledger_contract_account_balances(
                as_of=effective_date
            )
        )
        lines = []
        if (
            currency.compare_amounts(asset_balance, 0.0) >= 0
            and currency.compare_amounts(liability_balance, 0.0) <= 0
        ):
            overlap = currency.round(min(
                max(asset_balance, 0.0),
                max(-liability_balance, 0.0),
            ))
            if currency.compare_amounts(overlap, 0.0) > 0:
                lines = [
                    (0, 0, {
                        'name': _("Recurring billing reclass %s", label),
                        'account_id':
                            contract.contract_liability_account_id.id,
                        'debit': overlap, 'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': _("Contract asset billed %s", label),
                        'account_id': contract.contract_asset_account_id.id,
                        'debit': 0.0, 'credit': overlap,
                    }),
                ]
        else:
            # Refunds can leave a debit on the liability account; corrupted
            # legacy presentation can likewise leave a credit on the asset.
            # Shift the full wrong-side amount to the correct account.
            shift = currency.round(max(
                max(liability_balance, 0.0),
                max(-asset_balance, 0.0),
            ))
            if currency.compare_amounts(shift, 0.0) > 0:
                lines = [
                    (0, 0, {
                        'name': _("Contract asset restored %s", label),
                        'account_id': contract.contract_asset_account_id.id,
                        'debit': shift, 'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': _("Recurring credit reclass %s", label),
                        'account_id':
                            contract.contract_liability_account_id.id,
                        'debit': 0.0, 'credit': shift,
                    }),
                ]
        if lines:
            contract.sudo()._post_move(
                lines,
                date=effective_date,
                ref=_("Recurring billing reclass: %s", label),
            )
        if new_billed is not None:
            self._eh_normalize_financed_receivable(
                contract,
                effective_date,
                label,
                currency.round(new_billed),
            )

    def _eh_normalize_financed_receivable(
        self, contract, effective_date, label, new_billed,
    ):
        """Keep a billed arrears-financing discount out of contract liability.

        Once an invoice makes the right to consideration unconditional, the
        remaining unearned financing discount belongs with the receivable,
        not in IFRS 15 contract liability (IFRS 15.105-107).  This entry uses
        the configured receivable account as a contra-receivable and records a
        durable anchor for later effective-interest accretion.
        """
        if not (
            contract.financing_direction == 'arrears'
            and contract._financing_is_time_based()
        ):
            return False
        currency = contract.currency_id
        # ``amount_recognised`` is the posted present-value revenue measure,
        # while billing and each obligation's durable recognised anchor are
        # nominal consideration.  Comparing PV revenue directly with a
        # nominal invoice mislabels the financing discount as unsatisfied
        # performance and leaves it in contract liability after full
        # satisfaction.  Decide asset/liability presentation from the
        # like-for-like nominal anchors, then value only an unbilled asset at
        # its current financed carrying ratio.
        nominal_recognised = currency.round(sum(
            contract.obligation_ids.mapped('recognised_amount')
        ))
        nominal_gap = currency.round(nominal_recognised - new_billed)
        if currency.compare_amounts(nominal_gap, 0.0) >= 0:
            carrying_ratio = contract._financing_arrears_carrying_ratio(
                currency,
            )
            target_net = currency.round(nominal_gap * carrying_ratio)
        else:
            # Billing ahead of performance remains a genuine contract
            # liability at the nominal billed amount.
            target_net = nominal_gap
        asset_balance, liability_balance = (
            contract._eh_ledger_contract_account_balances(
                as_of=effective_date,
            )
        )
        current_net = currency.round(asset_balance + liability_balance)
        required = currency.round(target_net - current_net)
        existing = currency.round(
            contract.eh_recurring_financing_receivable_adjustment
        )
        if currency.compare_amounts(required, 0.0) < 0:
            # Only unwind a bridge adjustment already recorded by this engine;
            # do not manufacture a debit receivable to mask unrelated drift.
            required = -currency.round(min(-required, existing))
        if currency.compare_amounts(required, 0.0) == 0:
            return False

        amount = abs(required)
        lines = []
        if required > 0:
            from_liability = currency.round(min(
                amount, max(-liability_balance, 0.0),
            ))
            to_asset = currency.round(amount - from_liability)
            if from_liability:
                lines.append((0, 0, {
                    'name': _("Financing liability reclass %s", label),
                    'account_id': contract.contract_liability_account_id.id,
                    'debit': from_liability,
                    'credit': 0.0,
                }))
            if to_asset:
                lines.append((0, 0, {
                    'name': _("Financing contract asset reclass %s", label),
                    'account_id': contract.contract_asset_account_id.id,
                    'debit': to_asset,
                    'credit': 0.0,
                }))
            lines.append((0, 0, {
                'name': _("Unearned financing on receivable %s", label),
                'account_id': contract.receivable_account_id.id,
                'partner_id': contract.partner_id.id,
                'date_maturity': (
                    contract.financing_payment_date or effective_date
                ),
                'debit': 0.0,
                'credit': amount,
            }))
        else:
            from_asset = currency.round(min(
                amount, max(asset_balance, 0.0),
            ))
            to_liability = currency.round(amount - from_asset)
            lines.append((0, 0, {
                'name': _("Financing receivable adjustment released %s", label),
                'account_id': contract.receivable_account_id.id,
                'partner_id': contract.partner_id.id,
                'date_maturity': (
                    contract.financing_payment_date or effective_date
                ),
                'debit': amount,
                'credit': 0.0,
            }))
            if from_asset:
                lines.append((0, 0, {
                    'name': _("Financing contract asset restored %s", label),
                    'account_id': contract.contract_asset_account_id.id,
                    'debit': 0.0,
                    'credit': from_asset,
                }))
            if to_liability:
                lines.append((0, 0, {
                    'name': _("Financing liability restored %s", label),
                    'account_id': contract.contract_liability_account_id.id,
                    'debit': 0.0,
                    'credit': to_liability,
                }))
        contract.sudo()._post_move(
            lines,
            date=effective_date,
            ref=_("Recurring financing receivable reclass: %s", label),
        )
        contract.sudo().write({
            'eh_recurring_financing_receivable_adjustment': currency.round(
                existing + required
            ),
        })
        return True


class EhRevenueContract(models.Model):
    _inherit = 'eh.revenue.contract'

    _eh_guarded_fields = RevenueContract._eh_guarded_fields + (
        'eh_recurring_financing_receivable_adjustment',
    )

    eh_recurring_financing_receivable_adjustment = fields.Monetary(
        string="Deferred Financing Receivable Adjustment",
        readonly=True,
        copy=False,
        currency_field='currency_id',
        help="Unaccreted arrears-financing discount reclassified from the "
             "IFRS 15 contract position to the unconditional receivable by "
             "registered recurring billing.",
    )

    def _eh_has_registered_recurring_billing(self):
        self.ensure_one()
        return bool(self.env['account.move'].sudo().search_count([
            ('eh_revenue_contract_id', '=', self.id),
            ('eh_revenue_billing_registered', '=', True),
            ('state', '=', 'posted'),
            '|',
            ('eh_recurring_template_id', '!=', False),
            ('eh_recurring_proration_template_id', '!=', False),
        ], limit=1))

    def action_recognise(self, accounting_date=None):
        result = super().action_recognise(accounting_date=accounting_date)
        for contract in self.sudo():
            if (
                contract.financing_direction == 'arrears'
                and contract._financing_is_time_based()
                and contract._eh_has_registered_recurring_billing()
            ):
                posting_date = contract._eh_action_accounting_date(
                    accounting_date,
                )
                self.env['account.move']._eh_normalize_contract_position(
                    contract,
                    posting_date,
                    _("recognition on %s", posting_date),
                    new_billed=contract.amount_billed,
                )
        return result

    def _post_financing_accretion(self, as_of=None):
        self.ensure_one()
        currency = self.currency_id
        adjustment = currency.round(
            self.eh_recurring_financing_receivable_adjustment
        )
        if (
            self.financing_direction != 'arrears'
            or currency.compare_amounts(adjustment, 0.0) <= 0
        ):
            return super()._post_financing_accretion(as_of=as_of)
        posting_date = self._eh_action_accounting_date(as_of)
        accreted = self._financing_time_accreted_interest(
            currency,
            as_of=posting_date,
        )
        delta = currency.round(accreted - self.financing_interest_recognised)
        if currency.compare_amounts(delta, 0.0) == 0:
            return False
        if delta > 0:
            return super()._post_financing_accretion(as_of=as_of)

        amount = -delta
        full_interest = self._financing_signed_total_interest(currency)
        unaccreted_before = max(
            -(full_interest - self.financing_interest_recognised),
            0.0,
        )
        billed_share = (
            min(adjustment / unaccreted_before, 1.0)
            if currency.compare_amounts(unaccreted_before, 0.0) > 0
            else 0.0
        )
        from_receivable = currency.round(min(
            amount * billed_share,
            adjustment,
        ))
        remainder = currency.round(amount - from_receivable)
        _asset_before, liability_before = self._eh_ledger_contract_position()
        from_liability = currency.round(min(remainder, liability_before))
        to_asset = currency.round(remainder - from_liability)
        lines = [(0, 0, {
            'name': _("Financing interest income %s", self.name),
            'account_id': self.financing_account_id.id,
            'debit': 0.0,
            'credit': amount,
        })]
        if from_receivable:
            lines.append((0, 0, {
                'name': _("Financing receivable accretion %s", self.name),
                'account_id': self.receivable_account_id.id,
                'partner_id': self.partner_id.id,
                'date_maturity': self.financing_payment_date or posting_date,
                'debit': from_receivable,
                'credit': 0.0,
            }))
        if from_liability:
            lines.append((0, 0, {
                'name': _("Financing liability unwound %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': from_liability,
                'credit': 0.0,
            }))
        if to_asset:
            lines.append((0, 0, {
                'name': _("Financing interest accreted %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': to_asset,
                'credit': 0.0,
            }))
        self._post_move(lines, date=posting_date)
        self.sudo().write({
            'financing_interest_recognised': currency.round(
                self.financing_interest_recognised + delta
            ),
            'eh_recurring_financing_receivable_adjustment': currency.round(
                adjustment - from_receivable
            ),
        })
        return True

    def action_reverse_and_cancel(self, accounting_date=None):
        result = super().action_reverse_and_cancel(
            accounting_date=accounting_date,
        )
        self.sudo().write({
            'eh_recurring_financing_receivable_adjustment': 0.0,
        })
        return result
