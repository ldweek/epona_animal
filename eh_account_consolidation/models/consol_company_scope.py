# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Request-scoped company-closure search support for consolidation models."""

from odoo import _, api, fields, models, release
from odoo.exceptions import UserError


# These fragments are server-owned and keyed only by the current model name.
# Each query resolves the secured row to its consolidation entity with INNER
# JOINs. Orphaned graphs therefore never enter the explicit allow-list.
_SCOPE_TARGET_SQL = {
    'eh.consol.entity': (
        'eh_consol_entity AS target',
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = target.id',
    ),
    'eh.consol.member': (
        'eh_consol_member AS target',
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = target.entity_id',
    ),
    'eh.consol.run': (
        'eh_consol_run AS target',
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = target.entity_id',
    ),
    'eh.consol.run.line': (
        'eh_consol_run_line AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.elimination': (
        'eh_consol_elimination AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.elimination.line': (
        'eh_consol_elimination_line AS target',
        'JOIN eh_consol_elimination AS scope_elimination '
        'ON scope_elimination.id = target.elimination_id '
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = scope_elimination.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.unrealised.profit': (
        'eh_consol_unrealised_profit AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.acquisition.equity.component': (
        'eh_consol_acquisition_equity_component AS target',
        'JOIN eh_consol_member AS scope_member_target '
        'ON scope_member_target.id = target.member_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_member_target.entity_id',
    ),
    'eh.consol.equity.rollforward': (
        'eh_consol_equity_rollforward AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.disposal.event': (
        'eh_consol_disposal_event AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
    'eh.consol.trial.balance': (
        'eh_consol_trial_balance AS target',
        'JOIN eh_consol_run AS scope_run '
        'ON scope_run.id = target.run_id '
        'JOIN eh_consol_entity AS scope_entity '
        'ON scope_entity.id = scope_run.entity_id',
    ),
}


class EhConsolCompanyScopeMixin(models.AbstractModel):
    _name = 'eh.consol.company.scope.mixin'
    _description = "Consolidation company-closure search support"

    eh_company_scope_allowed = fields.Boolean(
        compute='_compute_eh_company_scope_allowed',
        search='_search_eh_company_scope_allowed',
        compute_sudo=False,
        help=(
            "Technical request-scoped flag. It is true only when the "
            "consolidation parent, dedicated ledger, and every member "
            "company are enabled in the caller's current company context."
        ),
    )

    @api.model
    def _eh_invalidate_company_scope_cache(self):
        """Drop every concrete closure flag after a graph mutation.

        On Odoo 16--18 record-rule evaluation can use ``filtered_domain`` on
        an already-browsed recordset.  Invalidating only the entity/member that
        changed would leave a cached True on its existing run/line/elimination
        descendants.  Model-wide invalidation is deliberately conservative and
        avoids relation traversal through the very rules being invalidated.
        """
        for model_name in _SCOPE_TARGET_SQL:
            self.env[model_name].invalidate_model([
                'eh_company_scope_allowed',
            ])

    @api.model
    def _eh_company_scope_subquery(self, record_ids=None):
        """Build the fixed SQL allow-list subquery and its bound parameters.

        Record rules cannot express ``NOT EXISTS`` portably on Odoo 16:
        negative dotted traversal through a one2many is existential there.
        Resolve the universal company-closure predicate directly instead.
        No ORM relation is read, so applying this from a record rule cannot
        recurse into the same rule or silently hide a forbidden member.
        """
        target_sql = _SCOPE_TARGET_SQL.get(self._name)
        if not target_sql:
            raise UserError(_(
                "Company-closure security is not configured for model %s.",
                self._name,
            ))
        allowed_ids = list(self.env.companies.ids)
        from_sql, join_sql = target_sql
        query = """
            SELECT target.id
              FROM %s
              %s
             WHERE scope_entity.id IS NOT NULL
               AND scope_entity.parent_company_id IS NOT NULL
               AND scope_entity.parent_company_id = ANY(%%s::integer[])
               AND (
                      scope_entity.consolidation_company_id IS NULL
                   OR scope_entity.consolidation_company_id
                      = ANY(%%s::integer[])
                   )
               AND NOT EXISTS (
                        SELECT 1
                          FROM eh_consol_member AS scope_member
                         WHERE scope_member.entity_id = scope_entity.id
                           AND (
                                  scope_member.company_id IS NULL
                               OR NOT (
                                      scope_member.company_id
                                      = ANY(%%s::integer[])
                                  )
                               )
                    )
               AND NOT EXISTS (
                        WITH RECURSIVE scope_company(id, path) AS (
                            SELECT root.id, ARRAY[root.id]
                              FROM res_company AS root
                             WHERE root.id = scope_entity.parent_company_id
                                OR root.id = scope_entity.consolidation_company_id
                                OR EXISTS (
                                    SELECT 1
                                      FROM eh_consol_member AS root_member
                                     WHERE root_member.entity_id = scope_entity.id
                                       AND root_member.company_id = root.id
                                )
                            UNION ALL
                            SELECT child.id, branch.path || child.id
                              FROM scope_company AS branch
                              JOIN res_company AS child
                                ON child.parent_id = branch.id
                             WHERE NOT child.id = ANY(branch.path)
                        )
                        SELECT 1
                          FROM scope_company
                         WHERE NOT scope_company.id = ANY(%%s::integer[])
                    )
        """ % (from_sql, join_sql)
        params = [allowed_ids, allowed_ids, allowed_ids, allowed_ids]
        if record_ids is not None:
            persisted_ids = [
                record_id for record_id in record_ids
                if isinstance(record_id, int)
            ]
            if not persisted_ids:
                return "SELECT NULL::integer WHERE FALSE", []
            query += " AND target.id = ANY(%s::integer[])"
            params.append(persisted_ids)
        return query, params

    @api.model
    def _eh_allowed_company_scope_ids(self, record_ids=None):
        """Return allowed IDs for field display/compute only.

        Security searches do not consume this two-statement helper. They embed
        the same subquery directly into the outer ORM SQL below, so a closure
        or rehome commit cannot race between an allow decision and its use.
        """
        query, params = self._eh_company_scope_subquery(record_ids)
        query += " ORDER BY target.id"
        self.env.cr.execute(query, params)
        return [row[0] for row in self.env.cr.fetchall()]

    @api.depends_context('allowed_company_ids', 'uid')
    def _compute_eh_company_scope_allowed(self):
        allowed_ids = set(
            self._eh_allowed_company_scope_ids(self.ids),
        )
        for record in self:
            record.eh_company_scope_allowed = (
                isinstance(record.id, int)
                and record.id in allowed_ids
            )

    @api.model
    def _search_eh_company_scope_allowed(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise NotImplementedError(
                "eh_company_scope_allowed supports Boolean = and != only",
            )
        want_allowed = value if operator == '=' else not value
        Domain = getattr(fields, 'Domain', None)
        if Domain is not None:
            # Odoo 19 custom domains splice this correlated allow-list into the
            # SAME SELECT that consumes the record rule. No read lock is taken,
            # so readonly transactions/replicas work and two readers can never
            # deadlock while upgrading locks for a later write.
            from odoo.tools import SQL

            def to_sql(model, alias, query):
                subquery, params = model._eh_company_scope_subquery()
                return SQL(
                    "%s IN (%s)",
                    model._field_to_sql(alias, 'id', query),
                    SQL(subquery, *params),
                )

            allowed = Domain.custom(to_sql=to_sql)
            return allowed if want_allowed else ~allowed

        # Odoo 16--18 accept Query objects as the right operand of ``id in``.
        # Wrap the fixed allow-list as a derived-table Query so it remains in
        # the same SQL statement without exposing private domain operators.
        subquery, params = self._eh_company_scope_subquery()
        from odoo.tools import Query

        if release.version_info[0] == 16:
            # Query predates the composable SQL wrapper in 16.0. ``mogrify``
            # binds the server-owned integer arrays before the derived table
            # is constructed; no caller value is interpolated.
            rendered = self.env.cr.mogrify(subquery, params).decode()
            allowed_query = Query(
                self.env.cr, 'eh_consol_scope_allowed', rendered,
            )
        else:
            from odoo.tools import SQL

            table = SQL("(%s)", SQL(subquery, *params))
            allowed_query = Query(
                self.env if release.version_info[0] >= 18 else self.env.cr,
                'eh_consol_scope_allowed', table,
            )
        return [(
            'id', 'in' if want_allowed else 'not in', allowed_query,
        )]
