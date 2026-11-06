# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.revenue.contract: an IFRS 15 customer contract.

Recognition posts the incremental revenue since the last run, crediting
revenue and debiting first any contract liability (billed ahead of
performance) then the contract asset. Billing debits the receivable and
credits first the contract asset then the contract liability. The two
mechanics keep the net contract position (cumulative recognised less
cumulative billed) correct on the balance sheet at all times.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL
from odoo.tools import float_compare


# Private in-process capabilities.  Client context is forgeable; object
# identity is not serialisable through RPC.
_REVENUE_MOVE_LINK_CAPABILITY = object()
_REVENUE_MODIFICATION_CAPABILITY = object()


class EhRevenueContract(models.Model):
    _name = 'eh.revenue.contract'
    _description = "Revenue contract (IFRS 15)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'contract_date desc, id desc'
    _rec_name = 'name'

    # Workflow-critical field: only the contract's own transitions
    # (action_activate / action_close / action_cancel and the modification
    # path that activates a spun-off separate contract) may change state. A
    # direct RPC write to state, skipping those actions and their journal
    # entries, is refused by eh.workflow.guard.write().
    _eh_guarded_fields = (
        'state', 'amount_billed', 'financing_interest_recognised',
        'close_released_amount', 'total_ssp', 'amount_recognised',
        'contract_asset', 'contract_liability', 'financing_pv',
        'financing_component_amount', 'currency_id',
        'eh_integrity_review_required', 'eh_integrity_review_reason',
        'eh_integrity_reviewed_at', 'eh_integrity_reviewed_by_id',
        'eh_reversed_at', 'eh_reversed_by_id',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"),
         ('done', "Closed"), ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    partner_id = fields.Many2one(
        'res.partner', string="Customer", required=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    contract_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    accounting_date = fields.Date(
        required=True, default=fields.Date.context_today, copy=False,
        help="Exact ledger date for the next recognition, billing, financing, "
             "closure, or reversal action.")

    transaction_price = fields.Monetary(
        currency_field='currency_id', required=True, tracking=True,
        help="Total consideration the entity expects to be entitled to "
             "(IFRS 15.47).")
    obligation_ids = fields.One2many(
        'eh.revenue.obligation', 'contract_id', copy=True)
    total_ssp = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the obligations' standalone selling prices.")
    amount_recognised = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Revenue posted to the configured revenue account. Under a "
             "significant financing component this is the present-value "
             "revenue in the ledger, not the nominal obligation anchor.")
    amount_billed = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id')
    contract_asset = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Revenue recognised but not yet billed (IFRS 15.107).")
    contract_liability = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Amounts billed but not yet recognised (IFRS 15.106).")

    bill_amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount to bill on the next Record Billing action.")

    # ---- significant financing component (IFRS 15.60-65) ----
    # Opt-in per contract. When off, financing_pv equals the transaction price
    # and recognition posts exactly as before (byte-identical default).
    financing_component = fields.Boolean(
        tracking=True,
        help="Payment timing differs materially from the transfer of goods "
             "or services, so the promised consideration is discounted to "
             "present value and the difference recognised as interest over "
             "time (IFRS 15.60-65).")
    financing_direction = fields.Selection(
        [('advance', "Customer pays in advance"),
         ('arrears', "Customer pays in arrears")],
        default='arrears',
        help="Payment in arrears: the entity finances the customer and earns "
             "interest income. Payment in advance: the customer finances the "
             "entity and it incurs interest expense (IFRS 15.61).")
    financing_rate = fields.Float(
        digits=(7, 4), default=0.0,
        help="Annual discount rate as a decimal fraction (0.10 = 10%). The "
             "accepted range is 0% through 100%. This is the rate used to "
             "reflect the financing, i.e. the "
             "rate in a separate financing transaction between the parties "
             "(IFRS 15.64).")
    financing_period_months = fields.Integer(
        default=0,
        help="Number of months between payment and transfer used to "
             "discount the consideration to present value.")
    financing_payment_date = fields.Date(
        tracking=True,
        help="Date the financed cash flow is due (the payment date in "
             "arrears, or the transfer date in advance). When set, the "
             "financing interest accretes on a time / effective-interest "
             "basis over the exact financing_period_months horizon, "
             "independent of performance-obligation progress (IFRS 15.65). "
             "An effective financing component cannot be activated without "
             "this date.")
    financing_pv = fields.Monetary(
        compute='_compute_financing', store=True, currency_field='currency_id',
        help="Present value of the transaction price after removing the "
             "financing component; the amount recognised as revenue.")
    financing_component_amount = fields.Monetary(
        compute='_compute_financing', store=True, currency_field='currency_id',
        help="Total interest recognised over the financing period: the "
             "transaction price less its present value.")
    financing_interest_recognised = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Cumulative interest income or expense already posted.")
    financing_account_id = fields.Many2one(
        'account.account', string="Interest Account", tracking=True,
        help="Interest income (arrears) or interest expense (advance) "
             "account used for the financing component.")

    # ---- closure validation (IFRS 15) ----
    # action_close blocks while any obligation is unsatisfied. The manager
    # override releases the remaining contract liability to one of two
    # documented destinations before closing. All fields default off, so a
    # fully satisfied contract closes exactly as before.
    close_with_remainder = fields.Boolean(
        copy=False, tracking=True,
        help="Manager override: close the contract although performance "
             "obligations remain unsatisfied. Any remaining contract "
             "liability is released per the destination selection and the "
             "reason is recorded. A remaining contract asset is not "
             "touched; bill it before closing.")
    close_reason = fields.Text(
        copy=False,
        help="Why the contract is closed with unsatisfied obligations "
             "(required for the override close).")
    close_release_to = fields.Selection(
        [('income', "Release to profit or loss"),
         ('refund', "Reclassify to refund liability")],
        default='income',
        help="Destination of a remaining contract liability on an override "
             "close. Release to profit or loss when the entity expects to "
             "keep the billed-ahead consideration with no further "
             "performance and no refund due (the IFRS 15.B46 breakage "
             "pattern). Reclassify to a refund liability when the customer "
             "is owed the unperformed balance back (IFRS 15.55).")
    refund_liability_account_id = fields.Many2one(
        'account.account', string="Refund Liability Account", tracking=True,
        domain="[('account_type', 'in', ['liability_current'])]",
        help="Liability account that receives the unperformed balance when "
             "the override close reclassifies it as refundable.")
    close_released_amount = fields.Monetary(
        readonly=True, copy=False, currency_field='currency_id',
        help="Contract liability released by an override close.")

    # ---- accounts ----
    revenue_account_id = fields.Many2one(
        'account.account', string="Revenue Account", tracking=True,
        domain="[('account_type', 'in', ['income', 'income_other'])]")
    contract_asset_account_id = fields.Many2one(
        'account.account', string="Contract Asset Account", tracking=True,
        domain="[('account_type', 'in', ['asset_current', 'asset_receivable'])]")
    contract_liability_account_id = fields.Many2one(
        'account.account', string="Contract Liability Account", tracking=True,
        domain="[('account_type', 'in', ['liability_current'])]")
    receivable_account_id = fields.Many2one(
        'account.account', string="Receivable Account", tracking=True,
        domain="[('account_type', '=', 'asset_receivable')]")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', 'in', ('sale', 'general')), "
               "('company_id', '=', company_id)]")

    move_ids = fields.One2many('account.move', 'eh_revenue_contract_id')
    move_count = fields.Integer(compute='_compute_move_count')
    eh_reversal_move_ids = fields.One2many(
        'account.move', 'eh_revenue_reversal_contract_id', readonly=True,
        copy=False, string="Reversal Entries")
    reversal_reason = fields.Text(
        copy=False,
        help="Manager explanation required before reversing all contract "
             "accounting and cancelling the contract.")
    eh_reversed_at = fields.Datetime(readonly=True, copy=False)
    eh_reversed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)

    notes = fields.Text()
    eh_integrity_review_required = fields.Boolean(
        string="Legacy Integrity Review Required", readonly=True, copy=False,
        index=True,
        help="Set by an upgrade when terminal workflow state conflicts with "
             "preserved posted contract-position evidence.")
    eh_integrity_review_reason = fields.Text(
        string="Legacy Integrity Review Reason", readonly=True, copy=False)
    eh_integrity_review_note = fields.Text(
        string="Legacy Review Attestation", copy=False,
        help="Manager evidence and conclusion used to accept preserved legacy "
             "rows as the opening baseline for future accounting.")
    eh_integrity_reviewed_at = fields.Datetime(readonly=True, copy=False)
    eh_integrity_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)

    _check_price = models.Constraint(
        'CHECK (transaction_price >= 0)',
        'Transaction price cannot be negative.')
    _check_financing_period = models.Constraint(
        'CHECK (financing_period_months >= 0)',
        'The financing period cannot be negative.')

    @api.constrains('financing_rate')
    def _check_financing_rate(self):
        for contract in self:
            contract._eh_validate_financing_rate()

    @api.constrains(
        'financing_component', 'financing_rate',
        'financing_period_months', 'financing_payment_date', 'contract_date',
    )
    def _check_financing_horizon(self):
        for contract in self:
            # Drafts may be assembled field by field. Once both dates and a
            # non-zero period are present, they must describe one horizon;
            # activation below also refuses an incomplete effective schedule.
            if not (
                contract.financing_component
                and contract.financing_rate
                and contract.financing_period_months
                and contract.financing_payment_date
                and contract.contract_date
            ):
                continue
            expected = contract.contract_date + relativedelta(
                months=contract.financing_period_months,
            )
            if contract.financing_payment_date != expected:
                raise ValidationError(_(
                    "Financing payment date %(payment)s must equal contract "
                    "date %(start)s plus %(months)s month(s): %(expected)s.",
                    payment=contract.financing_payment_date,
                    start=contract.contract_date,
                    months=contract.financing_period_months,
                    expected=expected,
                ))

    def _eh_validate_financing_rate(self):
        self.ensure_one()
        if self.financing_rate < 0.0 or self.financing_rate > 1.0:
            raise ValidationError(_(
                "Financing rate must be a decimal fraction from 0 through 1 "
                "(for example, 0.10 means 10%)."
            ))
        return True

    def _eh_validate_financing_schedule(self):
        self.ensure_one()
        self._eh_validate_financing_rate()
        effective = (
            self.financing_component
            and self.financing_rate
            and self.financing_period_months
        )
        if not effective:
            return True
        if not self.financing_payment_date:
            raise ValidationError(_(
                "An effective financing component needs an exact payment or "
                "transfer date; interest cannot follow revenue progress."
            ))
        expected = self.contract_date + relativedelta(
            months=self.financing_period_months,
        )
        if self.financing_payment_date != expected:
            raise ValidationError(_(
                "Financing period and payment date describe different "
                "horizons. Expected %(expected)s.", expected=expected,
            ))
        return True

    @api.depends(
        'transaction_price', 'obligation_ids.standalone_price',
        'obligation_ids.recognised_amount', 'amount_billed',
        'close_released_amount', 'revenue_account_id', 'contract_asset_account_id',
        'contract_liability_account_id', 'move_ids.state',
        'move_ids.line_ids.account_id', 'move_ids.line_ids.balance',
    )
    def _compute_totals(self):
        for c in self:
            c.total_ssp = sum(c.obligation_ids.mapped('standalone_price'))
            posted_lines = c.move_ids.filtered(
                lambda move: (
                    move.state == 'posted'
                    and move.company_id == c.company_id
                )
            ).line_ids
            # This public total is a ledger measure, not the nominal
            # obligation anchor. Under significant financing, recognised_amount
            # therefore equals posted present-value revenue exactly.
            revenue_balance = sum(posted_lines.filtered(
                lambda line: line.account_id == c.revenue_account_id
            ).mapped('balance'))
            c.amount_recognised = c.currency_id.round(-revenue_balance)
            # Position comes from posted GL evidence. Time-based financing
            # books present-value revenue, then changes the contract balance
            # through separate interest entries; nominal anchors cannot model
            # that balance exactly.
            accounts = (
                c.contract_asset_account_id | c.contract_liability_account_id
            )
            position_lines = posted_lines.filtered(
                lambda line: line.account_id in accounts
            )
            net = c.currency_id.round(sum(position_lines.mapped('balance')))
            c.contract_asset = max(net, 0.0)
            c.contract_liability = max(-net, 0.0)

    @api.depends('financing_component', 'financing_direction', 'financing_rate',
                 'financing_period_months', 'transaction_price')
    def _compute_financing(self):
        for c in self:
            currency = c.currency_id
            price = c.transaction_price
            if (c.financing_component and c.financing_rate
                    and c.financing_period_months):
                years = c.financing_period_months / 12.0
                factor = (1.0 + c.financing_rate) ** years
                if c.financing_direction == 'advance':
                    # Customer prepays cash now; revenue is that cash accreted
                    # forward to the transfer date, the excess is interest
                    # expense (IFRS 15.61, IE example 26).
                    revenue_base = price * factor
                else:
                    # Customer pays in arrears; revenue is the present value of
                    # the future payment, the shortfall is interest income
                    # (IFRS 15.64, IE example 29).
                    revenue_base = price / factor if factor else price
            else:
                revenue_base = price
            c.financing_pv = (
                currency.round(revenue_base) if currency else revenue_base)
            interest = c.financing_pv - price
            # Report the magnitude of the financing component; its sign is
            # implied by the direction (income in arrears, expense in advance).
            interest = interest if interest >= 0 else -interest
            c.financing_component_amount = (
                currency.round(interest) if currency else interest)

    def _financing_revenue_ratio(self):
        """Ratio of revenue to allocated (cash) amount for each recognised
        increment. Defaults to 1.0 so a contract without a financing component
        recognises exactly as before. Above 1.0 for advance (revenue exceeds
        cash, interest expense); below 1.0 for arrears (revenue is the present
        value, interest income)."""
        self.ensure_one()
        if (self.financing_component and self.transaction_price
                and self.financing_pv != self.transaction_price):
            return self.financing_pv / self.transaction_price
        return 1.0

    def _financing_is_time_based(self):
        """Effective financing always accretes by time (IFRS 15.65)."""
        self.ensure_one()
        return bool(
            self.financing_payment_date
            and self.financing_component
            and self._financing_revenue_ratio() != 1.0)

    def _financing_transfer_date(self):
        """First posted revenue-transfer date, or False before transfer."""
        self.ensure_one()
        transfers = self.move_ids.filtered(lambda move: (
            move.state == 'posted'
            and any(
                line.account_id == self.revenue_account_id
                and self.currency_id.compare_amounts(line.balance, 0.0) != 0
                for line in move.line_ids
            )
        ))
        return min(transfers.mapped('date')) if transfers else False

    def _eh_validate_first_arrears_transfer_date(self, posting_date):
        self.ensure_one()
        if not (
            self._financing_is_time_based()
            and self.financing_direction == 'arrears'
            and not self._financing_transfer_date()
        ):
            return True
        expected = self.financing_payment_date - relativedelta(
            months=self.financing_period_months,
        )
        if posting_date != expected:
            raise UserError(_(
                "First transfer on an arrears-financed contract must occur "
                "on %(expected)s so the %(months)s-month financing horizon "
                "ends on %(payment)s.",
                expected=expected,
                months=self.financing_period_months,
                payment=self.financing_payment_date,
            ))
        return True

    def _financing_signed_total_interest(self, currency):
        """Signed total interest over the whole financing period. Positive for
        advance (revenue exceeds cash, interest expense, a debit); negative for
        arrears (revenue below cash, interest income, a credit)."""
        self.ensure_one()
        return currency.round(self.financing_pv - self.transaction_price)

    def _financing_arrears_carrying_ratio(self, currency):
        """Current PV-plus-accretion per nominal currency unit."""
        self.ensure_one()
        if not (
            self._financing_is_time_based()
            and self.financing_direction == 'arrears'
            and self.transaction_price
        ):
            return 1.0
        carrying = currency.round(
            self.financing_pv - self.financing_interest_recognised
        )
        return min(max(carrying / self.transaction_price, 0.0), 1.0)

    def _financing_time_accreted_interest(self, currency, as_of=None):
        """Signed interest accreted from the contract date to as_of (today by
        default) on an effective-interest basis, capped at the payment date.

        The discount unwinds as the present value grows toward the nominal
        cash flow: at elapsed fraction t of the financing period the accreted
        interest is total_interest * ((1 + rate) ** (t * years) - 1) / factor,
        which is 0 at the contract date and the full interest at the payment
        date. This is driven purely by elapsed time, independent of
        performance-obligation progress, so a point-in-time transfer accretes
        the interest over the period rather than posting it all at t0."""
        self.ensure_one()
        total = self._financing_signed_total_interest(currency)
        if not total:
            return 0.0
        if self.financing_direction == 'arrears':
            # No interest income exists before control transfers. This also
            # prevents an unsatisfied contract from creating a contract asset.
            start = self._financing_transfer_date()
            if not start:
                return 0.0
        else:
            # Advance consideration finances the entity from payment/contract
            # inception until the transfer date.
            start = self.contract_date
        end = self.financing_payment_date
        if not end or not start or end <= start:
            return total
        as_of = as_of or fields.Date.context_today(self)
        if as_of <= start:
            return 0.0
        if as_of >= end:
            return total
        span = (end - start).days
        elapsed = (as_of - start).days
        fraction = elapsed / span if span else 1.0
        years = self.financing_period_months / 12.0
        factor = (1.0 + self.financing_rate) ** years
        if factor in (0.0, 1.0):
            # Degenerate rate/period: fall back to straight-line accretion.
            accreted = total * fraction
        else:
            grown = (1.0 + self.financing_rate) ** (fraction * years) - 1.0
            accreted = total * grown / (factor - 1.0)
        return currency.round(accreted)

    def _compute_move_count(self):
        for c in self:
            c.move_count = len(c.move_ids)

    # Account fields that must not move once revenue has posted: re-pointing
    # them would silently re-base already-posted entries.
    _POSTED_LOCKED = frozenset({
        'revenue_account_id', 'contract_asset_account_id',
        'contract_liability_account_id', 'receivable_account_id',
        'journal_id', 'transaction_price', 'company_id', 'partner_id',
        'contract_date',
        # The financing parameters and interest account change the revenue /
        # interest split; re-basing them after posting would silently restate
        # already-recognised revenue and interest with no matching entry.
        'financing_component', 'financing_direction', 'financing_rate',
        'financing_period_months', 'financing_account_id',
        'financing_payment_date',
    })

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(company_id)
                vals['name'] = self._eh_next_contract_reference(company)
        contracts = super().create(vals_list)
        contracts._eh_validate_references()
        return contracts

    @api.model
    def _eh_next_contract_reference(self, company):
        """Return next value from exact-company sequence, race-free."""
        company.ensure_one()
        self.env.cr.execute(SQL(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            'eh.revenue.contract.sequence:%s' % company.id,
        ))
        Sequence = self.env['ir.sequence'].sudo()
        sequence = Sequence.search([
            ('code', '=', 'eh.revenue.contract'),
            ('company_id', '=', company.id),
        ], order='id', limit=1)
        if not sequence:
            sequence = Sequence.create({
                'name': _(
                    "EH Revenue Contract Reference — %s",
                    company.display_name,
                ),
                'code': 'eh.revenue.contract',
                'prefix': 'REV/%(year)s/',
                'padding': 4,
                'company_id': company.id,
            })
        return sequence.with_company(company).next_by_id() or '/'

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        if 'eh_integrity_review_note' in vals:
            if any(not contract.eh_integrity_review_required for contract in self):
                raise AccessError(_(
                    "Legacy review attestation is immutable after review."
                ))
            self._check_manager()
        if 'reversal_reason' in vals and any(
                contract.state == 'cancelled' for contract in self):
            raise AccessError(_(
                "A completed contract-reversal explanation is immutable."
            ))
        self._eh_lock_for_transition()
        self._eh_validate_references(vals)
        # Once any revenue has posted, freeze the accounts, journal, price and
        # company: changing them would re-base recognised revenue with no
        # matching journal entry, silently overstating (or understating) it.
        # A sanctioned contract modification (IFRS 15.18-21) is the one path
        # allowed to revise transaction_price after posting; it re-runs
        # recognition so a balanced catch-up posts. Everything else in
        # _POSTED_LOCKED stays frozen even under that context.
        locked = set(self._POSTED_LOCKED)
        if (self.env.su
                and self.env.context.get('eh_revenue_modification')):
            locked.discard('transaction_price')
        if locked.intersection(vals):
            for c in self:
                if c._has_posted_revenue():
                    raise UserError(_(
                        "Contract %s has posted revenue; its accounts, "
                        "journal and transaction price are frozen. Post a "
                        "correction to adjust the recognised amount.",
                        c.display_name))
        close_evidence = {
            'close_with_remainder', 'close_reason', 'close_release_to',
            'refund_liability_account_id',
        }
        if close_evidence.intersection(vals) and any(
            c.state in ('done', 'cancelled') for c in self
        ):
            raise UserError(_(
                "A closed or cancelled contract's closure evidence is "
                "immutable. Record a separate correction instead."
            ))
        return super().write(vals)

    def unlink(self):
        # Once revenue or billing has posted, the contract carries the
        # posting-move link; deleting the master would orphan posted GL
        # entries. Block it. A draft or unposted contract has no move and
        # stays deletable.
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        posted = self.filtered(lambda c: c._has_posted_revenue())
        if posted:
            raise UserError(_(
                "A contract with posted revenue or billing cannot be "
                "deleted; its journal entries would be orphaned. Reverse "
                "the entries and cancel it instead."))
        applied_reviews = self.mapped('review_ids').filtered(
            lambda review: review.state == 'applied'
        )
        modification_evidence = self.env['eh.revenue.modification'].sudo().search(
            ['|', ('contract_id', 'in', self.ids),
             ('separate_contract_id', 'in', self.ids)],
            limit=1,
        )
        if applied_reviews or modification_evidence:
            raise UserError(_(
                "A contract with applied review or modification evidence "
                "cannot be deleted. Cancel it and retain the audit trail."
            ))
        return super().unlink()

    def _has_posted_revenue(self):
        self.ensure_one()
        return bool(self.move_ids)

    # ---- transitions ----

    def action_activate(self):
        for original in self.sorted('id'):
            c = original._eh_prepare_action(manager=False)
            if c.state != 'draft':
                raise UserError(_("Only draft contracts can be activated."))
            if not c.obligation_ids:
                raise UserError(_(
                    "Contract %s has no performance obligations.",
                    c.display_name))
            if not c.total_ssp:
                raise UserError(_(
                    "The obligations on %s have a zero total standalone "
                    "selling price; the transaction price cannot be "
                    "allocated.", c.display_name))
            c._eh_validate_financing_schedule()
            expected_allocation = c.currency_id.round(
                c.transaction_price
                + sum(c.obligation_ids.mapped('variable_included'))
            )
            actual_allocation = c.currency_id.round(
                sum(c.obligation_ids.mapped('allocated_price'))
            )
            if c.currency_id.compare_amounts(
                actual_allocation, expected_allocation,
            ) != 0:
                raise UserError(_(
                    "Allocated obligation prices on %(contract)s do not "
                    "reconcile to consideration: %(actual)s versus "
                    "%(expected)s.",
                    contract=c.display_name,
                    actual=actual_allocation,
                    expected=expected_allocation,
                ))
            c.state = 'active'

    def action_close(self, accounting_date=None):
        # Closing validates completion: every over-time obligation must be
        # at 100% and every point-in-time obligation satisfied. The one way
        # past an incomplete contract is the manager override
        # (close_with_remainder), which requires a reason and releases any
        # remaining contract liability to P&L or a refund liability before
        # the state flips.
        for original in self.sorted('id'):
            c = original._eh_prepare_action(manager=False)
            if c.state != 'active':
                raise UserError(_("Only active contracts can be closed."))
            currency = c.currency_id
            pending = currency.round(sum(
                c.obligation_ids.mapped('to_recognise')
            ))
            if currency.compare_amounts(pending, 0.0) != 0:
                raise UserError(_(
                    "Contract %s has unposted revenue. Run revenue "
                    "recognition before closing.", c.display_name
                ))
            asset, liability = c._eh_ledger_contract_position()
            if currency.compare_amounts(asset, 0.0) > 0:
                raise UserError(_(
                    "Contract %s still has an unbilled contract asset. "
                    "Record billing before closing.", c.display_name
                ))
            if c._financing_is_time_based():
                full_interest = c._financing_signed_total_interest(currency)
                outstanding_interest = currency.round(
                    full_interest - c.financing_interest_recognised
                )
                if currency.compare_amounts(outstanding_interest, 0.0) != 0:
                    raise UserError(_(
                        "Contract %s still has financing interest to accrue. "
                        "Keep it active through the financing payment date.",
                        c.display_name,
                    ))
            open_obs = c.obligation_ids.filtered(lambda o: (
                (o.satisfaction == 'over_time'
                 and float_compare(o.percent_complete, 100.0,
                                   precision_digits=2) < 0)
                or (o.satisfaction == 'point_in_time' and not o.satisfied)))
            if open_obs and not c.close_with_remainder:
                raise UserError(_(
                    "Contract %(contract)s cannot be closed: %(names)s not "
                    "fully satisfied. Complete the obligations, or set "
                    "Close With Remainder with a manager reason to release "
                    "the remaining balance.",
                    contract=c.display_name,
                    names=', '.join(open_obs.mapped('name'))))
            if c.close_with_remainder:
                c._check_manager()
                if not (c.close_reason or '').strip():
                    raise UserError(_(
                        "Closing %s with a remainder requires a reason.",
                        c.display_name))
                c._close_release_remainder(accounting_date=accounting_date)
            elif currency.compare_amounts(liability, 0.0) > 0:
                raise UserError(_(
                    "Contract %s still has a contract liability. Complete "
                    "recognition or use the documented manager close "
                    "override.", c.display_name
                ))
            c.state = 'done'

    def _close_release_remainder(self, accounting_date=None):
        """Release the remaining contract liability on an override close.

        Two documented destinations (close_release_to):

        * 'income': the entity keeps the billed-ahead consideration; no
          further performance is expected and no refund is due, so the
          liability is derecognised to profit or loss (the IFRS 15.B46
          breakage pattern). Entry: Dr contract liability / Cr revenue.
        * 'refund': the unperformed balance is owed back to the customer,
          so the contract liability is reclassified as a refund liability
          (IFRS 15.55). Entry: Dr contract liability / Cr refund liability.

        Balanced by construction; a zero remainder posts nothing. Returns
        True when an entry posted."""
        self.ensure_one()
        currency = self.currency_id
        _asset, remainder = self._eh_ledger_contract_position()
        if currency.compare_amounts(remainder, 0.0) <= 0:
            return False
        if not self.journal_id or not self.contract_liability_account_id:
            raise UserError(_(
                "Configure the journal and contract liability account on "
                "%s before closing with a remainder.", self.display_name))
        if self.close_release_to == 'refund':
            if not self.refund_liability_account_id:
                raise UserError(_(
                    "Configure the refund liability account on %s to close "
                    "with a refundable remainder.", self.display_name))
            credit_account = self.refund_liability_account_id
            credit_label = _(
                "Unperformed balance reclassified to refund %s", self.name)
        else:
            if not self.revenue_account_id:
                raise UserError(_(
                    "Configure the revenue account on %s to release the "
                    "remainder to profit or loss.", self.display_name))
            credit_account = self.revenue_account_id
            credit_label = _(
                "Remaining contract liability released %s", self.name)
        self._post_move([
            (0, 0, {
                'name': _("Contract liability closed out %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': remainder, 'credit': 0.0,
            }),
            (0, 0, {
                'name': credit_label,
                'account_id': credit_account.id,
                'debit': 0.0, 'credit': remainder,
            }),
        ], date=self._eh_action_accounting_date(accounting_date))
        self.close_released_amount += remainder
        return True

    def action_cancel(self):
        for original in self.sorted('id'):
            c = original._eh_prepare_action(manager=False)
            if c._has_posted_revenue():
                raise UserError(_(
                    "Cannot cancel %s: a revenue, billing, financing, or "
                    "close-out entry has been posted. "
                    "Reverse the entries first.", c.display_name))
            c.state = 'cancelled'

    def action_review_legacy_integrity(self):
        """Accept preserved legacy rows as a reviewed opening baseline.

        The migration flags and historic move seals are never rewritten. A
        manager can resume only after recording an attestation and after the
        engine proves that every linked move is posted, same-company evidence
        and that no terminal-state contradiction remains.
        """
        self.ensure_one()
        self._eh_check_access('write')
        self._check_manager()
        self._eh_lock_for_transition()
        if not self.eh_integrity_review_required:
            raise UserError(_(
                "Contract %s does not require legacy integrity review.",
                self.display_name,
            ))
        note = ' '.join((self.eh_integrity_review_note or '').split())
        if len(note) < 20:
            raise UserError(_(
                "Record at least 20 characters identifying the evidence "
                "reviewed and the conclusion reached."
            ))
        moves = self.move_ids
        if moves:
            moves.flush_recordset()
            self.env.cr.execute(SQL(
                "SELECT id FROM account_move WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                tuple(sorted(moves.ids)),
            ))
            moves.invalidate_recordset([
                'state', 'company_id', 'eh_revenue_contract_id',
                'reversed_entry_id',
            ])
            invalid = moves.filtered(lambda move: (
                move.state != 'posted'
                or move.company_id != self.company_id
                or move.eh_revenue_contract_id != self
            ))
            if invalid:
                raise UserError(_(
                    "Legacy review found draft, foreign-company, or detached "
                    "journal evidence (%s). Correct that quarantine before "
                    "resuming the contract.",
                    ', '.join(invalid.mapped('display_name')),
                ))
        currency = self.currency_id
        asset, liability = self._eh_ledger_contract_position()
        if self.state == 'done' and (
            currency.compare_amounts(asset, 0.0) != 0
            or currency.compare_amounts(liability, 0.0) != 0
        ):
            raise UserError(_(
                "A closed legacy contract with a remaining contract position "
                "requires an audited correction, not release."
            ))
        if self.state == 'cancelled' and moves:
            raise UserError(_(
                "A cancelled legacy contract with posted entries requires an "
                "audited correction, not release."
            ))
        if not moves and any(currency.compare_amounts(value, 0.0) != 0 for value in (
            self.amount_recognised, self.amount_billed,
            self.financing_interest_recognised, self.close_released_amount,
        )):
            raise UserError(_(
                "Legacy subledger anchors exist without linked journal "
                "evidence. Post a documented correction before release."
            ))
        reviewed_at = fields.Datetime.now()
        self._eh_workflow_action().write({
            'eh_integrity_review_required': False,
            'eh_integrity_reviewed_at': reviewed_at,
            'eh_integrity_reviewed_by_id': self.env.user.id,
        })
        self.message_post(body=_(
            "Legacy revenue integrity review completed by %(user)s. Historic "
            "quarantine markers remain unchanged. Attestation: %(note)s",
            user=self.env.user.display_name, note=note,
        ))
        return True

    def action_reverse_and_cancel(self, accounting_date=None):
        """Reverse every verified suite entry, reset anchors, and cancel.

        Originals and their lines are never unlocked or deleted. Each source
        receives exactly one verified sealed counter-entry with an explicit
        original link, and the contract is cancelled only after all reversals
        post successfully in the same transaction.
        """
        self.ensure_one()
        contract = self._eh_prepare_action(manager=True)
        if contract.state not in ('active', 'done'):
            raise UserError(_(
                "Only an active or closed contract can be reversed."
            ))
        reason = ' '.join((contract.reversal_reason or '').split())
        if len(reason) < 20:
            raise UserError(_(
                "Record at least 20 characters explaining the contract "
                "reversal."
            ))
        posted = contract.move_ids.filtered(lambda move: move.state == 'posted')
        invalid = posted.filtered(lambda move: (
            move.company_id != contract.company_id
            or move.eh_revenue_contract_id != contract
            or not move.eh_sealed
            or move.eh_legacy_unverified_seal
        ))
        if invalid:
            raise UserError(_(
                "Only current verified sealed revenue entries can use the "
                "automatic reversal. Review quarantined evidence separately."
            ))
        originals = posted.filtered(lambda move: not move.reversed_entry_id)
        ambiguous = originals.filtered(lambda source: (
            source._eh_reversal_moves()
            and (
                len(source._eh_reversal_moves()) != 1
                or source._eh_reversal_moves().state != 'posted'
                or source._eh_reversal_moves() not in posted
                or not source._eh_reversal_moves().eh_sealed
                or source._eh_reversal_moves().eh_legacy_unverified_seal
            )
        ))
        if ambiguous:
            raise UserError(_(
                "Existing revenue reversal evidence is incomplete or "
                "ambiguous. Quarantine and review it before cancellation."
            ))
        # A registered recurring invoice may already have its exact sealed
        # credit note.  Do not try to reverse that immutable source twice;
        # reverse only still-live originals, then close the contract once the
        # linked contract-asset/liability ledger is demonstrably zero.
        sources = originals.filtered(
            lambda move: not move._eh_reversal_moves()
        ).sorted('id')
        if not sources and not originals:
            raise UserError(_("This contract has no posted entries to reverse."))
        if sources.filtered(lambda move: move.move_type != 'entry'):
            raise UserError(_(
                "Invoice-backed recurring billing must be corrected from the "
                "invoice's Revenue Billing Correction action before reversing "
                "the remaining contract entries."
            ))
        reversal_date = contract._eh_action_accounting_date(accounting_date)
        latest_source_date = max(posted.mapped('date'))
        if reversal_date < latest_source_date:
            raise UserError(_(
                "Reversal date %(date)s cannot precede the latest source "
                "entry date %(source_date)s.",
                date=reversal_date, source_date=latest_source_date,
            ))
        reversals = self.env['account.move']
        if sources:
            reversals = (
                sources.sudo(False)._eh_reverse_with_verified_capability(
                    [{
                        'date': reversal_date,
                        'journal_id': source.journal_id.id,
                        'ref': _(
                            "Revenue contract reversal %s", contract.name,
                        ),
                    } for source in sources],
                    cancel=False,
                )
            )
            vals = {
                'eh_revenue_contract_id': contract.id,
                'eh_revenue_reversal_contract_id': contract.id,
            }
            reversals.with_context(
                eh_revenue_move_link_capability=(
                    _REVENUE_MOVE_LINK_CAPABILITY
                ),
            )._eh_write_sealed_metadata(vals, set(vals))
            reversals._eh_post_verified_reversal()
        asset_balance, liability_balance = (
            contract._eh_ledger_contract_account_balances()
        )
        if (
            contract.currency_id.compare_amounts(asset_balance, 0.0) != 0
            or contract.currency_id.compare_amounts(
                liability_balance, 0.0
            ) != 0
        ):
            raise UserError(_(
                "The verified reversals did not clear the contract asset and "
                "liability ledger. Cancellation was aborted."
            ))
        for obligation in contract.obligation_ids:
            obligation._eh_workflow_write({'recognised_amount': 0.0})
        contract._eh_workflow_action().write({
            'amount_billed': 0.0,
            'financing_interest_recognised': 0.0,
            'close_released_amount': 0.0,
            'bill_amount': 0.0,
            'state': 'cancelled',
            'eh_reversed_at': fields.Datetime.now(),
            'eh_reversed_by_id': self.env.user.id,
        })
        contract.message_post(body=_(
            "Contract accounting reversed by %(user)s on %(date)s through "
            "%(count)s verified sealed counter-entry(ies). Reason: %(reason)s",
            user=self.env.user.display_name, date=reversal_date,
            count=len(reversals), reason=reason,
        ))
        return True

    def action_recognise(self, accounting_date=None):
        self = self._eh_prepare_action(manager=True)
        if self.state != 'active':
            raise UserError(_("Revenue is recognised on active contracts."))
        self._validate_accounts(billing=False)
        currency = self.currency_id
        changed_obligations = self.obligation_ids.filtered(
            lambda ob: currency.compare_amounts(ob.to_recognise, 0.0) != 0
        )
        increment = currency.round(
            sum(self.obligation_ids.mapped('to_recognise')))
        cmp_zero = currency.compare_amounts(increment, 0.0)
        if cmp_zero == 0:
            if not changed_obligations:
                raise UserError(_(
                    "There is no revenue to recognise on %s.",
                    self.display_name,
                ))
            # Reallocation can move equal and opposite amounts between
            # obligations while leaving total revenue and GL unchanged. Keep
            # each cumulative anchor aligned with its target instead of
            # aborting and leaving stale per-obligation evidence.
            for ob in changed_obligations:
                ob._eh_workflow_write({
                    'recognised_amount': ob.target_recognised,
                })
            self.message_post(body=_(
                "Net-zero revenue reallocation synchronized %(count)s "
                "performance-obligation anchor(s); no journal entry was "
                "required.", count=len(changed_obligations),
            ))
            return True
        time_based = self._financing_is_time_based()
        posting_date = self._eh_action_accounting_date(accounting_date)
        if cmp_zero > 0:
            self._eh_validate_first_arrears_transfer_date(posting_date)
        if cmp_zero < 0:
            # Downward correction: the target recognised revenue has dropped
            # below what has already been posted (for example a reduced
            # percentage of completion or a de-satisfied obligation). Post a
            # balanced reversing entry that debits revenue and credits the
            # contract asset first, then the contract liability, mirroring the
            # forward recognition split. Manager-gated by _check_manager above.
            lines = self._recognition_reversal_lines(currency, -increment)
        else:
            lines = self._recognition_lines(currency, increment)
        self._post_move(lines, date=posting_date)
        # recognised_amount is guarded on eh.revenue.obligation by the shared
        # eh.workflow.guard mixin: only a server-initiated (env.su) write may
        # advance the cumulative-posted anchor. This sanctioned run (after
        # _check_manager and the posted GL move above) elevates through
        # _eh_workflow_write, so the guarded write is proven server-side rather
        # than by a forgeable context flag.
        for ob in self.obligation_ids:
            ob._eh_workflow_write({'recognised_amount': ob.target_recognised})
        if time_based:
            # Time-based mode (IFRS 15.65): the recognition entry booked only
            # the present-value revenue and its asset leg; the discount unwinds
            # on its own time schedule, so post the interest accreted to date.
            self._post_financing_accretion(as_of=posting_date)
        return True

    def action_accrue_financing(self, accounting_date=None):
        """Post the financing interest accreted to date on a time basis
        (IFRS 15.65), independent of performance-obligation progress. For a
        point-in-time obligation this lets the discount unwind over the
        financing period instead of front-loading the whole interest at the
        moment of transfer."""
        self = self._eh_prepare_action(manager=True)
        if self.state != 'active':
            raise UserError(_("Interest is accrued on active contracts."))
        if not self._financing_is_time_based():
            raise UserError(_(
                "Contract %s has no time-based financing component to "
                "accrue. Set a financing payment date first.",
                self.display_name))
        self._validate_accounts(billing=False)
        posting_date = self._eh_action_accounting_date(accounting_date)
        if not self._post_financing_accretion(as_of=posting_date):
            raise UserError(_(
                "There is no financing interest to accrue on %s yet.",
                self.display_name))
        return True

    def _post_financing_accretion(self, as_of=None):
        """Post the delta between the time-accreted interest to date and the
        interest already recognised. Returns True when an entry was posted.

        Arrears (interest income, signed total negative): the discount unwinds
        as a credit to interest income against a debit that grows the contract
        asset toward the nominal cash flow. Advance (interest expense, signed
        total positive): a debit to interest expense against a credit to the
        contract liability. Balanced by construction."""
        self.ensure_one()
        currency = self.currency_id
        posting_date = self._eh_action_accounting_date(as_of)
        accreted = self._financing_time_accreted_interest(
            currency, as_of=posting_date)
        delta = currency.round(accreted - self.financing_interest_recognised)
        if currency.compare_amounts(delta, 0.0) == 0:
            return False
        if delta < 0:
            # Arrears: interest income earned this period (a credit); the
            # matching debit grows the billed receivable in proportion to
            # nominal billing and the unbilled contract asset for the rest.
            # It never creates/clears a contract liability merely because the
            # remaining financing discount is unaccreted.
            amount = -delta
            billed_fraction = min(max(
                self.amount_billed / self.transaction_price
                if self.transaction_price else 0.0,
                0.0,
            ), 1.0)
            to_receivable = currency.round(amount * billed_fraction)
            to_asset = currency.round(amount - to_receivable)
            lines = [(0, 0, {
                'name': _("Financing interest income %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': 0.0, 'credit': amount,
            })]
            if to_receivable:
                lines.append((0, 0, {
                    'name': _("Financing receivable accreted %s", self.name),
                    'account_id': self.receivable_account_id.id,
                    'partner_id': self.partner_id.id,
                    'debit': to_receivable, 'credit': 0.0,
                }))
            if to_asset:
                lines.append((0, 0, {
                    'name': _("Financing contract asset accreted %s", self.name),
                    'account_id': self.contract_asset_account_id.id,
                    'debit': to_asset, 'credit': 0.0,
                }))
        else:
            # Advance: interest expense incurred this period (a debit); the
            # matching credit first clears any contract asset, then grows the
            # deferred-revenue liability.
            amount = delta
            asset_before, _liability_before = (
                self._eh_ledger_contract_position()
            )
            from_asset = currency.round(min(amount, asset_before))
            to_liability = currency.round(amount - from_asset)
            lines = [(0, 0, {
                'name': _("Financing interest expense %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': amount, 'credit': 0.0,
            })]
            if from_asset:
                lines.append((0, 0, {
                    'name': _("Financing contract asset unwound %s", self.name),
                    'account_id': self.contract_asset_account_id.id,
                    'debit': 0.0, 'credit': from_asset,
                }))
            if to_liability:
                lines.append((0, 0, {
                    'name': _("Financing interest accreted %s", self.name),
                    'account_id': self.contract_liability_account_id.id,
                    'debit': 0.0, 'credit': to_liability,
                }))
        self._post_move(lines, date=posting_date)
        self.financing_interest_recognised += delta
        return True

    def _recognition_lines(self, currency, increment):
        """Forward recognition: credit revenue, debit contract liability
        (released) then contract asset. increment is positive and is the
        cash-basis amount that clears against billing.

        When a significant financing component is present (IFRS 15.60-65) the
        revenue credit is the present-value portion of the increment and the
        difference is posted to the interest account: interest income (a
        credit) for a customer paying in arrears, interest expense (a debit)
        for a customer paying in advance. The contract asset / liability leg
        still moves by the full cash-basis increment, so the entry stays
        balanced by construction and billing clears it exactly.

        In time-based mode (a financing payment date is set) the interest is
        no longer embedded here: it accretes on its own time schedule through
        _post_financing_accretion. This entry then books only the present-value
        revenue and moves the contract asset / liability by that same present
        value, so it stays balanced and the interest is not front-loaded at
        the moment of transfer (IFRS 15.65)."""
        ratio = self._financing_revenue_ratio()
        time_based = self._financing_is_time_based()
        # In time-based mode the asset / liability leg tracks the present-value
        # revenue (interest is accreted separately); otherwise it tracks the
        # full cash-basis increment and carries the interest split.
        moved = currency.round(increment * ratio) if time_based else increment
        _asset_before, liability_before = self._eh_ledger_contract_position()
        from_liability = currency.round(min(moved, liability_before))
        to_asset = currency.round(moved - from_liability)
        revenue = currency.round(increment * ratio)
        interest = 0.0 if time_based else currency.round(revenue - increment)
        lines = [(0, 0, {
            'name': _("Revenue recognised %s", self.name),
            'account_id': self.revenue_account_id.id,
            'debit': 0.0, 'credit': revenue,
        })]
        if interest > 0:
            # Advance: revenue exceeds the cash increment; the excess is
            # interest expense (a debit).
            lines.append((0, 0, {
                'name': _("Financing interest expense %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': interest, 'credit': 0.0,
            }))
        elif interest < 0:
            # Arrears: revenue is below the cash increment; the shortfall is
            # interest income (a credit).
            lines.append((0, 0, {
                'name': _("Financing interest income %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': 0.0, 'credit': -interest,
            }))
        if from_liability:
            lines.append((0, 0, {
                'name': _("Contract liability released %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': from_liability, 'credit': 0.0,
            }))
        if to_asset:
            lines.append((0, 0, {
                'name': _("Contract asset %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': to_asset, 'credit': 0.0,
            }))
        return lines

    def _recognition_reversal_lines(self, currency, amount):
        """Downward correction: debit revenue, credit the contract asset first
        (unwinding recognised-not-billed), then the contract liability. amount
        is the positive cash-basis magnitude of the reduction. Balanced by
        construction: the debits (revenue plus any interest expense reversed)
        equal the credits (asset plus liability, plus any interest income
        reversed).

        With a financing component the revenue debit is only the present-value
        portion of the reversal; the interest leg is reversed in the opposite
        direction to the forward entry. The asset / liability credit still
        moves by the full cash-basis amount so billing continues to clear.

        In time-based mode the interest is not embedded here (it accretes on
        its own time schedule); the revenue debit and the asset / liability
        credit both move by the present value so the entry stays balanced."""
        ratio = self._financing_revenue_ratio()
        time_based = self._financing_is_time_based()
        moved = currency.round(amount * ratio) if time_based else amount
        asset_before, _liability_before = self._eh_ledger_contract_position()
        cr_asset = currency.round(min(moved, asset_before))
        cr_liability = currency.round(moved - cr_asset)
        revenue = currency.round(amount * ratio)
        interest = 0.0 if time_based else currency.round(revenue - amount)
        lines = [(0, 0, {
            'name': _("Revenue correction %s", self.name),
            'account_id': self.revenue_account_id.id,
            'debit': revenue, 'credit': 0.0,
        })]
        if interest > 0:
            # Advance forward posted interest expense (a debit); reverse it as
            # a credit here.
            lines.append((0, 0, {
                'name': _("Financing interest reversed %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': 0.0, 'credit': interest,
            }))
        elif interest < 0:
            # Arrears forward posted interest income (a credit); reverse it as
            # a debit here.
            lines.append((0, 0, {
                'name': _("Financing interest reversed %s", self.name),
                'account_id': self.financing_account_id.id,
                'debit': -interest, 'credit': 0.0,
            }))
        if cr_asset:
            lines.append((0, 0, {
                'name': _("Contract asset reversed %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': 0.0, 'credit': cr_asset,
            }))
        if cr_liability:
            lines.append((0, 0, {
                'name': _("Contract liability restored %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': 0.0, 'credit': cr_liability,
            }))
        return lines

    def action_bill(self, accounting_date=None):
        self = self._eh_prepare_action(manager=True)
        if self.state != 'active':
            raise UserError(_("Billing is recorded on active contracts."))
        self._validate_accounts(billing=True)
        currency = self.currency_id
        amount = currency.round(self.bill_amount)
        if currency.compare_amounts(amount, 0.0) <= 0:
            raise UserError(_("Enter a positive amount to bill."))
        remaining = currency.round(
            self.transaction_price - self.amount_billed
        )
        if currency.compare_amounts(amount, remaining) > 0:
            raise UserError(_(
                "Billing %(amount)s would exceed the remaining contract "
                "consideration %(remaining)s on %(contract)s.",
                amount=amount,
                remaining=max(remaining, 0.0),
                contract=self.display_name,
            ))
        asset_before, _liability_before = self._eh_ledger_contract_position()
        time_based_arrears = (
            self._financing_is_time_based()
            and self.financing_direction == 'arrears'
        )
        if time_based_arrears:
            # Invoice nominal cash flow, but present unearned financing as a
            # contra-receivable. Only current PV-plus-earned interest clears
            # or creates the IFRS 15 contract position; billing must not label
            # future IFRS 9 interest as a contract liability.
            moved = currency.round(
                amount * self._financing_arrears_carrying_ratio(currency)
            )
            financing_discount = currency.round(amount - moved)
        else:
            moved = amount
            financing_discount = 0.0
        cr_asset = currency.round(min(moved, asset_before))
        cr_liability = currency.round(moved - cr_asset)
        lines = [(0, 0, {
            'name': _("Billing %s", self.name),
            'account_id': self.receivable_account_id.id,
            'partner_id': self.partner_id.id,
            'debit': amount, 'credit': 0.0,
        })]
        if financing_discount:
            lines.append((0, 0, {
                'name': _("Unearned financing discount %s", self.name),
                'account_id': self.receivable_account_id.id,
                'partner_id': self.partner_id.id,
                'debit': 0.0, 'credit': financing_discount,
            }))
        if cr_asset:
            lines.append((0, 0, {
                'name': _("Contract asset billed %s", self.name),
                'account_id': self.contract_asset_account_id.id,
                'debit': 0.0, 'credit': cr_asset,
            }))
        if cr_liability:
            lines.append((0, 0, {
                'name': _("Contract liability %s", self.name),
                'account_id': self.contract_liability_account_id.id,
                'debit': 0.0, 'credit': cr_liability,
            }))
        self._post_move(
            lines, date=self._eh_action_accounting_date(accounting_date))
        self.amount_billed += amount
        self.bill_amount = 0.0
        return True

    # ---- contract modifications (IFRS 15.18-21) ----

    modification_ids = fields.One2many(
        'eh.revenue.modification', 'contract_id', readonly=True, copy=False,
        help="History of contract modifications applied (IFRS 15.18-21).")
    modification_count = fields.Integer(compute='_compute_modification_count')

    def _compute_modification_count(self):
        for c in self:
            c.modification_count = len(c.modification_ids)

    def _apply_modification(self, method, added_obligations=None,
                            new_transaction_price=None, description=None,
                            effective_date=None):
        """Apply an IFRS 15.18-21 contract modification.

        method:
          'separate'    - IFRS 15.20: the added distinct goods/services are
                          priced at their standalone selling price, so the
                          modification is a new, separate contract. A fresh
                          eh.revenue.contract is created for them; this
                          contract is untouched.
          'prospective' - IFRS 15.21(a): the remaining goods/services are
                          distinct from those already transferred. The
                          not-yet-recognised transaction price plus any added
                          consideration is reallocated across the remaining
                          (unsatisfied) obligations and any added obligations,
                          effective going forward. Already-posted revenue is
                          not disturbed.
          'catch_up'    - IFRS 15.21(b): the remaining goods/services are not
                          distinct and form part of a single partially
                          satisfied obligation. The transaction price is
                          revised and revenue is trued up at the modification
                          date through the normal recognition run (a balanced
                          catch-up entry or reversal).

        Returns the created separate contract for 'separate', otherwise self.
        """
        self = self._eh_prepare_action(manager=True)
        if self.state != 'active':
            raise UserError(_(
                "Only an active contract can be modified."))
        added_obligations = added_obligations or []
        currency = self.currency_id
        # The wizard always supplies its explicit effective date.  Keep the
        # lower-level API deterministic for integrations and tests which call
        # it directly by using the caller's accounting context date, rather
        # than a possibly stale value left in the next-action UI field.
        effective_date = self._eh_action_accounting_date(
            effective_date or fields.Date.context_today(self)
        )

        if method == 'separate':
            if not added_obligations:
                raise UserError(_(
                    "A separate-contract modification needs the added "
                    "distinct goods or services."))
            price = new_transaction_price
            if price is None:
                price = sum(
                    o.get('standalone_price', 0.0) for o in added_obligations)
            # Providing obligation_ids in the copy default overrides the
            # obligations copied from the source, so the separate contract
            # holds only the added distinct goods/services (IFRS 15.20).
            new_contract = self.copy({
                'name': '/',
                'state': 'draft',
                'transaction_price': currency.round(price),
                'amount_billed': 0.0,
                'financing_interest_recognised': 0.0,
                'obligation_ids': [(0, 0, dict(o)) for o in added_obligations],
            })
            # Use the public transition so the spun-off contract receives the
            # same allocation, financing, and obligation validation as every
            # other contract. A direct state assignment bypassed those gates.
            new_contract.action_activate()
            self.env['eh.revenue.modification'].with_context(
                eh_revenue_modification_capability=(
                    _REVENUE_MODIFICATION_CAPABILITY
                )
            ).create({
                'contract_id': self.id,
                'method': method,
                'effective_date': effective_date,
                'description': description or '',
                'separate_contract_id': new_contract.id,
                'price_before': self.transaction_price,
                'price_after': self.transaction_price,
            })
            return new_contract

        if method not in ('prospective', 'catch_up'):
            raise UserError(_("Unknown modification method '%s'.", method))

        # prospective / catch_up both revise this contract in place. Run under
        # the modification context so the sanctioned reallocation may add
        # obligations and revise transaction_price on a posted contract; the
        # existing per-line basis freeze still protects posted revenue.
        price_before = self.transaction_price
        contract = self.with_context(eh_revenue_modification=True)

        if method == 'prospective':
            # IFRS 15.21(a): the remaining goods/services are distinct from
            # those transferred. Pin every obligation that has recognised
            # revenue to what it has already earned so it takes no share of the
            # remaining transaction price; that price is reallocated across the
            # still-open (and any added) obligations going forward, with no
            # catch-up on what is already posted.
            residual_vals = []
            for ob in contract.obligation_ids:
                if ob.recognised_amount:
                    # Partially transferred over-time obligations contain two
                    # economically different populations after an IFRS
                    # 15.21(a) modification: service already transferred and
                    # distinct service still to transfer. Freeze the former at
                    # posted revenue and create a zero-progress residual
                    # segment for the latter. Without this split, freezing the
                    # whole obligation either reverses earned revenue or drops
                    # the remaining promised service from allocation.
                    pct = (
                        ob.percent_complete
                        if ob.satisfaction == 'over_time' else 100.0
                    )
                    pct = min(max(pct, 0.0), 100.0)
                    remaining = (100.0 - pct) / 100.0
                    if remaining > 0.0:
                        residual = {
                            'name': _("%s (remaining after modification)",
                                      ob.name),
                            'sequence': ob.sequence + 1,
                            'standalone_price': currency.round(
                                ob.standalone_price * remaining,
                            ),
                            'satisfaction': ob.satisfaction,
                            'percent_complete': 0.0,
                            'satisfied': False,
                            'progress_method': ob.progress_method,
                            'method_basis': ob.method_basis,
                            'variable_consideration': (
                                ob.variable_consideration
                            ),
                            'variable_method': ob.variable_method,
                            'variable_estimate': currency.round(
                                ob.variable_estimate * remaining,
                            ),
                            'variable_constraint': currency.round(
                                ob.variable_constraint * remaining,
                            ),
                            'discount_specific': currency.round(
                                ob.discount_specific * remaining,
                            ),
                        }
                        if ob.progress_method == 'input_cost':
                            residual.update({
                                'cost_incurred': 0.0,
                                'cost_total_estimate': max(
                                    ob.cost_total_estimate
                                    - ob.cost_incurred,
                                    0.0,
                                ),
                            })
                        elif ob.progress_method == 'output_units':
                            residual.update({
                                'units_delivered': 0.0,
                                'units_total': max(
                                    ob.units_total - ob.units_delivered,
                                    0.0,
                                ),
                            })
                        residual_vals.append(residual)
                        ob.write({
                            'standalone_price': currency.round(
                                ob.standalone_price * (1.0 - remaining),
                            ),
                            'variable_estimate': currency.round(
                                ob.variable_estimate * (1.0 - remaining),
                            ),
                            'variable_constraint': currency.round(
                                ob.variable_constraint * (1.0 - remaining),
                            ),
                            'discount_specific': currency.round(
                                ob.discount_specific * (1.0 - remaining),
                            ),
                        })
                    ob.write({
                        'allocation_frozen': True,
                        'frozen_allocation': ob.recognised_amount,
                    })
            if residual_vals:
                contract.obligation_ids = [
                    (0, 0, vals) for vals in residual_vals
                ]

        if added_obligations:
            contract.obligation_ids = [(0, 0, o) for o in added_obligations]
        if new_transaction_price is not None:
            contract.write({
                'transaction_price': currency.round(new_transaction_price)})

        self.env['eh.revenue.modification'].with_context(
            eh_revenue_modification_capability=(
                _REVENUE_MODIFICATION_CAPABILITY
            )
        ).create({
            'contract_id': self.id,
            'method': method,
            'effective_date': effective_date,
            'description': description or '',
            'price_before': price_before,
            'price_after': self.transaction_price,
        })

        if method == 'catch_up':
            # True up revenue at the modification date if any progress is
            # already recorded. The recognition run posts the balanced
            # difference (or reversal) between the new blended target and what
            # is already posted. If nothing has posted yet there is nothing to
            # true up, so skip silently.
            if any(self.obligation_ids.mapped('recognised_amount')):
                if currency.compare_amounts(
                        sum(self.obligation_ids.mapped('to_recognise')),
                        0.0) != 0:
                    self.action_recognise(accounting_date=effective_date)
            # Financing re-measurement (IFRS 15.60-65): with a time-based
            # financing component the interest accreted to date was computed
            # off the pre-modification transaction price. The revised price
            # rescales the total discount (present value and price move
            # together at the locked rate and period), so re-run the
            # accretion off the revised price and remaining schedule and
            # post the signed delta through the same interest accrual
            # accounts. In progress-driven mode the revenue-to-cash ratio
            # (financing_pv / transaction_price) is independent of the
            # price level, so the catch-up recognition entry above already
            # carries the revised interest split and no extra entry exists
            # to post.
            if (new_transaction_price is not None
                    and self._financing_is_time_based()):
                self._validate_accounts(billing=False)
                self._post_financing_accretion(as_of=effective_date)
        return self

    def action_open_modification_wizard(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'active':
            raise UserError(_("Only an active contract can be modified."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Modify Contract"),
            'res_model': 'eh.revenue.modification.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_contract_id': self.id},
        }

    def action_view_modifications(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Modifications"),
            'res_model': 'eh.revenue.modification',
            'view_mode': 'list,form',
            'domain': [('contract_id', '=', self.id)],
        }

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('eh_revenue_contract_id', '=', self.id)],
        }

    # ---- constraint reassessment (IFRS 15.56) ----

    review_ids = fields.One2many(
        'eh.revenue.constraint.review', 'contract_id', copy=False,
        help="Period-end reassessments of the variable consideration "
             "estimate and constraint (IFRS 15.56).")
    review_count = fields.Integer(compute='_compute_review_count')

    def _compute_review_count(self):
        for c in self:
            c.review_count = len(c.review_ids)

    def action_open_period_reviews(self):
        """Period-end helper (IFRS 15.56): open a draft constraint review
        for every obligation carrying variable consideration on the
        selected active contracts, skipping obligations that already have a
        draft review pending. The reviewer fills the revised amounts and
        rationale, then applies each review; the applied reviews are the
        period-by-period audit trail."""
        Review = self.env['eh.revenue.constraint.review']
        created = Review.browse()
        for c in self:
            if c.state != 'active':
                continue
            for ob in c.obligation_ids.filtered('variable_consideration'):
                if Review.search_count([
                        ('obligation_id', '=', ob.id),
                        ('state', '=', 'draft')]):
                    continue
                created |= Review.create({
                    'contract_id': c.id,
                    'obligation_id': ob.id,
                })
        if not created and not self.review_ids:
            raise UserError(_(
                "No performance obligation on %s carries variable "
                "consideration to review.",
                ', '.join(self.mapped('name'))))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Constraint Reviews"),
            'res_model': 'eh.revenue.constraint.review',
            'view_mode': 'list,form',
            'domain': [('contract_id', 'in', self.ids)],
        }

    def action_view_constraint_reviews(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Constraint Reviews"),
            'res_model': 'eh.revenue.constraint.review',
            'view_mode': 'list,form',
            'domain': [('contract_id', '=', self.id)],
            'context': {'default_contract_id': self.id},
        }

    # ---- helpers ----

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can post revenue or billing."))

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A revenue contract may only use a company enabled in the "
                "caller's current company context."
            ))

    @api.model
    def _eh_record_matches_company(self, record, company):
        if not record or not company:
            return True
        domain_builder = getattr(record, '_check_company_domain', None)
        if domain_builder:
            try:
                return bool(record.filtered_domain(domain_builder(company)))
            except (AttributeError, TypeError, ValueError):
                pass
        if 'company_ids' in record._fields:
            candidate = company
            while candidate:
                if candidate in record.company_ids:
                    return True
                candidate = (
                    candidate.parent_id
                    if 'parent_id' in candidate._fields
                    else candidate.browse()
                )
            return False
        if 'company_id' in record._fields:
            return record.company_id == company
        return True

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        account_types = {
            'revenue_account_id': {'income', 'income_other'},
            'contract_asset_account_id': {
                'asset_current', 'asset_receivable',
            },
            'contract_liability_account_id': {'liability_current'},
            'receivable_account_id': {'asset_receivable'},
            'refund_liability_account_id': {'liability_current'},
        }
        for contract in self:
            company_id = vals.get('company_id', contract.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            if not self.env.su:
                company._eh_check_access('read')
            partner_id = vals.get('partner_id', contract.partner_id.id)
            partner = self.env['res.partner'].browse(partner_id)
            if partner and not self.env.su:
                partner._eh_check_access('read')
            journal_id = vals.get('journal_id', contract.journal_id.id)
            journal = self.env['account.journal'].browse(journal_id)
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id != company:
                    raise ValidationError(_(
                        "The revenue journal must belong to the contract "
                        "company."
                    ))
                if journal.type not in ('sale', 'general'):
                    raise ValidationError(_(
                        "Revenue postings require a sales or general journal."
                    ))
            financing_direction = vals.get(
                'financing_direction', contract.financing_direction
            )
            financing_types = (
                {'expense', 'expense_direct_cost', 'expense_depreciation'}
                if financing_direction == 'advance'
                else {'income', 'income_other'}
            )
            policies = dict(account_types)
            policies['financing_account_id'] = financing_types
            for field_name, allowed_types in policies.items():
                account_id = vals.get(field_name, contract[field_name].id)
                account = self.env['account.account'].browse(account_id)
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if not self._eh_record_matches_company(account, company):
                    raise ValidationError(_(
                        "Revenue posting accounts must be available to the "
                        "contract company."
                    ))
                if account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(field)s has an incompatible account type "
                        "(%(account_type)s).",
                        field=contract._fields[field_name].string,
                        account_type=account.account_type,
                    ))
        return self

    def _eh_lock_for_transition(self):
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return
        self.flush_recordset()
        self.env.cr.execute(SQL(
            "SELECT id FROM eh_revenue_contract WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            ids,
        ))
        self.invalidate_recordset()

    def _eh_ledger_contract_position(self):
        """Return current posted net contract asset/liability from GL."""
        asset_balance, liability_balance = (
            self._eh_ledger_contract_account_balances()
        )
        net = self.currency_id.round(asset_balance + liability_balance)
        return max(net, 0.0), max(-net, 0.0)

    def _eh_ledger_contract_account_balances(self, as_of=None):
        """Return raw posted balances of asset and liability accounts.

        Positive balance is a debit. Keeping each account separate lets
        billing bridges normalize presentation after an invoice/refund without
        falling back to nominal subledger anchors.
        """
        self.ensure_one()
        self.flush_recordset([
            'contract_asset_account_id', 'contract_liability_account_id',
        ])
        self.env['account.move'].flush_model([
            'state', 'eh_revenue_contract_id',
        ])
        self.env['account.move.line'].flush_model([
            'move_id', 'account_id', 'balance',
        ])
        account_ids = tuple(sorted(set((
            self.contract_asset_account_id
            | self.contract_liability_account_id
        ).ids)))
        if not account_ids:
            return 0.0, 0.0
        if as_of:
            self.env.cr.execute(SQL(
                "SELECT aml.account_id, COALESCE(SUM(aml.balance), 0.0) "
                "FROM account_move_line aml "
                "JOIN account_move am ON am.id = aml.move_id "
                "WHERE am.eh_revenue_contract_id = %s "
                "AND am.company_id = %s "
                "AND am.state = 'posted' AND aml.account_id IN %s "
                "AND am.date <= %s GROUP BY aml.account_id",
                self.id, self.company_id.id, account_ids,
                fields.Date.to_date(as_of),
            ))
        else:
            self.env.cr.execute(SQL(
                "SELECT aml.account_id, COALESCE(SUM(aml.balance), 0.0) "
                "FROM account_move_line aml "
                "JOIN account_move am ON am.id = aml.move_id "
                "WHERE am.eh_revenue_contract_id = %s "
                "AND am.company_id = %s "
                "AND am.state = 'posted' AND aml.account_id IN %s "
                "GROUP BY aml.account_id",
                self.id, self.company_id.id, account_ids,
            ))
        balances = {
            account_id: self.currency_id.round(balance or 0.0)
            for account_id, balance in self.env.cr.fetchall()
        }
        return (
            balances.get(self.contract_asset_account_id.id, 0.0),
            balances.get(self.contract_liability_account_id.id, 0.0),
        )

    def _eh_prepare_action(self, manager):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        if manager:
            self._check_manager()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        if self.eh_integrity_review_required:
            raise UserError(_(
                "Contract %s is quarantined because legacy workflow state "
                "conflicts with preserved posted evidence. Resolve it through "
                "an audited correction before continuing.", self.display_name
            ))
        return self._eh_workflow_action()

    def _validate_accounts(self, billing):
        self.ensure_one()
        self._eh_validate_financing_schedule()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.contract_asset_account_id:
            missing.append(_("contract asset account"))
        if not self.contract_liability_account_id:
            missing.append(_("contract liability account"))
        if billing and not self.receivable_account_id:
            missing.append(_("receivable account"))
        if not billing and not self.revenue_account_id:
            missing.append(_("revenue account"))
        # The interest account is only needed when a financing component is
        # actually in effect (a rate and a period discount the price); an
        # inactive toggle leaves recognition byte-identical and does not
        # require it.
        if (not billing and self.financing_component
                and self._financing_revenue_ratio() != 1.0
                and not self.financing_account_id):
            missing.append(_("interest account"))
        if missing:
            raise UserError(_(
                "Configure the %s on contract %s first.",
                ', '.join(missing), self.display_name))

    def _eh_action_accounting_date(self, accounting_date=None):
        self.ensure_one()
        posting_date = fields.Date.to_date(
            accounting_date or self.accounting_date)
        if not posting_date:
            raise UserError(_(
                "Set an exact accounting date before posting contract %s.",
                self.display_name,
            ))
        if self.contract_date and posting_date < self.contract_date:
            raise UserError(_(
                "Accounting date %(date)s cannot precede contract date "
                "%(contract_date)s.",
                date=posting_date, contract_date=self.contract_date,
            ))
        return posting_date

    def _post_move(self, lines, date=None, ref=None):
        if not date:
            raise UserError(_(
                "Revenue journal entries require an explicit accounting date."
            ))
        move = self.env['account.move'].with_context(
            eh_revenue_move_link_capability=_REVENUE_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': fields.Date.to_date(date),
            'journal_id': self.journal_id.id,
            'ref': ref or self.name,
            'eh_revenue_contract_id': self.id,
            'eh_sealed': True,
            'line_ids': lines,
        })
        move.action_post()
        return move


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_revenue_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Revenue Contract",
        readonly=True, index=True, ondelete='restrict', copy=False,
        check_company=True)
    eh_revenue_reversal_contract_id = fields.Many2one(
        'eh.revenue.contract', string="Reversed Revenue Contract",
        readonly=True, index=True, ondelete='restrict', copy=False,
        check_company=True,
        help="Contract whose verified sealed accounting this entry reverses.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            vals.get('eh_revenue_contract_id')
            or vals.get('eh_revenue_reversal_contract_id')
            for vals in vals_list
        ) and not (
            self.env.context.get('eh_revenue_move_link_capability')
            is _REVENUE_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Revenue journal-entry provenance is engine-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if {
            'eh_revenue_contract_id', 'eh_revenue_reversal_contract_id',
        }.intersection(vals) and not (
            self.env.context.get('eh_revenue_move_link_capability')
            is _REVENUE_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "Revenue journal-entry provenance is immutable."
            ))
        return super().write(vals)
