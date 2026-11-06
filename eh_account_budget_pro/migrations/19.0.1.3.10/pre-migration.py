# -*- encoding: utf-8 -*-
"""Repair the 1.3.9 premature-retirement version transition.

The affected runtime linked a new draft child and immediately changed its
confirmed source to ``revised``. Restore only the exact, reciprocal and
uncontested shape. No line, commitment, posted actual, or lineage byte is
rewritten. Ambiguous shapes remain retired and are quarantined for review.
"""


_REVIEW_REASON = (
    'Legacy revision was retired before its replacement was approved, but '
    'its lineage or controlling scope is ambiguous; state and source rows '
    'were preserved for manager review.'
)

_REQUIRED_BUDGET_COLUMNS = (
    'id', 'state', 'parent_id', 'revised_budget_id', 'company_id',
    'write_date', 'eh_commitment_integrity_review_required',
    'eh_activity_integrity_review_required',
    'eh_version_integrity_review_required',
    'eh_version_integrity_review_reason',
    'eh_basis_integrity_review_required',
)
_REQUIRED_LINE_COLUMNS = (
    'id', 'budget_id', 'account_id', 'analytic_account_id',
    'period_from', 'period_to',
)


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def _columns_exist(cr, table_name, column_names):
    cr.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped",
        (table_name,),
    )
    existing = {row[0] for row in cr.fetchall()}
    return set(column_names).issubset(existing)


def _restore_prematurely_retired_budgets(cr):
    if not all(_table_exists(cr, table_name) for table_name in (
        'eh_budget_budget', 'eh_budget_line',
    )):
        return []
    if not _columns_exist(
        cr, 'eh_budget_budget', _REQUIRED_BUDGET_COLUMNS,
    ) or not _columns_exist(
        cr, 'eh_budget_line', _REQUIRED_LINE_COLUMNS,
    ):
        return []

    cr.execute("""
        WITH restorable AS (
            SELECT source.id
              FROM eh_budget_budget AS source
              JOIN eh_budget_budget AS replacement
                ON replacement.id = source.revised_budget_id
               AND replacement.parent_id = source.id
             WHERE source.state = 'revised'
               AND replacement.state = 'draft'
               AND replacement.company_id = source.company_id
               AND NOT COALESCE(
                   source.eh_commitment_integrity_review_required, FALSE
               )
               AND NOT COALESCE(
                   source.eh_activity_integrity_review_required, FALSE
               )
               AND NOT COALESCE(
                   source.eh_version_integrity_review_required, FALSE
               )
               AND NOT COALESCE(
                   source.eh_basis_integrity_review_required, FALSE
               )
               AND NOT COALESCE(
                   replacement.eh_commitment_integrity_review_required,
                   FALSE
               )
               AND NOT COALESCE(
                   replacement.eh_activity_integrity_review_required,
                   FALSE
               )
               AND NOT COALESCE(
                   replacement.eh_version_integrity_review_required,
                   FALSE
               )
               AND NOT COALESCE(
                   replacement.eh_basis_integrity_review_required, FALSE
               )
               AND EXISTS (
                   SELECT 1 FROM eh_budget_line AS source_line
                    WHERE source_line.budget_id = source.id
               )
               AND 1 = (
                   SELECT count(*) FROM eh_budget_budget AS child
                    WHERE child.parent_id = source.id
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM eh_budget_line AS source_line
                     JOIN eh_budget_line AS live_line
                       ON live_line.account_id = source_line.account_id
                      AND (
                          live_line.analytic_account_id IS NOT DISTINCT FROM
                              source_line.analytic_account_id
                          OR live_line.analytic_account_id IS NULL
                          OR source_line.analytic_account_id IS NULL
                      )
                      AND live_line.period_from IS NOT NULL
                      AND live_line.period_to IS NOT NULL
                      AND source_line.period_from IS NOT NULL
                      AND source_line.period_to IS NOT NULL
                      AND daterange(
                          live_line.period_from, live_line.period_to, '[]'
                      ) && daterange(
                          source_line.period_from,
                          source_line.period_to,
                          '[]'
                      )
                     JOIN eh_budget_budget AS live
                       ON live.id = live_line.budget_id
                    WHERE source_line.budget_id = source.id
                      AND live.id != source.id
                      AND live.company_id = source.company_id
                      AND live.state = 'confirmed'
               )
        ), restored AS (
            UPDATE eh_budget_budget AS source
               SET state = 'confirmed', write_date = NOW()
             WHERE source.id IN (SELECT id FROM restorable)
         RETURNING source.id
        )
        SELECT id FROM restored ORDER BY id
    """)
    restored_ids = [row[0] for row in cr.fetchall()]

    # Anything still matching the reciprocal premature-retirement outline
    # was not provably safe (for example multiple children, quarantine, or a
    # competing live controller). Keep all bytes and make the review explicit.
    cr.execute("""
        WITH unsafe AS (
            SELECT source.id AS source_id,
                   replacement.id AS replacement_id
              FROM eh_budget_budget AS source
              JOIN eh_budget_budget AS replacement
                ON replacement.id = source.revised_budget_id
               AND replacement.parent_id = source.id
             WHERE source.state = 'revised'
               AND replacement.state = 'draft'
        )
        UPDATE eh_budget_budget AS budget
           SET eh_version_integrity_review_required = TRUE,
               eh_version_integrity_review_reason = COALESCE(
                   NULLIF(budget.eh_version_integrity_review_reason, ''), %s
               )
         WHERE budget.id IN (
             SELECT source_id FROM unsafe
             UNION
             SELECT replacement_id FROM unsafe
         )
    """, (_REVIEW_REASON,))
    return restored_ids


def migrate(cr, version):
    if not version:
        return
    _restore_prematurely_retired_budgets(cr)
