# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Late-fee invoicing wizard.

A collector reviews a partner's open overdue receivables and decides
that a late-fee invoice is justified. This wizard does the maths and
posts the invoice in one transaction.

Two fee modes are supported:

* Flat fee: a fixed amount regardless of the overdue total. Useful
  when the late-payment policy says "after 30 days, $25 fee".
* Percentage of overdue: a per-month rate applied to the overdue
  items. Useful when the policy says "1.5% per month on overdue
  balance". Each receivable is weighted by its own overdue age, so a
  newer invoice is never charged as though it shared the oldest due date.

The wizard never silently discards a request. Cases with no overdue
receivable raise UserError that names the partner; partial-month
fractions round to two decimals via Decimal.
"""

import hashlib
from decimal import Decimal, ROUND_HALF_UP

from psycopg2 import errors

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhCollectionsLateFeeWizard(models.TransientModel):
    _name = 'eh.collections.late_fee.wizard'
    _description = "Late-fee invoicing wizard"

    case_id = fields.Many2one(
        'eh.collections.case', required=True, ondelete='cascade',
    )
    partner_id = fields.Many2one(
        related='case_id.partner_id', readonly=True,
    )
    company_id = fields.Many2one(
        related='case_id.company_id', readonly=True,
    )
    currency_id = fields.Many2one(
        related='case_id.currency_id', readonly=True,
    )
    today = fields.Date(default=fields.Date.context_today, required=True)

    fee_mode = fields.Selection([
        ('flat', "Flat amount"),
        ('percent', "Percent of overdue per month"),
    ], default='percent', required=True)
    flat_amount = fields.Monetary(
        currency_field='currency_id',
        help="Used when fee_mode = 'flat'.",
    )
    percent_per_month = fields.Float(
        digits=(8, 4), default=1.5,
        help=(
            "Used when fee_mode = 'percent'. Applied to the overdue "
            "receivable items, each prorated by its own overdue days / 30."
        ),
    )

    overdue_amount = fields.Monetary(
        compute='_compute_preview', currency_field='currency_id',
    )
    days_overdue = fields.Integer(compute='_compute_preview')
    computed_fee = fields.Monetary(
        compute='_compute_preview', currency_field='currency_id',
    )
    ledger_fingerprint = fields.Char(
        compute='_compute_preview',
        help="Stable identity of the live overdue journal-item exposure.",
    )

    journal_id = fields.Many2one(
        'account.journal',
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
        help="Sales journal that issues the late-fee invoice.",
    )
    income_account_id = fields.Many2one(
        'account.account',
        # No company leaf here: account.account is single-company (company_id)
        # before Odoo 18 and multi-company (company_ids Many2many) from 18, so
        # no single static leaf evaluates on every series - a bare
        # ('company_ids', ...) leaf raises "Invalid field 'company_ids'" on
        # 16/17 when the dropdown loads. Company scope is enforced at issue
        # time in Python (see the company_leaf resolution in action_issue),
        # which reads the correct field per version; the dropdown filters by
        # account type only.
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        help="Income account where the late fee lands.",
    )
    tax_ids = fields.Many2many(
        'account.tax',
        string="Late-fee taxes",
        domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]",
        help="Output taxes charged on the late-fee line. When left empty on "
             "apply, the company's default sale tax is used so a "
             "GST-registered issuer charges output tax.",
    )
    description = fields.Char(
        default="Late-payment fee",
        required=True,
    )
    auto_post = fields.Boolean(
        default=False,
        help="When True, the fee invoice is posted immediately. Off by "
             "default so the collector can review the draft first.",
    )

    @api.depends(
        'case_id', 'fee_mode', 'flat_amount', 'percent_per_month', 'today',
    )
    def _compute_preview(self):
        for wiz in self:
            case = wiz.case_id
            exposure_days = None
            exposure = None
            wiz.ledger_fingerprint = False
            if case and wiz.today:
                exposure = case._eh_late_fee_exposure(wiz.today)
                wiz.overdue_amount = exposure['total_amount']
                wiz.days_overdue = exposure['days_overdue']
                wiz.ledger_fingerprint = exposure['ledger_fingerprint']
                if wiz.fee_mode == 'percent':
                    exposure_days = exposure['exposure_days']
            else:
                wiz.overdue_amount = case.total_overdue_amount or 0.0
            if (
                exposure is None
                and exposure_days is None
                and case.oldest_overdue_date
                and wiz.today
            ):
                wiz.days_overdue = max(
                    (wiz.today - case.oldest_overdue_date).days, 0,
                )
            elif exposure is None and exposure_days is None:
                wiz.days_overdue = 0
            wiz.computed_fee = wiz._compute_fee_amount(
                exposure_days=exposure_days,
            )

    def _compute_fee_amount(self, exposure_days=None):
        """Pure-decimal compute so wizard preview and apply agree to the
        last cent.
        """
        if self.fee_mode == 'flat':
            return float(Decimal(str(self.flat_amount or 0.0)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            ))
        rate = Decimal(str(self.percent_per_month or 0.0)) / Decimal('100')
        if exposure_days is None:
            exposure_days = (
                Decimal(str(self.overdue_amount or 0.0))
                * Decimal(str(self.days_overdue or 0))
            )
        else:
            exposure_days = Decimal(str(exposure_days))
        fee = exposure_days * rate / Decimal('30')
        return float(fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _resolve_fee_taxes(self):
        """Resolve the output taxes to charge on the late-fee line.

        A late-fee invoice is a tax invoice. In a GST-registered
        jurisdiction it must carry output tax, so when the collector does
        not pick taxes explicitly the wizard falls back to the company's
        default sale tax. The fallback is company-scoped: if the company
        has no default sale tax configured (the byte-identical
        pre-existing case), no tax is applied and behaviour is unchanged.
        """
        self.ensure_one()
        if self.tax_ids:
            return self.tax_ids
        company = self.company_id
        default_tax = company.account_sale_tax_id
        if default_tax and default_tax.company_id == company:
            return default_tax
        return self.env['account.tax'].browse()

    def _issue_key(self):
        """Fingerprint the exact live exposure and requested fee policy."""
        self.ensure_one()
        payload = '|'.join([
            str(self.company_id.id),
            str(self.case_id.id),
            str(self.partner_id.commercial_partner_id.id),
            fields.Date.to_string(self.today),
            self.ledger_fingerprint or '',
            self.fee_mode or '',
            str(Decimal(str(self.flat_amount or 0.0))),
            str(Decimal(str(self.percent_per_month or 0.0))),
            str(Decimal(str(self.computed_fee or 0.0))),
        ])
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def action_apply(self):
        self.ensure_one()
        # A collector can leave this transient preview open while payments,
        # credit notes or new invoices change the ledger. Non-stored computed
        # fields do not receive dependency invalidations from those external
        # journal items, so recompute from the authoritative open AMLs at the
        # irreversible issue boundary.
        self._compute_preview()
        if self.overdue_amount <= 0.0:
            raise UserError(_(
                "Partner %s has no overdue receivable; nothing to "
                "charge a late fee on."
            ) % (self.partner_id.display_name or self.partner_id.name))
        if self.computed_fee <= 0.0:
            raise UserError(_(
                "Computed fee is zero or negative; adjust the rate or "
                "the overdue cut-off and try again."
            ))
        journal = self.journal_id
        if not journal:
            journal = self.env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', self.company_id.id),
            ], limit=1)
        if not journal:
            raise UserError(_(
                "No sales journal is configured for company %s; cannot "
                "issue a late-fee invoice."
            ) % self.company_id.display_name)
        income = self.income_account_id
        if not income:
            # account.account became multi-company (company_ids, Many2many) in
            # Odoo 18; before that it carries a single company_id. Resolve the
            # company-scope leaf at runtime so this fallback search runs on
            # 16/17 too (a bare ('company_ids', ...) leaf raises "Invalid field
            # 'company_ids' on model 'account.account'" there). The scope is
            # preserved on every series.
            Account = self.env['account.account']
            company_leaf = (
                ('company_ids', 'in', [self.company_id.id])
                if 'company_ids' in Account._fields
                else ('company_id', '=', self.company_id.id)
            )
            income = Account.search([
                ('account_type', 'in', ('income', 'income_other')),
                company_leaf,
            ], limit=1)
        if not income:
            raise UserError(_(
                "No income account is configured for company %s; cannot "
                "post the fee."
            ) % self.company_id.display_name)
        fee_taxes = self._resolve_fee_taxes()
        issue_key = self._issue_key()
        narration = _(
            "Late-payment fee on overdue %(amt).2f, %(days)s days overdue, "
            "%(mode)s.",
            amt=self.overdue_amount, days=self.days_overdue,
            mode=("flat" if self.fee_mode == 'flat'
                  else "%.2f%% per month" % (self.percent_per_month or 0.0)),
        )
        Action = self.env['eh.collections.action']
        try:
            with self.env.cr.savepoint():
                if Action.search_count([('late_fee_key', '=', issue_key)]):
                    raise UserError(_(
                        "This exact late fee has already been issued for "
                        "case %s.",
                        self.case_id.display_name,
                    ))
                move = self.env['account.move'].create({
                    'move_type': 'out_invoice',
                    'partner_id': self.partner_id.id,
                    'journal_id': journal.id,
                    'company_id': self.company_id.id,
                    'currency_id': self.currency_id.id,
                    'invoice_date': self.today,
                    'date': self.today,
                    'narration': narration,
                    'invoice_line_ids': [(0, 0, {
                        'name': self.description,
                        'quantity': 1.0,
                        'price_unit': self.computed_fee,
                        'account_id': income.id,
                        'tax_ids': [(6, 0, fee_taxes.ids)],
                    })],
                })
                if self.auto_post:
                    move.action_post()
                # Superuser mode is deliberately scoped to the append-only
                # evidence row. Public action-log creation rejects late-fee
                # links/keys so an RPC caller cannot forge or pre-empt an
                # invoice fingerprint; sudo preserves the acting user.
                evidence = Action.sudo().create({
                    'case_id': self.case_id.id,
                    'action_type': 'late_fee',
                    'summary': _(
                        "Late-fee invoice %(invoice)s issued for "
                        "%(amount).2f",
                        invoice=move.name or move.id,
                        amount=self.computed_fee,
                    ),
                    'detail': narration,
                    'contact_made': False,
                    'late_fee_move_id': move.id,
                    'late_fee_key': issue_key,
                })
                # Force the database uniqueness check inside this savepoint;
                # a concurrent duplicate must roll its invoice back too.
                evidence.flush_recordset(['late_fee_key'])
                self.case_id.message_post(body=_(
                    "Late-fee invoice %(ref)s issued for %(amount).2f "
                    "(state: %(state)s)."
                ) % {
                    'ref': move.name or move.id,
                    'amount': self.computed_fee,
                    'state': move.state,
                })
        except errors.UniqueViolation as exc:
            if getattr(exc.diag, 'constraint_name', '') != (
                'eh_collections_action_late_fee_key_unique'
            ):
                raise
            raise UserError(_(
                "This exact late fee has already been issued for case %s.",
                self.case_id.display_name,
            )) from exc
        return {
            'type': 'ir.actions.act_window',
            'name': _("Late-fee invoice"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'res_id': move.id,
        }
