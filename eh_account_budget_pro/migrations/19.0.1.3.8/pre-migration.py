# -*- encoding: utf-8 -*-
"""Quarantine approved legacy budgets whose actual scopes overlap.

Before 1.3.8, an account-wide line and an analytic line (or duplicate lines
for the same scope) could cover the same dates. Header and pivot actuals sum
line values, so those shapes double count ledger activity. Preserve every
row, mark only affected approved budgets for review, and let new approvals
use the model-level additive-scope validation.
"""


_OVERLAP_REASON = (
    'Approved budget contains overlapping account/analytic actual scopes '
    'created before additive-scope validation; rows were preserved as '
    'untrusted audit evidence and require a reviewed replacement.'
)


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not all(_table_exists(cr, table_name) for table_name in (
        'eh_budget_budget', 'eh_budget_line',
    )):
        return
    cr.execute(
        'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
        'eh_basis_integrity_review_required boolean DEFAULT FALSE'
    )
    cr.execute(
        'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
        'eh_basis_integrity_review_reason text'
    )
    cr.execute("""
        UPDATE eh_budget_budget AS budget
           SET eh_basis_integrity_review_required = TRUE,
               eh_basis_integrity_review_reason = COALESCE(
                   NULLIF(budget.eh_basis_integrity_review_reason, ''), %s
               )
         WHERE budget.state != 'draft'
           AND EXISTS (
               SELECT 1
                 FROM eh_budget_line AS left_line
                 JOIN eh_budget_line AS right_line
                   ON right_line.budget_id = left_line.budget_id
                  AND right_line.account_id = left_line.account_id
                  AND right_line.id > left_line.id
                  AND (
                       right_line.analytic_account_id
                           IS NOT DISTINCT FROM
                           left_line.analytic_account_id
                       OR right_line.analytic_account_id IS NULL
                       OR left_line.analytic_account_id IS NULL
                  )
                  AND right_line.period_from IS NOT NULL
                  AND right_line.period_to IS NOT NULL
                  AND left_line.period_from IS NOT NULL
                  AND left_line.period_to IS NOT NULL
                  AND daterange(
                       right_line.period_from,
                       right_line.period_to,
                       '[]'
                  ) && daterange(
                       left_line.period_from,
                       left_line.period_to,
                       '[]'
                  )
                WHERE left_line.budget_id = budget.id
           )
    """, (_OVERLAP_REASON,))
