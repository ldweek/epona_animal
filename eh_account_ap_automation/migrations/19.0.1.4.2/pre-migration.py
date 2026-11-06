# -*- encoding: utf-8 -*-
"""Scope legacy vendor AP settings without inventing company intent."""

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
    """Drop scalar Many2one FKs before company-dependent JSONB conversion."""
    cr.execute(
        """
        SELECT DISTINCT tc.constraint_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON kcu.constraint_schema = tc.constraint_schema
           AND kcu.constraint_name = tc.constraint_name
           AND kcu.table_schema = tc.table_schema
           AND kcu.table_name = tc.table_name
         WHERE tc.table_schema = current_schema()
           AND tc.table_name = 'res_partner'
           AND tc.constraint_type = 'FOREIGN KEY'
           AND kcu.column_name = %s
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

    fields = (
        'eh_ap_tolerance_profile_id',
        'eh_ap_invoice_ref_regex',
        'eh_ap_total_regex',
    )
    column_types = {field: _column_type(cr, field) for field in fields}
    legacy = [
        field for field in fields
        if column_types[field] and column_types[field] != 'jsonb'
    ]
    if not legacy:
        return

    cr.execute(
        "ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS "
        "eh_ap_legacy_config_quarantine text",
    )
    # A shared partner has no objectively correct target company.  Preserve
    # the exact observable values, clear them from live operation, and require
    # a manager to review them into each intended company after upgrade.
    value_sql = {
        field: field if column_types[field] else 'NULL'
        for field in fields
    }
    present_checks = [
        "%s IS NOT NULL" % field
        for field in fields if column_types[field]
    ]
    cr.execute(
        """
        UPDATE res_partner
           SET eh_ap_legacy_config_quarantine = COALESCE(
               eh_ap_legacy_config_quarantine,
               jsonb_strip_nulls(jsonb_build_object(
                   'schema', 'eh-ap-partner-config-quarantine-v1',
                   'reason', 'shared-partner-company-ambiguous',
                   'tolerance_profile_id', %(profile)s,
                   'invoice_ref_regex', %(invoice_ref)s,
                   'total_regex', %(total)s
               ))::text
           )
         WHERE company_id IS NULL
           AND (%(present)s)
        """ % {
            'profile': value_sql['eh_ap_tolerance_profile_id'],
            'invoice_ref': value_sql['eh_ap_invoice_ref_regex'],
            'total': value_sql['eh_ap_total_regex'],
            'present': ' OR '.join(present_checks),
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
        "AP automation 19.0.1.4.2: converted vendor AP settings to "
        "company-dependent JSON and quarantined %d ambiguous shared-vendor "
        "configuration row(s) without applying them to any company; dropped "
        "%d obsolete scalar foreign-key constraint(s).",
        quarantined,
        len(dropped_foreign_keys),
    )
