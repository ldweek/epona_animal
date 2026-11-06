# -*- encoding: utf-8 -*-
"""Preserve the operator-selected billing day on upgrade."""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT to_regclass('eh_recurring_invoice_template')"
    )
    if not cr.fetchone()[0]:
        return
    cr.execute(
        "ALTER TABLE eh_recurring_invoice_template "
        "ADD COLUMN IF NOT EXISTS billing_day integer"
    )
    # next_run_date is the actual billing date selected by the operator.
    # start_date is a contract boundary and may intentionally differ.
    cr.execute(
        "UPDATE eh_recurring_invoice_template "
        "SET billing_day = EXTRACT(DAY FROM COALESCE(next_run_date, "
        "start_date, CURRENT_DATE))::integer "
        "WHERE billing_day IS NULL OR billing_day NOT BETWEEN 1 AND 31"
    )
    cr.execute(
        "ALTER TABLE eh_recurring_invoice_template "
        "ALTER COLUMN billing_day SET DEFAULT 1, "
        "ALTER COLUMN billing_day SET NOT NULL"
    )
