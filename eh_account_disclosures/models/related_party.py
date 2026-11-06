# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 24 related-party register and transactions."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

# Register figures frozen once the party is finalised. Writing any of these on
# a finalised party is refused so a signed-off related-party disclosure cannot
# be silently re-keyed. Computed tie-out fields are never in this set, so they
# still recompute.
_RELATED_PARTY_FROZEN_FIELDS = frozenset({
    'name', 'partner_id', 'relationship', 'reporting_date', 'company_id',
    'transaction_ids', 'compensation_line_ids', 'compensation_date_from',
    'is_kmp', 'controlling_party_name', 'ultimate_controlling_party_name',
    'notes',
})

# IAS 24.17 compensation categories keyed by an account tag. The KMP-ledger
# prefill (action_populate_kmp) reads posted move lines and routes each line's
# expense into the category carried by its account's tags: an account.account.
# tag whose name equals one of these five category codes (case-insensitive,
# leading/trailing whitespace ignored) maps the account to that category. An
# account carrying no recognised category tag is skipped, so only accounts the
# preparer has deliberately tagged as a KMP-compensation category feed the
# note. The five codes are exactly the IAS 24.17(a)-(e) categories.
_IAS24_CATEGORY_CODES = (
    'short_term', 'post_employment', 'other_long_term',
    'termination', 'share_based',
)


class EhRelatedParty(models.Model):
    _name = 'eh.related.party'
    _description = "Related party (IAS 24)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'ledger_balance', 'balance_residual',
        'balance_tied', 'total_transactions', 'outstanding_balance',
        'total_compensation', 'total_doubtful_debt_provision',
        'total_doubtful_debt_expense',
    })

    name = fields.Char(required=True, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised related-party register is locked: its details and "
             "transactions cannot be edited or appended. Only a manager can "
             "finalise or reopen it. The advisory tie-out flags still "
             "recompute.")
    partner_id = fields.Many2one(
        'res.partner', string="Contact", check_company=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    relationship = fields.Selection(
        [('parent', "Parent"), ('subsidiary', "Subsidiary"),
         ('associate', "Associate"), ('joint_venture', "Joint venture"),
         ('kmp', "Key management personnel"),
         ('close_family', "Close family of KMP"),
         ('other', "Other related party")],
        default='other', required=True, tracking=True,
        help="Nature of the related-party relationship (IAS 24.9).")
    controlling_party_name = fields.Char(
        string="Controlling party", tracking=True,
        help="Name of the entity's immediate controlling party (IAS 24.13).")
    ultimate_controlling_party_name = fields.Char(
        string="Ultimate controlling party", tracking=True,
        help="Name of the ultimate controlling party when different from "
             "the immediate parent (IAS 24.13).")
    is_kmp = fields.Boolean(
        string="Key management personnel",
        help="Flags this party as key management personnel for the IAS 24.17 "
             "compensation prefill. Only KMP-flagged parties are populated "
             "from the ledger by Populate KMP Compensation. Defaults off, so "
             "an existing register is never auto-populated until it is "
             "deliberately flagged.")
    active = fields.Boolean(default=True)
    reporting_date = fields.Date(
        default=fields.Date.context_today,
        help="As-at date for the optional ledger tie-out of the outstanding "
             "balance against the linked contact's posted ledger.")
    transaction_ids = fields.One2many(
        'eh.related.party.transaction', 'party_id')
    ledger_balance = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Outstanding receivable/payable balance of the linked contact "
             "from posted move lines at the reporting date. Blank when no "
             "contact is linked, so a narrative-only relationship never "
             "shows as drifted.")
    balance_residual = fields.Monetary(
        compute='_compute_ledger', store=True, currency_field='currency_id',
        help="Entered outstanding balance less the ledger balance.")
    balance_tied = fields.Boolean(
        compute='_compute_ledger', store=True,
        help="True when no contact is linked (not applicable) or the entered "
             "outstanding balance equals the contact's ledger balance within "
             "currency rounding. False signals drift from the ledger.")
    total_transactions = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    outstanding_balance = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')

    # --- KMP compensation (IAS 24.17) -------------------------------------
    # IAS 24.17 requires key management personnel compensation in total and
    # for each of five categories: short-term employee benefits,
    # post-employment benefits, other long-term benefits, termination
    # benefits, and share-based payment. The category lines live on the KMP
    # register entry; the share-based category can prefill from the IFRS 2
    # engine's posted period charges (soft lookup) so the figure comes from
    # the ledger, not a spreadsheet.
    compensation_date_from = fields.Date(
        string="Compensation period start",
        help="Start of the reporting period the IAS 24.17 compensation "
             "covers. With the reporting date it bounds the share-based "
             "payment prefill window. Leave empty to take all posted "
             "share-based charges up to the reporting date.")
    compensation_line_ids = fields.One2many(
        'eh.related.party.compensation', 'party_id',
        string="KMP compensation")
    total_compensation = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Total key management personnel compensation (IAS 24.17 "
             "requires the total alongside the per-category amounts).")
    total_doubtful_debt_provision = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Aggregate provision for doubtful related-party balances "
             "(IAS 24.18(c)).")
    total_doubtful_debt_expense = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Aggregate bad or doubtful debt expense recognised in the "
             "period for related parties (IAS 24.18(d)).")
    notes = fields.Text()

    @api.depends('transaction_ids.amount', 'transaction_ids.balance',
                 'transaction_ids.doubtful_debt_provision',
                 'transaction_ids.doubtful_debt_expense',
                 'compensation_line_ids.amount')
    def _compute_totals(self):
        for p in self:
            p.total_transactions = sum(p.transaction_ids.mapped('amount'))
            p.outstanding_balance = sum(p.transaction_ids.mapped('balance'))
            p.total_compensation = sum(
                p.compensation_line_ids.mapped('amount'))
            p.total_doubtful_debt_provision = sum(
                p.transaction_ids.mapped('doubtful_debt_provision'))
            p.total_doubtful_debt_expense = sum(
                p.transaction_ids.mapped('doubtful_debt_expense'))

    @api.model
    def _ias24_category_of_account(self, account):
        """Return the IAS 24.17 category code an account maps to, or False.

        The mapping is by account tag: an account.account.tag whose name
        (case-insensitive, trimmed) equals one of the five IAS 24.17 category
        codes routes every posted line on that account into that category.
        An account carrying no such tag returns False and its lines are
        skipped, so only accounts the preparer has deliberately tagged as a
        KMP-compensation category feed the prefill. The first recognised tag
        wins when an account (mis)carries more than one category tag."""
        for tag in account.tag_ids:
            code = (tag.name or '').strip().lower()
            if code in _IAS24_CATEGORY_CODES:
                return code
        return False

    def action_populate_kmp(self):
        """Populate the IAS 24.17 compensation categories from the ledger for
        KMP-flagged parties.

        For each party flagged is_kmp with a linked contact, every posted
        account.move.line of that contact whose account carries a recognised
        IAS 24.17 category tag (see _ias24_category_of_account) inside the
        compensation window [compensation_date_from, reporting_date] is summed
        by category. The line's compensation expense is its ledger balance
        (debit - credit), so an expense account line (debit-positive)
        contributes a positive figure. One origin='ledger' compensation line
        per non-zero category is created or updated in place.

        Idempotent: the ledger lines are recomputed and rewritten each run;
        a category that drops to zero has its ledger line removed. Manual
        lines (origin='manual') and the share-based engine line (origin='sbp',
        fed by action_prefill_share_based) are never touched, so the ledger
        prefill and the IFRS 2 prefill compose without clobbering each other.
        Refused on a finalised register and on a party that is not KMP or has
        no linked contact."""
        self = self._eh_disclosure_prepare_action(engine=True)
        Comp = self.env['eh.related.party.compensation']
        for party in self:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its compensation cannot "
                    "be populated. Ask a manager to reopen it first.",
                    party.name))
            if not party.is_kmp:
                raise UserError(_(
                    "Related party %s is not flagged as key management "
                    "personnel, so its IAS 24.17 compensation is not "
                    "populated from the ledger. Flag it first, or key the "
                    "amounts manually.", party.name))
            if not party.partner_id:
                raise UserError(_(
                    "Related party %s has no linked contact, so there are no "
                    "posted move lines to derive its KMP compensation from. "
                    "Link a contact first.", party.name))
            currency = party.currency_id or party.company_id.currency_id
            date_to = party.reporting_date \
                or fields.Date.context_today(self)
            domain = [
                ('partner_id', '=', party.partner_id.id),
                ('company_id', 'in', party._eh_disclosure_company_ids()),
                ('parent_state', '=', 'posted'),
                ('date', '<=', date_to),
            ]
            if party.compensation_date_from:
                domain.append(('date', '>=', party.compensation_date_from))
            lines = self.env['account.move.line'].search(domain)
            totals = dict.fromkeys(_IAS24_CATEGORY_CODES, 0.0)
            for ml in lines:
                category = self._ias24_category_of_account(ml.account_id)
                if not category:
                    continue
                totals[category] += ml.balance
            existing = {
                line.category: line for line in party.compensation_line_ids}
            for category in _IAS24_CATEGORY_CODES:
                amount = currency.round(totals[category])
                line = existing.get(category)
                if line and line.origin in ('manual', 'sbp'):
                    # A reviewed manual figure is an explicit override. The
                    # SBP engine is authoritative for share-based amounts.
                    continue
                if currency.is_zero(amount):
                    # A category that no longer carries a ledger figure drops
                    # its engine line; a manual line for the same category is
                    # never in `existing`, so it survives untouched.
                    if line:
                        line.unlink()
                    continue
                if line:
                    line.write({'amount': amount})
                else:
                    Comp.create({
                        'party_id': party.id,
                        'category': category,
                        'origin': 'ledger',
                        'amount': amount,
                        'note': _("Posted KMP move lines on category-tagged "
                                  "accounts in the window."),
                    })
        return True

    def action_prefill_share_based(self):
        """Prefill the share-based payment category (IAS 24.17(e)) from the
        IFRS 2 engine.

        Each posted plan-level period charge is allocated across that plan's
        partner-linked grants in proportion to the grant's contribution to the
        cumulative IFRS 2 measure at the run date.  The selected KMP receives
        only allocations for grants linked to that exact partner; non-KMP and
        other grantees remain outside this party's IAS 24 figure.  Per-run
        currency rounding is conserved by assigning the deterministic
        remainder to the lowest (partner id, grant id), so separately running
        every KMP prefill can never duplicate the entity-wide charge.

        Soft lookup raises a clear error when the share-based payment module is
        absent. Idempotent: the single prefilled line is updated in place;
        manually keyed share-based lines are never touched."""
        self = self._eh_disclosure_prepare_action(engine=True)
        if 'eh.sbp.period.run' not in self.env:
            raise UserError(_(
                "Prefilling share-based compensation requires the "
                "Share-based Payment module "
                "(eh_account_share_based_payment). Install it or key the "
                "amount manually."))
        Comp = self.env['eh.related.party.compensation']
        for party in self:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its compensation "
                    "cannot be prefilled. Ask a manager to reopen it "
                    "first.", party.name))
            if not party.is_kmp:
                raise UserError(_(
                    "Related party %s is not flagged as key management "
                    "personnel, so no IFRS 2 grant allocation can be "
                    "prefilled.", party.name,
                ))
            if not party.partner_id:
                raise UserError(_(
                    "Related party %s has no selected contact. Link the exact "
                    "KMP grantee partner before prefilling share-based "
                    "compensation.", party.name,
                ))
            date_to = party.reporting_date \
                or fields.Date.context_today(self)
            domain = [
                ('company_id', 'in', party._eh_disclosure_company_ids()),
                ('state', '=', 'posted'),
                ('period_end', '<=', date_to),
            ]
            if party.compensation_date_from:
                domain.append(
                    ('period_end', '>=', party.compensation_date_from))
            runs = self.env['eh.sbp.period.run'].search(domain)
            if runs:
                # This is a read-only extract. Stabilise feeder evidence after
                # read ACL/rule checks without requiring feeder write access.
                for records in (runs, runs.plan_id, runs.plan_id.grant_ids):
                    records._eh_check_access('read')
                    records.flush_recordset()
                    if records:
                        lock_query = SQL(
                            "SELECT %s FROM %s WHERE %s IN %s "
                            "ORDER BY %s FOR SHARE",
                            SQL.identifier('id'),
                            SQL.identifier(records._table),
                            SQL.identifier('id'), tuple(sorted(records.ids)),
                            SQL.identifier('id'),
                        )
                        self.env.cr.execute(
                            lock_query.code, lock_query.params)
                runs.invalidate_recordset()
            currency = party.currency_id or party.company_id.currency_id
            amount = 0.0
            unallocated_details = []
            for run in runs:
                allocations, unallocated = \
                    party._eh_sbp_run_grant_allocations(run)
                amount += sum(
                    allocation
                    for grant, allocation in allocations.items()
                    if grant.partner_id == party.partner_id
                )
                if not currency.is_zero(unallocated):
                    unallocated_details.append(
                        "%s: %s %s" % (
                            run.display_name,
                            currency.round(unallocated),
                            currency.name,
                        )
                    )
            amount = currency.round(amount)
            note = _(
                "Partner-specific allocation of posted IFRS 2 period "
                "charges in the window. Allocated partner amounts plus any "
                "explicitly flagged unallocated share conserve each posted "
                "plan charge."
            )
            if unallocated_details:
                note += "\n" + _(
                    "Unallocated share for grants without a selected KMP "
                    "partner: %s",
                    "; ".join(unallocated_details),
                )
            line = party.compensation_line_ids.filtered(
                lambda l: l.category == 'share_based')[:1]
            if line.origin == 'manual':
                # A reviewed manual amount is an explicit override.
                continue
            if currency.is_zero(amount):
                if line:
                    line.unlink()
            elif line:
                line.write({
                    'origin': 'sbp', 'amount': amount, 'note': note,
                })
            else:
                Comp.create({
                    'party_id': party.id,
                    'category': 'share_based',
                    'origin': 'sbp',
                    'amount': amount,
                    'note': note,
                })
        return True

    def _eh_sbp_grant_contribution(self, grant, run):
        """Unrounded grant contribution to the run's cumulative measure."""
        self.ensure_one()
        plan = run.plan_id
        expected = grant._expected_to_vest()
        if not expected:
            return 0.0
        if plan.settlement == 'cash':
            if plan.graded_vesting:
                factor = sum(
                    tranche.portion_pct / 100.0
                    * tranche._vested_fraction(run.period_end)
                    for tranche in plan.tranche_ids
                )
            else:
                factor = plan._service_fraction(run.period_end)
            return expected * run.current_fair_value * factor

        if plan.graded_vesting:
            base = expected * sum(
                tranche.portion_pct / 100.0
                * tranche.fair_value
                * tranche._vested_fraction(run.period_end)
                for tranche in plan.tranche_ids
            )
        else:
            base = (
                expected * grant.grant_date_fair_value
                * plan._service_fraction(run.period_end)
            )
        # Plan modifications apply the same incremental fair value per expected
        # instrument. Allocate the plan helper's exact cumulative modification
        # amount by expected instruments so the grant weights mirror the engine
        # without reimplementing its month convention.
        total_expected = sum(
            candidate._expected_to_vest() for candidate in plan.grant_ids
        )
        modification = plan._modification_amount(run.period_end)
        if total_expected and modification:
            base += modification * expected / total_expected
        return base

    def _eh_sbp_run_grant_allocations(self, run):
        """Return partner allocations and the explicitly unallocated share."""
        self.ensure_one()
        plan = run.plan_id
        if (
            run.state != 'posted'
            or not run.move_id
            or run.move_id.state != 'posted'
            or ('eh_sbp_plan_id' in run.move_id._fields
                and run.move_id.eh_sbp_plan_id != plan)
            or ('eh_sealed' in run.move_id._fields and not run.move_id.eh_sealed)
        ):
            raise UserError(_(
                "Share-based period run %s lacks valid posted journal-entry "
                "provenance and cannot feed a KMP disclosure.",
                run.display_name,
            ))
        posted_at = run.move_id.create_date
        changed_grants = plan.grant_ids.filtered(
            lambda grant: (
                not posted_at
                or not grant.write_date
                or grant.write_date > posted_at
            )
        )
        if changed_grants:
            raise UserError(_(
                "Share-based period run %(run)s predates later edits to "
                "grant estimates/outcomes (%(grants)s). The feeder stores no "
                "immutable per-grant contribution snapshot for that run, so "
                "its partner allocation cannot be reconstructed without "
                "guessing. Use reviewed manual evidence for that period.",
                run=run.display_name,
                grants=', '.join(changed_grants.mapped('display_name')),
            ))
        all_contributions = {
            grant: self._eh_sbp_grant_contribution(grant, run)
            for grant in plan.grant_ids
        }
        all_contributions = {
            grant: contribution
            for grant, contribution in all_contributions.items()
            if contribution > 0.0
        }
        currency = plan.currency_id
        total = sum(all_contributions.values())
        if not all_contributions or currency.is_zero(total):
            if currency.is_zero(run.period_charge):
                return {}, 0.0
            raise UserError(_(
                "Share-based period run %s has a posted charge but no "
                "grant contribution from which a truthful KMP "
                "allocation can be reconstructed.", run.display_name,
            ))
        kmp_partner_ids = set(self.env['eh.related.party'].search([
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('is_kmp', '=', True),
            ('partner_id', '!=', False),
        ]).partner_id.ids)
        contributions = {
            grant: contribution
            for grant, contribution in all_contributions.items()
            if grant.partner_id.id in kmp_partner_ids
        }
        if not contributions:
            raise UserError(_(
                "Share-based period run %s has a posted charge but none of "
                "its contributing grants has a partner selected on a KMP "
                "register. Link and flag the eligible grantees before "
                "prefilling KMP compensation.",
                run.display_name,
            ))
        eligible_target = currency.round(
            run.period_charge * sum(contributions.values()) / total
        )
        allocations = {
            grant: currency.round(run.period_charge * contribution / total)
            for grant, contribution in contributions.items()
        }
        remainder = currency.round(
            eligible_target - sum(allocations.values())
        )
        if not currency.is_zero(remainder):
            first = min(
                contributions,
                key=lambda grant: (grant.partner_id.id, grant.id),
            )
            allocations[first] = currency.round(
                allocations[first] + remainder
            )
        if not currency.is_zero(
            currency.round(sum(allocations.values()) - eligible_target)
        ):
            raise UserError(_(
                "The partner allocation for share-based period run %s did "
                "not conserve its eligible share; no KMP line was changed.",
                run.display_name,
            ))
        unallocated = currency.round(run.period_charge - eligible_target)
        if not currency.is_zero(currency.round(
            sum(allocations.values()) + unallocated - run.period_charge
        )):
            raise UserError(_(
                "The allocated and unallocated shares for share-based period "
                "run %s do not conserve its posted charge; no KMP line was "
                "changed.", run.display_name,
            ))
        return allocations, unallocated

    @api.depends('partner_id', 'outstanding_balance', 'reporting_date',
                 'company_id')
    def _compute_ledger(self):
        for p in self:
            currency = p.currency_id or p.company_id.currency_id
            ledger = p._derive_ledger_balance()
            p.ledger_balance = ledger
            residual = p.outstanding_balance - ledger
            if currency:
                residual = currency.round(residual)
            p.balance_residual = residual
            if not p.partner_id:
                # No linked contact -> tie-out is not applicable, treat as
                # tied so a purely narrative relationship never shows drift.
                p.balance_tied = True
            else:
                p.balance_tied = currency.is_zero(residual) \
                    if currency else residual == 0.0

    def _derive_ledger_balance(self):
        """Return the outstanding receivable/payable balance of the linked
        contact from posted move lines at the reporting date.

        The balance is the ledger balance (debit - credit) of the contact's
        receivable and payable lines, positive for a net receivable and
        negative for a net payable, matching the natural sign of an
        outstanding related-party balance (IAS 24.18(b))."""
        self.ensure_one()
        if not self.partner_id or not self.company_id \
                or not self.reporting_date:
            return 0.0
        move_lines = self.env['account.move.line'].search([
            ('partner_id', '=', self.partner_id.id),
            ('company_id', 'in', self._eh_disclosure_company_ids()),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.reporting_date),
            ('account_id.account_type', 'in',
             ('asset_receivable', 'liability_payable')),
        ])
        return sum(move_lines.mapped(lambda ml: ml.debit - ml.credit))

    @api.model_create_multi
    def create(self, vals_list):
        # Creating a related party already finalised would skip the
        # manager-gated action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the party details and transactions once finalised (a
        # signed-off register is frozen for everyone; restate via a
        # manager-gated reopen). The state field itself is owned by the
        # inherited eh.workflow.guard, which refuses any non-superuser direct
        # write; the sanctioned finalise / reopen actions run under sudo.
        if _RELATED_PARTY_FROZEN_FIELDS.intersection(vals):
            for party in self:
                if party.state == 'finalised':
                    raise UserError(_(
                        "Related party %s is finalised and cannot be edited. "
                        "Ask a manager to reopen it first.", party.name))
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "related-party register."))

    def unlink(self):
        for party in self:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised and cannot be deleted. "
                    "Ask a manager to reopen it first.", party.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the register: details and transactions freeze. Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self._compute_totals()
        self._compute_ledger()
        self.flush_recordset([
            'ledger_balance', 'balance_residual', 'balance_tied',
            'total_transactions', 'outstanding_balance',
            'total_compensation', 'total_doubtful_debt_provision',
            'total_doubtful_debt_expense',
        ])
        for party in self:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is already finalised.", party.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised register to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhRelatedPartyTransaction(models.Model):
    _name = 'eh.related.party.transaction'
    _description = "Related-party transaction"
    _order = 'party_id, date desc, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'party_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})

    party_id = fields.Many2one(
        'eh.related.party', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='party_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='party_id.currency_id', store=True, readonly=True)

    date = fields.Date(required=True, default=fields.Date.context_today)
    transaction_type = fields.Selection(
        [('sale', "Sale of goods/services"),
         ('purchase', "Purchase of goods/services"),
         ('loan', "Loan"), ('guarantee', "Guarantee"),
         ('compensation', "KMP compensation"), ('other', "Other")],
        default='other', required=True)
    description = fields.Char()
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Amount of the transaction during the period.")
    balance = fields.Monetary(
        currency_field='currency_id',
        help="Outstanding balance at the reporting date (IAS 24.18(b)).")
    terms_and_conditions = fields.Text(
        string="Terms and conditions",
        help="Terms and settlement basis, including whether the balance is "
             "secured (IAS 24.18(b)(i)).")
    secured = fields.Boolean(
        help="Whether the outstanding balance is secured (IAS 24.18(b)(i)).")
    security_details = fields.Char(
        help="Nature of security supporting the related-party balance.")
    guarantee_details = fields.Text(
        help="Details of guarantees given or received (IAS 24.18(b)(ii)).")
    doubtful_debt_provision = fields.Monetary(
        currency_field='currency_id',
        help="Provision for doubtful debts related to this balance at the "
             "reporting date (IAS 24.18(c)).")
    doubtful_debt_expense = fields.Monetary(
        currency_field='currency_id',
        help="Expense recognised during the period for bad or doubtful "
             "related-party debts (IAS 24.18(d)).")

    @api.model_create_multi
    def create(self, vals_list):
        # A create-append hole silently moves the parent totals, so appending
        # a transaction to a finalised party is refused (create guard is
        # required).
        parties = self.env['eh.related.party'].browse([
            v.get('party_id') for v in vals_list if v.get('party_id')])
        for party in parties:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; no transaction can be "
                    "added. Ask a manager to reopen it first.", party.name))
        return super().create(vals_list)

    def write(self, vals):
        for txn in self:
            if txn.party_id.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its transactions cannot "
                    "be edited. Ask a manager to reopen it first.",
                    txn.party_id.name))
        return super().write(vals)

    def unlink(self):
        for txn in self:
            if txn.party_id.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its transactions cannot "
                    "be removed. Ask a manager to reopen it first.",
                    txn.party_id.name))
        return super().unlink()


class EhRelatedPartyCompensation(models.Model):
    _name = 'eh.related.party.compensation'
    _description = "KMP compensation category (IAS 24.17)"
    _order = 'party_id, category, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'party_id'
    _eh_disclosure_server_fields = frozenset({
        'company_id', 'currency_id', 'origin',
    })
    _eh_disclosure_engine_origins = frozenset({'ledger', 'sbp'})

    party_id = fields.Many2one(
        'eh.related.party', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='party_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='party_id.currency_id', store=True, readonly=True)

    category = fields.Selection(
        [('short_term', "Short-term employee benefits"),
         ('post_employment', "Post-employment benefits"),
         ('other_long_term', "Other long-term benefits"),
         ('termination', "Termination benefits"),
         ('share_based', "Share-based payment")],
        required=True, default='short_term',
        help="IAS 24.17 compensation category. The note discloses the "
             "total and each category.")
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Compensation recognised as an expense in the period for the "
             "category.")
    origin = fields.Selection(
        [('manual', "Manual"),
         ('ledger', "Ledger (posted move lines)"),
         ('sbp', "Share-based payment engine")],
        default='manual', required=True,
        help="Engine-origin lines are updated in place by their prefill: the "
             "ledger lines by Populate KMP Compensation, the share-based line "
             "by Prefill Share-based. Manual lines are never touched by "
             "either.")
    note = fields.Char()

    _unique_party_category = models.Constraint(
        'unique(party_id, category)',
        'Each IAS 24 compensation category can appear only once per party.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Create guard on child lines feeding a frozen parent.
        parties = self.env['eh.related.party'].browse([
            v.get('party_id') for v in vals_list if v.get('party_id')])
        for party in parties:
            if party.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; no compensation line "
                    "can be added. Ask a manager to reopen it first.",
                    party.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.party_id.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its compensation lines "
                    "cannot be edited. Ask a manager to reopen it first.",
                    line.party_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.party_id.state == 'finalised':
                raise UserError(_(
                    "Related party %s is finalised; its compensation lines "
                    "cannot be removed. Ask a manager to reopen it first.",
                    line.party_id.name))
        return super().unlink()
