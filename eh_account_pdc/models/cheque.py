# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Cheque record.

One model handles both directions:

* outgoing: we paid a vendor by cheque (drawn on our bank).
* incoming: we received a customer cheque (drawn on the customer bank).

State machine:

  draft -> registered -> presented -> cleared
                              \\-> bounced -> replaced (chained to a new
                                              cheque) or written off
                              \\-> cancelled

Outgoing cheques consume a serial from the cheque book. Incoming cheques
record the issuer bank, account, and cheque number as captured from the
physical instrument.

IFRS 9 recognition mapping (documented, not enforced by extra postings):

* registered: the entity becomes party to the contractual provisions of
  the instrument (IFRS 9.3.1.1). No journal entry: the underlying trade
  receivable or payable remains the recognised financial asset or
  liability until presentation.
* presented: no derecognition yet. The deposit entry moves the exposure
  from the receivable into a dedicated incoming-cheques-in-transit account
  (an internal control choice: the cheque is with the bank but cash is not
  yet received, IFRS 9.3.2.4(b) collection is not yet virtually certain).
  The linked invoice receivable is reconciled at this point, so the
  open credit exposure lives on the transit line until clearance.
* cleared: derecognition on settlement. Transit moves to Odoo's configured
  outstanding receipt/payment account; bank-statement reconciliation later
  transfers it to the bank GL (IFRS 9.3.2.4(a)).
* bounced: reinstatement. The present entry is reversed at the bank
  dishonour date, restoring the receivable; bank charges levied for the
  dishonour are expensed at the same date.

FX note: transit holdings of foreign currency cheques are monetary items
under IAS 21.8. The present and clear entries carry signed foreign amounts.
Clearance reconciles both transit legs, which makes Odoo recognise any
clear-date exchange gain/loss and proves the transit residual is zero.

ECL note: eh_ecl_exposure_lines() exposes the open transit holdings of
presented incoming cheques with days outstanding, so a loss allowance
engine can include exposures that have left the receivables population.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_PDC_FINANCIAL_FIELDS = frozenset({
    'name',
    'direction',
    'cheque_number',
    'book_id',
    'journal_id',
    'partner_id',
    'issuer_bank_name',
    'issuer_account',
    'amount',
    'currency_id',
    'company_id',
    'issue_date',
    'value_date',
    'invoice_id',
})


class EhCheque(models.Model):
    _name = 'eh.cheque'
    _description = "Post Dated Cheque"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
                'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'value_date desc, id desc'

    # Lifecycle state, actor/timestamp stamps, and generated accounting links
    # are audit facts. They may only be established by the cheque workflow,
    # whose transition actions run under sudo; a readonly view is not an ORM
    # security boundary.
    _eh_guarded_fields = (
        'state',
        'presented_at',
        'cleared_at',
        'bounced_at',
        'cancelled_at',
        'presented_by_id',
        'cleared_by_id',
        'bounced_by_id',
        'cancelled_by_id',
        'bounce_reason_id',
        'bounce_charges',
        'bounce_notes',
        'dishonour_date',
        'replaced_by_id',
        'replaces_id',
        'present_move_id',
        'clear_move_id',
        'bounce_move_id',
        'bounce_charge_move_id',
        'payee_name_snapshot',
    )

    name = fields.Char(
        string="Reference",
        required=True, copy=False, default='/',
        tracking=True,
    )
    direction = fields.Selection([
        ('outgoing', "Issued (Payable)"),
        ('incoming', "Received (Receivable)"),
    ], required=True, default='incoming', tracking=True)

    state = fields.Selection([
        ('draft', "Draft"),
        ('registered', "Registered"),
        ('presented', "Presented"),
        ('cleared', "Cleared"),
        ('bounced', "Bounced"),
        ('replaced', "Replaced"),
        ('cancelled', "Cancelled"),
    ], default='draft', required=True, tracking=True)

    cheque_number = fields.Char(
        required=True, copy=False, tracking=True,
        help="Cheque serial number as printed on the cheque.",
    )
    book_id = fields.Many2one(
        'eh.cheque.book', string="Cheque Book",
        domain="[('state', '=', 'in_use'), "
               "('journal_id', '=', journal_id), "
               "('company_id', '=', company_id)]",
        tracking=True,
        help="Required for issued cheques. Drives serial allocation.",
    )
    journal_id = fields.Many2one(
        'account.journal', string="Bank Journal",
        required=True, tracking=True,
        domain="[('type', '=', 'bank')]",
        ondelete='restrict',
    )

    partner_id = fields.Many2one(
        'res.partner', string="Counterparty",
        required=True, tracking=True, index=True,
    )
    payee_name_snapshot = fields.Char(
        string="Registered Payee / Counterparty Name",
        readonly=True, copy=False, tracking=True,
        help="Server-captured complete name of the counterparty at cheque "
             "registration. Printed cheque and register evidence use this "
             "snapshot so a later contact rename cannot rewrite history.",
    )
    issuer_bank_name = fields.Char(
        string="Issuer Bank",
        help="For incoming cheques: name of the bank the customer cheque "
             "is drawn on.",
    )
    issuer_account = fields.Char(
        string="Issuer Account",
        help="Customer bank account masked or last 4 digits.",
    )

    amount = fields.Monetary(required=True, tracking=True)
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    amount_in_words = fields.Char(
        compute='_compute_amount_in_words',
        help="Cheque amount spelled out, for the printed cheque face.",
    )

    @api.depends(
        'amount',
        'currency_id',
        'currency_id.name',
        'currency_id.symbol',
        'currency_id.rounding',
    )
    def _compute_amount_in_words(self):
        for cheque in self:
            currency = cheque.currency_id or cheque.company_id.currency_id
            cheque.amount_in_words = (
                currency.amount_to_text(cheque.amount or 0.0)
                if currency else ''
            )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    issue_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Date the cheque was written or received.",
    )
    value_date = fields.Date(
        required=True, tracking=True,
        help="Date on which the cheque becomes presentable.",
    )

    presented_at = fields.Datetime(readonly=True, tracking=True)
    cleared_at = fields.Datetime(readonly=True, tracking=True)
    bounced_at = fields.Datetime(readonly=True, tracking=True)
    cancelled_at = fields.Datetime(readonly=True, tracking=True)

    presented_by_id = fields.Many2one('res.users', readonly=True)
    cleared_by_id = fields.Many2one('res.users', readonly=True)
    bounced_by_id = fields.Many2one('res.users', readonly=True)
    cancelled_by_id = fields.Many2one('res.users', readonly=True)

    bounce_reason_id = fields.Many2one(
        'eh.cheque.bounce.reason', tracking=True,
    )
    bounce_charges = fields.Monetary(
        help="Bank charges levied for the bounce, captured on customer side.",
    )
    bounce_notes = fields.Text()
    dishonour_date = fields.Date(
        readonly=True, copy=False, tracking=True,
        help="Date the bank actually dishonoured the cheque. The bounce "
             "reversal and any bounce charge entry are dated here, not at "
             "the date the operator recorded the bounce.",
    )

    replaced_by_id = fields.Many2one(
        'eh.cheque', string="Replaced By", readonly=True, copy=False,
        help="If this cheque was replaced after a bounce, the new cheque.",
    )
    replaces_id = fields.Many2one(
        'eh.cheque', string="Replaces", readonly=True, copy=False,
        help="The original cheque this record replaces.",
    )

    invoice_id = fields.Many2one(
        'account.move', string="Linked Invoice",
        domain="[('move_type', 'in', ['out_invoice', 'in_invoice', "
               "'out_refund', 'in_refund'])]",
    )
    notes = fields.Text()

    present_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry posted on present (deposit / issue).",
    )
    clear_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry posted on clearance (cheque transit to the "
             "configured outstanding receipt/payment account).",
    )
    bounce_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Reversal of the present entry, posted on bounce.",
    )
    bounce_charge_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False,
        help="Journal entry expensing the bank charges levied for the "
             "dishonour (debit bounce charges expense, credit outstanding "
             "payments pending bank-statement reconciliation).",
    )

    is_overdue = fields.Boolean(
        compute='_compute_is_overdue', store=False, search='_search_is_overdue',
    )
    days_outstanding = fields.Integer(
        compute='_compute_days_outstanding', store=False,
        help="Days since the value date while the cheque is still an open "
             "exposure (registered or presented). Feeds ageing and loss "
             "allowance population.",
    )

    _uniq_book_serial = models.Constraint(
        'unique(book_id, cheque_number)',
        'A cheque serial cannot be reused within the same book.',
    )
    _check_positive_amount = models.Constraint(
        'CHECK (amount > 0)',
        'Cheque amount must be greater than zero.',
    )

    # ---- compute ----

    @api.depends('value_date', 'state')
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for cheque in self:
            cheque.is_overdue = bool(
                cheque.value_date
                and cheque.value_date < today
                and cheque.state in ('draft', 'registered', 'presented')
            )

    @api.depends('value_date', 'state')
    def _compute_days_outstanding(self):
        today = fields.Date.context_today(self)
        for cheque in self:
            if cheque.value_date and cheque.state in (
                    'registered', 'presented'):
                cheque.days_outstanding = max(
                    (today - cheque.value_date).days, 0)
            else:
                cheque.days_outstanding = 0

    def _search_is_overdue(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(_("Unsupported search operator on is_overdue."))
        positive = bool(value) if operator == '=' else not bool(value)
        today = fields.Date.context_today(self)
        domain = [
            ('value_date', '<', today),
            ('state', 'in', ['draft', 'registered', 'presented']),
        ]
        return domain if positive else ['!'] + domain

    # ---- onchange ----

    @api.onchange('direction', 'journal_id')
    def _onchange_direction_journal(self):
        if self.direction == 'incoming':
            self.book_id = False

    @api.onchange('book_id')
    def _onchange_book(self):
        if self.book_id and self.direction == 'outgoing':
            if self.book_id.state == 'in_use' and self.book_id.next_number <= self.book_id.end_number:
                self.cheque_number = str(self.book_id.next_number)

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.cheque') or '/'
                vals['name'] = seq
        cheques = super().create(vals_list)
        for cheque in cheques:
            if cheque.direction == 'outgoing' and cheque.book_id:
                cheque._validate_serial_against_book()
        return cheques

    @api.constrains('invoice_id', 'partner_id', 'direction', 'company_id')
    def _check_linked_invoice_identity(self):
        """Reject a structurally unrelated invoice at the ORM boundary.

        A view domain is only a convenience and can be bypassed by imports or
        RPC.  Account equality is not identity either: every customer usually
        shares the same receivable account.  The cheque therefore proves the
        invoice company, commercial counterparty, and debit/credit direction
        before the link can become accounting input.
        """
        for cheque in self:
            cheque._pdc_validate_linked_invoice(require_open=False)

    @api.constrains(
        'journal_id', 'book_id', 'company_id', 'issue_date', 'value_date',
    )
    def _check_cheque_accounting_scope(self):
        """Enforce server-side scope and chronology, not view-only domains."""
        for cheque in self:
            journal = cheque.journal_id
            if journal and (
                journal.type != 'bank'
                or not cheque._pdc_company_record_allowed(
                    journal, cheque.company_id,
                )
            ):
                raise UserError(_(
                    "Cheque %(cheque)s must use a bank journal available to "
                    "company %(company)s.",
                    cheque=cheque.cheque_number or cheque.name,
                    company=cheque.company_id.display_name,
                ))
            book = cheque.book_id
            if book and (
                book.company_id != cheque.company_id
                or book.journal_id != journal
            ):
                raise UserError(_(
                    "Cheque %(cheque)s must use a cheque book from the same "
                    "company and bank journal.",
                    cheque=cheque.cheque_number or cheque.name,
                ))
            if (
                cheque.issue_date
                and cheque.value_date
                and cheque.value_date < cheque.issue_date
            ):
                raise UserError(_(
                    "Cheque %(cheque)s value date cannot precede its issue "
                    "date.",
                    cheque=cheque.cheque_number or cheque.name,
                ))

    @api.model
    def _pdc_company_record_allowed(self, record, company):
        """Use each Odoo series' authoritative company-domain contract."""
        if not record or not company:
            return True
        checker = getattr(record, '_check_company_domain', None)
        if checker:
            try:
                return bool(record.filtered_domain(checker(company)))
            except (AttributeError, TypeError):
                # Odoo 16 does not expose the hierarchy-aware helper on every
                # accounting model. Its records use an exact company field.
                pass
        record_company = getattr(record, 'company_id', False)
        return not record_company or record_company == company

    def write(self, vals):
        relevant = _PDC_FINANCIAL_FIELDS.intersection(vals)
        if not self.env.su and relevant:
            self._eh_check_access('write')
            self._eh_lock_for_transition()
            frozen = self.filtered(lambda cheque: cheque.state != 'draft')
            if frozen:
                raise AccessError(_(
                    "Registered PDC cheque financial facts are immutable. "
                    "Cancel and recreate the cheque instead of changing "
                    "%(fields)s on %(cheques)s.",
                    fields=', '.join(sorted(relevant)),
                    cheques=', '.join(frozen.mapped('display_name')),
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            self._eh_lock_for_transition()
            retained = self.filtered(lambda cheque: cheque.state != 'draft')
            if retained:
                raise AccessError(_(
                    "Registered PDC cheques are accounting evidence and "
                    "cannot be deleted. Cancel them through the workflow.",
                ))
        return super().unlink()

    def _validate_serial_against_book(self):
        self.ensure_one()
        if not self.cheque_number or not self.cheque_number.isdigit():
            raise UserError(_(
                "Outgoing cheque serial must be numeric within a cheque book.",
            ))
        serial = int(self.cheque_number)
        if not (self.book_id.start_number <= serial <= self.book_id.end_number):
            raise UserError(_(
                "Cheque serial %(serial)s is outside book range "
                "%(start)s-%(end)s.",
                serial=serial,
                start=self.book_id.start_number,
                end=self.book_id.end_number,
            ))

    # ---- transitions ----

    def _eh_authorize_cheque_action(self, operation='write'):
        """Prove ACL, company rule, and manager status before any sudo."""
        self._eh_check_access(operation)
        if not self.env.su and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can operate PDC cheques.",
            ))

    def _eh_lock_for_transition(self):
        """Serialize every state decision for these cheque rows."""
        if not self.ids:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_cheque WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(sorted(self.ids)),),
        )
        self.invalidate_recordset()

    def action_print_cheque(self):
        """Render the physical cheque PDF (payee, amount, words, date,
        serial) for printing onto pre-numbered cheque stock."""
        self.ensure_one()
        self._eh_authorize_cheque_action(operation='read')
        return self.env.ref(
            'eh_account_pdc.action_report_cheque_print'
        ).report_action(self, config=False)

    def action_register(self):
        self._eh_authorize_cheque_action()
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state != 'draft':
                raise UserError(_(
                    "Only draft cheques can be registered.",
                ))
            cheque._pdc_validate_linked_invoice(require_open=True)
            if cheque.direction == 'outgoing':
                if not cheque.book_id:
                    raise UserError(_(
                        "Issued cheques require a cheque book.",
                    ))
                cheque._validate_serial_against_book()
                book = cheque.book_id
                serial = int(cheque.cheque_number)
                book.consume_next_serial(expected_serial=serial)
            # ``display_name`` is context-sensitive (a caller can request an
            # email, address, VAT, or database id in it). ``complete_name``
            # is the stored, context-independent legal/display name and is
            # therefore safe to seal as accounting evidence. The workflow
            # overwrites any value that could have arrived through a
            # privileged import rather than trusting it.
            payee_name = (
                cheque.partner_id.complete_name
                or cheque.partner_id.name
                or ''
            ).strip()
            if not payee_name:
                raise UserError(_(
                    "The cheque counterparty must have a name before the "
                    "cheque can be registered.",
                ))
            cheque.write({
                'payee_name_snapshot': payee_name,
                'state': 'registered',
            })

    def action_present(self):
        self._eh_authorize_cheque_action()
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        today = fields.Date.context_today(self)
        for cheque in self:
            if cheque.state not in ('registered',):
                raise UserError(_(
                    "Only registered cheques can be presented to the bank.",
                ))
            # Re-prove the linked open item immediately before posting.  The
            # invoice can have been paid/reversed after cheque registration;
            # silently posting an unallocated partner credit in that case
            # would make the immutable link misleading.
            cheque._pdc_validate_linked_invoice(require_open=True)
            # A post-dated cheque is not negotiable until the value date.
            # Real banks refuse to deposit it; presenting in Odoo before
            # the value date would post the transit JE in advance and
            # mismatch when the bank actually clears later. No context flag,
            # including one supplied by UID 1, may bypass this legal date.
            if cheque.value_date and cheque.value_date > today:
                raise UserError(_(
                    "Cheque %(name)s cannot be presented before its value "
                    "date %(value_date)s. Today is %(today)s.",
                    name=cheque.cheque_number or cheque.id,
                    value_date=fields.Date.to_string(cheque.value_date),
                    today=fields.Date.to_string(today),
                ))
            move = cheque._post_pdc_move('present')
            cheque.write({
                'state': 'presented',
                'presented_at': fields.Datetime.now(),
                'presented_by_id': self.env.user.id,
                'present_move_id': move.id,
            })

    def action_clear(self):
        self._eh_authorize_cheque_action()
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state != 'presented':
                raise UserError(_(
                    "Only presented cheques can be marked as cleared.",
                ))
            cheque._pdc_assert_presentation_move(cheque.present_move_id)
            move = cheque._post_pdc_move('clear')
            cheque.write({
                'state': 'cleared',
                'cleared_at': fields.Datetime.now(),
                'cleared_by_id': self.env.user.id,
                'clear_move_id': move.id,
            })

    def action_cancel(self):
        self._eh_authorize_cheque_action()
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state in ('cleared', 'replaced'):
                raise UserError(_(
                    "Cleared or replaced cheques cannot be cancelled.",
                ))
            # A presented cheque carries a posted presentation entry and, when
            # linked, a reconciled invoice marked paid. A bare cancel would
            # strand that entry and leave the invoice reading paid though
            # nothing was collected. Force the bounce path, which reverses the
            # entry and re-opens the invoice, before the cheque can be voided.
            if cheque.state == 'presented' and cheque.present_move_id \
                    and cheque.present_move_id.state == 'posted' \
                    and not cheque.clear_move_id:
                raise UserError(_(
                    "Cheque %s has a posted presentation entry. Bounce it "
                    "first (this reverses the entry and re-opens the linked "
                    "invoice); a cancelled cheque must not leave a live "
                    "journal entry behind.", cheque.name))
            cheque.write({
                'state': 'cancelled',
                'cancelled_at': fields.Datetime.now(),
                'cancelled_by_id': self.env.user.id,
            })

    def action_open_bounce_wizard(self):
        self.ensure_one()
        self._eh_authorize_cheque_action()
        if self.state != 'presented':
            raise UserError(_(
                "Only presented cheques can be bounced.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.cheque.bounce.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_cheque_id': self.id},
        }

    def action_open_replace_wizard(self):
        self.ensure_one()
        self._eh_authorize_cheque_action()
        if self.state != 'bounced':
            raise UserError(_(
                "Only bounced cheques can be replaced.",
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.cheque.replace.wizard',
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_cheque_id': self.id},
        }

    def _mark_bounced(self, reason, charges=0.0, notes=None,
                      dishonour_date=None, force_current_date=False):
        """Bounce the cheque, dating the accounting at the bank dishonour.

        ``dishonour_date`` is mandatory for every caller. The reversal and
        any bounce-charge entry share that validated accounting date, so a
        programmatic call cannot silently split one bank event across dates.
        """
        self._eh_authorize_cheque_action()
        if not dishonour_date:
            raise UserError(_(
                "A bank dishonour date is required before a cheque can be "
                "bounced. It dates both the reversal and any bank charges.",
            ))
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        for cheque in self:
            if cheque.state != 'presented':
                raise UserError(_(
                    "Only presented cheques can be bounced.",
                ))
            # Fail closed before resolving dates or creating any entry. The
            # link alone is insufficient evidence: before provenance fields
            # existed it could be repointed to any posted journal entry.
            present_move = cheque._pdc_assert_presentation_move(
                cheque.present_move_id,
            )
            acct_date = cheque._pdc_dishonour_accounting_date(
                dishonour_date, force_current_date=force_current_date,
            )
            reversal = cheque._post_pdc_move(
                'bounce', reverse_of=present_move,
                date_override=acct_date,
            )
            # Re-open the linked invoice. Presentation reconciled the
            # present move's receivable leg against the invoice, marking
            # it paid; a bounce collected nothing, so that reconciliation
            # must be broken (invoice residual and payment_state revert)
            # and the cheque's own two receivable legs netted so only the
            # invoice is left open.
            if cheque.invoice_id:
                cheque._pdc_reopen_invoice_on_bounce(reversal)
            charge_move = False
            if charges:
                charge_move = cheque._post_bounce_charge_move(
                    charges,
                    acct_date,
                )
            cheque.write({
                'state': 'bounced',
                'bounced_at': fields.Datetime.now(),
                'bounced_by_id': self.env.user.id,
                'bounce_reason_id': reason.id if reason else False,
                'bounce_charges': charges or 0.0,
                'bounce_notes': notes or False,
                'bounce_move_id': reversal.id if reversal else False,
                'bounce_charge_move_id': charge_move.id if charge_move else False,
                'dishonour_date': dishonour_date or False,
            })
            cheque.message_post(
                body=_("Cheque bounced: %s",
                       reason.name if reason else _("(no reason)")),
            )

    # ---- accounting helpers ----

    def _pdc_assert_presentation_move(self, move):
        """Prove ``move`` is this cheque's workflow-generated presentation.

        A posted state and a Many2one pointer are not provenance: an RPC
        client could historically repoint ``present_move_id`` to an unrelated
        entry and make the bounce path reverse it. The move-side fields below
        can only be created/written under sudo, so the conjunction proves the
        entry came from this cheque's own presentation transition.
        """
        self.ensure_one()
        if not move or move != self.present_move_id:
            raise UserError(_(
                "Cheque %(cheque)s has no valid presentation entry to "
                "reverse.",
                cheque=self.cheque_number or self.name,
            ))
        valid = (
            move.state == 'posted'
            and move.move_type == 'entry'
            and move.company_id == self.company_id
            and move.journal_id == self.journal_id
            and move.eh_pdc_cheque_id == self
            and move.eh_pdc_transition == 'present'
        )
        if not valid:
            raise UserError(_(
                "The journal entry linked as the presentation of cheque "
                "%(cheque)s does not carry matching server provenance. "
                "Nothing was reversed; ask an administrator to repair the "
                "cheque-to-entry link.",
                cheque=self.cheque_number or self.name,
            ))
        live_reversals = move._eh_reversal_moves().filtered(
            lambda reversal: reversal.state != 'cancel',
        )
        if live_reversals:
            raise UserError(_(
                "The presentation entry for cheque %(cheque)s has already "
                "been reversed by %(moves)s. Refusing to create a second "
                "reversal.",
                cheque=self.cheque_number or self.name,
                moves=', '.join(live_reversals.mapped('display_name')),
            ))
        return move

    def _pdc_partner_account(self):
        """Resolve counterparty account without coupling it to bank routing."""
        self.ensure_one()
        if self.direction == 'incoming':
            partner_account = self.partner_id.with_company(
                self.company_id,
            ).property_account_receivable_id
        else:
            partner_account = self.partner_id.with_company(
                self.company_id,
            ).property_account_payable_id
        if not partner_account:
            raise UserError(_(
                "Partner %s has no %s account configured for company %s.",
                self.partner_id.display_name,
                'receivable' if self.direction == 'incoming' else 'payable',
                self.company_id.display_name,
            ))
        return partner_account

    def _pdc_validate_linked_invoice(self, require_open=False):
        """Prove that ``invoice_id`` is the intended source open item.

        ``account_id`` alone is never sufficient: unrelated partners share
        control accounts and refunds carry the opposite balance sign.  Draft
        cheque editing validates stable identity; registration/presentation
        additionally require a posted, still-open line of the correct sign.
        """
        self.ensure_one()
        invoice = self.invoice_id
        if not invoice:
            return invoice.line_ids
        expected_type = (
            'out_invoice' if self.direction == 'incoming' else 'in_invoice'
        )
        if invoice.company_id != self.company_id:
            raise UserError(_(
                "Linked invoice %(invoice)s belongs to %(invoice_company)s, "
                "not cheque company %(cheque_company)s.",
                invoice=invoice.display_name,
                invoice_company=invoice.company_id.display_name,
                cheque_company=self.company_id.display_name,
            ))
        if invoice.commercial_partner_id != (
                self.partner_id.commercial_partner_id):
            raise UserError(_(
                "Linked invoice %(invoice)s belongs to %(invoice_partner)s, "
                "not cheque counterparty %(cheque_partner)s.",
                invoice=invoice.display_name,
                invoice_partner=invoice.commercial_partner_id.display_name,
                cheque_partner=(
                    self.partner_id.commercial_partner_id.display_name
                ),
            ))
        if invoice.move_type != expected_type:
            raise UserError(_(
                "%(direction)s cheque %(cheque)s requires a %(move_type)s "
                "invoice; %(invoice)s is %(actual)s and carries the wrong "
                "open-item direction.",
                direction=self.direction.title(),
                cheque=self.cheque_number or self.name,
                move_type=expected_type,
                invoice=invoice.display_name,
                actual=invoice.move_type,
            ))
        if not require_open:
            return invoice.line_ids
        if invoice.state != 'posted':
            raise UserError(_(
                "Linked invoice %(invoice)s must be posted before cheque "
                "%(cheque)s can enter the accounting workflow.",
                invoice=invoice.display_name,
                cheque=self.cheque_number or self.name,
            ))
        partner_account = self._pdc_partner_account()
        open_lines = invoice.line_ids.filtered(
            lambda line: line.account_id == partner_account
            and not line.reconciled
            and not invoice.company_currency_id.is_zero(
                line.amount_residual)
        )
        expected_positive = self.direction == 'incoming'
        signed_lines = open_lines.filtered(
            lambda line: (
                line.amount_residual > 0.0
                if expected_positive else line.amount_residual < 0.0
            )
        )
        if len(open_lines) != 1 or signed_lines != open_lines:
            raise UserError(_(
                "Linked invoice %(invoice)s must contain exactly one open "
                "%(account)s item with the correct sign for an %(direction)s "
                "cheque. It may already be paid, reversed, or use a different "
                "control account.",
                invoice=invoice.display_name,
                account=partner_account.display_name,
                direction=self.direction,
            ))
        return open_lines

    def _pdc_configured_transit_account(self):
        """Return dedicated direction-specific cheque transit account."""
        self.ensure_one()
        journal = self.journal_id
        if self.direction == 'incoming':
            transit = journal.eh_pdc_incoming_transit_account_id
            expected_type = 'asset_current'
            field_label = _("Incoming Cheques in Transit")
        else:
            transit = journal.eh_pdc_outgoing_transit_account_id
            expected_type = 'liability_current'
            field_label = _("Issued Cheques in Transit")
        if not transit:
            raise UserError(_(
                "Bank journal %(journal)s has no %(field)s account. "
                "Configure a dedicated reconcilable PDC transit account; "
                "bank-statement suspense cannot be reused.",
                journal=journal.display_name,
                field=field_label,
            ))
        if transit.account_type != expected_type or not transit.reconcile:
            raise UserError(_(
                "%(field)s on bank journal %(journal)s must be a "
                "reconcilable %(type)s account.",
                field=field_label,
                journal=journal.display_name,
                type=expected_type,
            ))
        forbidden = journal.default_account_id | journal.suspense_account_id
        method_line = (
            journal.eh_pdc_inbound_payment_method_line_id
            if self.direction == 'incoming'
            else journal.eh_pdc_outbound_payment_method_line_id
        )
        if method_line:
            forbidden |= method_line.payment_account_id
        if transit in forbidden:
            raise UserError(_(
                "%(field)s must be distinct from bank GL and bank-statement "
                "suspense and outstanding accounts.",
                field=field_label,
            ))
        return transit

    def _pdc_outstanding_account(self, direction=None):
        """Resolve Odoo outstanding account used by bank reconciliation.

        Choosing a payment-method line, rather than a free account field,
        makes the settlement leg visible to Odoo's normal outstanding
        receipts/payments and bank-reconciliation machinery.
        """
        self.ensure_one()
        direction = direction or (
            'inbound' if self.direction == 'incoming' else 'outbound')
        if direction not in ('inbound', 'outbound'):
            raise UserError(_("Unsupported PDC settlement direction."))
        journal = self.journal_id
        method_line = (
            journal.eh_pdc_inbound_payment_method_line_id
            if direction == 'inbound'
            else journal.eh_pdc_outbound_payment_method_line_id
        )
        if not method_line:
            raise UserError(_(
                "Bank journal %(journal)s has no PDC %(direction)s payment "
                "method. Select a method line with a reconcilable "
                "outstanding account before processing clearance or bank "
                "charges.",
                journal=journal.display_name,
                direction=direction,
            ))
        if (
            method_line.journal_id != journal
            or method_line.payment_type != direction
        ):
            raise UserError(_(
                "Configured PDC %(direction)s payment method does not "
                "belong to bank journal %(journal)s.",
                direction=direction,
                journal=journal.display_name,
            ))
        outstanding = method_line.payment_account_id
        if not outstanding or not outstanding.reconcile:
            raise UserError(_(
                "PDC %(direction)s payment method on %(journal)s needs a "
                "reconcilable outstanding account.",
                direction=direction,
                journal=journal.display_name,
            ))
        transit = (
            journal.eh_pdc_incoming_transit_account_id
            if direction == 'inbound'
            else journal.eh_pdc_outgoing_transit_account_id
        )
        forbidden = (
            journal.default_account_id
            | journal.suspense_account_id
            | transit
        )
        if outstanding in forbidden:
            raise UserError(_(
                "PDC %(direction)s outstanding account must be distinct "
                "from bank GL, bank-statement suspense, and cheque transit.",
                direction=direction,
            ))
        return outstanding

    def _pdc_presentation_transit_line(self, require_reconcile=False):
        """Return exact holding leg proved by presentation entry shape.

        Reading the posted leg, instead of today's journal configuration,
        preserves accounting identity if configuration changes after
        presentation and lets legacy open presentations clear their actual
        historical account.
        """
        self.ensure_one()
        move = self._pdc_assert_presentation_move(self.present_move_id)
        partner_account = self._pdc_partner_account()
        posting_lines = move.line_ids.filtered(
            lambda line: line.display_type not in (
                'line_section', 'line_subsection', 'line_note',
            ))
        partner_lines = posting_lines.filtered(
            lambda line: line.account_id == partner_account)
        transit_lines = posting_lines - partner_lines
        if len(partner_lines) != 1 or len(transit_lines) != 1:
            raise UserError(_(
                "Presentation entry for cheque %(cheque)s does not contain "
                "exactly one counterparty leg and one transit leg.",
                cheque=self.cheque_number or self.name,
            ))
        transit_line = transit_lines
        expected_amount = self.amount
        currency = self.currency_id
        wrong_side = (
            self.direction == 'incoming' and transit_line.debit <= 0.0
        ) or (
            self.direction == 'outgoing' and transit_line.credit <= 0.0
        )
        if (
            wrong_side
            or transit_line.currency_id != currency
            or not currency.is_zero(
                abs(transit_line.amount_currency) - expected_amount
            )
        ):
            raise UserError(_(
                "Presentation entry for cheque %(cheque)s has an invalid "
                "transit amount, currency, or debit/credit direction.",
                cheque=self.cheque_number or self.name,
            ))
        if require_reconcile and not transit_line.account_id.reconcile:
            raise UserError(_(
                "Transit account %(account)s on cheque %(cheque)s is not "
                "reconcilable. Enable reconciliation before clearance so "
                "FX residuals cannot remain stranded.",
                account=transit_line.account_id.display_name,
                cheque=self.cheque_number or self.name,
            ))
        return transit_line

    def _pdc_lock_date(self):
        """Return the strictest (latest) accounting lock date that applies to
        PDC postings for this cheque's company, or False when none is set.

        Field-presence guarded so this stays cross-version safe: Odoo 16/17/18
        expose period_lock_date; fiscalyear_lock_date and hard_lock_date exist
        on 19 too. A missing field is simply skipped. Postings dated on or
        before this date must be refused; a locked period is closed.
        """
        self.ensure_one()
        company = self.company_id
        candidates = (
            'fiscalyear_lock_date',
            'period_lock_date',
            'hard_lock_date',
        )
        lock = False
        for fname in candidates:
            if fname not in company._fields:
                continue
            value = company[fname]
            if value and (not lock or value > lock):
                lock = value
        return lock

    def _pdc_check_lock_date(self, move_date):
        """Refuse a PDC posting whose date falls into a locked period.

        Opt-in-safe: only blocks when a lock date is actually set on the
        company; otherwise prior behaviour is preserved so existing tests
        and un-locked databases are unaffected.
        """
        self.ensure_one()
        lock = self._pdc_lock_date()
        if lock and move_date and move_date <= lock:
            raise UserError(_(
                "Cannot post PDC entry for cheque %(name)s dated "
                "%(date)s: the period is locked up to %(lock)s. "
                "Change the accounting date to an open period.",
                name=self.cheque_number or self.name,
                date=fields.Date.to_string(move_date),
                lock=fields.Date.to_string(lock),
            ))

    def _pdc_dishonour_accounting_date(self, dishonour_date,
                                       force_current_date=False):
        """Validate and resolve the accounting date for a dishonour.

        * The dishonour cannot precede the presentation date: the bank
          cannot dishonour a cheque it has not yet received.
        * Backdating is warning-free only within an open period. When the
          dishonour date falls into a locked period the caller must set
          force_current_date, which books the reversal at today instead
          (the standard current-period alternative when the original
          period is closed).
        """
        self.ensure_one()
        today = fields.Date.context_today(self)
        if dishonour_date > today:
            raise UserError(_(
                "The dishonour date %(dishonour)s cannot be after today "
                "(%(today)s).",
                dishonour=fields.Date.to_string(dishonour_date),
                today=fields.Date.to_string(today),
            ))
        present_date = self.present_move_id.date or (
            self.presented_at and self.presented_at.date())
        if present_date and dishonour_date < present_date:
            raise UserError(_(
                "The dishonour date %(dishonour)s cannot precede the "
                "presentation date %(present)s of cheque %(name)s.",
                dishonour=fields.Date.to_string(dishonour_date),
                present=fields.Date.to_string(present_date),
                name=self.cheque_number or self.name,
            ))
        lock = self._pdc_lock_date()
        if lock and dishonour_date <= lock:
            if not force_current_date:
                raise UserError(_(
                    "The dishonour date %(dishonour)s falls in a locked "
                    "period (locked up to %(lock)s). Enable 'Post at "
                    "Current Date' to book the bounce reversal in the "
                    "current period instead, and disclose the dishonour "
                    "date on the cheque record.",
                    dishonour=fields.Date.to_string(dishonour_date),
                    lock=fields.Date.to_string(lock),
                ))
            self._pdc_check_lock_date(today)
            return today
        return dishonour_date

    def _pdc_bounce_charge_account(self):
        """Expense account for bounce charges: journal first, company
        fallback. Empty means charges stay informational (no entry)."""
        self.ensure_one()
        return (
            self.journal_id.eh_pdc_bounce_charge_account_id
            or self.company_id.eh_pdc_bounce_charge_account_id
        )

    def _post_bounce_charge_move(self, charges, acct_date):
        """Post Dr charge expense / Cr outstanding payments on dishonour.

        Bank GL changes only when bank-statement reconciliation confirms the
        withdrawal. Returns False when no expense account is configured; in
        that case the amount stays informational and chatter exposes omission.
        """
        self.ensure_one()
        expense_acc = self._pdc_bounce_charge_account()
        if not expense_acc:
            self.message_post(body=_(
                "Bounce charges %(amount)s %(currency)s recorded without a "
                "journal entry: no bounce charges expense account is "
                "configured on the bank journal or the company.",
                amount=charges,
                currency=self.currency_id.name,
            ))
            return False
        outstanding_acc = self._pdc_outstanding_account('outbound')
        self._pdc_check_lock_date(acct_date)
        currency = self.currency_id
        company_currency = self.company_id.currency_id
        if currency and currency != company_currency:
            company_amount = currency._convert(
                charges, company_currency, self.company_id, acct_date,
            )
        else:
            company_amount = charges
        ref = _("PDC bounce charges: %s") % self.name
        move = self.env['account.move']._eh_create_pdc_sealed(
            self, 'bounce_charge', {
                'journal_id': self.journal_id.id,
                'date': acct_date,
                'ref': ref,
                'company_id': self.company_id.id,
                'eh_pdc_cheque_id': self.id,
                'eh_pdc_transition': 'bounce_charge',
                'line_ids': [
                    (0, 0, {
                        'name': ref,
                        'account_id': expense_acc.id,
                        'partner_id': self.partner_id.id,
                        'currency_id': currency.id,
                        'amount_currency': charges,
                        'debit': company_amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': ref,
                        'account_id': outstanding_acc.id,
                        'partner_id': self.partner_id.id,
                        'currency_id': currency.id,
                        'amount_currency': -charges,
                        'debit': 0.0,
                        'credit': company_amount,
                    }),
                ],
            },
        )
        move.action_post()
        return move

    def _post_pdc_move(self, transition, reverse_of=False, date_override=None):
        """Post the journal entry for a PDC state transition.

        transitions: 'present', 'clear', 'bounce'
        For 'bounce' we post a reversal of `reverse_of` (the present move),
        dated at `date_override` (the validated dishonour date).
        """
        self.ensure_one()
        if transition not in ('present', 'clear', 'bounce'):
            raise UserError(_(
                "Unsupported PDC accounting transition: %(transition)s",
                transition=transition,
            ))
        ref = _("PDC %s: %s") % (transition, self.name)
        if transition == 'bounce':
            reverse_of = self._pdc_assert_presentation_move(reverse_of)
            if not date_override:
                raise UserError(_(
                    "A validated dishonour accounting date is required for "
                    "every PDC bounce reversal.",
                ))
            reversal_date = date_override
            # Guard: never reverse into a locked period. When the origin
            # period is closed we roll forward to the earliest open date;
            # this asserts the rolled date is genuinely open. A validated
            # date_override was already lock-checked, cheap to re-assert.
            self._pdc_check_lock_date(reversal_date)
            reversal = reverse_of._eh_reverse_with_verified_capability(
                [{'date': reversal_date, 'ref': ref}],
                cancel=False,
            )
            reversal._eh_set_pdc_provenance(self, 'bounce')
            self._eh_seal_reversal(reversal)
            # _reverse_moves(cancel=False) returns a draft entry; the
            # reinstatement of the receivable/payable must be recognised
            # at the dishonour date, so post it. Sudo for parity with the
            # present/clear moves, which are created and posted sudo.
            if reversal.state == 'draft':
                reversal.sudo()._eh_post_verified_reversal()
            return reversal
        partner_acc = self._pdc_partner_account()
        if transition == 'present':
            transit_acc = self._pdc_configured_transit_account()
            settlement_acc = False
        else:
            transit_acc = self._pdc_presentation_transit_line(
                require_reconcile=True,
            ).account_id
            settlement_acc = self._pdc_outstanding_account()
            if settlement_acc == transit_acc:
                raise UserError(_(
                    "Cheque %(cheque)s outstanding settlement account "
                    "cannot equal its proved presentation transit account.",
                    cheque=self.cheque_number or self.name,
                ))
        amount = self.amount
        currency = self.currency_id
        if self.direction == 'incoming':
            if transition == 'present':
                # DR incoming-cheque transit, CR receivable
                debit_acc, credit_acc = transit_acc, partner_acc
            else:  # clear
                # DR outstanding receipts, CR incoming-cheque transit
                debit_acc, credit_acc = settlement_acc, transit_acc
        else:  # outgoing
            if transition == 'present':
                # DR payable, CR issued-cheque transit
                debit_acc, credit_acc = partner_acc, transit_acc
            else:  # clear
                # DR issued-cheque transit, CR outstanding payments
                debit_acc, credit_acc = transit_acc, settlement_acc
        move_date = fields.Date.context_today(self)
        # Refuse present/clear postings into a locked period.
        self._pdc_check_lock_date(move_date)
        company_currency = self.company_id.currency_id
        # debit/credit are always in the company currency; amount_currency
        # carries the cheque-currency amount. For a foreign-currency cheque,
        # convert the amount to the company currency for the balance and keep
        # the foreign amount (signed) in amount_currency. Without this, FX
        # cheques posted with amount_currency 0 and an unconverted balance,
        # breaking reconciliation and the revaluation cron downstream.
        if currency and currency != company_currency:
            company_amount = currency._convert(
                amount, company_currency, self.company_id, move_date,
            )
        else:
            company_amount = amount
        move_vals = {
            'journal_id': self.journal_id.id,
            'date': move_date,
            'ref': ref,
            'company_id': self.company_id.id,
            'eh_pdc_cheque_id': self.id,
            'eh_pdc_transition': transition,
            'line_ids': [
                (0, 0, {
                    'name': ref,
                    'account_id': debit_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': amount,
                    'debit': company_amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': ref,
                    'account_id': credit_acc.id,
                    'partner_id': self.partner_id.id,
                    'currency_id': currency.id,
                    'amount_currency': -amount,
                    'debit': 0.0,
                    'credit': company_amount,
                }),
            ],
        }
        move = self.env['account.move']._eh_create_pdc_sealed(
            self, transition, move_vals,
        )
        move.action_post()
        if transition == 'present' and self.invoice_id:
            self._reconcile_present_with_invoice(move, partner_acc)
        if transition == 'clear':
            self._pdc_reconcile_clearance_transit(move, transit_acc)
        return move

    def _pdc_reconcile_clearance_transit(self, clear_move, transit_account):
        """Reconcile presentation and clearance holding legs atomically.

        For foreign-currency cheques Odoo creates its standard posted exchange
        difference move at clearance date. Full reconciliation is also the
        invariant proving no company- or transaction-currency residual was
        stranded in cheque transit.
        """
        self.ensure_one()
        present_line = self._pdc_presentation_transit_line(
            require_reconcile=True,
        )
        clear_lines = clear_move.line_ids.filtered(
            lambda line: line.account_id == transit_account
            and line.display_type not in (
                'line_section', 'line_subsection', 'line_note',
            )
        )
        if len(clear_lines) != 1 or present_line.account_id != transit_account:
            raise UserError(_(
                "Clearance entry for cheque %(cheque)s does not contain "
                "exactly one leg on its proved presentation transit account.",
                cheque=self.cheque_number or self.name,
            ))
        if present_line.reconciled or clear_lines.reconciled:
            raise UserError(_(
                "Cheque %(cheque)s transit leg was already reconciled before "
                "clearance. Refusing an ambiguous settlement.",
                cheque=self.cheque_number or self.name,
            ))
        transit_lines = present_line | clear_lines
        transit_lines.reconcile()
        transit_lines.invalidate_recordset([
            'reconciled', 'amount_residual', 'amount_residual_currency',
        ])
        if not all(transit_lines.mapped('reconciled')):
            raise UserError(_(
                "Cheque %(cheque)s clearance left a residual in transit. "
                "Settlement was rolled back; review currency and exchange-"
                "difference configuration.",
                cheque=self.cheque_number or self.name,
            ))

    def _reconcile_present_with_invoice(self, move, partner_acc):
        """Reconcile the present move's partner leg against the linked
        invoice's open receivable/payable line so the source invoice is
        marked paid when the cheque is deposited.

        Without this the cheque transit/AR leg and the invoice both sit as
        separate open items on the same partner for the cheque's whole
        lifecycle, breaking open-item integrity (IFRS 9). We reconcile only
        the two lines on the shared partner account so the entry stays
        balanced and the invoice's amount_residual falls to zero.
        """
        self.ensure_one()
        invoice = self.invoice_id
        invoice_lines = self._pdc_validate_linked_invoice(require_open=True)
        # The invoice's open receivable/payable line and the present move's
        # partner leg must share the same reconcilable account; only then can
        # the framework match them. AR/AP accounts are reconcilable by type.
        if not partner_acc.reconcile:
            raise UserError(_(
                "Linked invoice %(invoice)s uses non-reconcilable account "
                "%(account)s; cheque presentation was rolled back.",
                invoice=invoice.display_name,
                account=partner_acc.display_name,
            ))
        move_lines = move.line_ids.filtered(
            lambda line: line.account_id == partner_acc
            and not line.reconciled
        )
        if len(move_lines) != 1:
            raise UserError(_(
                "Cheque %(cheque)s presentation entry does not contain its "
                "single proved counterparty leg; reconciliation was rolled "
                "back.",
                cheque=self.cheque_number or self.name,
            ))
        (invoice_lines | move_lines).reconcile()

    def _pdc_reopen_invoice_on_bounce(self, reversal):
        """Undo the presentation reconciliation so the linked invoice
        re-opens, then net the present and reversal receivable legs.

        On presentation the present move's receivable leg was reconciled with
        the invoice's open receivable line (invoice -> paid). A bounce reverses
        the presentation but does not, by itself, break that reconciliation, so
        without this the invoice keeps reading paid while an orphan receivable
        from the reversal floats free. Here we: (1) break the present<->invoice
        match, which returns the invoice residual and reverts payment_state,
        then (2) reconcile the present leg against the reversal leg (equal and
        opposite on the same partner account) so the cheque nets flat and only
        the invoice is left open.
        """
        self.ensure_one()
        if not reversal or not self.present_move_id or not self.invoice_id:
            return
        partner_acc = self._pdc_partner_account()
        if not partner_acc.reconcile:
            return
        present_leg = self.present_move_id.line_ids.filtered(
            lambda l: l.account_id == partner_acc)
        # Break the invoice reconciliation (re-opens invoice + present leg).
        present_leg.filtered('reconciled').remove_move_reconcile()
        # Net the cheque's own two receivable legs so the invoice's own line
        # is the only open item left on the partner account.
        rev_leg = reversal.line_ids.filtered(
            lambda l: l.account_id == partner_acc and not l.reconciled)
        net = (present_leg | rev_leg).filtered(
            lambda l: not l.reconciled and l.account_id.reconcile)
        if len(net) >= 2:
            net.reconcile()

    # ---- loss allowance (ECL) provider hook ----

    def eh_ecl_exposure_lines(self, reporting_date=None):
        """Open credit exposures held in cheque transit for these cheques.

        Provider hook for a loss-allowance engine (eh_account_ecl populate
        step). Rationale: at present time the linked invoice receivable is
        reconciled (see _reconcile_present_with_invoice), so a presented
        incoming cheque's exposure leaves the receivables population and
        sits on the dedicated cheque-transit account until clearance. An ECL run
        that only ages open receivables would silently drop it; this hook
        returns those transit holdings so they can be added back.

        Scope: incoming presented cheques only. Registered cheques are
        excluded on purpose: their receivable is still open and is already
        captured by the standard populate-from-receivables sweep, so
        including them here would double count. Outgoing cheques are
        liabilities, out of ECL scope.

        Returns a list of dicts, one per open transit line:
        cheque_id, move_line_id, partner_id, account_id, currency_id,
        amount_residual (company currency), amount_residual_currency
        (cheque currency), due_date (value date), days_outstanding
        (reporting date minus value date, floored at zero).
        """
        self._eh_check_access('read')
        reporting_date = reporting_date or fields.Date.context_today(self)
        exposures = []
        for cheque in self:
            if cheque.direction != 'incoming':
                continue
            if cheque.state != 'presented':
                continue
            move = cheque.present_move_id
            if not move or move.state != 'posted':
                continue
            line = cheque._pdc_presentation_transit_line()
            if line.account_id.reconcile:
                if line.reconciled:
                    continue
                residual = line.amount_residual
                residual_currency = line.amount_residual_currency
                if not residual:
                    continue
            else:
                # Legacy non-reconcilable holding: residual tracking is not
                # available, so expose full posted balance until remediated.
                residual = line.balance
                residual_currency = line.amount_currency

            due = cheque.value_date or move.date
            days = (reporting_date - due).days
            exposures.append({
                'cheque_id': cheque.id,
                'move_line_id': line.id,
                'partner_id': cheque.partner_id.id,
                'account_id': line.account_id.id,
                'currency_id': (line.currency_id or cheque.currency_id).id,
                'amount_residual': residual,
                'amount_residual_currency': residual_currency,
                'due_date': due,
                'days_outstanding': max(days, 0),
            })
        return exposures

    def _mark_replaced_by(self, new_cheque):
        self.ensure_one()
        self._eh_authorize_cheque_action()
        self._eh_lock_for_transition()
        self = self._eh_workflow_action()
        if self.state != 'bounced' or self.replaced_by_id:
            raise UserError(_(
                "Only an unreplaced bounced cheque can be replaced.",
            ))
        self.write({
            'state': 'replaced',
            'replaced_by_id': new_cheque.id,
        })
        new_cheque.sudo().write({'replaces_id': self.id})

    # ---- cron ----

    @api.model
    def _cron_auto_present(self, batch_size=200):
        """Move registered cheques to presented when value_date arrives.

        Per record try/except so a single failure does not stop the batch.
        Audit trail: each transition is tracked through the standard
        message_post chain via state tracking.
        """
        today = fields.Date.context_today(self)
        domain = [
            ('state', '=', 'registered'),
            ('value_date', '<=', today),
        ]
        cheques = self._pdc_rotating_due_batch(domain, batch_size)
        # Per record savepoint via the shared batch mixin: without it, a
        # failure mid batch leaves the transaction aborted and every
        # subsequent cheque in the run also fails. The "single failure
        # does not stop the batch" promise only holds with the savepoint.
        return self._eh_for_each_savepoint(
            cheques,
            lambda cheque: cheque.action_present(),
            log_label="Auto presentation",
        )

    @api.model
    def _pdc_rotating_due_batch(self, domain, batch_size):
        """Return due-only keyset page and rotate beyond persistent failures."""
        try:
            size = int(batch_size or 200)
        except (TypeError, ValueError, OverflowError):
            size = 200
        size = min(max(size, 1), 1000)
        params = self.env['ir.config_parameter'].sudo()
        key = 'eh_account_pdc.auto_present_cursor'
        try:
            cursor = max(0, int(params.get_param(key, '0') or 0))
        except (TypeError, ValueError, OverflowError):
            cursor = 0
        cheques = self.search(
            list(domain) + [('id', '>', cursor)],
            order='id',
            limit=size,
        )
        if not cheques and cursor:
            cheques = self.search(domain, order='id', limit=size)
        params.set_param(key, str(cheques[-1].id if cheques else 0))
        return cheques

    # ---- helpers ----

    @api.depends(
        'cheque_number', 'partner_id', 'partner_id.display_name',
        'amount', 'currency_id', 'currency_id.name',
    )
    def _compute_display_name(self):
        for cheque in self:
            partner_label = (
                cheque.partner_id.display_name
                if cheque.partner_id else ''
            )
            label = "%s / %s" % (cheque.cheque_number or '', partner_label)
            if cheque.amount and cheque.currency_id:
                label = "%s (%s)" % (label, cheque.currency_id.name)
            cheque.display_name = label
