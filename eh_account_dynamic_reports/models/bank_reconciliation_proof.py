# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank Reconciliation proof statement.

Per bank/cash journal in scope, a proof that bridges the bank's reported
cash to the company's book (GL) cash and surfaces any unexplained gap. The
bridge, authored from first principles:

    Last statement balance
  + Outstanding receipts (deposits in transit not yet on the statement)
  - Outstanding payments (cheques issued not yet cleared)
  = Adjusted bank balance

    Bank-account balance per GL
  + Outstanding receipts
  - Outstanding payments
  = Adjusted book cash

    Unexplained difference = Adjusted book cash - Adjusted bank balance

Outstanding items must be present on both sides of the bridge.  They explain
why cash recorded in the books has not yet appeared on the bank statement;
they are not themselves an unexplained bank-account difference.  When the
difference is non-zero a balance_check line is emitted (kind='balance_check'
so the PDF renderer paints it red).

Per-journal mechanics:

* Last statement balance: the most recent account.bank.statement on or
  before date_to establishes the bank-side starting point; its
  balance_start plus the sum of its in-window line amounts is the bank
  balance as the statement last saw it.
* Outstanding receipts / payments: residuals sitting in the journal's
  inbound / outbound outstanding-payment (suspense) accounts as of date_to.
  Historical residuals are rebuilt from partial-reconciliation max_date,
  so a payment settled after the report date remains visible at that date.
  Each renders as an unfoldable section listing the individual move lines
  (date, label, amount) with drill-down to the journal item.
* Book balance per GL: cumulative SUM(aml.balance) on the journal's
  default_account_id up to date_to via MoveLineQuery (the same cumulative
  read the Cash Flow report uses for cash balances).

FALLBACKS: a journal with no statement yields an empty last-statement
section (book balance only) rather than an error; a journal with no
configured outstanding accounts yields an empty outstanding section.
Unexpected accounting-read failures are allowed to reach the report
orchestrator, which records and surfaces them instead of publishing a
misleading zero balance.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.translate import LazyTranslate

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery

_lt = LazyTranslate(__name__)


class EhBankReconciliationHandler(models.AbstractModel):
    _name = 'eh.account.dynamic.report.handler.bank_reconciliation'
    _inherit = 'eh.account.dynamic.report.handler'
    _description = "Bank Reconciliation proof report handler"

    REPORT_CODE = 'bank_reconciliation'
    REPORT_NAME = _lt("Bank Reconciliation")

    # Statement evidence cannot be partitioned by these GL-only dimensions.
    # Ignore forged/stale values as well as hiding their controls in the UI;
    # otherwise only one side of the reconciliation bridge would be filtered.
    _UNSUPPORTED_OPTION_KEYS = frozenset({
        'partner_ids',
        'account_ids',
        'account_type_ids',
        'analytic_account_ids',
        'analytic_plan_ids',
        'presentation_currency_id',
        'show_zero',
    })

    @api.model
    def compute(self, options):
        options = dict(options or {})
        for key in self._UNSUPPORTED_OPTION_KEYS:
            options.pop(key, None)
        date_to = self._extract_date(options, 'date_to')
        company_ids = options.get('company_ids') or [self.env.company.id]
        posted_only = bool(options.get('posted_only', True))
        journals = self._resolve_bank_journals(options, company_ids)
        currency = self._resolve_proof_currency(
            journals, options=options, company_ids=company_ids,
        )

        meta = {
            'report_code': self.REPORT_CODE,
            'date_to': self._iso_date(date_to),
            'company_ids': sorted(int(c) for c in company_ids),
            'posted_only': posted_only,
            # Orchestrator post-processors (notably presentation-currency
            # conversion) run after compute() and therefore cannot observe the
            # local options copy above. This payload contract tells them which
            # original options must remain inert for this proof.
            'unsupported_option_keys': sorted(
                self._UNSUPPORTED_OPTION_KEYS),
            # A bank proof is authored in the bank journal currency.  This is
            # intentionally distinct from presentation-currency translation:
            # statement evidence already exists in journal currency, so the GL
            # and outstanding legs are rebuilt in that same currency.
            'proof_currency_id': currency.id,
        }

        lines = []
        grand_book = 0.0
        grand_adjusted_book = 0.0
        grand_diff = 0.0
        for journal in journals:
            (
                section_lines,
                book_balance,
                adjusted_book,
                difference,
            ) = self._build_journal_proof(
                journal, date_to=date_to,
                company_ids=company_ids, posted_only=posted_only,
                options=options, currency=currency,
            )
            lines.extend(section_lines)
            grand_book += book_balance
            grand_adjusted_book += adjusted_book
            grand_diff += difference

        if not journals:
            meta['note'] = _(
                "No bank or cash journals are in scope for this company.")

        return {
            'columns': self._build_columns(),
            'lines': lines,
            'totals': {
                'amount': self._eh_round_monetary(
                    grand_adjusted_book, currency=currency),
                'book_balance': self._eh_round_monetary(
                    grand_book, currency=currency),
                'adjusted_book_balance': self._eh_round_monetary(
                    grand_adjusted_book, currency=currency),
                'difference': self._eh_round_monetary(
                    grand_diff, currency=currency),
            },
            'generated_at': fields.Datetime.now().isoformat(),
            'meta': meta,
            'currency': self._currency_payload(currency),
        }

    # ---- column layout ----

    @api.model
    def _build_columns(self):
        return [
            {'expression_label': 'label', 'name': _("Description"),
             'figure_type': 'string'},
            {'expression_label': 'amount', 'name': _("Amount"),
             'figure_type': 'monetary'},
        ]

    # ---- journal resolution ----

    @api.model
    def _resolve_bank_journals(self, options, company_ids):
        """Bank/cash journals in scope.

        Honour options['journal_ids'] when given, filtered to bank/cash
        type; otherwise every bank/cash journal of the in-scope companies.
        """
        Journal = self.env['account.journal'].sudo()
        domain = [
            ('company_id', 'in', list(company_ids)),
            ('type', 'in', ('bank', 'cash')),
        ]
        journal_ids = options.get('journal_ids')
        if journal_ids:
            domain.append(('id', 'in', list(journal_ids)))
        return Journal.search(domain, order='id')

    @api.model
    def _resolve_proof_currency(self, journals, options, company_ids):
        """Return the one journal currency in which the proof is coherent.

        A single numeric column cannot truthfully add an AUD bank statement to
        a USD bank statement.  Keep each journal section in its native bank
        currency and fail closed when the selected journals do not share one;
        the user can then run one proof per currency without any FX plug hiding
        an unexplained reconciliation difference.
        """
        currencies = {
            (journal.currency_id or journal.company_id.currency_id).id
            for journal in journals
        }
        if len(currencies) > 1:
            raise UserError(_(
                "Bank reconciliation journals use different currencies. "
                "Select journals with one shared currency and rerun the proof."
            ))
        if currencies:
            return self.env['res.currency'].browse(next(iter(currencies)))
        return self._eh_monetary_currency(
            options=options, company_ids=company_ids,
        )

    @staticmethod
    def _currency_payload(currency):
        return {
            'id': currency.id,
            'name': currency.name,
            'symbol': currency.symbol,
            'position': currency.position,
            'decimal_places': currency.decimal_places,
            'multi_currency': False,
        }

    # ---- per-journal proof ----

    @api.model
    def _build_journal_proof(
        self, journal, date_to, company_ids, posted_only, options,
        currency=None,
    ):
        """Return lines, raw GL bank, adjusted book cash, and difference."""
        currency = currency or (
            journal.currency_id or journal.company_id.currency_id)
        section_id = "journal-%s" % journal.id
        lines = [{
            'id': "%s-header" % section_id,
            'name': _("%(journal)s (%(code)s)",
                      journal=journal.name, code=journal.code or ''),
            'level': 0,
            'columns': [{'expression_label': 'amount', 'value': ''}],
            'unfoldable': False,
            'meta': {'kind': 'section_header', 'section_id': section_id,
                     'journal_id': journal.id},
        }]

        # (a) Last statement balance (bank-side starting point).
        last_stmt_balance = self._last_statement_balance(
            journal, date_to, currency=currency)
        lines.append(self._proof_line(
            "%s-last-stmt" % section_id,
            _("Last statement balance"), last_stmt_balance,
            kind='statement_balance', currency=currency))

        # (b) Outstanding receipts (inbound suspense, unreconciled).
        receipt_lines, receipts_total = self._outstanding_section(
            journal, date_to, section_id, inbound=True,
            label=_("Outstanding receipts"), company_ids=company_ids,
            posted_only=posted_only, currency=currency)
        lines.extend(receipt_lines)

        # (c) Outstanding payments (outbound suspense, unreconciled).
        payment_lines, payments_total = self._outstanding_section(
            journal, date_to, section_id, inbound=False,
            label=_("Outstanding payments"), company_ids=company_ids,
            posted_only=posted_only, currency=currency)
        lines.extend(payment_lines)

        adjusted_bank = self._eh_round_monetary(
            last_stmt_balance + receipts_total - payments_total,
            currency=currency,
        )
        lines.append(self._proof_line(
            "%s-adjusted" % section_id,
            _("Adjusted bank balance"), adjusted_bank, kind='subtotal',
            currency=currency))

        # (d) Book balance per GL (cumulative SUM on the bank account).
        book_balance = self._book_balance(
            journal, date_to, company_ids, posted_only, options,
            currency=currency)
        lines.append(self._proof_line(
            "%s-book" % section_id,
            _("Book balance per GL"), book_balance, kind='cash_balance',
            currency=currency))

        # (e) Outstanding items also belong to book cash.  Comparing the
        # adjusted statement to the raw bank-account GL would classify every
        # legitimate deposit in transit / uncleared payment as unexplained.
        adjusted_book = self._eh_round_monetary(
            book_balance + receipts_total - payments_total,
            currency=currency,
        )
        lines.append(self._proof_line(
            "%s-adjusted-book" % section_id,
            _("Adjusted book cash"), adjusted_book, kind='subtotal',
            currency=currency))

        # (f) Unexplained difference = adjusted book - adjusted bank.
        difference = self._eh_round_monetary(
            adjusted_book - adjusted_bank, currency=currency)
        lines.append(self._proof_line(
            "%s-difference" % section_id,
            _("Unexplained difference"), difference, kind='balance_check',
            currency=currency))

        return lines, book_balance, adjusted_book, difference

    # ---- (a) last statement ----

    @api.model
    def _last_statement_balance(self, journal, date_to, currency=None):
        """balance_start + sum(in-window line amounts) of the latest
        statement on or before date_to. Zero when the journal has no
        statement (empty last-statement section, never an error)."""
        Statement = self.env['account.bank.statement'].sudo()
        # The statement.date field is computed from its posted lines and can be
        # False for a statement with no posted line yet. Fetch candidates by
        # journal and pick the most recent on or before date_to in Python,
        # treating a date-less statement as dated by its newest line. Do not
        # turn an access/database failure into an accounting zero.
        candidates = Statement.search(
            [('journal_id', '=', journal.id)],
            order='id desc')
        statement = self._pick_latest_statement(candidates, date_to)
        if not statement:
            return 0.0
        in_window = statement.line_ids.filtered(
            lambda l: l.date and l.date <= date_to)
        total = sum(in_window.mapped('amount'))
        currency = currency or (
            journal.currency_id or journal.company_id.currency_id)
        return self._eh_round_monetary(
            float(statement.balance_start or 0.0) + float(total or 0.0),
            currency=currency,
        )

    @staticmethod
    def _statement_effective_date(statement):
        """Best-effort date for a statement: its computed date, else the
        max line date, else None."""
        if statement.date:
            return statement.date
        line_dates = [l.date for l in statement.line_ids if l.date]
        return max(line_dates) if line_dates else None

    @api.model
    def _pick_latest_statement(self, candidates, date_to):
        """From a recordset of candidate statements, return the most recent
        whose effective date is on or before date_to. A statement with no
        resolvable date is eligible (an opening balance carries no line
        date) and ordered last so a dated statement always wins."""
        best = None
        best_key = None
        for stmt in candidates:
            eff = self._statement_effective_date(stmt)
            if eff is not None and eff > date_to:
                continue
            # Sort key: dated statements rank by date then id; a date-less
            # statement ranks below every dated one but is still eligible.
            key = (1, eff, stmt.id) if eff is not None else (0, None, stmt.id)
            if best is None:
                best, best_key = stmt, key
                continue
            # Compare with None-safe handling (date-less ranks lowest).
            if self._key_gt(key, best_key):
                best, best_key = stmt, key
        return best

    @staticmethod
    def _key_gt(a, b):
        """Return True if sort key a outranks b. Keys are
        (has_date, date_or_None, id); a date-less key (has_date=0) always
        ranks below a dated one."""
        if a[0] != b[0]:
            return a[0] > b[0]
        if a[0] == 1:  # both dated
            if a[1] != b[1]:
                return a[1] > b[1]
            return a[2] > b[2]
        return a[2] > b[2]  # both date-less, newest id wins

    # ---- (b/c) outstanding sections ----

    @api.model
    def _outstanding_accounts(self, journal, inbound):
        """Return the journal's inbound/outbound outstanding-payment
        account ids, guarded for journals without payment methods."""
        if inbound:
            accounts = (
                journal._get_journal_inbound_outstanding_payment_accounts())
        else:
            accounts = (
                journal._get_journal_outbound_outstanding_payment_accounts())
        return accounts.ids if accounts else []

    @staticmethod
    def _historical_residual_sql(date_to):
        """Line-currency residual of ``aml`` at ``date_to``.

        ``amount_residual`` and ``reconciled`` describe today. Rebuild from
        accounting evidence dated through the cutoff so later settlements do
        not rewrite history.  ``debit_amount_currency`` and
        ``credit_amount_currency`` are expressed in the matched line's own
        currency, which lets a foreign-currency bank proof remain entirely in
        journal currency. ``max_date`` exists on Odoo 16 through 19.
        """
        return SQL("""(
            aml.amount_currency
            - COALESCE((
                SELECT SUM(apr.debit_amount_currency)
                  FROM account_partial_reconcile apr
                 WHERE apr.debit_move_id = aml.id
                   AND apr.max_date <= %s
            ), 0)
            + COALESCE((
                SELECT SUM(apr.credit_amount_currency)
                  FROM account_partial_reconcile apr
                 WHERE apr.credit_move_id = aml.id
                   AND apr.max_date <= %s
            ), 0)
        )""", date_to, date_to)

    @api.model
    def _outstanding_section(
        self, journal, date_to, parent_section_id, inbound, label,
        company_ids, posted_only, currency=None,
    ):
        """Build an unfoldable section of unreconciled outstanding move
        lines. Returns (lines, total). Empty (header + zero total) when
        the journal has no suspense accounts or no open items."""
        sub_id = "%s-%s" % (
            parent_section_id, 'receipts' if inbound else 'payments')
        account_ids = self._outstanding_accounts(journal, inbound)
        header = {
            'id': "%s-header" % sub_id,
            'name': label,
            'level': 1,
            'columns': [{'expression_label': 'amount', 'value': ''}],
            'unfoldable': True,
            'unfolded': True,
            'meta': {'kind': 'section_line', 'section_id': sub_id},
        }
        if not account_ids:
            header['columns'] = [{'expression_label': 'amount', 'value': 0.0}]
            return [header], 0.0

        historical_residual = self._historical_residual_sql(date_to)
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_accounts(account_ids)
        # Payment accounts may be shared by multiple bank journals. Ownership
        # is the move-line journal, not merely the configured account.
        query.where_journals([journal.id])
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        # Some configurations use one shared suspense account for both payment
        # directions. Classify by signed residual so one item appears exactly
        # once: debit-positive receipts, credit-negative payments.
        query.where_raw(SQL(
            "%s > 0" if inbound else "%s < 0",
            historical_residual,
        ))
        query.select_field('id', alias='aml_id')
        query.select_field('date')
        query.select_field('currency_id')
        query.select_field('name', alias='line_label')
        query.select(SQL("am.name"), 'move_name')
        query.select(historical_residual, 'historical_residual')
        query.order_by('date', 'ASC')
        query.order_by('id', 'ASC')
        move_lines = query.execute()

        lines = [header]
        total = 0.0
        currency = currency or (
            journal.currency_id or journal.company_id.currency_id)
        for ml in move_lines:
            if ml.get('currency_id') != currency.id:
                raise UserError(_(
                    "Outstanding item %(line)s is in a different currency "
                    "from bank journal %(journal)s. Correct the journal item "
                    "before rerunning the reconciliation proof.",
                    line=ml.get('move_name') or ml.get('aml_id'),
                    journal=journal.display_name,
                ))
            residual = float(ml.get('historical_residual') or 0.0)
            if self._eh_is_zero_monetary(residual, currency=currency):
                continue
            amount = self._eh_round_monetary(
                abs(residual), currency=currency)
            total += amount
            lines.append({
                'id': "outstanding-%s" % ml['aml_id'],
                'name': self._outstanding_line_label(ml),
                'level': 2,
                'parent_id': "%s-header" % sub_id,
                'columns': [{'expression_label': 'amount', 'value': amount}],
                'unfoldable': False,
                'meta': {
                    'kind': 'outstanding_item',
                    'move_line_id': ml['aml_id'],
                    'date': (
                        self._iso_date(ml.get('date'))
                        if ml.get('date') else ''
                    ),
                },
            })
        total = self._eh_round_monetary(total, currency=currency)
        header['columns'] = [{'expression_label': 'amount', 'value': total}]
        return lines, total

    @staticmethod
    def _outstanding_line_label(ml):
        bits = []
        line_date = ml.get('date')
        if line_date:
            bits.append(
                line_date.isoformat()
                if hasattr(line_date, 'isoformat') else str(line_date)
            )
        label = ml.get('line_label') or ml.get('move_name') or '/'
        bits.append(label)
        return " ".join(b for b in bits if b)

    # ---- (d) book balance ----

    @api.model
    def _book_balance(
        self, journal, date_to, company_ids, posted_only, options,
        currency=None,
    ):
        """Cumulative journal-currency GL balance through ``date_to``."""
        account = journal.default_account_id
        if not account:
            return 0.0
        query = MoveLineQuery(self.env, company_ids=company_ids)
        query.where_accounts([account.id])
        # A default account can be shared. Each proof must reconcile only the
        # ledger entries owned by its journal.
        query.where_journals([journal.id])
        query.where_date_range(date_to=date_to)
        if posted_only:
            query.where_posted_only()
        # Statement balances are in journal currency.  ``amount_currency`` is
        # therefore the only GL amount that can be compared to them without a
        # closing-rate approximation. Group first so a malformed manual entry
        # in another currency is surfaced instead of silently raw-summed.
        query.select_field('currency_id')
        query.select(
            SQL("COALESCE(SUM(aml.amount_currency), 0)"), 'balance')
        query.group_by('currency_id')
        rows = query.execute()
        if not rows:
            return 0.0
        currency = currency or (
            journal.currency_id or journal.company_id.currency_id)
        wrong_currency = [
            row for row in rows
            if row.get('currency_id') != currency.id
            and not currency.is_zero(float(row.get('balance') or 0.0))
        ]
        if wrong_currency:
            raise UserError(_(
                "Bank journal %(journal)s contains default-account entries "
                "outside its journal currency. Correct those journal items "
                "before rerunning the reconciliation proof.",
                journal=journal.display_name,
            ))
        return self._eh_round_monetary(
            sum(
                float(row.get('balance') or 0.0)
                for row in rows
                if row.get('currency_id') == currency.id
            ),
            currency=currency,
        )

    # ---- line factory ----

    @api.model
    def _proof_line(self, line_id, name, amount, kind, currency=None):
        return {
            'id': line_id,
            'name': name,
            'level': 1,
            'columns': [
                {
                    'expression_label': 'amount',
                    'value': self._eh_round_monetary(
                        amount, currency=currency),
                },
            ],
            'unfoldable': False,
            'meta': {'kind': kind},
        }

    # ---- drilldown ----

    @api.model
    def get_drilldown_action(self, options, line_id):
        """Outstanding rows drill to the underlying journal item."""
        if not line_id or not isinstance(line_id, str):
            return None
        if not line_id.startswith('outstanding-'):
            return None
        try:
            ml_id = int(line_id.split('-', 1)[1])
        except (ValueError, IndexError):
            return None
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Item"),
            'res_model': 'account.move.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', '=', ml_id)],
        }
