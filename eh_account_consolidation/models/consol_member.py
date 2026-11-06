# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.member: a subsidiary company in a consolidation entity.

Holds the parent's ownership percentage (drives NCI computation),
the consolidation method (full / equity / proportional), and the
member's functional currency (drives IAS 21 translation).

Method semantics enforced by the run engine (consol_run.py):

* full: 100% of translated balances roll up; NCI carved for
  ownership below 100%, measured per nci_basis (proportionate share
  of equity, or fair value at acquisition plus the minority share of
  post-acquisition equity movement, IFRS 3.19).
* proportional: translation happens FIRST at full value, then every
  translated balance is scaled by ownership_pct / 100 before rollup,
  so FX translation is never distorted by the scaling. NCI does not
  exist under the proportional method (there is no minority in a
  proportionately consolidated interest), so NCI configuration is
  blocked by constraint and the carve is skipped.
* equity: not rolled up; IAS 28 one-line pick-up of the parent's
  share of the period result. Configuration (investment account +
  share-of-profit account) is mandatory unless the IAS 28.1A fair
  value option (fv_option) is elected, in which case no pick-up is
  booked and the run carries a memo disclosure line instead.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_METHOD_CHOICES = [
    ('full', "Full consolidation"),
    ('proportional', "Proportional"),
    ('equity', "Equity method"),
]

_ACCOUNT_REFERENCE_FIELDS = (
    'investment_account_id', 'goodwill_account_id', 'nci_account_id',
    'equity_elimination_account_id', 'share_of_profit_account_id',
)

_CLOSURE_FIELDS = frozenset({'entity_id', 'company_id'})
_EVIDENCE_FIELDS = _CLOSURE_FIELDS | frozenset({
    'ownership_pct', 'method', 'functional_currency_id',
    'investment_account_id', 'investment_amount',
    'acquisition_equity', 'acquisition_date', 'historical_rate',
    'nci_basis', 'nci_fair_value', 'fv_option',
    'reporting_date_offset_months', 'policy_aligned',
    'cta_position_id',
    'goodwill_account_id', 'nci_account_id',
    'equity_elimination_account_id', 'share_of_profit_account_id',
    'consolidation_account_map', 'acquisition_component_ids',
    'investment_currency_id',
})
_SERVER_OWNED_FIELDS = frozenset({'investment_currency_id'})
_MANAGER_ONLY_FIELDS = frozenset({
    'disposal_pct', 'disposal_loses_control',
})

_ACCOUNT_ROLE_TYPES = {
    'investment_account_id': frozenset({
        'asset_current', 'asset_non_current', 'asset_fixed',
    }),
    'goodwill_account_id': frozenset({'asset_non_current', 'asset_fixed'}),
    'nci_account_id': frozenset({'equity', 'equity_unaffected'}),
    'equity_elimination_account_id': frozenset({
        'equity', 'equity_unaffected',
    }),
    'share_of_profit_account_id': frozenset({'income', 'income_other'}),
}

_FINITE_ACCOUNTING_FIELDS = (
    'ownership_pct', 'investment_amount', 'acquisition_equity',
    'historical_rate', 'nci_fair_value', 'disposal_pct',
)
_NONNEGATIVE_ACCOUNTING_FIELDS = frozenset({
    'ownership_pct', 'investment_amount', 'acquisition_equity',
    'historical_rate', 'nci_fair_value', 'disposal_pct',
})


class EhConsolMember(models.Model):
    _name = 'eh.consol.member'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidation member company"
    _order = 'entity_id, sequence, id'
    _rec_name = 'company_id'

    @api.model
    def _eh_validate_finite_values(self, vals=None, current=None):
        vals = vals or {}
        for field_name in _FINITE_ACCOUNTING_FIELDS:
            value = vals.get(
                field_name,
                current[field_name] if current else 0.0,
            )
            number = float(value or 0.0)
            if not math.isfinite(number):
                raise ValidationError(_(
                    "Consolidation member field %s must be a finite number.",
                    field_name,
                ))
            if (
                field_name in _NONNEGATIVE_ACCOUNTING_FIELDS
                and number < 0.0
            ):
                raise ValidationError(_(
                    "Consolidation member field %s cannot be negative.",
                    field_name,
                ))
        ownership = float(vals.get(
            'ownership_pct',
            current.ownership_pct if current else 100.0,
        ) or 0.0)
        disposal = float(vals.get(
            'disposal_pct',
            current.disposal_pct if current else 100.0,
        ) or 0.0)
        if ownership > 100.0 or disposal > 100.0:
            raise ValidationError(_(
                "Ownership and disposal percentages cannot exceed 100."
            ))
        historical_rate = float(vals.get(
            'historical_rate',
            current.historical_rate if current else 0.0,
        ) or 0.0)
        acquisition_date = vals.get(
            'acquisition_date',
            current.acquisition_date if current else False,
        )
        if historical_rate > 0.0 and not acquisition_date:
            raise ValidationError(_(
                "A historical acquisition rate requires the acquisition date "
                "that proves which IAS 21 rate the amount uses."
            ))
        return True

    entity_id = fields.Many2one(
        'eh.consol.entity', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)

    company_id = fields.Many2one(
        'res.company', required=True,
        help=(
            "Subsidiary company. Must be different from the parent "
            "company on the entity (the parent is consolidated by "
            "definition; a self-reference would double-count)."
        ),
    )
    ownership_pct = fields.Float(
        default=100.0, digits=(7, 4), required=True,
        help=(
            "Parent's ownership of this subsidiary, as a percentage "
            "from 0.0 to 100.0. Drives the non-controlling interest "
            "(NCI) split: NCI = subsidiary equity x (1 - "
            "ownership_pct / 100). 100% ownership produces zero "
            "NCI."
        ),
    )
    method = fields.Selection(
        _METHOD_CHOICES, default='full', required=True,
        help=(
            "Consolidation method. Full: 100% of subsidiary balances "
            "roll up, NCI carved out. Proportional: ownership% of "
            "balances roll up. Equity: subsidiary not rolled up; "
            "instead, parent's share of subsidiary equity carried "
            "as a single investment line."
        ),
    )

    functional_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_functional_currency', store=True, readonly=False,
        help=(
            "Functional currency of the subsidiary. Defaults to the "
            "company's currency_id on the company record. Override "
            "when the functional currency under IAS 21 differs from "
            "the booking currency (rare)."
        ),
    )

    investment_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Investment Account",
        help=(
            "Account in the PARENT's books that carries the investment in "
            "this subsidiary. Used to detect whether the investment has "
            "been eliminated against the subsidiary's equity: an unposted "
            "elimination against this account means consolidated equity is "
            "still double-counted. When the acquisition basis is complete, "
            "the run automatically eliminates this balance under IFRS 3."
        ),
    )
    investment_amount = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Carrying amount (cost) of the parent's investment in this "
            "subsidiary, in the presentation currency. Used by the "
            "investment-elimination entry."
        ),
    )
    investment_currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_investment_currency', store=True, readonly=True,
        copy=False,
    )

    acquisition_equity = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Subsidiary's total book equity at the acquisition date, as a "
            "positive number. Stated in the presentation currency when no "
            "historical rate is set; when a historical rate is set it is "
            "stated in the subsidiary's functional currency and translated "
            "at that rate (IAS 21.23(b): non-monetary equity at the "
            "historical rate). This is the pre-acquisition equity removed by "
            "the IFRS 3 investment elimination through the reconciled "
            "account-by-account acquisition components. Set this together "
            "with the components, the "
            "investment account and amount to have the run auto-eliminate "
            "the parent's investment against the sub's acquisition-date "
            "equity."
        ),
    )
    acquisition_component_ids = fields.One2many(
        'eh.consol.acquisition.equity.component', 'member_id', copy=True,
        string="Acquisition Equity Components",
        help=(
            "Account-by-account acquisition-date equity basis. The components "
            "must reconcile exactly to Acquisition Equity and are eliminated "
            "on their own mapped share-capital/reserve accounts."
        ),
    )
    acquisition_date = fields.Date(
        help=(
            "Date control was obtained (IFRS 3 acquisition date). "
            "Documentation anchor for the acquisition-date equity and the "
            "historical rate used by the investment elimination."
        ),
    )
    historical_rate = fields.Float(
        digits=(20, 12), default=0.0,
        help=(
            "Optional acquisition-date (historical) exchange rate, quoted as "
            "presentation-currency units per one unit of the subsidiary's "
            "functional currency. When set above zero, acquisition_equity is "
            "read as a functional-currency figure and multiplied by this "
            "rate in the IFRS 3 investment elimination. Leave at zero when "
            "acquisition_equity is already stated in the presentation "
            "currency (previous behaviour, unchanged)."
        ),
    )
    nci_basis = fields.Selection(
        [
            ('proportionate', "Proportionate share of net assets"),
            ('fair_value', "Fair value (full goodwill)"),
        ],
        default='proportionate', required=True,
        help=(
            "IFRS 3.19 measurement basis for non-controlling interest. "
            "Proportionate: NCI = minority share of the subsidiary's "
            "identifiable net assets (partial goodwill). Fair value: NCI is "
            "recognised at its acquisition-date fair value (nci_fair_value), "
            "and goodwill includes the goodwill attributable to the minority "
            "(full goodwill). Post-acquisition, both bases attribute the "
            "minority share of the equity movement to NCI: NCI carrying "
            "amount = acquisition-date NCI (proportionate share or fair "
            "value) + (1 - ownership) x post-acquisition equity movement."
        ),
    )
    nci_fair_value = fields.Monetary(
        currency_field='investment_currency_id',
        help=(
            "Acquisition-date fair value of the non-controlling interest, in "
            "the presentation currency, as a positive number. Required when "
            "the NCI basis is fair value; ignored on the proportionate "
            "basis."
        ),
    )
    fv_option = fields.Boolean(
        string="IAS 28.1A Fair Value Option",
        default=False,
        help=(
            "Fair value election for an equity-method interest held by a "
            "venture capital organisation, mutual fund or similar entity "
            "(IAS 28.18-19 via IFRS 9). When set, the run books NO equity "
            "pick-up for this member: the investment is expected to be "
            "remeasured at fair value through profit or loss by the "
            "fair-value engine, and the run carries a memo disclosure line "
            "recording the election instead."
        ),
    )
    reporting_date_offset_months = fields.Integer(
        default=0,
        help=(
            "Gap in whole months between this member's own reporting date "
            "and the group's reporting date. IFRS 10.B92-B93 caps the "
            "difference at three months (with adjustment for significant "
            "transactions); a run blocks when any member exceeds three "
            "months unless the run-level override is set with a reason."
        ),
    )
    policy_aligned = fields.Boolean(
        default=True,
        help=(
            "Confirms this member's financial statements are prepared under "
            "uniform group accounting policies (IFRS 10.B87) or that "
            "conforming adjustments have been made. A run blocks while this "
            "is unchecked unless the run-level override is set with a "
            "reason."
        ),
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        ondelete='restrict',
        help=(
            "Optional link to the IAS 21 CTA reserve position "
            "(eh_account_fx_revaluation) tracking this foreign operation's "
            "cumulative translation reserve on the parent's books. The "
            "run's CTA lines for this member carry the link, and the "
            "member-disposal recycling logs against it. The position's own "
            "Dispose action remains the ledger-side reclassification; the "
            "run-side recycling here keeps the consolidated memo set "
            "aligned (IAS 21.48)."
        ),
    )
    disposal_pct = fields.Float(
        string="Disposal %", default=100.0, digits=(5, 2),
        help=(
            "Share of this member's accumulated CTA to recycle to profit or "
            "loss on the next Dispose action. 100 reclassifies the full "
            "remaining balance (IAS 21.48, loss of control); below 100 "
            "reclassifies the proportionate share (IAS 21.48A-C, partial "
            "disposal)."
        ),
    )
    disposal_loses_control = fields.Boolean(
        string="Disposal Loses Control",
        default=False,
        help=(
            "For a partial disposal of a fully consolidated subsidiary, "
            "confirm that the transaction causes loss of control. Loss of "
            "control reclassifies the entire remaining parent-attributable "
            "CTA to profit or loss (IAS 21.48); when control is retained, only "
            "the disposal percentage is reattributed to NCI with no P&L "
            "recycling (IAS 21.48C). A 100% disposal always loses control."
        ),
    )
    goodwill_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Goodwill Account",
        help=(
            "Account (asset) on the consolidated chart carrying goodwill "
            "recognised on acquisition, being the excess of the investment "
            "cost over the parent's share of acquisition-date equity. A "
            "positive goodwill asset. Bargain-purchase gains are credited to "
            "the entity's explicit income account instead."
        ),
    )
    nci_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="NCI Account",
        help=(
            "Equity account on the consolidated chart carrying the "
            "non-controlling interest recognised on acquisition (the "
            "minority's share of acquisition-date equity). Used by the "
            "auto-elimination."
        ),
    )
    equity_elimination_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Equity Elimination Account",
        help=(
            "Legacy compatibility default for imported acquisition bases. "
            "New configurations should set a consolidated target on every "
            "Acquisition Equity Component so share capital and each reserve "
            "remain separately traceable."
        ),
    )
    share_of_profit_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Share of Profit Account",
        help=(
            "Income account on the consolidated chart credited with the "
            "parent's share of an equity-method associate's period profit "
            "(IAS 28). Paired with the investment account, which is debited "
            "by the same share so the equity pick-up balances."
        ),
    )
    retained_earnings_account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        string="Opening Retained Earnings Account",
        help=(
            "Source-company equity_unaffected account that receives net "
            "profit or loss posted before a consolidation run's period start. "
            "Required when the source chart has more than one such account; "
            "prevents unclosed prior results from being mislabeled as CTA."
        ),
    )

    consolidation_account_map = fields.Text(
        help=(
            "Optional mapping of subsidiary chart-of-account codes "
            "to consolidated chart-of-account codes, one per line, "
            "format 'sub_code -> consol_code'. Lines not listed map "
            "1:1 by code. Used when subsidiaries follow different "
            "local CoA conventions and need to be remapped to the "
            "group standard."
        ),
    )

    notes = fields.Char()

    @api.model
    def _eh_parse_account_map_text(self, mapping_text):
        """Parse ``source -> target`` rows strictly and deterministically."""
        mapping = {}
        for line_number, raw_line in enumerate((mapping_text or '').splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.count('->') != 1:
                raise ValidationError(_(
                    "Account-map line %(line)d must use exactly 'source -> "
                    "target'.",
                    line=line_number,
                ))
            source, target = (part.strip() for part in line.split('->'))
            if not source or not target:
                raise ValidationError(_(
                    "Account-map line %d has an empty source or target code.",
                    line_number,
                ))
            if source in mapping:
                raise ValidationError(_(
                    "Subsidiary account code %s is mapped more than once.",
                    source,
                ))
            mapping[source] = target
        return mapping

    def _eh_resolved_account_map(self, mapping_text=None):
        """Return ``{source_account_id: target_account}``, fail closed.

        Mapping is resolved in the member's exact branch closure and against
        the dedicated consolidation chart when configured (otherwise parent).
        One code must identify exactly one account in each chart and the two
        account roles must match, preventing a text mapping from silently
        moving balances between statement classifications.
        """
        self.ensure_one()
        mapping = self._eh_parse_account_map_text(
            self.consolidation_account_map
            if mapping_text is None else mapping_text,
        )
        if not mapping:
            return {}
        Account = self.env['account.account'].sudo()
        company_field = (
            'company_ids' if 'company_ids' in Account._fields else 'company_id'
        )
        source_companies = self.entity_id._eh_member_source_companies(self)
        target_company = (
            self.entity_id.consolidation_company_id
            or self.entity_id.parent_company_id
        )
        resolved = {}
        for source_code, target_code in mapping.items():
            sources = Account.with_company(self.company_id).search([
                ('code', '=', source_code),
                (company_field, 'in', source_companies.ids),
            ], limit=2, order='id')
            targets = Account.with_company(target_company).search([
                ('code', '=', target_code),
                (company_field, 'in', target_company.ids),
            ], limit=2, order='id')
            if len(sources) != 1 or len(targets) != 1:
                raise ValidationError(_(
                    "Account map %(source)s -> %(target)s must resolve exactly "
                    "one source and one group-chart account (found %(sc)d and "
                    "%(tc)d).",
                    source=source_code, target=target_code,
                    sc=len(sources), tc=len(targets),
                ))
            if sources.account_type != targets.account_type:
                raise ValidationError(_(
                    "Account map %(source)s -> %(target)s crosses account roles "
                    "(%(source_role)s to %(target_role)s), which is forbidden.",
                    source=source_code, target=target_code,
                    source_role=sources.account_type,
                    target_role=targets.account_type,
                ))
            resolved[sources.id] = targets
        return resolved

    _check_ownership = models.Constraint(
        'CHECK (ownership_pct >= 0 AND ownership_pct <= 100)',
        'Ownership percentage must be between 0 and 100.',
    )

    _unique_member = models.Constraint(
        'unique(entity_id, company_id)',
        'Each company can appear at most once per consolidation entity.',
    )

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        for member in self:
            entity = self.env['eh.consol.entity'].browse(
                vals.get('entity_id', member.entity_id.id))
            entity._eh_assert_company_closure('write')
            entity._eh_assert_company_ids_allowed((
                vals.get('company_id', member.company_id.id),
            ))
            company = self.env['res.company'].browse(vals.get(
                'company_id', member.company_id.id,
            ))
            if company in (
                entity.parent_company_id | entity.consolidation_company_id
            ):
                raise ValidationError(_(
                    "A consolidation member must be distinct from both the "
                    "parent and the dedicated consolidation ledger company."
                ))
            expected = (
                entity.parent_company_id | entity.consolidation_company_id
            )
            for field_name in _ACCOUNT_REFERENCE_FIELDS:
                account_id = vals.get(field_name)
                account = (
                    self.env['account.account'].browse(account_id)
                    if field_name in vals else member[field_name]
                )
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if not (self._eh_account_company_ids(account) & set(expected.ids)):
                    raise ValidationError(_(
                        "Consolidation account references must belong to the "
                        "parent or dedicated consolidation company."
                    ))
                allowed_types = _ACCOUNT_ROLE_TYPES[field_name]
                if account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(field)s must use an account with one of these "
                        "roles: %(roles)s.",
                        field=self._fields[field_name].string,
                        roles=', '.join(sorted(allowed_types)),
                    ))
            retained_account = (
                self.env['account.account'].browse(
                    vals.get('retained_earnings_account_id'))
                if 'retained_earnings_account_id' in vals
                else member.retained_earnings_account_id
            )
            if retained_account:
                if not self.env.su:
                    retained_account._eh_check_access('read')
                if company.id not in self._eh_account_company_ids(
                    retained_account,
                ):
                    raise ValidationError(_(
                        "The opening retained-earnings account must belong to "
                        "the member's source company."
                    ))
                if retained_account.account_type != 'equity_unaffected':
                    raise ValidationError(_(
                        "The opening retained-earnings account must use the "
                        "Current Year Earnings / Unallocated Earnings role "
                        "(account type equity_unaffected)."
                    ))
            position_id = vals.get('cta_position_id')
            position = (
                self.env['eh.fx.cta.position'].browse(position_id)
                if 'cta_position_id' in vals else member.cta_position_id
            )
            if position:
                if not self.env.su:
                    position._eh_check_access('read')
                if position.company_id not in entity._eh_scope_companies():
                    raise ValidationError(_(
                        "The CTA position must belong to a company in the "
                        "consolidation closure."
                    ))
                if (
                    position.gain_account_id
                    and position.gain_account_id.account_type
                    not in {'income', 'income_other'}
                ):
                    raise ValidationError(_(
                        "A linked CTA position's gain account must be an "
                        "income account."
                    ))
                if (
                    position.loss_account_id
                    and position.loss_account_id.account_type
                    not in {
                        'expense', 'expense_other',
                        'expense_depreciation', 'expense_direct_cost',
                    }
                ):
                    raise ValidationError(_(
                        "A linked CTA position's loss account must be an "
                        "expense account."
                    ))
            mapping_text = vals.get(
                'consolidation_account_map', member.consolidation_account_map,
            )
            member._eh_resolved_account_map(mapping_text=mapping_text)
        return self

    def _eh_lock_configuration(self, extra_entity_ids=(),
                               extra_company_ids=(),
                               extra_position_ids=()):
        expected_links = {
            member.id: (
                member.entity_id.id,
                member.company_id.id,
                member.cta_position_id.id,
            )
            for member in self
        }
        entities = self.mapped('entity_id') | self.env['eh.consol.entity'].browse(
            [entity_id for entity_id in extra_entity_ids if entity_id])
        entity_ids = tuple(sorted(entities.ids))
        if entity_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_consol_entity WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (entity_ids,))
            entities.invalidate_recordset()
        # Member company/CTA caches can predate the entity lock.  Writers use
        # this same entity-first order, so reload every child after the lock
        # before deciding which company and CTA rows form the protected
        # closure.
        entity_members = entities.mapped('member_ids')
        entity_members.invalidate_recordset([
            'entity_id', 'company_id', 'cta_position_id',
        ])
        company_ids = {
            int(company_id) for company_id in extra_company_ids if company_id
        }
        for entity in entities:
            company_ids.update(entity._eh_scope_companies().ids)
        company_ids = tuple(sorted(company_ids))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (company_ids,))
        # Canonical writer order is entity -> closure companies -> runs ->
        # member. A compute/review/close takes the same prefix, so a member
        # scope change cannot cross it and silently change the source graph.
        entities._eh_lock_scope_runs()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, entity_id, company_id, cta_position_id "
                "FROM eh_consol_member WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A consolidation member changed concurrently. Retry the "
                    "operation."
                ))
            if any(
                expected_links.get(member_id)
                != (
                    entity_id,
                    company_id,
                    cta_position_id or False,
                )
                for member_id, entity_id, company_id, cta_position_id
                in locked_rows
            ):
                raise UserError(_(
                    "A consolidation member changed entity, company, or CTA "
                    "position concurrently. Retry the operation."
                ))
            self.invalidate_recordset()
        position_ids = {
            int(position_id) for position_id in extra_position_ids
            if position_id
        }
        position_ids.update(self.mapped('cta_position_id').ids)
        if position_ids:
            self.env.cr.execute(
                "SELECT id FROM eh_fx_cta_position WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(sorted(position_ids)),),
            )
        return entities

    def _eh_restamp_owned_outputs(self):
        ids = tuple(sorted(self.ids))
        if not ids:
            return self
        self.env['eh.consol.entity'].flush_model([
            'presentation_currency_id',
        ])
        self.env.cr.execute(
            """
            UPDATE eh_consol_member AS member
               SET investment_currency_id = entity.presentation_currency_id
              FROM eh_consol_entity AS entity
             WHERE entity.id = member.entity_id
               AND member.id IN %s
            """,
            (ids,),
        )
        self.invalidate_recordset(['investment_currency_id'])
        return self

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            forged = set(vals) & _SERVER_OWNED_FIELDS
            if forged:
                raise AccessError(_(
                    "Member calculated fields are server-owned: %s",
                    ', '.join(sorted(forged)),
                ))
            manager_only = set(vals) & _MANAGER_ONLY_FIELDS
            if manager_only and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'
            ):
                raise AccessError(_(
                    "Only a consolidation manager may set member disposal "
                    "action inputs."
                ))
            self._eh_validate_finite_values(vals)
            entity = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
            if entity:
                entity._eh_assert_company_closure('write')
                entity._eh_assert_company_ids_allowed((vals.get('company_id'),))
                company = self.env['res.company'].browse(vals.get('company_id'))
                if company in (
                    entity.parent_company_id
                    | entity.consolidation_company_id
                ):
                    raise ValidationError(_(
                        "A consolidation member must be distinct from both "
                        "the parent and the dedicated consolidation ledger "
                        "company."
                    ))
        entity_ids = {vals.get('entity_id') for vals in vals_list}
        company_ids = {vals.get('company_id') for vals in vals_list}
        entities = self._eh_lock_configuration(
            extra_entity_ids=entity_ids,
            extra_company_ids=company_ids,
            extra_position_ids={
                vals.get('cta_position_id') for vals in vals_list
            },
        )
        entities._eh_assert_scope_mutable()
        entities._eh_assert_company_closure('write')
        for vals in vals_list:
            entity = self.env['eh.consol.entity'].browse(vals.get('entity_id'))
            entity._eh_assert_company_ids_allowed((vals.get('company_id'),))
            company = self.env['res.company'].browse(vals.get('company_id'))
            if company in (
                entity.parent_company_id | entity.consolidation_company_id
            ):
                raise ValidationError(_(
                    "A consolidation member must be distinct from both the "
                    "parent and the dedicated consolidation ledger company."
                ))
        members = super().create(vals_list)
        members._eh_restamp_owned_outputs()
        members._eh_invalidate_company_scope_cache()
        members._eh_validate_references()
        return members

    def write(self, vals):
        forged = set(vals) & _SERVER_OWNED_FIELDS
        if forged:
            raise AccessError(_(
                "Member calculated fields are server-owned: %s",
                ', '.join(sorted(forged)),
            ))
        if set(vals) & _MANAGER_ONLY_FIELDS and not self.env.user.has_group(
            'eh_account_base.group_eh_manager'
        ):
            raise AccessError(_(
                "Only a consolidation manager may change member disposal "
                "action inputs."
            ))
        if not self.env.su:
            self._eh_check_access('write')
        for member in self:
            self._eh_validate_finite_values(vals, current=member)
        self._eh_validate_references(vals)
        entities = self._eh_lock_configuration(
            extra_entity_ids=(vals.get('entity_id'),),
            extra_company_ids=(vals.get('company_id'),),
            extra_position_ids=(vals.get('cta_position_id'),),
        )
        if set(vals) & _EVIDENCE_FIELDS:
            entities._eh_assert_scope_mutable()
        for member in self:
            self._eh_validate_finite_values(vals, current=member)
        self._eh_validate_references(vals)
        result = super().write(vals)
        self._eh_restamp_owned_outputs()
        if set(vals) & _CLOSURE_FIELDS:
            self._eh_invalidate_company_scope_cache()
        self._eh_validate_references()
        return result

    def unlink(self):
        self._eh_validate_references()
        entities = self._eh_lock_configuration()
        entities._eh_assert_scope_mutable()
        self._eh_validate_references()
        result = super().unlink()
        self._eh_invalidate_company_scope_cache()
        return result

    @api.depends('company_id')
    def _compute_functional_currency(self):
        for member in self:
            if not member.functional_currency_id and member.company_id:
                member.functional_currency_id = (
                    member.company_id.currency_id
                )

    @api.depends('entity_id.presentation_currency_id')
    def _compute_investment_currency(self):
        for member in self:
            member.investment_currency_id = (
                member.entity_id.presentation_currency_id
            )

    @api.constrains('company_id', 'entity_id')
    def _check_not_parent_or_ledger(self):
        for rec in self:
            if (
                rec.entity_id
                and rec.company_id in (
                    rec.entity_id.parent_company_id
                    | rec.entity_id.consolidation_company_id
                )
            ):
                raise ValidationError(_(
                    "Member %s is the parent or dedicated consolidation "
                    "ledger company of entity %s. Both are excluded from "
                    "the source-member closure.",
                    rec.company_id.display_name,
                    rec.entity_id.display_name,
                ))

    @api.constrains('method', 'nci_basis', 'nci_account_id')
    def _check_proportional_no_nci(self):
        """NCI does not exist under the proportional method: only the
        parent's share of each balance rolls up, so there is no minority
        interest left in the consolidated set to carve. Configuring an NCI
        account or a fair-value NCI basis on a proportional member is
        therefore a contradiction and is blocked here (the run engine also
        never books NCI for proportional members)."""
        for rec in self:
            if rec.method != 'proportional':
                continue
            if rec.nci_account_id or rec.nci_basis == 'fair_value':
                raise ValidationError(_(
                    "Member %s uses the proportional method: only the "
                    "parent's share of each balance rolls up, so no "
                    "non-controlling interest exists. Remove the NCI "
                    "account / fair-value NCI basis, or switch the member "
                    "to full consolidation.",
                    rec.company_id.display_name,
                ))

    @api.constrains('nci_basis', 'nci_fair_value', 'method',
                    'ownership_pct', 'acquisition_equity')
    def _check_fair_value_nci_config(self):
        """The fair-value NCI basis (IFRS 3.19 full goodwill) needs an
        acquisition-date anchor: the FV of the minority at acquisition and
        the acquisition-date equity the post-acquisition movement is
        measured against. It is only meaningful on a full-method member
        with a genuine minority (ownership below 100%)."""
        for rec in self:
            if rec.nci_basis != 'fair_value':
                continue
            if rec.method != 'full':
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis applies to full "
                    "consolidation only.",
                    rec.company_id.display_name,
                ))
            if rec.ownership_pct >= 100.0:
                raise ValidationError(_(
                    "Member %s: a wholly-owned subsidiary has no "
                    "non-controlling interest, so the fair-value NCI basis "
                    "does not apply at 100%% ownership.",
                    rec.company_id.display_name,
                ))
            if not rec.nci_fair_value or rec.nci_fair_value <= 0.0:
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis requires a "
                    "positive acquisition-date NCI fair value.",
                    rec.company_id.display_name,
                ))
            if not rec.acquisition_equity or rec.acquisition_equity <= 0.0:
                raise ValidationError(_(
                    "Member %s: the fair-value NCI basis requires the "
                    "acquisition-date equity, so the post-acquisition "
                    "movement attributed to the minority can be measured.",
                    rec.company_id.display_name,
                ))

    @api.constrains('fv_option', 'method')
    def _check_fv_option_equity_only(self):
        for rec in self:
            if rec.fv_option and rec.method != 'equity':
                raise ValidationError(_(
                    "Member %s: the IAS 28.1A fair value option applies to "
                    "equity-method interests only.",
                    rec.company_id.display_name,
                ))

    @api.constrains('reporting_date_offset_months')
    def _check_offset_non_negative(self):
        for rec in self:
            if rec.reporting_date_offset_months < 0:
                raise ValidationError(_(
                    "The reporting date offset cannot be negative.",
                ))

    @api.constrains('historical_rate', 'acquisition_date')
    def _check_historical_rate(self):
        for rec in self:
            if rec.historical_rate < 0.0:
                raise ValidationError(_(
                    "The historical rate cannot be negative.",
                ))
            if rec.historical_rate > 0.0 and not rec.acquisition_date:
                raise ValidationError(_(
                    "A historical acquisition rate requires the acquisition "
                    "date that proves which IAS 21 rate the amount uses.",
                ))

    @api.constrains(*_FINITE_ACCOUNTING_FIELDS)
    def _check_finite_accounting_values(self):
        for member in self:
            self._eh_validate_finite_values({}, current=member)

    # ---- IAS 21.48 disposal recycling ----

    def action_dispose_member(self):
        """Apply IAS 21 CTA treatment for a member disposal.

        Manager-gated. Targets the entity's most recent computed / reviewed
        run and books a balanced pair of kind='cta_recycle' run lines:

          loss of control: entire remaining CTA -> profit or loss

          retained control: disposal_pct of remaining CTA -> NCI

          Dr CTA equity account   (removes the reserve slice)
          Cr FX gain or NCI       (accumulated gain, credit-negative CTA)

        or the mirrored pair through the FX loss/NCI account when the
        accumulated CTA is a loss (debit-positive). The remaining balance
        is the member's kind='cta' lines net of any prior recycle legs on
        the CTA account, so repeated partial disposals never recycle more
        than the accumulated reserve. Delegated to the run engine so the
        lines carry the engine context flag.
        """
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can dispose a member and "
                "recycle its translation reserve."
            ))
        with self.env.cr.savepoint():
            self.mapped('entity_id')._eh_assert_company_closure('write')
            self._eh_validate_references()
            self._eh_lock_configuration()
            self._eh_validate_references()
            for member in self.sorted('id'):
                member._eh_validate_references()
                member._eh_dispose_cta(
                    member.disposal_pct,
                    loses_control=member.disposal_loses_control,
                )
        return True

    def _eh_dispose_cta(self, pct, loses_control=False):
        self.ensure_one()
        self._eh_validate_finite_values({}, current=self)
        if not math.isfinite(float(pct or 0.0)):
            raise UserError(_("Disposal percentage must be finite."))
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only a consolidation manager can dispose a member and "
                "recycle its translation reserve.",
            ))
        if pct is None or pct <= 0.0 or pct > 100.0:
            raise UserError(_(
                "Disposal %% must be greater than 0 and at most 100. "
                "Got %.2f.",
            ) % (pct or 0.0))
        run = self.env['eh.consol.run'].search([
            ('entity_id', '=', self.entity_id.id),
            ('state', 'in', ('computed', 'reviewed')),
        ], order='period_to desc, id desc', limit=1)
        if not run:
            raise UserError(_(
                "No computed or reviewed consolidation run exists for "
                "entity %s; compute a run before recycling the member's "
                "CTA on disposal.",
                self.entity_id.display_name,
            ))
        run._eh_assert_company_closure()
        run._eh_lock_for_transition()
        return run._eh_recycle_member_cta(
            self, pct, loses_control=loses_control,
        )
