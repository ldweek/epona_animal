# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.entity: a consolidated reporting entity.

A consolidation entity defines a parent perspective and the set of
subsidiary companies that roll up into it. The entity owns the
presentation currency (the currency consolidated balances are
reported in) and the list of member companies via the
eh.consol.member intermediate model.

Per IFRS 10, an entity controls another when it has power over the
relevant activities, exposure to variable returns, and the ability
to use its power to affect those returns. This module assumes
control determination is made off-platform; the entity definition
captures the result (which subsidiaries are in scope) plus the
ownership percentage that drives NCI computation.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_CLOSURE_FIELDS = frozenset({
    'parent_company_id', 'presentation_currency_id',
    'consolidation_company_id', 'member_ids',
})

# Current configuration is displayed beside every historical run.  Freezing
# only the company closure would let an actor rewrite the accounts and policy
# that supposedly produced already-settled lines/GL evidence.
_EVIDENCE_FIELDS = _CLOSURE_FIELDS | frozenset({
    'consolidation_journal_id',
    'cta_account_id', 'nci_account_id', 'parent_retained_earnings_account_id',
    'bargain_purchase_gain_account_id',
    'cta_gain_account_id', 'cta_loss_account_id',
    'auto_eliminate_investment', 'auto_eliminate_intragroup',
})

_ACCOUNT_ROLE_TYPES = {
    'cta_account_id': frozenset({'equity', 'equity_unaffected'}),
    'nci_account_id': frozenset({'equity', 'equity_unaffected'}),
    'parent_retained_earnings_account_id': frozenset({
        'equity_unaffected',
    }),
    'cta_gain_account_id': frozenset({'income', 'income_other'}),
    'cta_loss_account_id': frozenset({
        'expense', 'expense_other', 'expense_depreciation',
        'expense_direct_cost',
    }),
    'bargain_purchase_gain_account_id': frozenset({
        'income', 'income_other',
    }),
}


class EhConsolEntity(models.Model):
    _name = 'eh.consol.entity'
    _description = "Consolidated reporting entity"
    _order = 'name'
    _inherit = [
        'mail.thread', 'mail.activity.mixin',
        'eh.consol.company.scope.mixin',
    ]

    @api.model
    def _eh_assert_company_ids_allowed(self, company_ids):
        if self.env.su:
            return
        requested = {int(company_id) for company_id in company_ids if company_id}
        if not requested.issubset(set(self.env.companies.ids)):
            # Keep this deliberately generic: a guessed forbidden id must not
            # become a company existence/name oracle.
            raise AccessError(_(
                "A consolidation may only be viewed or operated when its "
                "parent, every member, and its consolidation ledger company "
                "are all enabled in the caller's current company context."
            ))

    def _eh_descendant_companies(self, roots):
        """Return roots plus every Odoo branch below them, including inactive.

        A branch posts on its own ``company_id`` even though root-level chart,
        currency and fiscal settings are delegated. Consolidating only the root
        therefore omits real ledger rows. The closure is resolved server-side,
        then the caller's enabled-company set is checked against the exact IDs.
        """
        roots = roots.exists()
        if not roots:
            return roots
        return self.env['res.company'].sudo().with_context(
            active_test=False,
        ).search([('id', 'child_of', roots.ids)])

    def _eh_operating_scope_companies(self):
        """Parent/member legal-entity roots and their branches, never ledger."""
        self.ensure_one()
        roots = self.parent_company_id | self.member_ids.mapped('company_id')
        return self._eh_descendant_companies(roots)

    def _eh_member_source_companies(self, member):
        self.ensure_one()
        if member.entity_id != self:
            raise ValidationError(_(
                "A member source scope must belong to its consolidation entity."
            ))
        return self._eh_descendant_companies(member.company_id)

    def _eh_scope_companies(self):
        self.ensure_one()
        roots = (
            self.parent_company_id
            | self.member_ids.mapped('company_id')
            | self.consolidation_company_id
        )
        return self._eh_descendant_companies(roots)

    def _eh_assert_company_closure(self, operation='read'):
        if not self.env.su:
            self._eh_check_access(operation)
            for entity in self:
                entity._eh_assert_company_ids_allowed(
                    entity._eh_scope_companies().ids)
        return self

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    @api.model
    def _eh_member_company_ids_from_commands(self, commands):
        company_ids = set()
        for command in commands or ():
            if command[0] == 0:
                company_ids.add(command[2].get('company_id'))
            elif command[0] in (1, 4) and command[1]:
                member = self.env['eh.consol.member'].browse(command[1])
                if not self.env.su:
                    member._eh_check_access('read')
                company_ids.add(member.company_id.id)
        return {company_id for company_id in company_ids if company_id}

    @api.model
    def _eh_validate_reference_values(self, vals, current=None):
        parent_id = vals.get(
            'parent_company_id',
            current.parent_company_id.id if current else self.env.company.id,
        )
        ledger_id = vals.get(
            'consolidation_company_id',
            current.consolidation_company_id.id if current else False,
        )
        presentation_currency_id = vals.get(
            'presentation_currency_id',
            current.presentation_currency_id.id
            if current else self.env.company.currency_id.id,
        )
        member_ids = (
            set(current.member_ids.mapped('company_id').ids)
            if current else set()
        ) | self._eh_member_company_ids_from_commands(vals.get('member_ids'))
        if parent_id and parent_id in member_ids:
            raise ValidationError(_(
                "The consolidation parent company must be distinct from "
                "every member company."
            ))
        if ledger_id and ledger_id in member_ids:
            raise ValidationError(_(
                "The dedicated consolidation ledger company cannot also be "
                "a consolidation member."
            ))
        # A root already includes every branch below it. Allowing a branch to
        # appear as another member (or as the dedicated ledger) would pull the
        # same AML twice. Fail before any source extraction.
        operating_roots = self.env['res.company'].browse(
            [company_id for company_id in {parent_id, *member_ids} if company_id]
        )
        for root in operating_roots:
            descendants = self._eh_descendant_companies(root) - root
            overlap = descendants & operating_roots
            if overlap:
                raise ValidationError(_(
                    "Parent/member company roots cannot overlap through an "
                    "Odoo branch hierarchy; configure only the highest root "
                    "because its branches are included automatically."
                ))
            if ledger_id and self.env['res.company'].browse(ledger_id) in descendants:
                raise ValidationError(_(
                    "The dedicated consolidation ledger cannot be a branch of "
                    "an operating parent/member company."
                ))
        scope_ids = {parent_id, ledger_id, *member_ids} - {False, None}
        self._eh_assert_company_ids_allowed(scope_ids)
        ledger = self.env['res.company'].browse(ledger_id)
        if (
            ledger
            and ledger.currency_id.id != presentation_currency_id
        ):
            raise ValidationError(_(
                "The consolidation ledger company currency must equal the "
                "entity presentation currency."
            ))
        journal = self.env['account.journal'].browse(vals.get(
            'consolidation_journal_id',
            current.consolidation_journal_id.id if current else False,
        ))
        if journal:
            if not self.env.su:
                journal._eh_check_access('read')
            if (
                not ledger_id
                or journal.company_id.id != ledger_id
                or journal.type != 'general'
            ):
                raise ValidationError(_(
                    "The consolidation journal must be a general journal in "
                    "the dedicated consolidation ledger company."
                ))
        for field_name in (
            'cta_account_id', 'nci_account_id', 'cta_gain_account_id',
            'cta_loss_account_id', 'parent_retained_earnings_account_id',
            'bargain_purchase_gain_account_id',
        ):
            account = self.env['account.account'].browse(vals.get(
                field_name, current[field_name].id if current else False))
            if not account:
                continue
            if not self.env.su:
                account._eh_check_access('read')
            if not (self._eh_account_company_ids(account) & scope_ids):
                raise ValidationError(_(
                    "Consolidation account references must belong to the "
                    "authorised company closure."
                ))
            allowed_types = _ACCOUNT_ROLE_TYPES[field_name]
            if account.account_type not in allowed_types:
                raise ValidationError(_(
                    "%(field)s must use an account with one of these roles: "
                    "%(roles)s.",
                    field=self._fields[field_name].string,
                    roles=', '.join(sorted(allowed_types)),
                ))
            if (
                field_name == 'parent_retained_earnings_account_id'
                and parent_id not in self._eh_account_company_ids(account)
            ):
                raise ValidationError(_(
                    "The parent opening retained-earnings account must belong "
                    "to the consolidation parent company."
                ))
        return scope_ids

    def _eh_lock_configuration(self, extra_company_ids=()):
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_consol_entity WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            if {row[0] for row in self.env.cr.fetchall()} != set(ids):
                raise UserError(_(
                    "A consolidation entity changed concurrently. Retry the "
                    "operation."
                ))
            self.invalidate_recordset()
        # The entity row is the serialization root for its closure.  A
        # member rehome may have committed while this transaction waited for
        # that row, so discard child relation/value caches before deriving
        # the company set protected by the next lock tier.
        members = self.mapped('member_ids')
        members.invalidate_recordset([
            'entity_id', 'company_id', 'cta_position_id',
        ])
        company_ids = set(extra_company_ids)
        for entity in self:
            company_ids.update(entity._eh_scope_companies().ids)
        company_ids = tuple(sorted(
            int(company_id) for company_id in company_ids if company_id))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (company_ids,))
        return self

    def _eh_lock_scope_runs(self):
        """Lock every run whose evidence depends on this entity closure."""
        entity_ids = tuple(sorted(self.ids))
        if not entity_ids:
            return []
        self.env.cr.execute(
            """
            SELECT run.id,
                   run.state,
                   run.move_id,
                   run.reversal_move_id,
                   run.legacy_evidence_quarantined,
                   EXISTS (
                       SELECT 1
                         FROM eh_consol_run_move_history_rel AS history
                        WHERE history.run_id = run.id
                   ) AS has_history,
                   EXISTS (
                       SELECT 1
                         FROM eh_consol_run_line AS line
                        WHERE line.run_id = run.id
                   ) AS has_lines,
                   EXISTS (
                       SELECT 1
                         FROM eh_consol_elimination AS elimination
                        WHERE elimination.run_id = run.id
                          AND (
                              elimination.state IS DISTINCT FROM 'draft'
                              OR elimination.posted_at IS NOT NULL
                              OR elimination.posted_by_id IS NOT NULL
                          )
                   ) AS has_elimination_evidence
                   , EXISTS (
                       SELECT 1
                         FROM eh_consol_disposal_event AS disposal
                        WHERE disposal.run_id = run.id
                   ) AS has_disposal_evidence
              FROM eh_consol_run AS run
             WHERE run.entity_id IN %s
             ORDER BY run.id
             FOR UPDATE OF run
            """,
            (entity_ids,),
        )
        return self.env.cr.fetchall()

    def _eh_assert_scope_mutable(self):
        """Freeze historical parent/member/ledger identity fail-closed."""
        settled = [
            row for row in self._eh_lock_scope_runs()
            if row[1] in ('computed', 'reviewed', 'closed')
            or row[2] or row[3] or row[4] or row[5] or row[6] or row[7]
            or row[8]
        ]
        if settled:
            raise UserError(_(
                "The consolidation parent, presentation currency, ledger "
                "and member closure cannot change after a run is settled or "
                "has accounting history. Create a new consolidation entity "
                "for the revised scope."
            ))
        return self

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Display name of the consolidated entity. Convention: "
            "'<Group> Consolidated' or '<Parent> Group'."
        ),
    )
    code = fields.Char(
        required=True, copy=False,
        help=(
            "Stable programmatic identifier (alphanumeric + "
            "underscore). Used for cross-record references so a "
            "renamed name does not break external links."
        ),
    )

    presentation_currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        tracking=True,
        help=(
            "Currency the consolidated balances are reported in. "
            "Member companies whose functional currency differs are "
            "translated per IAS 21 at run time."
        ),
    )
    parent_company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        tracking=True,
        help=(
            "Root company of the consolidated entity. Used for "
            "default-currency resolution and ownership of the "
            "consolidated reports themselves."
        ),
    )

    consolidation_company_id = fields.Many2one(
        'res.company',
        string="Consolidation Ledger Company",
        tracking=True,
        help=(
            "Optional dedicated company reserved for the consolidated "
            "ledger. When set, a computed run can post a single balanced, "
            "immutable account.move into this company's books (IFRS 10 "
            "auditability), and closing the run then requires that move to be "
            "posted. Leave empty to keep the run a memo-only set of run "
            "lines, which is the default behaviour. This must NOT be an "
            "operating company whose transactions are already pulled into the "
            "consolidation, or the subsidiaries would be double-counted; use "
            "a company created solely to hold the consolidated result. Its "
            "chart of accounts must carry, by code, every account referenced "
            "by the run (share the parent chart, or map codes 1:1)."
        ),
    )
    consolidation_journal_id = fields.Many2one(
        'account.journal',
        ondelete='restrict',
        string="Consolidation Journal",
        tracking=True,
        domain="[('type', '=', 'general'),"
               " ('company_id', '=', consolidation_company_id)]",
        help=(
            "General journal in the consolidation ledger company that the "
            "posted consolidation move is booked to. When left empty, the "
            "first general journal in the consolidation company is used."
        ),
    )
    cta_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="CTA / Translation Reserve Account",
        tracking=True,
        help=(
            "Equity account on the parent chart the IAS 21 currency "
            "translation adjustment (CTA) is booked to. When set, the run "
            "books the CTA plug to this account explicitly. When left empty "
            "the run falls back to a name heuristic (an equity account whose "
            "name contains 'translation' or 'CTA'); if that also resolves "
            "nothing and the CTA is non-zero the compute is refused rather "
            "than silently posting a CTA line with no account."
        ),
    )
    nci_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Default NCI Account",
        tracking=True,
        help=(
            "Equity account on the parent chart the non-controlling interest "
            "carve-out is booked to for members that do not set their own NCI "
            "account. When left empty the run falls back to a name heuristic "
            "(an equity account whose name contains 'non-controlling' or "
            "'minority'); if that also resolves nothing and an NCI carve-out "
            "is non-zero the compute is refused rather than silently posting "
            "an NCI line with no account."
        ),
    )
    parent_retained_earnings_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Parent Opening Retained Earnings Account",
        tracking=True,
        help=(
            "Parent-company equity_unaffected account that receives net "
            "profit or loss posted before a run's period start. Required when "
            "the parent chart has more than one such account; prevents opening "
            "results from being mislabeled as CTA."
        ),
    )
    bargain_purchase_gain_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Bargain Purchase Gain Account",
        tracking=True,
        help=(
            "Income account used for an IFRS 3.34 bargain-purchase gain. A "
            "negative goodwill residual is never credited to the goodwill "
            "asset account."
        ),
    )

    auto_eliminate_investment = fields.Boolean(
        string="Auto-Eliminate Investment (IFRS 3)",
        default=True,
        tracking=True,
        help=(
            "When enabled (default, the shipped behaviour), each compute "
            "auto-generates the IFRS 3 acquisition elimination for every "
            "fully-configured full-method member: the parent's investment "
            "is removed against the subsidiary's acquisition-date equity, "
            "acquisition-date NCI is recognised on the member's NCI basis, "
            "and the residual is carried as goodwill (or a bargain-purchase "
            "credit). When disabled, no elimination is auto-generated and "
            "the run falls back to the diagnostic warning that the "
            "investment is not eliminated, leaving the entry to a manual "
            "elimination."
        ),
    )
    cta_gain_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="CTA Recycling Gain Account",
        tracking=True,
        help=(
            "Income account credited when a member's accumulated "
            "translation GAIN is recycled from the CTA reserve to profit "
            "or loss on disposal of the member (IAS 21.48). A member whose "
            "CTA position link carries its own reclass gain account uses "
            "that instead."
        ),
    )
    cta_loss_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="CTA Recycling Loss Account",
        tracking=True,
        help=(
            "Expense account debited when a member's accumulated "
            "translation LOSS is recycled from the CTA reserve to profit "
            "or loss on disposal of the member (IAS 21.48). A member whose "
            "CTA position link carries its own reclass loss account uses "
            "that instead."
        ),
    )

    auto_eliminate_intragroup = fields.Boolean(
        string="Auto-Eliminate Intragroup Balances",
        default=False,
        tracking=True,
        help=(
            "When enabled, each compute also generates automatic elimination "
            "run lines (IFRS 10.B86) for intragroup balances BETWEEN member "
            "companies, in addition to any manual elimination entries: "
            "reciprocal receivables versus payables, and sales income against "
            "the counterparty's purchases / cost of sales. Each auto "
            "elimination is a balanced pair that nets to zero, so the run "
            "still balances and the CTA is unaffected. When a reciprocal "
            "intragroup balance does not agree between the two companies, a "
            "diagnostic is surfaced rather than the difference being silently "
            "plugged. Default off: leaving it unchecked reproduces the prior "
            "behaviour exactly."
        ),
    )

    member_ids = fields.One2many(
        'eh.consol.member', 'entity_id', copy=True,
        help=(
            "Subsidiary companies in scope for the consolidation, "
            "with ownership percentage and consolidation method."
        ),
    )
    member_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    run_ids = fields.One2many(
        'eh.consol.run', 'entity_id', readonly=True,
    )
    run_count = fields.Integer(
        compute='_compute_counts', store=False,
    )

    active = fields.Boolean(
        default=True,
        help=(
            "Soft-archive flag. Inactive entities are hidden from "
            "the run-creation picker but existing runs stay readable."
        ),
    )
    notes = fields.Html()

    _unique_code = models.Constraint(
        'unique(code)',
        'Consolidation entity code must be unique.',
    )

    @api.depends('member_ids', 'run_ids')
    def _compute_counts(self):
        for entity in self:
            entity.member_count = len(entity.member_ids)
            entity.run_count = len(entity.run_ids)

    @api.model
    def _eh_assert_unique_code_values(self, vals_list, current=None):
        """Give callers a controlled error while retaining the SQL race guard."""
        codes = [
            vals.get('code')
            for vals in vals_list
            if vals.get('code')
        ]
        if len(codes) != len(set(codes)):
            raise ValidationError(_(
                "Consolidation entity code must be unique."
            ))
        if not codes:
            return True
        domain = [('code', 'in', codes)]
        if current:
            domain.append(('id', 'not in', current.ids))
        if self.sudo().search_count(domain):
            raise ValidationError(_(
                "Consolidation entity code must be unique."
            ))
        return True

    @api.model_create_multi
    def create(self, vals_list):
        self._eh_assert_unique_code_values(vals_list)
        scope_ids = set()
        for vals in vals_list:
            scope_ids.update(self._eh_validate_reference_values(vals))
        self._eh_lock_configuration(extra_company_ids=scope_ids)
        for vals in vals_list:
            self._eh_validate_reference_values(vals)
        self._eh_assert_unique_code_values(vals_list)
        entities = super().create(vals_list)
        entities._eh_invalidate_company_scope_cache()
        entities._eh_assert_company_closure('write')
        return entities

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        scope_ids = set()
        for entity in self:
            scope_ids.update(
                self._eh_validate_reference_values(vals, current=entity))
        self._eh_lock_configuration(extra_company_ids=scope_ids)
        self._eh_assert_unique_code_values(
            [vals for _entity in self], current=self,
        )
        if set(vals) & _EVIDENCE_FIELDS:
            self._eh_assert_scope_mutable()
        for entity in self:
            self._eh_validate_reference_values(vals, current=entity)
        result = super().write(vals)
        if 'presentation_currency_id' in vals:
            self.mapped('member_ids')._eh_restamp_owned_outputs()
        if set(vals) & _CLOSURE_FIELDS:
            self._eh_invalidate_company_scope_cache()
        self._eh_assert_company_closure('write')
        return result

    def unlink(self):
        self._eh_assert_company_closure('unlink')
        self._eh_lock_configuration()
        self._eh_assert_scope_mutable()
        self._eh_assert_company_closure('unlink')
        result = super().unlink()
        self._eh_invalidate_company_scope_cache()
        return result

    @api.constrains('code')
    def _check_code_format(self):
        import re
        pattern = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]*$')
        for rec in self:
            if not pattern.match(rec.code or ''):
                raise ValidationError(_(
                    "Consolidation entity code must match "
                    "[a-zA-Z][a-zA-Z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('consolidation_company_id', 'presentation_currency_id')
    def _check_consolidation_company_currency(self):
        """The consolidation ledger company currency must equal the
        presentation currency.

        The posted consolidation move books run-line amounts (which are in the
        presentation currency, per IAS 21) directly as debit/credit in the
        ledger company's own currency, without conversion. If the two differ
        the posted move would be in the wrong currency scale, so equality is
        required at configuration time rather than silently mis-scaling the
        books.
        """
        for rec in self:
            ledger = rec.consolidation_company_id
            if not ledger:
                continue
            if ledger.currency_id != rec.presentation_currency_id:
                raise ValidationError(_(
                    "The consolidation ledger company (%(company)s) uses "
                    "currency %(company_ccy)s, but the entity's presentation "
                    "currency is %(pres_ccy)s. The consolidation move is "
                    "posted directly in the presentation currency, so the "
                    "ledger company must use that same currency. Set the "
                    "ledger company currency to %(pres_ccy)s (or pick a "
                    "ledger company that already uses it).",
                    company=ledger.display_name,
                    company_ccy=ledger.currency_id.name,
                    pres_ccy=rec.presentation_currency_id.name,
                ))

    @api.constrains('consolidation_company_id', 'parent_company_id',
                    'member_ids')
    def _check_consolidation_company_dedicated(self):
        """The consolidation ledger company must be dedicated.

        Posting the consolidated move into the parent's or a member's
        operating company would double-count that company's own balances
        into the consolidated ledger, so the target must be a separate
        company reserved for consolidation.
        """
        for rec in self:
            ledger = rec.consolidation_company_id
            if not ledger:
                continue
            operating = rec.parent_company_id | rec.member_ids.mapped(
                'company_id')
            if ledger in operating:
                raise ValidationError(_(
                    "The consolidation ledger company (%s) must be a "
                    "dedicated company, not the parent company or any "
                    "member's operating company.",
                ) % ledger.display_name)

    def copy_data(self, default=None):
        # code is copy=False, required and unique, so a plain Duplicate
        # would leave it blank and fail both the required and the format
        # constraint. Generate a unique copy code instead.
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        if 'code' not in default:
            for entity, vals in zip(self, vals_list):
                vals['code'] = entity._eh_next_copy_code()
        return vals_list

    def _eh_next_copy_code(self):
        self.ensure_one()
        base = "%s_copy" % (self.code or 'entity')
        candidate, n = base, 1
        while self.sudo().with_context(active_test=False).search_count(
            [('code', '=', candidate)],
        ):
            n += 1
            candidate = "%s%d" % (base, n)
        return candidate

    def action_view_members(self):
        self.ensure_one()
        self._eh_assert_company_closure('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Members"),
            'res_model': 'eh.consol.member',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('entity_id', '=', self.id)],
            'context': {'default_entity_id': self.id},
        }

    def action_view_runs(self):
        self.ensure_one()
        self._eh_assert_company_closure('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Runs"),
            'res_model': 'eh.consol.run',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('entity_id', '=', self.id)],
            'context': {'default_entity_id': self.id},
        }
