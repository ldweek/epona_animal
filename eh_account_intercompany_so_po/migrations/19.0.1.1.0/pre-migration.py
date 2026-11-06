# -*- encoding: utf-8 -*-
"""Prepare durable reviewer metadata without releasing any quarantine.

The 1.0.3 migration correctly quarantined legacy, client-writable pair links.
1.1.0 adds a manager attestation workflow; upgrade must preserve every
quarantine until that workflow proves one exact pair at runtime.
"""


def migrate(cr, version):
    if not version:
        return
    for table in ('sale_order', 'purchase_order'):
        cr.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "eh_ic_integrity_review_note text"
        )
        cr.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "eh_ic_integrity_reviewed_by_id integer"
        )
        cr.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
            "eh_ic_integrity_reviewed_at timestamp without time zone"
        )
