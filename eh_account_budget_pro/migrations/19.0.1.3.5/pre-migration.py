# -*- encoding: utf-8 -*-
"""Fail closed for legacy budget baselines and revision evidence."""


_ACTIVITY_REASON = (
    'Legacy approved activity baselines predate parent-state immutability; '
    'rows were preserved but excluded from flexible-budget calculations.'
)
_VERSION_REASON = (
    'Legacy budget version links predate engine-owned lineage controls; '
    'links were preserved but cannot be extended as trusted lineage.'
)
_REVISION_REASON = (
    'Legacy forecast revisions predate engine-owned snapshot controls; '
    'rows were preserved but excluded from latest-revision reporting.'
)


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'eh_budget_budget'):
        return
    for column in (
        'eh_activity_integrity_review_required',
        'eh_version_integrity_review_required',
    ):
        cr.execute(
            'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
            f'{column} boolean DEFAULT FALSE'
        )
    for column in (
        'eh_activity_integrity_review_reason',
        'eh_version_integrity_review_reason',
    ):
        cr.execute(
            'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
            f'{column} text'
        )

    if _table_exists(cr, 'eh_budget_activity'):
        cr.execute("""
            UPDATE eh_budget_budget budget
               SET eh_activity_integrity_review_required = TRUE,
                   eh_activity_integrity_review_reason = COALESCE(
                       NULLIF(budget.eh_activity_integrity_review_reason, ''), %s
                   )
             WHERE budget.state != 'draft'
               AND EXISTS (
                   SELECT 1 FROM eh_budget_activity activity
                    WHERE activity.budget_id = budget.id
               )
        """, (_ACTIVITY_REASON,))
    cr.execute("""
        UPDATE eh_budget_budget budget
           SET eh_version_integrity_review_required = TRUE,
               eh_version_integrity_review_reason = COALESCE(
                   NULLIF(budget.eh_version_integrity_review_reason, ''), %s
               )
         WHERE budget.parent_id IS NOT NULL
            OR budget.revised_budget_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM eh_budget_budget child
                 WHERE child.parent_id = budget.id
                    OR child.revised_budget_id = budget.id
            )
    """, (_VERSION_REASON,))

    if _table_exists(cr, 'eh_budget_forecast_revision'):
        cr.execute(
            'ALTER TABLE eh_budget_forecast_revision '
            'ADD COLUMN IF NOT EXISTS '
            'eh_integrity_review_required boolean DEFAULT FALSE'
        )
        cr.execute(
            'ALTER TABLE eh_budget_forecast_revision '
            'ADD COLUMN IF NOT EXISTS eh_integrity_review_reason text'
        )
        cr.execute("""
            UPDATE eh_budget_forecast_revision
               SET eh_integrity_review_required = TRUE,
                   eh_integrity_review_reason = COALESCE(
                       NULLIF(eh_integrity_review_reason, ''), %s
                   )
        """, (_REVISION_REASON,))
