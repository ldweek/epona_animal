# -*- encoding: utf-8 -*-
"""Preserve legacy anomaly evidence while superseding unsound rule versions."""

from odoo import SUPERUSER_ID


def migrate(cr, version):
    # Old provider findings were identifiable only by the rule key. Mark their
    # provenance without rewriting the original description bytes. A raw-input
    # hash cannot be reconstructed truthfully, so it deliberately stays NULL.
    cr.execute("""
        UPDATE eh_account_anomaly_finding
           SET source = 'provider',
               provider_key = COALESCE(
                   NULLIF(provider_key, ''), 'legacy_unknown'
               )
         WHERE rule = 'llm_flag'
           AND COALESCE(source, 'deterministic') != 'provider'
    """)

    # v1 weekend findings used accounting date as if it were posting time;
    # v1 reversal findings used unsigned same-day duplicates; v1 round
    # findings used the user's current selection as their median. Keep every
    # evidence byte and reviewed decision, but withdraw only still-open legacy
    # results. Fresh v2 rules may then create independently.
    cr.execute("""
        WITH changed AS (
            UPDATE eh_account_anomaly_finding
               SET state = 'withdrawn',
                   reviewed_by_id = NULL,
                   reviewed_at = NULL
             WHERE state = 'open'
               AND rule IN ('weekend_post', 'reversal_pair', 'round_outlier')
         RETURNING id
        )
        INSERT INTO eh_account_anomaly_review_event
                    (finding_id, action, actor_id, occurred_at, note)
             SELECT id, 'withdrawn', %s, NOW() AT TIME ZONE 'UTC',
                    'Legacy rule version superseded during audited upgrade.'
               FROM changed
    """, (SUPERUSER_ID,))
