# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 12 disclosure of interests in other entities."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .integrity import (
    DISCLOSURE_ENGINE_CAPABILITY, DISCLOSURE_ENGINE_CONTEXT,
)

# Register figures frozen once the interest is finalised. Writing any of these
# on a finalised interest is refused so a signed-off IFRS 12 disclosure cannot
# be silently re-keyed. The computed non-controlling interest is never in this
# set, so it still recomputes.
_ENTITY_INTEREST_FROZEN_FIELDS = frozenset({
    'name', 'company_id', 'interest_type', 'principal_place', 'ownership_pct',
    'voting_rights_pct', 'consolidation_method', 'restriction_line_ids',
    'significant_judgements', 'consol_run_res_id', 'consol_run_name',
    'summarised_assets', 'summarised_liabilities', 'summarised_revenue',
    'summarised_profit', 'nci_carrying_amount', 'notes',
})

# account_type prefixes classifying a consolidation run line into a broad
# IFRS 12.B10/B12 summarised-financial-information bucket. Kept local so the
# feed stays decoupled from the consolidation engine's internals.
_ASSET_TYPE_PREFIX = 'asset'
_LIABILITY_TYPE_PREFIX = 'liability'
_INCOME_TYPES = ('income', 'income_other')
_EXPENSE_TYPES = ('expense', 'expense_other', 'expense_depreciation',
                  'expense_direct_cost')


class EhEntityInterest(models.Model):
    _name = 'eh.entity.interest'
    _description = "Interest in another entity (IFRS 12)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'interest_type, name'
    _rec_name = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'nci_pct', 'total_restricted',
        'consol_run_res_id', 'consol_run_name',
    })

    name = fields.Char(
        required=True, tracking=True, help="Name of the other entity.")
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised interest disclosure is locked: its figures cannot "
             "be edited. Only a manager can finalise or reopen it. The "
             "computed non-controlling interest still recomputes.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True, tracking=True)

    interest_type = fields.Selection(
        [('subsidiary', "Subsidiary"), ('associate', "Associate"),
         ('joint_venture', "Joint venture"),
         ('joint_operation', "Joint operation"),
         ('structured', "Unconsolidated structured entity")],
        default='subsidiary', required=True, tracking=True,
        help="Type of interest disclosed (IFRS 12.10-31).")
    principal_place = fields.Char(
        string="Principal place of business", tracking=True)
    ownership_pct = fields.Float(
        digits=(7, 4), tracking=True,
        help="Ownership interest held (percentage).")
    nci_pct = fields.Float(
        compute='_compute_nci', store=True, digits=(7, 4),
        help="Non-controlling interest, for a subsidiary.")
    voting_rights_pct = fields.Float(digits=(7, 4), tracking=True)
    consolidation_method = fields.Selection(
        [('full', "Full consolidation"), ('equity', "Equity method"),
         ('proportionate', "Proportionate"), ('none', "Not consolidated")],
        default='full', tracking=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    # --- Restrictions and significant judgements (IFRS 12) ---------------
    # IFRS 12.13 (subsidiaries) and 12.22 (joint arrangements and
    # associates) require disclosure of significant restrictions on the
    # ability to access or use assets and settle liabilities - dividend
    # blocks, loan covenants, regulatory ring-fencing - with the carrying
    # amounts affected. IFRS 12.7-9 require the significant judgements and
    # assumptions behind the control / joint control / significant
    # influence conclusion.
    restriction_line_ids = fields.One2many(
        'eh.entity.interest.restriction', 'interest_id',
        string="Restrictions")
    total_restricted = fields.Monetary(
        compute='_compute_total_restricted', store=True,
        currency_field='currency_id',
        help="Sum of the carrying amounts affected by the listed "
             "restrictions (IFRS 12.13(c) / 12.22(a)).")
    significant_judgements = fields.Text(
        string="Significant judgements and assumptions",
        tracking=True,
        help="The significant judgements and assumptions made in "
             "determining that the entity controls, jointly controls or "
             "significantly influences the other entity, including when "
             "the conclusion runs against the presumption from the voting "
             "share (IFRS 12.7-9).")

    # --- Summarised financial information (IFRS 12.12/B10-B12) ------------
    # IFRS 12.12 and B10-B12 require, for a subsidiary with material NCI,
    # the NCI proportion and summarised financial information (assets,
    # liabilities, revenue, profit or loss) of the subsidiary. These figures
    # are a consolidation output, so they are pulled from the latest
    # consolidation run's member and NCI lines (soft lookup) rather than
    # keyed. The run is a soft dependency, so the reference is a plain id +
    # name pair, not a Many2one to a model that may not be installed.
    consol_run_res_id = fields.Integer(
        string="Consolidation run id", readonly=True, copy=False,
        help="Database id of the consolidation run this interest last "
             "populated its summarised figures from. Plain id (not a link) "
             "so the register installs without the consolidation engine.")
    consol_run_name = fields.Char(
        string="Consolidation run", readonly=True, copy=False)
    summarised_assets = fields.Monetary(
        currency_field='currency_id', copy=False,
        help="Total assets of the subsidiary from the consolidation run's "
             "translated member balances (IFRS 12.B10(b)).")
    summarised_liabilities = fields.Monetary(
        currency_field='currency_id', copy=False,
        help="Total liabilities of the subsidiary from the consolidation "
             "run's translated member balances (IFRS 12.B10(b)).")
    summarised_revenue = fields.Monetary(
        currency_field='currency_id', copy=False,
        help="Revenue of the subsidiary from the consolidation run's "
             "translated member balances (IFRS 12.B10(b)), as a positive "
             "figure.")
    summarised_profit = fields.Monetary(
        currency_field='currency_id', copy=False,
        help="Profit or loss of the subsidiary from the consolidation run's "
             "translated member balances (IFRS 12.B10(b)); positive for a "
             "profit.")
    nci_carrying_amount = fields.Monetary(
        currency_field='currency_id', copy=False,
        help="Accumulated non-controlling interest carrying amount for the "
             "subsidiary from the consolidation run's NCI lines "
             "(IFRS 12.B10(a)).")
    notes = fields.Text()

    _check_ownership = models.Constraint(
        'CHECK (ownership_pct >= 0 AND ownership_pct <= 100)',
        'Ownership percentage must be between 0 and 100.',
    )
    _check_voting_rights = models.Constraint(
        'CHECK (voting_rights_pct >= 0 AND voting_rights_pct <= 100)',
        'Voting-rights percentage must be between 0 and 100.',
    )

    @api.depends('interest_type', 'ownership_pct')
    def _compute_nci(self):
        for r in self:
            r.nci_pct = (100.0 - r.ownership_pct
                         if r.interest_type == 'subsidiary' else 0.0)

    @api.depends('restriction_line_ids.carrying_amount')
    def _compute_total_restricted(self):
        for r in self:
            r.total_restricted = sum(
                r.restriction_line_ids.mapped('carrying_amount'))

    # --- populate summarised figures from the consolidation run ----------

    def _find_consol_member(self, run):
        """Return the run's member whose subsidiary company name matches this
        interest's name (case-insensitive, trimmed), or an empty recordset.

        The interest register keys the subsidiary by name (IFRS 12 discloses
        the entity by name), so the feed matches the interest to a member by
        the member company's display name. Returns the empty recordset when no
        member matches, so the caller can report a clear diagnostic."""
        self.ensure_one()
        target = (self.name or '').strip().lower()
        for member in run.entity_id.member_ids:
            if (member.company_id.display_name or '').strip().lower() \
                    == target:
                return member
        return run.entity_id.member_ids.browse()

    def action_populate_from_consolidation(self):
        """Fill this interest's NCI proportion and summarised financial
        information from the latest consolidation run (IFRS 12.12/B10-B12).

        Soft lookup: refused with a clear message when the consolidation
        engine (eh.consol.run) is not installed. For each interest, the latest
        settled (computed / reviewed / closed) run whose entity carries a
        member matching the interest name (see _find_consol_member) is taken.
        From that run:

          ownership_pct  <- member.ownership_pct (nci_pct recomputes to
                            100 - ownership for a subsidiary)
          summarised_assets / _liabilities / _revenue / _profit
                         <- the member's translated kind='subsidiary_balance'
                            lines bucketed by account_type (assets debit-
                            positive; liabilities, revenue and profit re-signed
                            to a positive presentation)
          nci_carrying_amount
                         <- the member's kind='nci' lines, re-signed positive

        Restrictions and the significant-judgements narrative are left
        untouched: they are narrative by nature and correctly stay manual.
        Idempotent: every populate overwrites the same summarised fields.
        Refused on a finalised interest."""
        self = self._eh_disclosure_prepare_action(engine=True)
        if 'eh.consol.run' not in self.env:
            raise UserError(_(
                "Populating summarised financial information requires the "
                "Consolidation module (eh_account_consolidation). Install it, "
                "or key the figures manually."))
        Run = self.env['eh.consol.run']
        for interest in self:
            if interest.state == 'finalised':
                raise UserError(_(
                    "Interest %s is finalised; its summarised figures cannot "
                    "be populated. Ask a manager to reopen it first.",
                    interest.name))
            candidates = Run.search([
                ('state', 'in', ('computed', 'reviewed', 'closed')),
                ('entity_id.parent_company_id', '=', interest.company_id.id),
            ], order='period_to desc, id desc')
            matched_run = matched_member = False
            for candidate in candidates:
                member = interest._find_consol_member(candidate)
                if member:
                    matched_run, matched_member = candidate, member
                    break
            if not matched_member:
                raise UserError(_(
                    "No settled consolidation run carries a member matching "
                    "interest %s. Compute a run whose entity includes a "
                    "member company named exactly like this interest, or key "
                    "the figures manually.", interest.name))
            interest._apply_consolidation_figures(matched_run, matched_member)
        return True

    def _apply_consolidation_figures(self, run, member):
        """Write the summarised figures for `member` from `run` onto this
        interest. Split out so the numbers are derived in one auditable place."""
        self.ensure_one()
        target_currency = self.currency_id or run.presentation_currency_id
        source_currency = run.presentation_currency_id
        scale = (
            member.ownership_pct / 100.0
            if getattr(member, 'method', 'full') == 'proportional' else 1.0
        )
        if getattr(member, 'method', 'full') == 'proportional' and not scale:
            raise UserError(_(
                "Proportional consolidation member %s has zero ownership, "
                "so its full summarised figures cannot be reconstructed.",
                member.display_name,
            ))
        member_lines = run.line_ids.filtered(
            lambda l: l.member_id == member
            and l.kind == 'subsidiary_balance')
        assets = liabilities = income = expense = 0.0
        for line in member_lines:
            acc_type = line.account_id.account_type or ''
            if acc_type.startswith(_ASSET_TYPE_PREFIX):
                assets += line.amount / scale
            elif acc_type.startswith(_LIABILITY_TYPE_PREFIX):
                liabilities += line.amount / scale
            elif acc_type in _INCOME_TYPES:
                income += line.amount / scale
            elif acc_type in _EXPENSE_TYPES:
                expense += line.amount / scale
        # Liabilities and income are credit-negative in Odoo's sign
        # convention; re-sign them to a positive presentation. Profit or loss
        # is the negated sum of the P&L amounts (income credit-negative,
        # expense debit-positive), positive for a profit.
        nci_lines = run.line_ids.filtered(
            lambda l: l.member_id == member and l.kind == 'nci')
        nci_carrying = -sum(nci_lines.mapped('amount')) / scale
        def convert(amount):
            return source_currency._convert(
                amount, target_currency, self.company_id,
                getattr(run, 'period_to', fields.Date.context_today(self)),
            )
        self.sudo().write({
            'consol_run_res_id': run.id,
            'consol_run_name': run.display_name,
            'ownership_pct': member.ownership_pct,
            'summarised_assets': target_currency.round(convert(assets)),
            'summarised_liabilities': target_currency.round(
                convert(-liabilities)),
            'summarised_revenue': target_currency.round(convert(-income)),
            'summarised_profit': target_currency.round(
                convert(-(income + expense))),
            'nci_carrying_amount': target_currency.round(
                convert(nci_carrying)),
        })

    @api.model_create_multi
    def create(self, vals_list):
        # Creating an interest already finalised would skip the manager-gated
        # action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the interest figures once finalised (a signed-off disclosure
        # is frozen for everyone; restate via a manager-gated reopen). The
        # state field itself is owned by the inherited eh.workflow.guard,
        # which refuses any non-superuser direct write; the sanctioned
        # finalise / reopen actions run under sudo.
        if _ENTITY_INTEREST_FROZEN_FIELDS.intersection(vals):
            for interest in self:
                if interest.state == 'finalised':
                    raise UserError(_(
                        "Interest %s is finalised and cannot be edited. Ask a "
                        "manager to reopen it first.", interest.name))
        clear_provenance = bool(
            {'ownership_pct', 'summarised_assets', 'summarised_liabilities',
             'summarised_revenue', 'summarised_profit',
             'nci_carrying_amount'}.intersection(vals)
            and self.env.context.get(DISCLOSURE_ENGINE_CONTEXT)
            != DISCLOSURE_ENGINE_CAPABILITY
        )
        result = super().write(vals)
        if clear_provenance:
            self.filtered('consol_run_res_id').sudo().write({
                'consol_run_res_id': False,
                'consol_run_name': False,
            })
        return result

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen an "
                "interest disclosure."))

    def unlink(self):
        for interest in self:
            if interest.state == 'finalised':
                raise UserError(_(
                    "Interest %s is finalised and cannot be deleted. Ask a "
                    "manager to reopen it first.", interest.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the interest disclosure: figures freeze. Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self._compute_nci()
        self._compute_total_restricted()
        self.flush_recordset(['nci_pct', 'total_restricted'])
        for interest in self:
            if interest.state == 'finalised':
                raise UserError(_(
                    "Interest %s is already finalised.", interest.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised interest disclosure to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhEntityInterestRestriction(models.Model):
    _name = 'eh.entity.interest.restriction'
    _description = "Restriction on an entity interest (IFRS 12.13/22)"
    _order = 'interest_id, kind, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'interest_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})

    interest_id = fields.Many2one(
        'eh.entity.interest', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='interest_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='interest_id.currency_id', store=True, readonly=True)

    kind = fields.Selection(
        [('dividend', "Dividend / distribution restriction"),
         ('loan', "Loan or advance restriction (covenant)"),
         ('regulatory', "Regulatory / statutory ring-fence"),
         ('other', "Other restriction")],
        required=True, default='dividend',
        help="Nature of the significant restriction on the ability to "
             "access or use the entity's assets or settle its liabilities "
             "(IFRS 12.13(a)-(b) / 12.22(a)).")
    description = fields.Char(
        required=True,
        help="The restriction as it will read in the note, e.g. 'Dividends "
             "require central bank approval'.")
    carrying_amount = fields.Monetary(
        currency_field='currency_id', string="Carrying amount affected",
        help="Carrying amount, in the consolidated statements, of the "
             "assets and liabilities the restriction applies to "
             "(IFRS 12.13(c)).")

    @api.model_create_multi
    def create(self, vals_list):
        # Create guard on child lines feeding a frozen parent.
        interests = self.env['eh.entity.interest'].browse([
            v.get('interest_id') for v in vals_list
            if v.get('interest_id')])
        for interest in interests:
            if interest.state == 'finalised':
                raise UserError(_(
                    "Interest %s is finalised; no restriction can be "
                    "added. Ask a manager to reopen it first.",
                    interest.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.interest_id.state == 'finalised':
                raise UserError(_(
                    "Interest %s is finalised; its restrictions cannot be "
                    "edited. Ask a manager to reopen it first.",
                    line.interest_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.interest_id.state == 'finalised':
                raise UserError(_(
                    "Interest %s is finalised; its restrictions cannot be "
                    "removed. Ask a manager to reopen it first.",
                    line.interest_id.name))
        return super().unlink()
