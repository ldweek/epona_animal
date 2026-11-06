# -*- encoding: utf-8 -*-
"""Preserve pre-upgrade broken-promise idempotency by action identity."""


def migrate(cr, version):
    del version
    cr.execute("""
        WITH handled AS (
            SELECT DISTINCT ON (action.case_id)
                   action.case_id,
                   action.id AS action_id
              FROM eh_collections_action AS action
              JOIN eh_collections_case AS case_rec
                ON case_rec.id = action.case_id
             WHERE case_rec.last_broken_promise_handled_at IS NOT NULL
               AND action.promise_amount <> 0.0
               AND action.promise_date IS NOT NULL
               AND action.action_date
                   <= case_rec.last_broken_promise_handled_at
             ORDER BY action.case_id,
                      action.action_date DESC,
                      action.id DESC
        )
        UPDATE eh_collections_case AS case_rec
           SET last_broken_promise_action_id = handled.action_id
          FROM handled
         WHERE handled.case_id = case_rec.id
           AND case_rec.last_broken_promise_action_id IS NULL
    """)
