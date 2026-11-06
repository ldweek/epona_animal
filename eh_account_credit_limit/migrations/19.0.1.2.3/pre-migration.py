# -*- encoding: utf-8 -*-
"""Scope legacy partner credit settings without inventing company intent."""

import logging

from psycopg2 import sql


_logger = logging.getLogger(__name__)


def _column_type(cr, column):
    cr.execute(
        """
        SELECT udt_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'res_partner'
           AND column_name = %s
        """,
        [column],
    )
    row = cr.fetchone()
    return row[0] if row else None


def _drop_column_foreign_keys(cr, column):
    """Drop scalar Many2one FKs that cannot survive JSONB conversion."""
    cr.execute(
        """
        SELECT DISTINCT constraint_name
          FROM information_schema.key_column_usage
         WHERE table_schema = current_schema()
           AND table_name = 'res_partner'
           AND column_name = %s
           AND constraint_name IN (
               SELECT constraint_name
                 FROM information_schema.table_constraints
                WHERE table_schema = current_schema()
                  AND table_name = 'res_partner'
                  AND constraint_type = 'FOREIGN KEY'
           )
        """,
        [column],
    )
    constraints = [row[0] for row in cr.fetchall()]
    for constraint in constraints:
        cr.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                sql.Identifier('res_partner'),
                sql.Identifier(constraint),
            )
        )
    return constraints


def migrate(cr, version):
    if not version:
        return

    fields = ('eh_credit_limit', 'eh_credit_policy_id')
    column_types = {field: _column_type(cr, field) for field in fields}
    legacy = [
        field for field in fields
        if column_types[field] and column_types[field] != 'jsonb'
    ]
    if not legacy:
        return

    cr.execute(
        "ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS "
        "eh_credit_legacy_config_quarantine text",
    )
    limit_present = (
        "eh_credit_limit IS NOT NULL AND "
        "eh_credit_limit NOT IN ('null'::jsonb, '{}'::jsonb)"
        if column_types['eh_credit_limit'] == 'jsonb'
        else (
            "COALESCE(eh_credit_limit, 0) != 0"
            if column_types['eh_credit_limit'] else "FALSE"
        )
    )
    policy_present = (
        "eh_credit_policy_id IS NOT NULL AND "
        "eh_credit_policy_id NOT IN ('null'::jsonb, '{}'::jsonb)"
        if column_types['eh_credit_policy_id'] == 'jsonb'
        else (
            "eh_credit_policy_id IS NOT NULL"
            if column_types['eh_credit_policy_id'] else "FALSE"
        )
    )
    limit_value = (
        'eh_credit_limit' if column_types['eh_credit_limit'] else 'NULL'
    )
    policy_value = (
        'eh_credit_policy_id' if column_types['eh_credit_policy_id'] else 'NULL'
    )
    cr.execute(
        """
        UPDATE res_partner
           SET eh_credit_legacy_config_quarantine = COALESCE(
               eh_credit_legacy_config_quarantine,
               jsonb_strip_nulls(jsonb_build_object(
                   'schema', 'eh-credit-partner-config-quarantine-v1',
                   'reason', 'shared-partner-company-ambiguous',
                   'credit_limit', %(limit_value)s,
                   'credit_policy_id', %(policy_value)s
               ))::text
           )
         WHERE company_id IS NULL
           AND ((%(limit_present)s) OR (%(policy_present)s))
        """ % {
            'limit_value': limit_value,
            'policy_value': policy_value,
            'limit_present': limit_present,
            'policy_present': policy_present,
        },
    )
    quarantined = cr.rowcount

    dropped_foreign_keys = []
    for field in legacy:
        dropped_foreign_keys.extend(_drop_column_foreign_keys(cr, field))
        cr.execute(
            """
            ALTER TABLE res_partner
            ALTER COLUMN %s TYPE jsonb
            USING CASE
                WHEN %s IS NULL OR company_id IS NULL THEN NULL
                ELSE jsonb_build_object(company_id::text, to_jsonb(%s))
            END
            """ % (field, field, field),
        )

    _logger.warning(
        "Credit limit 19.0.1.2.3: converted partner settings to "
        "company-dependent JSON and quarantined %d ambiguous shared-partner "
        "configuration row(s) without applying them to any company; dropped "
        "%d obsolete scalar foreign-key constraint(s).",
        quarantined,
        len(dropped_foreign_keys),
    )
