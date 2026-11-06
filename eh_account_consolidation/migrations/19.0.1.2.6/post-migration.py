# -*- encoding: utf-8 -*-
"""Quarantine pre-v2 evidence while leaving clean drafts rebuildable."""


_QUARANTINE_NOTE = (
    "[19.0.1.2.6] Legacy consolidation graph retained but quarantined: "
    "server-owned run identity, posting-account mapping and IAS 36 test "
    "snapshots, including posting-currency precision, were not durably "
    "recorded by the v2 engine."
)


def migrate(cr, version):
    if not version:
        return

    # Do not shape-bless legacy evidence. Before v2, run names were caller-
    # writable, target accounts were re-resolved from mutable live codes and
    # IAS 36 inputs had no tested-basis snapshot.  Even a self-consistent row
    # graph cannot prove those historical facts.  Preserve every row/FK, but
    # require a clean new run for trusted evidence.
    cr.execute(
        """
        UPDATE eh_consol_run AS run
           SET legacy_evidence_quarantined = TRUE,
               legacy_evidence_quarantine_reason = CASE
                   WHEN COALESCE(
                       run.legacy_evidence_quarantine_reason, ''
                   ) LIKE '%%[19.0.1.2.6]%%'
                   THEN run.legacy_evidence_quarantine_reason
                   WHEN COALESCE(
                       BTRIM(run.legacy_evidence_quarantine_reason), ''
                   ) = ''
                   THEN %s
                   ELSE run.legacy_evidence_quarantine_reason
                        || E'\n' || %s
               END
         WHERE (
                COALESCE(run.engine_provenance_version, 0) < 2
                AND (
                    run.state IS DISTINCT FROM 'draft'
                    OR run.move_id IS NOT NULL
                    OR run.reversal_move_id IS NOT NULL
                    OR run.computed_at IS NOT NULL
                    OR run.computed_by_id IS NOT NULL
                    OR run.reviewed_at IS NOT NULL
                    OR run.reviewed_by_id IS NOT NULL
                    OR run.closed_at IS NOT NULL
                    OR run.closed_by_id IS NOT NULL
                    OR run.reversed_at IS NOT NULL
                    OR run.reversed_by_id IS NOT NULL
                    OR COALESCE(run.cta_amount, 0.0) <> 0.0
                    OR COALESCE(run.nci_amount, 0.0) <> 0.0
                    OR COALESCE(run.goodwill_impairment_amount, 0.0) <> 0.0
                    OR EXISTS (
                        SELECT 1 FROM eh_consol_run_line AS line
                         WHERE line.run_id = run.id
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_consol_run_move_history_rel AS history
                         WHERE history.run_id = run.id
                    )
                    OR EXISTS (
                        SELECT 1
                          FROM eh_consol_elimination AS elimination
                         WHERE elimination.run_id = run.id
                           AND (
                               elimination.state IS DISTINCT FROM 'draft'
                               OR elimination.posted_at IS NOT NULL
                               OR elimination.posted_by_id IS NOT NULL
                           )
                    )
                )
            )
            OR (
                COALESCE(run.posting_currency_rounding, 0.0) <= 0.0
                AND (
                    run.move_id IS NOT NULL
                    OR run.reversal_move_id IS NOT NULL
                    OR EXISTS (
                        SELECT 1
                          FROM eh_consol_run_move_history_rel AS history
                         WHERE history.run_id = run.id
                    )
                )
            )
        """,
        (_QUARANTINE_NOTE, _QUARANTINE_NOTE),
    )
