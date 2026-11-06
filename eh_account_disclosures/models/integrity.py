# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared integrity controls for disclosure headers and engine rows."""

import secrets

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import SQL


DISCLOSURE_ENGINE_CONTEXT = 'eh_disclosure_engine_capability'
DISCLOSURE_ENGINE_CAPABILITY = secrets.token_urlsafe(32)

_DISCLOSURE_SEQUENCES = {
    'eh.segment.report': (
        'EH Segment Report Reference', 'SEG/%(year)s/', 4),
    'eh.fin.maturity.run': (
        'EH Maturity Analysis Run Reference', 'MAT/%(year)s/', 4),
    'eh.fin.credit.note': (
        'EH Credit-Risk Note Reference', 'CRN/%(year)s/', 4),
    'eh.fin.sensitivity': (
        'EH Sensitivity Analysis Reference', 'SENS/%(year)s/', 4),
}


def _query_as_sql(query):
    """Return a framework search query as composable SQL on Odoo 16-19.

    Odoo 16 returns ``(code, params)`` from ``Query.select()`` while later
    series return an ``SQL`` object.  Keep that version split at this one
    boundary; disclosure extractors only consume suite-owned query objects.
    """
    selected = query.select()
    if isinstance(selected, SQL):
        return selected
    code, params = selected
    return SQL(code, *params)


def _access_query(model, domain):
    """Build a set-based, ACL and record-rule scoped ID subquery."""
    query = model._search(domain)
    # Odoo 16 may attach the model's default order.  Ordering is irrelevant
    # inside an access-membership subquery and can force avoidable sorts.
    query.order = None
    return _query_as_sql(query)


class _AccessScopedAggregateQuery:
    """Small SQL query builder shared by disclosure cutoff aggregates.

    Framework ``Query`` changed shape between 16 and 17/18 (``table``, SQL
    joins, and grouping are not portable).  This builder intentionally owns
    only the stable subset needed here: WHERE predicates, GROUP BY, and a
    final SELECT over a prebuilt FROM clause.
    """

    def __init__(self, table, from_clause):
        self.table_alias = table
        self.from_clause = from_clause
        self.where_clauses = []
        self.groupby = None

    def add_where(self, clause):
        self.where_clauses.append(clause)

    def select(self, *columns):
        where_clause = SQL(" AND ").join(self.where_clauses)
        return SQL(
            "SELECT %s FROM %s%s%s",
            SQL(", ").join(columns),
            self.from_clause,
            SQL(" WHERE %s", where_clause) if where_clause else SQL(),
            SQL(" GROUP BY %s", self.groupby) if self.groupby else SQL(),
        )


def execute_query_dict(env, query):
    """Execute composable SQL and return dictionaries on Odoo 16-19."""
    # Odoo 19 moved search-query flushing into SQL metadata consumption.
    # Odoo 16/17 flush while building `_search`; Odoo 18/19 expose this
    # helper.  Preserve either contract before using the cross-series cursor
    # execution path.
    flush_query = getattr(env, 'flush_query', None)
    if flush_query:
        flush_query(query)
    env.cr.execute(query.code, query.params)
    return env.cr.dictfetchall()


def cutoff_residuals(move_lines, cutoff_date):
    """Return company/foreign residuals reconstructed at ``cutoff_date``.

    ``amount_residual`` and ``reconciled`` describe the record *now*.  They
    therefore cannot support an as-at disclosure: a payment dated after the
    reporting date may have fully reconciled an invoice that was still open at
    the cutoff.  Odoo's own aged-balance convention dates a partial
    reconciliation by ``max_date`` (the later accounting date of its two
    matched lines).  Rebuild the two residuals from the original move-line
    amounts and only the partials whose max date is on or before the cutoff.

    The helper deliberately uses the caller's ORM environment and never sudo:
    the source move lines have already passed their model/company rules, and a
    disclosure action must not turn a guessed line id into an accounting-data
    oracle.
    """
    result = {}
    if not move_lines:
        return result
    move_lines._eh_check_access('read')
    line_ids = set(move_lines.ids)
    for line in move_lines:
        result[line.id] = [line.balance, line.amount_currency]
    partials = move_lines.env['account.partial.reconcile'].search([
        ('max_date', '<=', cutoff_date),
        '|',
        ('debit_move_id', 'in', sorted(line_ids)),
        ('credit_move_id', 'in', sorted(line_ids)),
    ])
    for partial in partials:
        debit_id = partial.debit_move_id.id
        if debit_id in line_ids:
            result[debit_id][0] -= partial.amount
            result[debit_id][1] -= partial.debit_amount_currency
        credit_id = partial.credit_move_id.id
        if credit_id in line_ids:
            result[credit_id][0] += partial.amount
            result[credit_id][1] += partial.credit_amount_currency
    for line in move_lines:
        company_currency = line.company_id.currency_id
        foreign_currency = line.currency_id or company_currency
        result[line.id] = (
            company_currency.round(result[line.id][0]),
            foreign_currency.round(result[line.id][1]),
        )
    return result


def cutoff_residual_query(move_line_model, domain, cutoff_date):
    """Return an access-scoped AML query and its two as-at residual SQLs.

    This is the set-based counterpart of :func:`cutoff_residuals`.  Disclosure
    extracts that only need an aggregate must not instantiate every historical
    receivable/payable in the ORM cache or put every line id in one PostgreSQL
    ``IN`` list.  ``_search`` retains account.move.line ACLs and record rules;
    one lateral aggregate reconstructs both residual currencies for each
    readable line without exposing a reconciliation counterpart.

    The caller owns the final projection/grouping and executes the returned
    suite query.  Both source tables are joined to readable-ID subqueries
    built by ``_search``.  This retains ACLs and record rules without relying
    on framework ``Query`` internals that differ across Odoo 16-19.  The
    partial-reconcile access check mirrors ``cutoff_residuals``.
    """
    Partial = move_line_model.env['account.partial.reconcile']
    move_line_model.flush_model([
        'balance', 'amount_currency', 'date', 'date_maturity',
        'account_id', 'currency_id',
    ])
    Partial.flush_model([
        'debit_move_id', 'credit_move_id', 'amount',
        'debit_amount_currency', 'credit_amount_currency', 'max_date',
    ])

    aml_alias = 'eh_cutoff_aml'
    aml_access_alias = 'eh_cutoff_aml_access'
    apr_alias = 'eh_cutoff_apr'
    apr_access_alias = 'eh_cutoff_apr_access'
    partial_alias = 'eh_cutoff_partial'

    aml_access = _access_query(move_line_model, domain)
    partial_access = _access_query(
        Partial, [('max_date', '<=', cutoff_date)],
    )
    aml_id = SQL.identifier(aml_alias, 'id')
    apr_debit = SQL.identifier(apr_alias, 'debit_move_id')
    apr_credit = SQL.identifier(apr_alias, 'credit_move_id')
    company_delta = SQL(
        "COALESCE(SUM(CASE WHEN %s = %s "
        "THEN -%s ELSE %s END), 0) AS company_delta",
        apr_debit, aml_id,
        SQL.identifier(apr_alias, 'amount'),
        SQL.identifier(apr_alias, 'amount'),
    )
    foreign_delta = SQL(
        "COALESCE(SUM(CASE WHEN %s = %s "
        "THEN -%s ELSE %s END), 0) AS foreign_delta",
        apr_debit, aml_id,
        SQL.identifier(apr_alias, 'debit_amount_currency'),
        SQL.identifier(apr_alias, 'credit_amount_currency'),
    )
    from_clause = SQL(
        "%s AS %s "
        "JOIN (%s) AS %s ON %s = %s "
        "LEFT JOIN LATERAL ("
        "SELECT %s, %s "
        "FROM %s AS %s "
        "JOIN (%s) AS %s ON %s = %s "
        "WHERE (%s = %s OR %s = %s)"
        ") AS %s ON TRUE",
        SQL.identifier(move_line_model._table), SQL.identifier(aml_alias),
        aml_access, SQL.identifier(aml_access_alias),
        SQL.identifier(aml_access_alias, 'id'), aml_id,
        company_delta, foreign_delta,
        SQL.identifier(Partial._table), SQL.identifier(apr_alias),
        partial_access, SQL.identifier(apr_access_alias),
        SQL.identifier(apr_access_alias, 'id'),
        SQL.identifier(apr_alias, 'id'),
        apr_debit, aml_id, apr_credit, aml_id,
        SQL.identifier(partial_alias),
    )
    query = _AccessScopedAggregateQuery(aml_alias, from_clause)
    company_residual = SQL(
        "%s + COALESCE(%s, 0)",
        SQL.identifier(aml_alias, 'balance'),
        SQL.identifier(partial_alias, 'company_delta'),
    )
    foreign_residual = SQL(
        "%s + COALESCE(%s, 0)",
        SQL.identifier(aml_alias, 'amount_currency'),
        SQL.identifier(partial_alias, 'foreign_delta'),
    )
    return query, company_residual, foreign_residual


class EhDisclosureParentGuard(models.AbstractModel):
    _name = 'eh.disclosure.parent.guard'
    _description = "Disclosure header integrity guard"

    # Header values derived by a disclosure engine must not be writable over
    # RPC, but the engine must still run in the caller's non-sudo environment
    # so its accounting searches retain ACLs and record rules.  The process-
    # random capability installed by ``_eh_disclosure_prepare_action`` proves
    # that such a write came from a sanctioned server action.  Concrete
    # headers list their engine-owned outputs here and keep only their state
    # machine in ``eh.workflow.guard`` (whose sanctioned transition is sudo).
    _eh_disclosure_server_fields = frozenset()

    def _eh_disclosure_is_engine(self):
        return (
            self.env.context.get(DISCLOSURE_ENGINE_CONTEXT)
            == DISCLOSURE_ENGINE_CAPABILITY
        )

    def _eh_disclosure_assert_server_vals(self, vals):
        if (
            not self.env.su
            and not self._eh_disclosure_is_engine()
            and self._eh_disclosure_server_fields.intersection(vals)
        ):
            raise AccessError(_(
                "Disclosure company anchors, computed outputs, and engine "
                "evidence are server-owned."
            ))

    include_branch_companies = fields.Boolean(
        string="Include Branch Companies",
        help=(
            "Include this company's readable descendants in ledger-backed "
            "disclosure extracts. Disabled keeps the source strictly scoped "
            "to the disclosure company."
        ),
    )

    def _eh_disclosure_company_ids(self):
        """Return the explicitly authorised company scope for this header."""
        self.ensure_one()
        if not self.include_branch_companies:
            return [self.company_id.id]
        all_ids = set(self.env['res.company'].sudo().search([
            ('id', 'child_of', self.company_id.id),
        ]).ids)
        readable_ids = set(self.env['res.company'].search([
            ('id', 'in', sorted(all_ids)),
        ]).ids)
        if readable_ids != all_ids:
            raise AccessError(_(
                "Branch-company extraction is unavailable because the "
                "current user cannot read every company in the selected "
                "branch hierarchy."
            ))
        return sorted(all_ids)

    @api.model
    def _eh_disclosure_next_sequence(self, code, company):
        """Return a number from an exact-company sequence, creating it once."""
        if company.id not in self.env.companies.ids and not self.env.su:
            raise AccessError(_(
                "A disclosure sequence can only be used for an enabled "
                "company."
            ))
        definition = _DISCLOSURE_SEQUENCES.get(code)
        if not definition:
            raise UserError(_("Unknown disclosure sequence code: %s") % code)
        company_lock = SQL(
            "SELECT %s FROM %s WHERE %s = %s FOR UPDATE",
            SQL.identifier('id'),
            SQL.identifier('res_company'),
            SQL.identifier('id'),
            company.id,
        )
        self.env.cr.execute(company_lock.code, company_lock.params)
        Sequence = self.env['ir.sequence'].sudo().with_company(company)
        sequence = Sequence.search([
            ('code', '=', code),
            ('company_id', '=', company.id),
        ], limit=1)
        if not sequence:
            label, prefix, padding = definition
            sequence = Sequence.create({
                'name': '%s - %s' % (label, company.display_name),
                'code': code,
                'prefix': prefix,
                'padding': padding,
                'company_id': company.id,
            })
        return sequence.next_by_id()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._eh_disclosure_assert_server_vals(vals)
        if not self.env.su:
            for vals in vals_list:
                company_id = vals.get('company_id') or self.env.company.id
                if company_id not in self.env.companies.ids:
                    raise AccessError(_(
                        "A disclosure can only be created in a company "
                        "enabled in the caller's current company context."
                    ))
        return super().create(vals_list)

    def write(self, vals):
        self._eh_disclosure_assert_server_vals(vals)
        if (
            not self.env.su
            and vals.get('company_id')
            and vals['company_id'] not in self.env.companies.ids
        ):
            raise AccessError(_(
                "A disclosure cannot be moved to a company outside the "
                "caller's current company context."
            ))
        # Take the same header lock used by finalisation, then re-read state.
        # Model-specific guards run before this mixin in the MRO; without this
        # second post-lock check, an edit that observed ``draft`` could wait
        # behind finalisation and then commit into the newly-finalised note.
        self._eh_disclosure_lock()
        if (
            set(vals) - {'state'}
            and 'state' in self._fields
            and self.filtered(lambda record: record.state == 'finalised')
        ):
            raise UserError(_(
                "A finalised disclosure is immutable. Reopen it before "
                "changing its signed-off inputs or evidence."
            ))
        return super().write(vals)

    def unlink(self):
        self._eh_disclosure_lock()
        if (
            'state' in self._fields
            and self.filtered(lambda record: record.state == 'finalised')
        ):
            raise UserError(_(
                "A finalised disclosure cannot be deleted. Reopen it first."
            ))
        return super().unlink()

    def _eh_disclosure_lock(self):
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            lock_query = SQL(
                "SELECT %s FROM %s WHERE %s IN %s ORDER BY %s FOR UPDATE",
                SQL.identifier('id'), SQL.identifier(self._table),
                SQL.identifier('id'), tuple(ids), SQL.identifier('id'),
            )
            self.env.cr.execute(lock_query.code, lock_query.params)
            self.invalidate_recordset()
            if hasattr(self, '_check_company'):
                self._check_company()
        return self

    def _eh_disclosure_prepare_action(self, manager=True, engine=False):
        if (
            manager
            and not self.env.user.has_group(
                'eh_account_base.group_eh_manager')
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager can run this disclosure "
                "action."
            ))
        records = self._eh_disclosure_lock()
        if engine:
            records = records.with_context(**{
                DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY,
            })
        return records


class EhDisclosureChildGuard(models.AbstractModel):
    _name = 'eh.disclosure.child.guard'
    _description = "Disclosure detail integrity guard"

    _eh_disclosure_parent_field = None
    _eh_disclosure_server_fields = frozenset()
    _eh_disclosure_origin_field = 'origin'
    _eh_disclosure_engine_origins = frozenset()
    _eh_disclosure_engine_owned = False

    def _eh_disclosure_is_engine(self):
        return (
            self.env.context.get(DISCLOSURE_ENGINE_CONTEXT)
            == DISCLOSURE_ENGINE_CAPABILITY
        )

    def _eh_disclosure_parents_from_ids(self, parent_ids):
        parent_field = self._fields[self._eh_disclosure_parent_field]
        parents = self.env[parent_field.comodel_name].browse(
            sorted({parent_id for parent_id in parent_ids if parent_id})
        )
        if parents and not self.env.su:
            parents._eh_check_access('write')
        if parents:
            parents.flush_recordset()
            lock_query = SQL(
                "SELECT %s FROM %s WHERE %s IN %s ORDER BY %s FOR UPDATE",
                SQL.identifier('id'), SQL.identifier(parents._table),
                SQL.identifier('id'), tuple(parents.ids),
                SQL.identifier('id'),
            )
            self.env.cr.execute(lock_query.code, lock_query.params)
            parents.invalidate_recordset()
        return parents

    def _eh_disclosure_assert_draft_parents(self, parents):
        finalised = parents.filtered(
            lambda parent: parent.state == 'finalised'
        )
        if finalised:
            raise UserError(_(
                "Detail rows on a finalised disclosure are immutable. "
                "Reopen the disclosure before changing its evidence."
            ))

    def _eh_disclosure_vals_are_engine_owned(self, vals):
        if self._eh_disclosure_engine_owned:
            return True
        origin_field = self._eh_disclosure_origin_field
        return bool(
            origin_field
            and origin_field in self._fields
            and vals.get(origin_field) in self._eh_disclosure_engine_origins
        )

    def _eh_disclosure_records_are_engine_owned(self):
        if self._eh_disclosure_engine_owned:
            return bool(self)
        origin_field = self._eh_disclosure_origin_field
        return bool(
            origin_field
            and origin_field in self._fields
            and any(
                record[origin_field] in self._eh_disclosure_engine_origins
                for record in self
            )
        )

    @api.model_create_multi
    def create(self, vals_list):
        engine = self._eh_disclosure_is_engine()
        if not engine:
            if any(
                self._eh_disclosure_server_fields.intersection(vals)
                for vals in vals_list
            ):
                raise AccessError(_(
                    "Disclosure company anchors, computed outputs, and "
                    "engine evidence are server-owned."
                ))
            if any(
                self._eh_disclosure_vals_are_engine_owned(vals)
                for vals in vals_list
            ):
                raise AccessError(_(
                    "Engine-generated disclosure evidence cannot be created "
                    "through a direct model call."
                ))
        parents = self._eh_disclosure_parents_from_ids([
            vals.get(self._eh_disclosure_parent_field)
            for vals in vals_list
        ])
        self._eh_disclosure_assert_draft_parents(parents)
        return super().create(vals_list)

    def write(self, vals):
        engine = self._eh_disclosure_is_engine()
        if not self.env.su:
            self._eh_check_access('write')
        parent_field = self._eh_disclosure_parent_field
        current_parents = self.mapped(parent_field)
        target_id = vals.get(parent_field)
        parent_ids = current_parents.ids + ([target_id] if target_id else [])
        parents = self._eh_disclosure_parents_from_ids(parent_ids)
        self._eh_disclosure_assert_draft_parents(parents)
        if not engine:
            if self._eh_disclosure_server_fields.intersection(vals):
                raise AccessError(_(
                    "Disclosure company anchors, computed outputs, and "
                    "engine evidence are server-owned."
                ))
            if (
                self._eh_disclosure_records_are_engine_owned()
                or self._eh_disclosure_vals_are_engine_owned(vals)
            ):
                raise AccessError(_(
                    "Engine-generated disclosure evidence is immutable."
                ))
        return super().write(vals)

    def unlink(self):
        engine = self._eh_disclosure_is_engine()
        if not self.env.su:
            self._eh_check_access('unlink')
        parents = self._eh_disclosure_parents_from_ids(
            self.mapped(self._eh_disclosure_parent_field).ids
        )
        self._eh_disclosure_assert_draft_parents(parents)
        if not engine and self._eh_disclosure_records_are_engine_owned():
            raise AccessError(_(
                "Engine-generated disclosure evidence is immutable."
            ))
        return super().unlink()
