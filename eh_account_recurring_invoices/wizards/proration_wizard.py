# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Proration wizard for mid-period recurring invoice changes.

A subscription customer changes plan mid-month. The recurring invoice
template needs to: (1) issue a credit note for the unused remainder of
the current period at the old price, (2) issue an invoice for the new
plan from today through the period end at the new price, (3) update
the template so the next full-period invoice uses the new price.

This wizard does all three in one transaction. Both the credit and the
catch-up invoice are posted under one savepoint so a partial failure
rolls back cleanly. The wizard records the change on the template's
chatter so the auditor can trace pricing history per subscription.

Maths convention: prorate by calendar days remaining versus calendar
days in the period (not 30/360, not actual/365). This is the
convention every subscription-billing platform uses; the alternative
30-day-month rule is documented but not implemented here because it
diverges from the contract on every February.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


PRORATION_MOVE_CONTEXT = 'eh_recurring_proration_capability'
PRORATION_MOVE_CAPABILITY = object()


class EhRecurringInvoiceProrationWizard(models.TransientModel):
    _name = 'eh.recurring.invoice.proration.wizard'
    _description = "Recurring invoice proration wizard"

    template_id = fields.Many2one(
        'eh.recurring.invoice.template',
        required=True,
        ondelete='cascade',
    )
    change_date = fields.Date(
        required=True, default=fields.Date.context_today,
        help=(
            "Date the new pricing takes effect. The remaining unused "
            "portion of the current period at the old price is "
            "credited; the same window at the new price is invoiced."
        ),
    )
    new_amount = fields.Monetary(
        required=True,
        currency_field='currency_id',
        help="The unit price for the new plan, per period.",
    )
    currency_id = fields.Many2one(
        related='template_id.currency_id', readonly=True,
    )
    period_end_date = fields.Date(
        compute='_compute_period_end',
        help="Last day of the current billing period under the existing schedule.",
    )
    period_start_date = fields.Date(
        compute='_compute_period_end',
        help="First day of the posted generated invoice being adjusted.",
    )
    days_remaining = fields.Integer(
        compute='_compute_period_end',
    )
    period_length_days = fields.Integer(
        compute='_compute_period_end',
    )
    old_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
    )
    credit_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Refund for the unused remainder at the old price.",
    )
    new_period_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Charge for the same remaining window at the new price.",
    )
    net_amount = fields.Monetary(
        compute='_compute_period_end',
        currency_field='currency_id',
        help="Net invoice (positive: customer owes; negative: company refunds).",
    )

    @api.constrains('new_amount')
    def _check_new_amount_positive(self):
        for wiz in self:
            currency = wiz.currency_id
            if currency and currency.compare_amounts(wiz.new_amount, 0.0) <= 0:
                raise ValidationError(_("The new unit price must be positive."))

    @api.depends('template_id', 'change_date', 'new_amount')
    def _compute_period_end(self):
        for wiz in self:
            if not wiz.template_id or not wiz.change_date:
                wiz.period_start_date = False
                wiz.period_end_date = False
                wiz.days_remaining = 0
                wiz.period_length_days = 0
                wiz.old_amount = 0.0
                wiz.credit_amount = 0.0
                wiz.new_period_amount = 0.0
                wiz.net_amount = 0.0
                continue
            tpl = wiz.template_id
            source = wiz._eh_billed_period_move()
            if len(source) == 1:
                period_start = source.eh_recurring_period_start
                period_end = source.eh_recurring_period_end
            else:
                # Preview fallback for a template that has never generated.
                # action_apply still requires exact posted source evidence.
                period_start, period_end = tpl._eh_period_bounds(
                    tpl.next_run_date or wiz.change_date
                )
            wiz.period_start_date = period_start
            wiz.period_end_date = period_end
            length = (period_end - period_start).days + 1
            remaining = (
                (period_end - wiz.change_date).days + 1
                if period_start <= wiz.change_date <= period_end
                else 0
            )
            wiz.period_length_days = length
            wiz.days_remaining = remaining
            # Prorate the source line's commercial price, not amount_untaxed.
            # price_unit remains tax-inclusive when an output tax has
            # price_include=True; amount_untaxed would strip that tax and then
            # feeding the reduced number back as price_unit would under-credit
            # the customer. Discount and quantity mirror the posted source.
            old_unit = (
                wiz._eh_source_commercial_amount(source)
                if len(source) == 1
                else sum(
                    (l.quantity or 0.0) * (l.price_unit or 0.0)
                    for l in tpl.line_ids
                ) or 0.0
            )
            total_qty = sum(tpl.line_ids.mapped('quantity')) or 0.0
            wiz.old_amount = old_unit
            if length > 0:
                ratio = remaining / float(length)
            else:
                ratio = 0.0
            currency = wiz.currency_id
            wiz.credit_amount = currency.round(old_unit * ratio)
            # new_amount is a per-unit price (it becomes line.price_unit on
            # apply), so charge it against the same quantity the next
            # full-period invoice will bill.
            wiz.new_period_amount = currency.round(
                wiz.new_amount * total_qty * ratio)
            wiz.net_amount = currency.round(
                wiz.new_period_amount - wiz.credit_amount)

    def _eh_single_source_line(self, source=None):
        """Return one unambiguous billed commercial line with a clear error."""
        self.ensure_one()
        source = source or self._eh_billed_period_move()
        if len(source) != 1:
            raise UserError(_(
                "Proration requires exactly one posted generated source "
                "invoice for the effective service period."
            ))
        lines = source.invoice_line_ids.filtered(
            lambda move_line: move_line.display_type
            not in ('line_section', 'line_note')
        )
        if len(lines) != 1:
            raise UserError(_(
                "The posted source invoice has %(count)s commercial lines. "
                "Proration requires exactly one source line so its account, "
                "taxes and gross price can be preserved without guessing.",
                count=len(lines),
            ))
        if not lines.account_id:
            raise UserError(_(
                "The posted source invoice line has no resolved income "
                "account; repair the source evidence before proration."
            ))
        return lines

    def _eh_source_commercial_amount(self, source=None):
        source_line = self._eh_single_source_line(source)
        return (
            (source_line.quantity or 0.0)
            * (source_line.price_unit or 0.0)
            * (1.0 - (source_line.discount or 0.0) / 100.0)
        )

    def _eh_billed_period_move(self):
        """Return exact posted generated invoice covering change_date."""
        self.ensure_one()
        if not self.template_id or not self.change_date:
            return self.env['account.move']
        return self.env['account.move'].search([
            ('eh_recurring_template_id', '=', self.template_id.id),
            ('eh_recurring_proration_role', '=', False),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('eh_sealed', '=', True),
            ('eh_legacy_unverified_seal', '=', False),
            ('eh_recurring_legacy_unverified', '=', False),
            ('eh_recurring_period_start', '<=', self.change_date),
            ('eh_recurring_period_end', '>=', self.change_date),
        ], order='id desc', limit=2)

    def action_apply(self):
        """Issue the credit + new-price invoice in one savepoint, then
        update the template price to `new_amount`. The change is logged
        on the template's chatter.
        """
        self.ensure_one()
        template = self.template_id
        template._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can post a recurring "
                "invoice proration."))
        template.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_recurring_invoice_template "
            "WHERE id = %s FOR UPDATE",
            (template.id,),
        )
        template.invalidate_recordset()
        template.line_ids.invalidate_recordset()
        template._eh_assert_integrity_trusted()
        template._eh_validate_generation_targets()

        existing = self.env['account.move'].search([
            ('eh_recurring_proration_template_id', '=', template.id),
            ('eh_recurring_proration_date', '=', self.change_date),
        ])
        if existing:
            roles = set(existing.mapped('eh_recurring_proration_role'))
            if (roles == {'credit', 'charge'} and len(existing) == 2
                    and all(move.state == 'posted' for move in existing)
                    and len(existing.mapped(
                        'eh_recurring_proration_source_move_id')) == 1):
                return self._result_action(existing)
            raise UserError(_(
                "An incomplete or inconsistent proration already exists for "
                "this template and date. Review its journal entries before "
                "retrying."
            ))

        # Recompute from the locked current price. A waiting retry must not
        # use amounts cached before another transaction changed the template.
        self.invalidate_recordset()
        source = self._eh_billed_period_move()
        if len(source) != 1:
            raise UserError(_(
                "Proration requires exactly one posted generated invoice "
                "whose immutable service period contains the change date. "
                "Post the source invoice first, or correct legacy invoices "
                "without period evidence through a reviewed credit note."
            ))
        source.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE",
            (source.id,),
        )
        source.invalidate_recordset()
        if (
            source.state != 'posted'
            or not source.eh_sealed
            or source.eh_legacy_unverified_seal
            or source.eh_recurring_legacy_unverified
            or source.eh_recurring_template_id != template
            or not source.eh_recurring_period_start
            or not source.eh_recurring_period_end
            or not (
                source.eh_recurring_period_start
                <= self.change_date
                <= source.eh_recurring_period_end
            )
        ):
            raise UserError(_(
                "The posted recurring invoice changed while proration was "
                "being prepared; no credit or charge was created."
            ))
        duplicate_credit = self.env['account.move'].search([
            ('eh_recurring_proration_source_move_id', '=', source.id),
            ('eh_recurring_proration_role', '=', 'credit'),
            ('state', '!=', 'cancel'),
        ], limit=1)
        if duplicate_credit:
            raise UserError(_(
                "Source invoice %(source)s already has active proration "
                "credit %(credit)s. Review that credit before another plan "
                "change; one billed period cannot be credited twice.",
                source=source.display_name,
                credit=duplicate_credit.display_name,
            ))
        self._compute_period_end()
        if (template.line_ids
                and self.currency_id.compare_amounts(
                    self.new_amount, template.line_ids[0].price_unit,
                ) == 0):
            raise UserError(_(
                "The new price equals the template's current price; no "
                "proration change is required."
            ))
        if self.days_remaining <= 0:
            raise UserError(_(
                "No days remaining in the current period; nothing to prorate. "
                "Choose a date inside the source invoice's service period."
            ))
        if not template.line_ids:
            raise UserError(_(
                "Template %s has no lines to prorate.",
            ) % template.name)
        if len(template.line_ids) > 1:
            raise UserError(_(
                "Proration on a multi-line template is ambiguous. Split the "
                "template so each plan has its own template, or apply the "
                "proration manually."
            ))

        with self.env.cr.savepoint():
            credit = self._create_credit_note()
            invoice = self._create_new_price_invoice()
            # Post both under the savepoint. Left in draft (the prior
            # behaviour) the proration credit and catch-up charge never reach
            # AR or revenue, so the price change is invisible to the ledger;
            # the savepoint keeps the pair atomic on any failure.
            (credit + invoice).action_post()
            self._eh_reconcile_source_credit(source, credit)
            template.line_ids[0].price_unit = self.new_amount
            template.message_post(body=_(
                "Proration applied %(date)s: credited %(credit).2f "
                "(unused %(rem)s of %(len)s days at old price), invoiced "
                "%(charge).2f at new price %(new).2f. Credit move: "
                "%(credit_ref)s; new-price invoice: %(invoice_ref)s.",
                date=self.change_date.isoformat(),
                credit=self.credit_amount, rem=self.days_remaining,
                len=self.period_length_days,
                charge=self.new_period_amount, new=self.new_amount,
                credit_ref=credit.name or credit.id,
                invoice_ref=invoice.name or invoice.id,
            ))

        return self._result_action(credit + invoice)

    def _eh_reconcile_source_credit(self, source, credit):
        """Reconcile the proration credit against its exact billed source."""
        source_lines = source.line_ids.filtered(lambda line: (
            line.account_id.account_type == 'asset_receivable'
            and not line.reconciled
        ))
        credit_lines = credit.line_ids.filtered(lambda line: (
            line.account_id.account_type == 'asset_receivable'
            and not line.reconciled
        ))
        if not source_lines or not credit_lines:
            raise UserError(_(
                "Proration could not find open receivable lines on both the "
                "source invoice and credit note. The atomic pair was rolled "
                "back; review payment/reconciliation state and retry."
            ))
        lines = source_lines | credit_lines
        if (
            len(lines.mapped('company_id')) != 1
            or len(lines.mapped('account_id')) != 1
            or len(lines.mapped('currency_id')) != 1
        ):
            raise UserError(_(
                "Proration receivable lines do not share one company, "
                "account and currency; no ambiguous reconciliation was "
                "created."
            ))
        lines.reconcile()
        credit_lines.invalidate_recordset(['amount_residual', 'reconciled'])
        if any(not line.reconciled for line in credit_lines):
            raise UserError(_(
                "The proration credit did not fully reconcile against its "
                "source invoice; the atomic price change was rolled back."
            ))

    def _result_action(self, moves):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Proration result"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', 'in', moves.ids)],
        }

    def _create_credit_note(self):
        tpl = self.template_id
        line = tpl.line_ids[0]
        source = self._eh_billed_period_move().ensure_one()
        source_line = self._eh_single_source_line(source)
        move_context = dict(tpl._eh_move_create_context())
        move_context[PRORATION_MOVE_CONTEXT] = PRORATION_MOVE_CAPABILITY
        AccountMove = self.env['account.move'].with_context(**move_context)
        line_dict = {
            'name': "Proration credit: " + (line.name or tpl.name),
            'quantity': 1.0,
            'price_unit': self.credit_amount,
            'account_id': source_line.account_id.id,
        }
        if source_line.product_id:
            line_dict['product_id'] = source_line.product_id.id
        # Carry the template line's output tax onto the proration line, and
        # the fiscal position onto the move, exactly as the normal generation
        # path does (_build_invoice_vals). Without this the credit reverses
        # only the net and drops the GST/VAT it originally clawed back.
        if source_line.tax_ids:
            line_dict['tax_ids'] = [(6, 0, source_line.tax_ids.ids)]
        vals = {
            'move_type': 'out_refund',
            'partner_id': source.partner_id.id,
            'journal_id': source.journal_id.id,
            'company_id': source.company_id.id,
            'currency_id': source.currency_id.id,
            'invoice_date': self.change_date,
            'date': self.change_date,
            'eh_recurring_proration_template_id': tpl.id,
            'eh_recurring_proration_source_move_id': source.id,
            'eh_recurring_proration_date': self.change_date,
            'eh_recurring_proration_role': 'credit',
            'narration': _(
                "Proration credit: unused %(rem)s of %(len)s days at "
                "previous price.",
                rem=self.days_remaining, len=self.period_length_days,
            ),
            'invoice_line_ids': [(0, 0, line_dict)],
        }
        if source.fiscal_position_id:
            vals['fiscal_position_id'] = source.fiscal_position_id.id
        return AccountMove._eh_create_sealed(
            tpl._eh_prepare_proration_move_vals(vals)
        )

    def _create_new_price_invoice(self):
        tpl = self.template_id
        line = tpl.line_ids[0]
        source = self._eh_billed_period_move().ensure_one()
        source_line = self._eh_single_source_line(source)
        move_context = dict(tpl._eh_move_create_context())
        move_context[PRORATION_MOVE_CONTEXT] = PRORATION_MOVE_CAPABILITY
        AccountMove = self.env['account.move'].with_context(**move_context)
        line_dict = {
            'name': "Proration charge: " + (line.name or tpl.name),
            'quantity': 1.0,
            'price_unit': self.new_period_amount,
            # The posted source is authoritative for the account selected by
            # Odoo's product/fiscal-position engine. A valid product-driven
            # template need not carry an explicit line.account_id.
            'account_id': source_line.account_id.id,
        }
        if source_line.product_id:
            line_dict['product_id'] = source_line.product_id.id
        # Same output tax and fiscal-position treatment as the normal
        # generation path, so the catch-up charge is a compliant tax invoice.
        if source_line.tax_ids:
            line_dict['tax_ids'] = [(6, 0, source_line.tax_ids.ids)]
        vals = {
            'move_type': 'out_invoice',
            'partner_id': tpl.partner_id.id,
            'journal_id': tpl.journal_id.id,
            'company_id': tpl.company_id.id,
            'currency_id': tpl.currency_id.id,
            'invoice_date': self.change_date,
            'date': self.change_date,
            'eh_recurring_proration_template_id': tpl.id,
            'eh_recurring_proration_source_move_id': source.id,
            'eh_recurring_proration_date': self.change_date,
            'eh_recurring_proration_role': 'charge',
            'narration': _(
                "Proration charge at new price for %(rem)s of %(len)s days.",
                rem=self.days_remaining, len=self.period_length_days,
            ),
            'invoice_line_ids': [(0, 0, line_dict)],
        }
        if source.fiscal_position_id:
            vals['fiscal_position_id'] = source.fiscal_position_id.id
        return AccountMove._eh_create_sealed(
            tpl._eh_prepare_proration_move_vals(vals)
        )


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_recurring_proration_template_id = fields.Many2one(
        'eh.recurring.invoice.template', readonly=True, copy=False,
        ondelete='restrict', index=True, check_company=True,
    )
    eh_recurring_proration_source_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        index=True, check_company=True,
        help="Posted generated invoice whose service period is adjusted.",
    )
    eh_recurring_proration_date = fields.Date(
        readonly=True, copy=False, index=True,
    )
    eh_recurring_proration_role = fields.Selection(
        [('credit', "Credit"), ('charge', "Charge")],
        readonly=True, copy=False,
    )

    _unique_recurring_proration_role = models.Constraint(
        'UNIQUE (eh_recurring_proration_template_id, '
        'eh_recurring_proration_date, eh_recurring_proration_role)',
        'A recurring template can create only one proration credit and one '
        'proration charge per effective date.',
    )

    @staticmethod
    def _eh_proration_fields():
        return {
            'eh_recurring_proration_template_id',
            'eh_recurring_proration_source_move_id',
            'eh_recurring_proration_date',
            'eh_recurring_proration_role',
        }

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            any(vals.get(field_name) for field_name in self._eh_proration_fields())
            for vals in vals_list
        ) and not (
                self.env.context.get(PRORATION_MOVE_CONTEXT)
                is PRORATION_MOVE_CAPABILITY):
            raise AccessError(_(
                "Recurring-proration journal provenance is server-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if self._eh_proration_fields().intersection(vals) and not (
                self.env.context.get(PRORATION_MOVE_CONTEXT)
                is PRORATION_MOVE_CAPABILITY):
            raise AccessError(_(
                "Recurring-proration journal provenance is server-owned."
            ))
        return super().write(vals)

    def button_draft(self):
        protected = self.filtered('eh_recurring_proration_template_id')
        if self:
            linked_source_ids = set(self.search([
                ('eh_recurring_proration_source_move_id', 'in', self.ids),
            ]).mapped('eh_recurring_proration_source_move_id').ids)
            protected |= self.filtered(
                lambda move: move.id in linked_source_ids,
            )
        if protected:
            raise UserError(_(
                "A recurring proration entry or its billed-period source "
                "cannot be reset to draft. Correct the complete pair through "
                "an auditable reversal."
            ))
        return super().button_draft()


class EhRecurringInvoiceCorrectionWizard(models.TransientModel):
    _name = 'eh.recurring.invoice.correction.wizard'
    _description = "Recurring invoice controlled correction"

    move_id = fields.Many2one(
        'account.move',
        string="Recurring Invoice",
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    date = fields.Date(
        string="Correction Accounting Date",
        required=True,
        default=fields.Date.context_today,
    )
    reason = fields.Text(
        required=True,
        help=(
            "Audit explanation stored on the recurring template and the "
            "linked counter-entry reference."
        ),
    )

    def action_reverse(self):
        self.ensure_one()
        reversal = self.move_id._eh_correct_recurring_invoice(
            self.date, self.reason,
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _("Recurring Invoice Correction"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': reversal.id,
        }
