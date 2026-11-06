# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.gov.grant: a government grant recognised and amortised under IAS 20.

On receipt an income-related grant, or an asset-related grant on the
deferred-income basis, is credited to deferred income and released to grant
income over the matching periods. An asset-related grant on the netting basis
is deducted from the asset on receipt. Repayment first reverses unamortised
deferred income, with any excess to profit or loss.

Non-monetary grants (IAS 20.23): a grant of a non-monetary asset is
recognised at the asset's fair value. Receipt debits the received asset at
fair value and credits deferred income (income approach) or the asset
contra (netting approach); every later release and clawback flows off the
fair-value base.

Conditions and clawback (IAS 20.7/8, 20.32): each grant carries a register
of attached conditions (open / fulfilled / breached). When the grant is
configured to defer income until the conditions are met, releases to income
are blocked while any condition is open or breached. A breach accrues the
repayment obligation before cash moves: the clawback first reverses any
unamortised deferred income and charges the excess to profit or loss,
crediting a clawback liability that the Repay action later settles against
cash.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class EhGovGrant(models.Model):
    _name = 'eh.gov.grant'
    _description = "Government grant (IAS 20)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'grant_date desc, id desc'
    _rec_name = 'name'

    # State machine guard: the lifecycle state may change only through the
    # record's own action_* methods (which flag the write), never a direct
    # RPC/ORM write, so a plain user cannot skip an action and its journal
    # entry by writing state straight to a posted value.
    _eh_guarded_fields = ('state',)

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('received', "Received"),
         ('closed', "Closed"), ('repayable', "Repayable"),
         ('repaid', "Repaid"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    grant_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    grantor_id = fields.Many2one(
        'res.partner', string="Grantor", tracking=True,
        domain="['|', ('company_id', '=', False), "
               "('company_id', 'parent_of', company_id)]",
        help="Government or public-sector counterparty that awarded the "
             "grant. It is carried onto every grant journal line.")
    agreement_reference = fields.Char(
        tracking=True,
        help="Grant agreement, award letter or programme reference used to "
             "trace the accounting record to its source evidence.")
    agreement_attachment_ids = fields.Many2many(
        'ir.attachment', 'eh_gov_grant_attachment_rel',
        'grant_id', 'attachment_id', string="Agreement Attachments",
        copy=False,
        help="Award letters, signed agreements and other source evidence.")

    grant_type = fields.Selection(
        [('income_related', "Income related"),
         ('asset_related', "Asset related")],
        default='income_related', required=True, tracking=True)
    asset_approach = fields.Selection(
        [('deferred_income', "Deferred income"),
         ('deduct_asset', "Deduction from asset")],
        default='deferred_income',
        help="Presentation of an asset-related grant (IAS 20.24).")
    grant_kind = fields.Selection(
        [('monetary', "Monetary"),
         ('non_monetary', "Non-monetary asset")],
        default='monetary', required=True, tracking=True,
        help="A non-monetary grant transfers an asset (land, equipment) "
             "instead of cash. It is recognised at the fair value of the "
             "asset received (IAS 20.23): receipt debits the received asset "
             "at fair value, and every later release or clawback flows off "
             "that fair-value base.")

    amount = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True)
    asset_fair_value = fields.Monetary(
        currency_field='currency_id', tracking=True,
        help="Fair value of the non-monetary asset received; the "
             "measurement base of a non-monetary grant (IAS 20.23).")
    recognised_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Grant income recognised to date.")
    remaining = fields.Monetary(
        compute='_compute_remaining', store=True, currency_field='currency_id',
        help="Deferred income not yet released to profit or loss.")
    amortise_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to release to income on the next Amortise action.")
    amortise_date = fields.Date(
        help="Accounting date of the next Amortise release. IAS 20.12 requires "
             "the grant to be recognised over the periods that match the "
             "related costs, so each release posts in its earning period, not "
             "the grant's original period. Defaults to today when left blank.")
    repayment_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount repaid to the grantor on the next Repay action "
             "(IAS 20.32). For an accrued clawback, zero settles the full "
             "outstanding liability and a positive amount settles only that "
             "portion.")
    repayment_date = fields.Date(
        default=fields.Date.context_today,
        help="Accounting date of the next cash settlement or returned-asset "
             "entry. Defaults to today and remains editable before Repay.")

    # ---- conditions and clawback ----
    condition_ids = fields.One2many(
        'eh.gov.grant.condition', 'grant_id', copy=True)
    defer_until_conditions = fields.Boolean(
        string="Defer Income Until Conditions Met", tracking=True,
        help="IAS 20.7/8: a grant is recognised only when there is "
             "reasonable assurance the attached conditions will be complied "
             "with. When set, releases to income are blocked while any "
             "condition in the register is open or breached. Off by default "
             "so existing grants keep their behaviour.")
    open_condition_count = fields.Integer(
        compute='_compute_condition_stats')
    breached_condition_count = fields.Integer(
        compute='_compute_condition_stats')
    clawback_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount repayable to the grantor when a condition is breached "
             "(IAS 20.32). The breach accrues this as a clawback liability "
             "before cash moves; the Repay action settles it.")
    clawback_date = fields.Date(
        default=fields.Date.context_today,
        help="Accounting date of the breach clawback accrual. Defaults to "
             "today and can be set to the actual breach event date.")
    clawback_liability_account_id = fields.Many2one(
        'account.account', string="Clawback Liability Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]",
        help="Liability credited when a breach makes the grant repayable "
             "before the cash is paid (IAS 20.32); debited again when the "
             "Repay action settles the clawback.")
    clawback_accrued = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Clawback liability accrued on breach and not yet settled.")
    clawback_settled = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative breach clawback settled against cash. This durable "
             "server-owned amount prevents the same breach from being accrued "
             "twice when part of the original grant remains deferred.")
    deferred_reversed = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Deferred income reversed by a breach clawback accrual; keeps "
             "the remaining release headroom honest after a breach.")

    # ---- accounts ----
    cash_account_id = fields.Many2one(
        'account.account', string="Cash / Receivable Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_receivable', 'asset_current'])]")
    deferred_income_account_id = fields.Many2one(
        'account.account', string="Deferred Income Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['liability_current', 'liability_non_current'])]")
    grant_income_account_id = fields.Many2one(
        'account.account', string="Grant Income Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]")
    asset_account_id = fields.Many2one(
        'account.account', string="Asset Account", tracking=True,
        check_company=True,
        help="For the netting approach: the asset whose carrying amount the "
             "grant reduces.")
    received_asset_account_id = fields.Many2one(
        'account.account', string="Received Asset Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_fixed', 'asset_non_current', 'asset_current'])]",
        help="Asset account debited at fair value on receipt of a "
             "non-monetary grant (IAS 20.23).")
    repayment_expense_account_id = fields.Many2one(
        'account.account', string="Repayment Expense Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_other'])]",
        help="Where any excess of a repayment over the unamortised deferred "
             "income balance is charged to profit or loss (IAS 20.32).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general')]")

    reversal_date = fields.Date(
        tracking=True,
        help="Explicit accounting date for the controlled counter-entry. It "
             "cannot precede any grant entry or use a future/locked date.")
    reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False, index=True)
    reversed_at = fields.Datetime(readonly=True, copy=False)

    move_ids = fields.One2many('account.move', 'eh_gov_grant_id')
    move_count = fields.Integer(compute='_compute_move_count')

    notes = fields.Text()

    _check_amount = models.Constraint(
        'CHECK (amount >= 0)', 'Grant amount cannot be negative.')
    _check_fair_value = models.Constraint(
        'CHECK (asset_fair_value >= 0)',
        'The asset fair value cannot be negative.')
    _check_workflow_amounts = models.Constraint(
        'CHECK (amortise_amount >= 0 AND repayment_amount >= 0 '
        'AND clawback_amount >= 0)',
        'Amortisation, repayment and clawback amounts cannot be negative.')

    def _measurement_base(self):
        """The grant's measurement base: the cash amount for a monetary
        grant, the fair value of the asset received for a non-monetary
        grant (IAS 20.23)."""
        self.ensure_one()
        return (self.asset_fair_value if self.grant_kind == 'non_monetary'
                else self.amount)

    @api.depends('amount', 'asset_fair_value', 'grant_kind',
                 'recognised_amount', 'deferred_reversed', 'grant_type',
                 'asset_approach')
    def _compute_remaining(self):
        for g in self:
            if g.grant_type == 'asset_related' \
                    and g.asset_approach == 'deduct_asset':
                g.remaining = 0.0
            else:
                # A breach clawback reverses deferred income without
                # releasing it to income, so both recognised and reversed
                # amounts consume the base.
                g.remaining = (g._measurement_base() - g.recognised_amount
                               - g.deferred_reversed)

    def _compute_move_count(self):
        for g in self:
            g.move_count = len(g.move_ids)

    @api.depends('condition_ids.state')
    def _compute_condition_stats(self):
        for g in self:
            states = g.condition_ids.mapped('state')
            g.open_condition_count = states.count('open')
            g.breached_condition_count = states.count('breached')

    # Measurement inputs that fix the size and accounting basis of the grant.
    # Once recognition has begun (state received or later) these must not
    # change, or deferred income could be re-based above what was ever credited
    # and released beyond it (IAS 20 integrity).
    _FROZEN_FIELDS = (
        'amount', 'grant_date', 'grant_type', 'asset_approach',
        'grant_kind', 'asset_fair_value', 'received_asset_account_id',
        'deferred_income_account_id', 'asset_account_id', 'journal_id',
        'company_id', 'defer_until_conditions', 'condition_ids',
        'agreement_attachment_ids',
    )

    # These fields can be genuinely unnecessary at receipt and required only
    # by a later release/breach/settlement.  A received grant may therefore
    # fill an empty value exactly once, under manager control and before that
    # value has been consumed.  Existing values remain immutable.
    _LATE_CONFIG_FIELDS = (
        'cash_account_id', 'grant_income_account_id',
        'repayment_expense_account_id', 'clawback_amount',
        'clawback_liability_account_id', 'grantor_id',
        'agreement_reference',
    )

    _ENGINE_ANCHORS = (
        'currency_id', 'remaining', 'recognised_amount', 'clawback_accrued',
        'clawback_settled', 'deferred_reversed', 'reversed_by_id',
        'reversed_at',
    )
    _ACCOUNT_REFERENCE_FIELDS = (
        'cash_account_id', 'deferred_income_account_id',
        'grant_income_account_id', 'asset_account_id',
        'received_asset_account_id', 'repayment_expense_account_id',
        'clawback_liability_account_id',
    )

    def _eh_lock(self):
        ids = sorted(set(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_gov_grant WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [ids],
            )
            self.invalidate_recordset()
        return self

    def _eh_check_caller_scope(
            self, operation='write', require_active_company=True):
        if self.env.su:
            return
        self._eh_check_access(operation)
        if any(grant.company_id not in self.env.companies for grant in self):
            raise AccessError(_(
                "The grant company is outside the caller's allowed "
                "companies."))
        if require_active_company and any(
                grant.company_id != self.env.company for grant in self):
            raise AccessError(_(
                "Grant actions must run in the grant's exact company."))

    def _eh_validate_posting_scope(self):
        self.ensure_one()
        self._eh_check_caller_scope('write')
        if self.journal_id:
            if (not self._eh_reference_matches_company(self.journal_id)
                    or self.journal_id.type != 'general'):
                raise AccessError(_(
                    "The grant journal must be a general journal available "
                    "to the grant company hierarchy."))
            self.journal_id._eh_check_access('read')
        accounts = self.env['account.account']
        for field_name in self._ACCOUNT_REFERENCE_FIELDS:
            accounts |= self[field_name]
        if any(not self._eh_reference_matches_company(account)
               for account in accounts):
            raise AccessError(_(
                "Every grant posting account must be available to the grant "
                "company hierarchy."))
        if accounts:
            accounts._eh_check_access('read')
        if self.grantor_id:
            if not self._eh_reference_matches_company(self.grantor_id):
                raise AccessError(_(
                    "The grantor must be shared or available to the grant "
                    "company hierarchy."))
            self.grantor_id._eh_check_access('read')

    def _eh_reference_matches_company(self, record):
        """Use each accounting model's authoritative company hierarchy."""
        self.ensure_one()
        if not record:
            return True
        domain_builder = getattr(record, '_check_company_domain', None)
        filtered_domain = getattr(record, 'filtered_domain', None)
        if callable(domain_builder) and callable(filtered_domain):
            try:
                return bool(record.filtered_domain(
                    domain_builder(self.company_id),
                ))
            except (AttributeError, TypeError, ValueError):
                # Odoo 16-18 use the exact company fields below.
                pass
        if 'company_ids' in record._fields:
            return self.company_id in record.company_ids
        if 'company_id' in record._fields:
            return not record.company_id or record.company_id == self.company_id
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._ENGINE_ANCHORS)
                and not (self.env.su
                         and self.env.context.get('eh_grant_engine'))):
            raise AccessError(_(
                "Grant recognition and clawback balances are server-owned."))
        prepared = []
        for incoming in vals_list:
            vals = dict(incoming)
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id,
                ).exists()
                if not company:
                    raise UserError(_("Select an existing grant company."))
                if not self.env.su and company not in self.env.companies:
                    raise AccessError(_(
                        "The grant company is outside your allowed companies."
                    ))
                sequence_date = fields.Date.to_date(
                    vals.get('grant_date') or fields.Date.context_today(self)
                )
                sequence = self._eh_company_sequence(company)
                vals['name'] = sequence.with_context(
                    ir_sequence_date=sequence_date,
                ).next_by_id(sequence_date=sequence_date) or '/'
            prepared.append(vals)
        return super().create(prepared)

    @api.model
    def _eh_company_sequence(self, company):
        """Return a race-safe annual counter owned by ``company``."""
        company.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (company.id,),
        )
        Sequence = self.env['ir.sequence'].sudo().with_context(
            active_test=False,
        )
        sequence = Sequence.search([
            ('code', '=', 'eh.gov.grant'),
            ('company_id', '=', company.id),
        ], order='active desc, id', limit=1)
        if not sequence:
            sequence = Sequence.create({
                'name': _(
                    "EH Government Grant Reference - %s",
                    company.display_name,
                ),
                'code': 'eh.gov.grant',
                'prefix': 'GRANT/%(year)s/',
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
        else:
            configuration = {
                'active': True,
                'prefix': 'GRANT/%(year)s/',
                'padding': 4,
                'use_date_range': True,
            }
            changed = {
                field_name: value
                for field_name, value in configuration.items()
                if sequence[field_name] != value
            }
            if changed:
                sequence.write(changed)
        return sequence

    # States in which the grant carries a posted GL move (receipt / release /
    # repayment). Its figures are frozen and its record cannot be deleted.
    _POSTED_STATES = ('received', 'closed', 'repayable', 'repaid', 'reversed')

    def _eh_validate_late_configuration(self, vals):
        """Allow only one manager-reviewed fill of unused optional inputs."""
        touched = set(vals) & set(self._LATE_CONFIG_FIELDS)
        if not touched:
            return
        for grant in self.filtered(lambda rec: rec.state in self._POSTED_STATES):
            changed = []
            for field_name in touched:
                current = grant[field_name]
                new_value = vals[field_name]
                if field_name == 'clawback_amount':
                    new_amount = float(new_value or 0.0)
                    if grant.currency_id.compare_amounts(
                            current, new_amount) == 0:
                        continue
                    allowed = (
                        grant.state in ('received', 'closed')
                        and grant.currency_id.is_zero(current)
                        and grant.currency_id.compare_amounts(
                            new_amount, 0.0) > 0
                        and grant.currency_id.is_zero(grant.clawback_accrued)
                        and grant.currency_id.is_zero(grant.clawback_settled)
                    )
                elif field_name == 'agreement_reference':
                    current_text = (current or '').strip()
                    new_text = (new_value or '').strip()
                    if current_text == new_text:
                        continue
                    allowed = (
                        grant.state not in ('repaid', 'reversed')
                        and not current_text
                        and bool(new_text)
                    )
                else:
                    new_id = int(new_value or 0)
                    if current.id == new_id:
                        continue
                    allowed_states = (
                        ('received', 'closed', 'repayable')
                        if field_name == 'grantor_id'
                        else ('received', 'closed')
                    )
                    allowed = (
                        grant.state in allowed_states
                        and not current
                        and bool(new_id)
                        and not (
                            field_name == 'grant_income_account_id'
                            and not grant.currency_id.is_zero(
                                grant.recognised_amount)
                        )
                        and not (
                            field_name == 'clawback_liability_account_id'
                            and not grant.currency_id.is_zero(
                                grant.clawback_accrued)
                        )
                    )
                if not allowed:
                    changed.append(field_name)
            if changed:
                raise UserError(_(
                    "Grant %(name)s already carries posted evidence. Optional "
                    "later-use configuration may only fill an empty value "
                    "once, before that value is consumed. Blocked field(s): "
                    "%(fields)s.",
                    name=grant.display_name, fields=', '.join(changed),
                ))
            if touched:
                grant._check_manager()

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock()
        self._eh_validate_late_configuration(vals)
        if 'reversal_date' in vals:
            altered = self.filtered(lambda grant: (
                grant.state == 'reversed'
                and grant.reversal_date
                != fields.Date.to_date(vals.get('reversal_date'))
            ))
            if altered:
                raise UserError(_(
                    "The accounting date of a posted grant reversal is "
                    "immutable."))
        if (set(vals) & set(self._ENGINE_ANCHORS)
                and not (self.env.su
                         and self.env.context.get('eh_grant_engine'))):
            raise AccessError(_(
                "Grant recognition and clawback balances are server-owned."))
        frozen = [f for f in self._FROZEN_FIELDS if f in vals]
        # recognised_amount is a posted figure (grant income recognised to
        # date) driving the release schedule. action_amortise legitimately
        # increments it under the eh_grant_amortise flag; a raw ORM write of
        # it on a posted grant by anyone else is refused.
        if 'recognised_amount' in vals and not (
                self.env.su and self.env.context.get('eh_grant_amortise')):
            frozen.append('recognised_amount')
        # clawback_accrued and deferred_reversed are likewise posted figures
        # (the breach accrual and its deferred-income reversal). Only the
        # sanctioned clawback flows may move them.
        if not (self.env.su and self.env.context.get('eh_grant_clawback')):
            for fname in ('clawback_accrued', 'deferred_reversed'):
                if fname in vals:
                    frozen.append(fname)
        # The settlement must debit the same account that carries the
        # accrual, so the liability account is locked while a clawback is
        # outstanding.
        if 'clawback_liability_account_id' in vals and not (
                self.env.su and self.env.context.get('eh_grant_clawback')):
            pending = self.filtered(lambda g: g.clawback_accrued)
            if pending:
                raise UserError(_(
                    "The clawback liability account on %(name)s cannot "
                    "change while an accrued clawback is outstanding; the "
                    "settlement must post to the account that carries the "
                    "accrual. Settle it with the Repay action first.",
                    name=', '.join(pending.mapped('display_name'))))
        if frozen:
            for g in self:
                if g.state in self._POSTED_STATES:
                    raise UserError(_(
                        "Grant %(name)s has been received: its amount, "
                        "accounts and measurement basis are frozen and cannot "
                        "be re-based after recognition begins (IAS 20). "
                        "Blocked field(s): %(fields)s.",
                        name=g.display_name, fields=', '.join(frozen)))
        # The state of a grant that carries a posted move is itself a control
        # point: resetting it back to draft would silently lift the figure
        # freeze above and orphan the posted GL entries. A raw ORM state write
        # that moves a posted grant OUT of the posted set (toward draft), made
        # without the sanctioned-transition context flag, must be
        # manager-gated so a plain user cannot un-freeze a GL-backed grant.
        # The action methods carry the flag on their own legitimate
        # transitions (which stay within the posted set) after their manager
        # check and move handling.
        if 'state' in vals and not (
                self.env.su
                and self.env.context.get('eh_grant_state_change')):
            leaving = self.filtered(
                lambda g: g.state in self._POSTED_STATES
                and vals['state'] not in self._POSTED_STATES)
            if leaving:
                leaving._check_manager()
        return super().write(vals)

    def unlink(self):
        self._eh_lock()
        posted = self.filtered(lambda g: g.state in self._POSTED_STATES)
        if posted:
            raise UserError(_(
                "Grant %s carries a posted GL entry (receipt / release / "
                "repayment) and cannot be deleted; its move would be "
                "orphaned. Cancel or reverse the entries first.",
                ', '.join(posted.mapped('display_name'))))
        return super().unlink()

    @property
    def _is_netting(self):
        self.ensure_one()
        return (self.grant_type == 'asset_related'
                and self.asset_approach == 'deduct_asset')

    # ---- actions ----

    def action_receive(self):
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._eh_validate_posting_scope()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        if self.state != 'draft':
            raise UserError(_("Only a draft grant can be received."))
        self._eh_validate_agreement_provenance()
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        currency = self.currency_id
        # IAS 20.23: a non-monetary grant is measured at the fair value of
        # the asset received; a monetary grant at the cash amount.
        amount = currency.round(self._measurement_base())
        if self.grant_kind == 'non_monetary':
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_(
                    "A non-monetary grant is recognised at the fair value "
                    "of the asset received (IAS 20.23); set a positive "
                    "asset fair value."))
            if not self.received_asset_account_id:
                raise UserError(_(
                    "A non-monetary grant needs the received asset "
                    "account: the asset is debited at its fair value on "
                    "receipt (IAS 20.23)."))
            debit_account = self.received_asset_account_id
            debit_label = _(
                "Non-monetary grant asset at fair value %s", self.name)
        else:
            if currency.compare_amounts(amount, 0.0) <= 0:
                raise UserError(_("The grant amount must be positive."))
            if not self.cash_account_id:
                raise UserError(_(
                    "Configure the cash / receivable account first."))
            debit_account = self.cash_account_id
            debit_label = _("Grant received %s", self.name)
        if self._is_netting:
            if not self.asset_account_id:
                raise UserError(_(
                    "The netting approach needs an asset account."))
            legs = [
                (debit_account, amount, 0.0, debit_label),
                (self.asset_account_id, 0.0, amount,
                 _("Grant deducted from asset %s", self.name)),
            ]
        else:
            if not self.deferred_income_account_id:
                raise UserError(_(
                    "Configure the deferred income account first."))
            legs = [
                (debit_account, amount, 0.0, debit_label),
                (self.deferred_income_account_id, 0.0, amount,
                 _("Deferred grant income %s", self.name)),
            ]
        self._post_move(legs)
        # A netting grant is recognised in full as a credit to the selected
        # asset account. This module does not create a fixed-asset record or
        # recalculate its depreciation schedule; there is no deferred income
        # to release here, so the grant lifecycle completes on receipt.
        self.state = 'closed' if self._is_netting else 'received'
        return True

    def action_amortise(self):
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._eh_validate_posting_scope()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        if self.state != 'received':
            raise UserError(_(
                "Amortisation applies to a received grant."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant is deducted from the asset's "
                "carrying amount in full on receipt (IAS 20.27), so there is "
                "no deferred income for this action to release. This module "
                "posts no depreciation or amortisation schedule."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            raise UserError(_(
                "A breach clawback of %(amt).2f is outstanding on "
                "%(name)s; the grant is repayable (IAS 20.32) and no "
                "further income may be released. Settle the clawback with "
                "the Repay action.",
                amt=self.clawback_accrued, name=self.display_name))
        # IAS 20.7/8: a grant is recognised only when there is reasonable
        # assurance the conditions attaching to it will be complied with.
        # The gate is opt-in (defer_until_conditions) so existing grants
        # keep their behaviour.
        if self.defer_until_conditions:
            blocking = self.condition_ids.filtered(
                lambda c: c.state != 'fulfilled')
            if blocking:
                raise UserError(_(
                    "Grant income on %(name)s is deferred until its "
                    "conditions are met (IAS 20.7/8). Not yet fulfilled: "
                    "%(conditions)s.",
                    name=self.display_name,
                    conditions=', '.join(blocking.mapped('name'))))
        if not self.grant_income_account_id:
            raise UserError(_("Configure the grant income account first."))
        amount = currency.round(self.amortise_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to amortise."))
        if currency.compare_amounts(amount, self.remaining) > 0:
            raise UserError(_(
                "Cannot amortise %(amt).2f: only %(rem).2f of deferred income "
                "remains.", amt=amount, rem=self.remaining))
        # IAS 20.12: the release is recognised in the period whose costs it
        # matches, so it posts on the amortisation date, not the grant date.
        release_date = self.amortise_date or fields.Date.context_today(self)
        self._post_move([
            (self.deferred_income_account_id, amount, 0.0,
             _("Release deferred grant %s", self.name)),
            (self.grant_income_account_id, 0.0, amount,
             _("Grant income %s", self.name)),
        ], move_date=release_date)
        self.sudo().with_context(eh_grant_amortise=True).write({
            'recognised_amount': self.recognised_amount + amount,
            'amortise_amount': 0.0,
            'amortise_date': False,
        })
        if currency.is_zero(self.remaining):
            self.state = 'closed'
        return True

    def action_accrue_clawback(self):
        """Accrue the repayment obligation arising from a breach (IAS 20.32).

        A grant that becomes repayable is accounted for prospectively: the
        repayment is applied first against any unamortised deferred credit,
        and the excess is recognised immediately in profit or loss. When
        the cash has not yet been paid, the obligation is carried as a
        clawback liability: Dr deferred income (up to the unamortised
        balance) + Dr repayment expense (excess) / Cr clawback liability
        (full clawback). The Repay action later settles the liability
        against cash. Normally triggered by breaching a condition in the
        register; also available directly once a condition is breached.
        """
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._eh_validate_posting_scope()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        if self.state not in ('received', 'closed'):
            raise UserError(_(
                "A clawback accrues on a received or fully amortised grant "
                "on the deferred-income basis."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant was deducted from the asset's "
                "carrying amount on receipt (IAS 20.27); a repayment is "
                "recorded as an increase to the asset's carrying amount in "
                "the asset register, not through this clawback accrual."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            raise UserError(_(
                "A clawback of %(amt).2f is already accrued on %(name)s; "
                "settle it with the Repay action.",
                amt=self.clawback_accrued, name=self.display_name))
        if currency.compare_amounts(self.clawback_settled, 0.0) > 0:
            raise UserError(_(
                "The breach clawback on %(name)s was already settled; it "
                "cannot be accrued twice.", name=self.display_name))
        amount = currency.round(self.clawback_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_(
                "Set the clawback amount on %(name)s: the amount repayable "
                "to the grantor under the breached condition (IAS 20.32).",
                name=self.display_name))
        if not self.clawback_liability_account_id:
            raise UserError(_(
                "Configure the clawback liability account on %(name)s so "
                "the repayment obligation can be accrued before the cash "
                "is paid.", name=self.display_name))
        if not self.deferred_income_account_id:
            raise UserError(_("Configure the deferred income account first."))
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        deferred_balance = currency.round(self.remaining)
        # IAS 20.32 order: the clawback first reverses the unamortised
        # deferred income; only the excess hits profit or loss.
        reversed_deferred = min(amount, deferred_balance)
        excess = currency.round(amount - reversed_deferred)
        legs = []
        if currency.compare_amounts(reversed_deferred, 0.0) > 0:
            legs.append((
                self.deferred_income_account_id, reversed_deferred, 0.0,
                _("Reverse unamortised grant on breach %s", self.name)))
        if currency.compare_amounts(excess, 0.0) > 0:
            if not self.repayment_expense_account_id:
                raise UserError(_(
                    "The clawback exceeds the unamortised deferred income "
                    "by %(exc).2f. Configure the repayment expense account "
                    "so the excess can be charged to profit or loss "
                    "(IAS 20.32).", exc=excess))
            legs.append((
                self.repayment_expense_account_id, excess, 0.0,
                _("Grant clawback excess to profit or loss %s", self.name)))
        legs.append((
            self.clawback_liability_account_id, 0.0, amount,
            _("Grant clawback liability %s", self.name)))
        # A breach is a current-period event: use its explicit event date,
        # never the grant's original receipt period.
        breach_date = self._eh_event_date(
            self.clawback_date, _("clawback accrual"),
        )
        self._post_move(legs, move_date=breach_date)
        self.sudo().with_context(eh_grant_clawback=True).write({
            'clawback_accrued': amount,
            'deferred_reversed': self.deferred_reversed + reversed_deferred,
            'state': 'repayable',
        })
        self.message_post(body=_(
            "Clawback accrued on breach: deferred income reversed "
            "%(rev).2f, excess to profit or loss %(exc).2f, clawback "
            "liability %(amt).2f (IAS 20.32).",
            rev=reversed_deferred, exc=excess, amt=amount))
        return True

    def action_repay(self):
        """Repay a grant that has become repayable (IAS 20.32).

        With a breach clawback outstanding, this settles the accrued
        liability against cash (the deferred-income reversal and any excess
        expense were already booked by the accrual). Otherwise the
        repayment first reverses any unamortised deferred income balance
        and any excess is recognised immediately in profit or loss as an
        expense. The resulting journal entry is balanced by construction
        and the grant moves to 'repaid'.
        """
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self._eh_validate_posting_scope()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        if self.state not in ('received', 'repayable'):
            raise UserError(_(
                "Only a received or repayable grant on the deferred-income "
                "basis can be repaid."))
        if self._is_netting:
            raise UserError(_(
                "A netting-approach grant is not repaid through this action. "
                "It was deducted from the asset's carrying amount on receipt "
                "(IAS 20.27); this module has no fixed-asset subledger or "
                "depreciation engine to remeasure that external asset record."))
        currency = self.currency_id
        if currency.compare_amounts(self.clawback_accrued, 0.0) > 0:
            if not self.journal_id:
                raise UserError(_("Configure the journal first."))
            requested = currency.round(self.repayment_amount)
            amount = (
                requested
                if currency.compare_amounts(requested, 0.0) > 0
                else currency.round(self.clawback_accrued)
            )
            if currency.compare_amounts(amount, self.clawback_accrued) > 0:
                raise UserError(_(
                    "Cannot settle %(amount).2f: only %(outstanding).2f of "
                    "clawback liability is outstanding.",
                    amount=amount, outstanding=self.clawback_accrued,
                ))
            repayment_date = self._eh_event_date(
                self.repayment_date, _("clawback settlement"),
            )
            self._post_move([
                (self.clawback_liability_account_id, amount, 0.0,
                 _("Settle grant clawback liability %s", self.name)),
                self._repayment_credit_leg(
                    amount, _("Grant clawback settled %s", self.name),
                ),
            ], move_date=repayment_date)
            outstanding = currency.round(self.clawback_accrued - amount)
            self.sudo().with_context(eh_grant_clawback=True).write({
                'clawback_accrued': outstanding,
                'clawback_settled': self.clawback_settled + amount,
                'repayment_amount': 0.0,
                'state': (
                    'repayable'
                    if not currency.is_zero(outstanding)
                    else (
                        'repaid' if currency.is_zero(self.remaining)
                        else 'received'
                    )
                ),
            })
            return True
        if not self.deferred_income_account_id:
            raise UserError(_("Configure the deferred income account first."))
        currency = self.currency_id
        amount = currency.round(self.repayment_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to repay."))
        if not self.journal_id:
            raise UserError(_("Configure the journal first."))
        deferred_balance = currency.round(self.remaining)
        # The portion of the repayment reversing deferred income, capped at the
        # unamortised balance; any remainder is an expense (excess to P&L).
        reversed_deferred = min(amount, deferred_balance)
        excess = currency.round(amount - reversed_deferred)
        legs = []
        if currency.compare_amounts(reversed_deferred, 0.0) > 0:
            legs.append((
                self.deferred_income_account_id, reversed_deferred, 0.0,
                _("Reverse unamortised grant %s", self.name)))
        if currency.compare_amounts(excess, 0.0) > 0:
            if not self.repayment_expense_account_id:
                raise UserError(_(
                    "The repayment exceeds the unamortised deferred income by "
                    "%(exc).2f. Configure the repayment expense account so the "
                    "excess can be charged to profit or loss.", exc=excess))
            legs.append((
                self.repayment_expense_account_id, excess, 0.0,
                _("Grant repayment excess to profit or loss %s", self.name)))
        legs.append(self._repayment_credit_leg(
            amount, _("Grant repaid %s", self.name),
        ))
        # A repayment (IAS 20.32) posts on its explicit settlement/return
        # date, not the original grant period.
        repayment_date = self._eh_event_date(
            self.repayment_date, _("grant repayment"),
        )
        self._post_move(legs, move_date=repayment_date)
        deferred_reversed = currency.round(
            self.deferred_reversed + reversed_deferred,
        )
        remaining_after = currency.round(
            self._measurement_base()
            - self.recognised_amount
            - deferred_reversed,
        )
        self.sudo().with_context(eh_grant_clawback=True).write({
            'deferred_reversed': deferred_reversed,
            'repayment_amount': 0.0,
            'state': (
                'repaid' if currency.is_zero(remaining_after)
                else 'received'
            ),
        })
        return True

    def action_cancel(self):
        self._check_manager()
        self._eh_check_caller_scope('write')
        self._eh_lock()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        for g in self:
            if g.move_ids:
                raise UserError(_(
                    "Reverse the posted entries before cancelling %s.",
                    g.display_name))
            g.state = 'cancelled'

    def action_reverse(self):
        """Neutralise every grant entry with exact sealed counter-entries.

        Posted grant history never returns to draft and is never deleted.
        Manager supplies one current, open accounting date; source entries and
        counter-entries remain linked, sealed, and attributable to this grant.
        A corrected grant must be recorded as a new source record.
        """
        self.ensure_one()
        self._check_manager()
        self._eh_check_caller_scope('write')
        with self.env.cr.savepoint():
            self._eh_lock()
            self._eh_validate_posting_scope()
            if self.state not in ('received', 'closed', 'repayable', 'repaid'):
                raise UserError(_(
                    "Only a posted grant can use controlled reversal."))
            if not self.reversal_date:
                raise UserError(_(
                    "Enter an explicit reversal accounting date first."))
            today = fields.Date.context_today(self)
            if self.reversal_date > today:
                raise UserError(_(
                    "A grant reversal cannot use a future accounting date."))
            originals = self.move_ids.filtered(
                lambda move: not move.reversed_entry_id,
            ).sorted(lambda move: (move.date, move.id), reverse=True)
            if not originals:
                raise UserError(_(
                    "No grant journal entries are available to reverse."))
            if self.reversal_date < max(originals.mapped('date')):
                raise UserError(_(
                    "Reversal date cannot precede the latest grant entry."))
            invalid = originals.filtered(lambda move: (
                move.state != 'posted'
                or not move.eh_sealed
                or move.eh_legacy_unverified_seal
                or move.eh_gov_grant_id != self
                or move.company_id != self.company_id
            ))
            if invalid:
                raise UserError(_(
                    "Every grant entry must retain exact verified sealed "
                    "provenance before controlled reversal."))
            existing = self.env['account.move'].sudo().search([
                ('reversed_entry_id', 'in', originals.ids),
            ], limit=1)
            if existing:
                raise UserError(_(
                    "A grant entry already has a counter-entry; duplicate "
                    "reversal is blocked."))
            self._eh_require_open_accounting_date(self.reversal_date)
            for original in originals:
                source_signature = self._eh_move_signature(original)
                expected_reverse = sorted(
                    (account_id, credit, debit)
                    for account_id, debit, credit in source_signature
                )
                reversal = original._eh_reverse_with_verified_capability([{
                    'date': self.reversal_date,
                    'journal_id': original.journal_id.id,
                    'ref': _("Reversal of government grant %s", self.name),
                }], cancel=False)
                reversal.sudo().with_context(
                    eh_grant_engine=True,
                )._eh_write_sealed_metadata(
                    {'eh_gov_grant_id': self.id},
                    {'eh_gov_grant_id'},
                )
                reversal._eh_post_verified_reversal()
                reversal.invalidate_recordset([
                    'state', 'date', 'eh_sealed', 'eh_gov_grant_id',
                    'reversed_entry_id',
                ])
                if (
                    reversal.state != 'posted'
                    or reversal.date != self.reversal_date
                    or not reversal.eh_sealed
                    or reversal.eh_gov_grant_id != self
                    or reversal.reversed_entry_id != original
                    or self._eh_move_signature(reversal) != expected_reverse
                ):
                    raise UserError(_(
                        "Grant reversal failed exact date, seal, provenance, "
                        "or accounting-signature validation."))
            self.sudo().with_context(
                eh_grant_engine=True,
                eh_grant_state_change=True,
            ).write({
                'state': 'reversed',
                'reversed_by_id': self.env.user.id,
                'reversed_at': fields.Datetime.now(),
            })
        return True

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_gov_grant_id', '=', self.id)],
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can post grant entries."))

    def _eh_validate_agreement_provenance(self):
        self.ensure_one()
        missing = []
        if not self.grantor_id:
            missing.append(_("grantor"))
        if not (self.agreement_reference or '').strip():
            missing.append(_("agreement reference"))
        if missing:
            raise UserError(_(
                "Configure the %(fields)s on grant %(grant)s before posting "
                "so every entry remains attributable to its source award.",
                fields=', '.join(missing), grant=self.display_name,
            ))

    def _eh_event_date(self, value, label):
        """Return an explicit event date that cannot predate its GL chain."""
        self.ensure_one()
        event_date = fields.Date.to_date(
            value or fields.Date.context_today(self)
        )
        posted_dates = self.move_ids.filtered(
            lambda move: move.state == 'posted',
        ).mapped('date')
        latest_date = max(posted_dates) if posted_dates else self.grant_date
        if event_date < latest_date:
            raise UserError(_(
                "The %(event)s date %(date)s cannot precede the latest grant "
                "entry date %(latest)s.",
                event=label, date=event_date, latest=latest_date,
            ))
        return event_date

    def _repayment_credit_leg(self, amount, label):
        """Credit cash for money grants or derecognise the granted asset."""
        self.ensure_one()
        if self.grant_kind == 'non_monetary':
            if not self.received_asset_account_id:
                raise UserError(_(
                    "Configure the received asset account before returning "
                    "a non-monetary grant."))
            account = self.received_asset_account_id
        else:
            if not self.cash_account_id:
                raise UserError(_(
                    "Configure the cash / receivable account first."))
            account = self.cash_account_id
        return account, 0.0, amount, label

    def _eh_require_open_accounting_date(self, posting_date):
        """Fail before Odoo can silently shift dated subledger evidence."""
        self.ensure_one()
        posting_date = fields.Date.to_date(posting_date)
        checker = getattr(self.company_id, '_get_violated_lock_dates', None)
        if checker:
            try:
                violated = checker(posting_date, False, self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    violated = checker(posting_date, False)
                except TypeError:
                    raise journal_signature_error
        else:
            fiscal_checker = self.company_id._get_user_fiscal_lock_date
            try:
                fiscal_lock = fiscal_checker(self.journal_id)
            except TypeError as journal_signature_error:
                try:
                    fiscal_lock = fiscal_checker()
                except TypeError:
                    raise journal_signature_error
            violated = (
                [(fiscal_lock, 'fiscalyear_lock_date')]
                if fiscal_lock and posting_date <= fiscal_lock
                else []
            )
        if violated:
            raise UserError(_(
                "Accounting date %(date)s for grant %(grant)s is locked. "
                "Choose an explicit open date; silent redating would destroy "
                "grant provenance.",
                date=posting_date, grant=self.display_name,
            ))

    def _eh_move_signature(self, move):
        self.ensure_one()
        return sorted(
            (
                line.account_id.id,
                self.currency_id.round(line.debit),
                self.currency_id.round(line.credit),
            )
            for line in move.line_ids
            if not self.currency_id.is_zero(line.debit - line.credit)
        )

    def _post_move(self, legs, move_date=None):
        # IAS 20.12 systematic matching and period cutoff: each move posts on
        # its own accounting date, not the grant's original period. The receipt
        # posts on the grant (receipt) date; each amortisation release posts in
        # its earning period; a repayment posts on the repayment date. Callers
        # pass move_date; it falls back to grant_date only when unset.
        self._eh_validate_agreement_provenance()
        requested_date = fields.Date.to_date(move_date or self.grant_date)
        self._eh_require_open_accounting_date(requested_date)
        lines = [(0, 0, {
            'name': label, 'account_id': account.id,
            'partner_id': self.grantor_id.id,
            'debit': debit, 'credit': credit,
        }) for account, debit, credit, label in legs]
        with self.env.cr.savepoint():
            move = self.env['account.move']._eh_create_sealed({
                'move_type': 'entry',
                'company_id': self.company_id.id,
                'date': requested_date,
                'journal_id': self.journal_id.id,
                'ref': self.name,
                'eh_sealed': True,
                'line_ids': lines,
            })
            move.sudo().with_context(
                eh_grant_engine=True,
            )._eh_write_sealed_metadata(
                {'eh_gov_grant_id': self.id},
                {'eh_gov_grant_id'},
            )
            move.action_post()
            move.invalidate_recordset(['state', 'date', 'eh_sealed'])
            if (
                move.state != 'posted'
                or move.date != requested_date
                or not move.eh_sealed
            ):
                raise UserError(_(
                    "Grant entry did not post on exact requested date "
                    "%(date)s with verified seal; posting was rolled back.",
                    date=requested_date,
                ))
            return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_gov_grant_id = fields.Many2one(
        'eh.gov.grant', string="Government Grant", readonly=True,
        index=True, ondelete='restrict', copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if (any(vals.get('eh_gov_grant_id') for vals in vals_list)
                and not (self.env.su
                         and self.env.context.get('eh_grant_engine'))):
            raise AccessError(_(
                "Government-grant move provenance is server-owned."))
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_gov_grant_id' in vals:
            if not (self.env.su
                    and self.env.context.get('eh_grant_engine')):
                raise AccessError(_(
                    "Government-grant move provenance is server-owned."))
            if any(move.eh_gov_grant_id
                   and move.eh_gov_grant_id.id
                   != (vals.get('eh_gov_grant_id') or False)
                   for move in self):
                raise AccessError(_(
                    "Government-grant move provenance is immutable."))
        return super().write(vals)
