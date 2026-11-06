# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.aoci.reserve.map: per-company AOCI sub-reserve mapping (IAS 1.106).

Other comprehensive income accumulates in equity, but IAS 1.106 requires the
equity roll-forward to show each component of accumulated OCI separately:
the foreign currency translation reserve (IAS 21), the revaluation surplus
(IAS 16 / IAS 38), FVOCI debt and FVOCI equity reserves (IFRS 9), and defined
benefit remeasurements (IAS 19) each keep their own identity, because their
recycling behaviour differs (CTA and FVOCI-debt recycle to P&L; revaluation
surplus, FVOCI-equity and DB remeasurements never do).

One mapping row per company per component kind:

* ``source_account_ids``: the OCI *flow* accounts other suite modules post
  into during the year (CTA positions, revaluation wizards, fair value
  remeasurements, benefit valuations, ...).
* ``reserve_account_id``: the dedicated AOCI sub-reserve equity account that
  carries the accumulated balance of this component.

At year-end close, the closing entry reclassifies each mapped flow account's
NET posted movement of the fiscal year into its sub-reserve, so equity
carries per-component AOCI instead of commingled balances. Only the net
period movement moves: an amount recycled to P&L during the year (e.g. a CTA
disposal reclassification under IAS 21.48) has already left the flow account,
so the close never double-moves it.

A row without a reserve account is *incomplete*: its source accounts are
reported on the year-end run's unmapped-OCI warning list and block posting
unless a manager overrides with a documented reason.

``action_seed_from_modules`` soft-discovers known OCI flow accounts from the
installed suite modules (same soft-lookup approach the statement modules use:
probe ``'model' in self.env``, skip when absent) and seeds mapping rows for
the user to complete.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

# Kinds of accumulated-OCI sub-reserve (IAS 1.106 components).
AOCI_KINDS = [
    ('cta', "Foreign Currency Translation (CTA)"),
    ('revaluation_surplus', "Revaluation Surplus"),
    ('fvoci_debt', "FVOCI Debt Reserve"),
    ('fvoci_equity', "FVOCI Equity Reserve"),
    ('db_remeasurement', "Defined Benefit Remeasurement"),
    ('other', "Other OCI Reserve"),
]

_EQUITY_TYPES = ('equity', 'equity_unaffected')


class EhAociReserveMap(models.Model):
    _name = 'eh.aoci.reserve.map'
    _description = "AOCI sub-reserve mapping"
    _order = 'company_id, kind, id'
    _rec_name = 'kind'

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    kind = fields.Selection(
        AOCI_KINDS, required=True, default='other',
        help="Which accumulated-OCI component this row carries "
             "(IAS 1.106).",
    )
    reserve_account_id = fields.Many2one(
        'account.account', string="AOCI Sub-Reserve Account",
        domain=(
            "[('account_type', '=', 'equity'),"
            " ('company_ids', 'parent_of', company_id)]"
        ),
        check_company=True,
        help="Equity account carrying the accumulated balance of this OCI "
             "component. The year-end close reclassifies each source "
             "account's net period movement here. While empty the row is "
             "incomplete: its source accounts appear on the year-end run's "
             "unmapped-OCI warning list.",
    )
    source_account_ids = fields.Many2many(
        'account.account', 'eh_aoci_map_source_rel',
        'map_id', 'account_id', string="OCI Flow Accounts",
        domain=(
            "[('account_type', 'in', ('equity', 'equity_unaffected')) ,"
            " ('company_ids', 'parent_of', company_id)]"
        ),
        check_company=True,
        help="The OCI flow accounts the suite modules post this component's "
             "gains and losses into during the year. Their net period "
             "movement is swept into the sub-reserve account at year-end.",
    )
    is_complete = fields.Boolean(
        compute='_compute_is_complete',
        help="True when the row carries both a sub-reserve account and at "
             "least one source account, so the year-end close can "
             "reclassify it.",
    )

    _unique_company_kind = models.Constraint(
        'unique(company_id, kind)',
        'One AOCI sub-reserve mapping per company per component kind.',
    )

    @api.depends('reserve_account_id', 'source_account_ids')
    def _compute_is_complete(self):
        for rec in self:
            rec.is_complete = bool(
                rec.reserve_account_id and rec.source_account_ids)

    @api.depends('kind')
    def _compute_display_name(self):
        labels = dict(AOCI_KINDS)
        for rec in self:
            rec.display_name = labels.get(rec.kind, rec.kind or '')

    @api.model
    def _eh_source_account_ids(self, commands, current=None):
        ids = set(current.source_account_ids.ids) if current else set()
        for command in commands or ():
            if command[0] == 6:
                ids = set(command[2])
            elif command[0] == 4:
                ids.add(command[1])
            elif command[0] in (2, 3):
                ids.discard(command[1])
            elif command[0] == 5:
                ids.clear()
        return ids

    @api.model
    def _eh_validate_values(self, vals, current=None):
        if current and not self.env.su:
            current._eh_check_access('write')
        company_id = vals.get(
            'company_id', current.company_id.id if current else self.env.company.id)
        if not self.env.su and company_id not in self.env.companies.ids:
            raise AccessError(_(
                "An AOCI mapping may only use a company enabled in the "
                "caller's current company context."
            ))
        reserve = self.env['account.account'].browse(vals.get(
            'reserve_account_id',
            current.reserve_account_id.id if current else False))
        source_ids = self._eh_source_account_ids(
            vals.get('source_account_ids'), current=current)
        accounts = reserve | self.env['account.account'].browse(source_ids)
        if accounts and not self.env.su:
            accounts._eh_check_access('read')
        company = self.env['res.company'].browse(company_id)
        foreign = accounts.filtered(
            lambda account: not self._account_matches_company(
                account, company,
            )
        )
        if foreign:
            raise ValidationError(_(
                "Every AOCI reserve/source account must belong to the mapping "
                "company."
            ))
        return company_id

    def _eh_lock_for_update(self, extra_company_ids=()):
        company_ids = tuple(sorted(
            set(self.mapped('company_id').ids) |
            {int(company_id) for company_id in extra_company_ids if company_id}
        ))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (company_ids,))
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_aoci_reserve_map WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            self.invalidate_recordset()
        return self

    @api.model_create_multi
    def create(self, vals_list):
        company_ids = {
            self._eh_validate_values(vals) for vals in vals_list}
        self._eh_lock_for_update(extra_company_ids=company_ids)
        for vals in vals_list:
            self._eh_validate_values(vals)
        return super().create(vals_list)

    def write(self, vals):
        company_ids = {
            self._eh_validate_values(vals, current=record)
            for record in self
        }
        self._eh_lock_for_update(extra_company_ids=company_ids)
        for record in self:
            self._eh_validate_values(vals, current=record)
        return super().write(vals)

    def unlink(self):
        for record in self:
            self._eh_validate_values({}, current=record)
        self._eh_lock_for_update()
        for record in self:
            self._eh_validate_values({}, current=record)
        return super().unlink()

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    @staticmethod
    def _account_companies(account):
        """Companies an account belongs to, across the 18+ multi-company
        (company_ids) and pre-18 single-company (company_id) schemas."""
        if 'company_ids' in account._fields:
            return account.company_ids
        return account.company_id

    @api.model
    def _account_matches_company(self, account, company):
        """Apply the chart model's branch-aware company contract."""
        checker = getattr(account, '_check_company_domain', None)
        if checker:
            try:
                return bool(account.filtered_domain(checker(company)))
            except (AttributeError, TypeError):
                # Odoo 16 has no hierarchy-aware account-company contract.
                pass
        return company in self._account_companies(account)

    @api.constrains('reserve_account_id', 'source_account_ids', 'company_id')
    def _check_accounts(self):
        for rec in self:
            if rec.reserve_account_id:
                if rec.reserve_account_id.account_type != 'equity':
                    raise ValidationError(_(
                        "The AOCI sub-reserve account must be an equity "
                        "account; accumulated OCI is a component of equity "
                        "(IAS 1.106)."))
                if not rec._account_matches_company(
                        rec.reserve_account_id, rec.company_id):
                    raise ValidationError(_(
                        "The AOCI sub-reserve account must belong to the "
                        "mapping's company."))
                if rec.reserve_account_id in rec.source_account_ids:
                    raise ValidationError(_(
                        "The AOCI sub-reserve account cannot also be one of "
                        "its own source accounts; the close would "
                        "reclassify the reserve into itself."))
            for account in rec.source_account_ids:
                if account.account_type not in _EQUITY_TYPES:
                    raise ValidationError(_(
                        "OCI flow account %(code)s %(name)s must be an "
                        "equity account: OCI accumulates in equity, never "
                        "in profit or loss.",
                        code=account.code or '', name=account.name))
                if not rec._account_matches_company(account, rec.company_id):
                    raise ValidationError(_(
                        "OCI flow account %(code)s %(name)s must belong to "
                        "the mapping's company.",
                        code=account.code or '', name=account.name))

    @api.constrains('source_account_ids', 'company_id')
    def _check_sources_unique_per_company(self):
        """A flow account may feed exactly one sub-reserve: the same source
        on two rows of one company would double-move its balance at close."""
        for rec in self:
            if not rec.source_account_ids:
                continue
            domain = [('company_id', '=', rec.company_id.id)]
            # 17 passes NewId objects into SQL domains; only exclude self
            # in the domain when the id is a real database id, otherwise
            # subtract the record from the result set instead.
            if isinstance(rec.id, int):
                domain.append(('id', '!=', rec.id))
            siblings = self.search(domain) - rec
            overlap = rec.source_account_ids & siblings.mapped(
                'source_account_ids')
            if overlap:
                raise ValidationError(_(
                    "OCI flow account(s) %(codes)s are already mapped to "
                    "another AOCI sub-reserve of this company. A source "
                    "account may feed exactly one sub-reserve, otherwise "
                    "the close would move its balance twice.",
                    codes=', '.join(
                        a.code or a.name for a in overlap)))

    # ------------------------------------------------------------------
    # discovery (soft lookups across installed suite modules)
    # ------------------------------------------------------------------
    @api.model
    def _discover_oci_sources(self, company):
        """Known OCI flow accounts per kind, from installed suite modules.

        Soft lookups only (same approach as the statements modules): each
        probe checks the model and field exist in the registry before
        reading, so any subset of the suite may be installed. Returns a
        dict mapping every kind in ``AOCI_KINDS`` to an account.account
        recordset (possibly empty).
        """
        Account = self.env['account.account']
        found = {kind: Account.browse() for kind, _label in AOCI_KINDS}

        def probe(model_name, field_name, kind, extra=None):
            if model_name not in self.env:
                return
            Model = self.env[model_name]
            if field_name not in Model._fields:
                return
            domain = list(extra or [])
            if 'company_id' in Model._fields:
                domain.append(('company_id', '=', company.id))
            if not self.env.su:
                Model._eh_check_access('read')
                visible = Model.search(domain)
                records = Model.sudo().search(domain)
                if set(visible.ids) != set(records.ids):
                    raise AccessError(_(
                        "An OCI source model contains rows the caller is not "
                        "authorised to read."
                    ))
            else:
                records = Model.sudo().search(domain)
            accounts = records.mapped(field_name).filtered(
                lambda a: a and self._account_matches_company(a, company))
            if accounts and not self.env.su:
                accounts.sudo(False)._eh_check_access('read')
            found[kind] |= accounts

        # IAS 21: CTA reserve positions and consolidation entities.
        probe('eh.fx.cta.position', 'cta_account_id', 'cta')
        probe('eh.consol.entity', 'cta_account_id', 'cta')
        # IAS 16 / IAS 40: revaluation surplus carried on investment
        # property configuration (the assets module takes its reserve
        # account as a wizard parameter, so it has no stored account to
        # discover).
        probe('eh.investment.property', 'revaluation_surplus_account_id',
              'revaluation_surplus')
        # IFRS 9: fair value items routed to OCI, split by the derived
        # classification; unclassified legacy items fall to 'other'.
        probe('eh.fair.value.item', 'oci_account_id', 'fvoci_debt',
              extra=[('ifrs9_classification', '=', 'fvoci_debt')])
        probe('eh.fair.value.item', 'oci_account_id', 'fvoci_equity',
              extra=[('ifrs9_classification', '=', 'fvoci_equity')])
        probe('eh.fair.value.item', 'oci_account_id', 'other',
              extra=[('ifrs9_classification', '=', False),
                     ('routing', '=', 'oci')])
        # IAS 19: defined benefit remeasurement OCI accounts.
        probe('eh.benefit.plan', 'oci_account_id', 'db_remeasurement')
        # IFRS 9 hedge accounting: cash flow hedge reserve.
        probe('eh.fx.hedge', 'oci_account_id', 'other')

        # An account claimed by a specific kind must not also surface under
        # 'other' (e.g. an account reused across configurations).
        specific = Account.browse()
        for kind, _label in AOCI_KINDS:
            if kind != 'other':
                specific |= found[kind]
        found['other'] -= specific
        return found

    @api.model
    def action_seed_from_modules(self, company=None):
        """Seed mapping rows from the installed suite modules' known OCI
        flow accounts.

        Idempotent: accounts already claimed by any row of the company are
        never re-added, existing rows keep their reserve account, and a
        kind with no discovered accounts creates nothing. The seeded rows
        carry sources only; the user completes them by choosing the AOCI
        sub-reserve account per component.
        """
        company = company or self.env.company
        if not self.env.su and company not in self.env.companies:
            raise AccessError(_(
                "You cannot seed AOCI mappings outside your companies."
            ))
        if not self.env.su:
            self._eh_check_access('create')
        self._eh_lock_for_update(extra_company_ids=(company.id,))
        discovered = self._discover_oci_sources(company)
        existing = self.search([('company_id', '=', company.id)])
        claimed = existing.mapped('source_account_ids')
        touched = self.browse()
        for kind, _label in AOCI_KINDS:
            accounts = discovered.get(kind)
            if not accounts:
                continue
            new_accounts = accounts - claimed
            if not new_accounts:
                continue
            row = existing.filtered(lambda r: r.kind == kind)[:1]
            if row:
                row.write({'source_account_ids': [
                    (4, account.id) for account in new_accounts]})
            else:
                row = self.create({
                    'company_id': company.id,
                    'kind': kind,
                    'source_account_ids': [(6, 0, new_accounts.ids)],
                })
                existing |= row
            claimed |= new_accounts
            touched |= row
        return touched
