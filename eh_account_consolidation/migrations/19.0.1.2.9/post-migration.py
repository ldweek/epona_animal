# -*- encoding: utf-8 -*-
"""Release only provably empty drafts from the old blanket quarantine."""


_RELEASE_NOTE = (
    "[19.0.1.2.9] Empty legacy draft released for deletion or a clean "
    "current-engine rebuild; no accounting/output evidence was present."
)


def migrate(cr, version):
    if not version:
        return

    # 1.2.6 originally quarantined every version-zero row, including a draft
    # that had never been computed.  Release only the strict empty shape.  A
    # single line, child input, state/audit stamp, GL pointer or non-zero output
    # keeps the row quarantined.  Preserve the reason text as an audit trail.
    cr.execute(
        """
        UPDATE eh_consol_run AS run
           SET legacy_evidence_quarantined = FALSE,
               legacy_evidence_quarantine_reason = CASE
                   WHEN COALESCE(
                       run.legacy_evidence_quarantine_reason, ''
                   ) LIKE '%%[19.0.1.2.9]%%'
                   THEN run.legacy_evidence_quarantine_reason
                   WHEN COALESCE(
                       BTRIM(run.legacy_evidence_quarantine_reason), ''
                   ) = ''
                   THEN %s
                   ELSE run.legacy_evidence_quarantine_reason
                        || E'\n' || %s
               END
         WHERE run.legacy_evidence_quarantined IS TRUE
           AND COALESCE(run.engine_provenance_version, 0) = 0
           AND run.state = 'draft'
           AND run.move_id IS NULL
           AND run.reversal_move_id IS NULL
           AND run.posting_predecessor_run_id IS NULL
           AND run.computed_at IS NULL
           AND run.computed_by_id IS NULL
           AND run.reviewed_at IS NULL
           AND run.reviewed_by_id IS NULL
           AND run.closed_at IS NULL
           AND run.closed_by_id IS NULL
           AND run.reversed_at IS NULL
           AND run.reversed_by_id IS NULL
           AND COALESCE(run.cta_amount, 0.0) = 0.0
           AND COALESCE(run.nci_amount, 0.0) = 0.0
           AND COALESCE(run.goodwill_recoverable_amount, 0.0) = 0.0
           AND COALESCE(run.goodwill_impairment_amount, 0.0) = 0.0
           AND COALESCE(run.goodwill_test_recoverable_amount, 0.0) = 0.0
           AND run.goodwill_test_account_id IS NULL
           AND run.goodwill_test_currency_id IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM eh_consol_run_line AS line
                WHERE line.run_id = run.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM eh_consol_run_move_history_rel AS history
                WHERE history.run_id = run.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM eh_consol_elimination AS elimination
                WHERE elimination.run_id = run.id
           )
           AND NOT EXISTS (
               SELECT 1 FROM eh_consol_unrealised_profit AS unrealised
                WHERE unrealised.run_id = run.id
           )
        """,
        (_RELEASE_NOTE, _RELEASE_NOTE),
    )
