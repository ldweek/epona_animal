# -*- encoding: utf-8 -*-
"""Add deterministic provenance columns before controlled undo is enabled."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("SELECT to_regclass('eh_reconciliation_audit')")
    if not cr.fetchone()[0]:
        return
    columns = (
        ('decision_group', 'varchar'),
        ('adjusting_move_id', 'integer'),
        ('original_audit_id', 'integer'),
        ('reversal_move_id', 'integer'),
        ('reversal_date', 'date'),
        ('undo_reason', 'text'),
    )
    for name, data_type in columns:
        cr.execute(
            "ALTER TABLE eh_reconciliation_audit "
            "ADD COLUMN IF NOT EXISTS %s %s" % (name, data_type)
        )
    cr.execute(
        "CREATE INDEX IF NOT EXISTS "
        "eh_reconciliation_audit_decision_group_idx "
        "ON eh_reconciliation_audit (decision_group)"
    )
    # Old rows cannot be linked retrospectively to one exact reconcile graph.
    # Leave decision_group NULL so the runtime refuses automated undo rather
    # than inventing provenance from a merely plausible current shape.
