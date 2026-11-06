# -*- encoding: utf-8 -*-
"""Fail closed on every unstamped legacy IAS 8 / IAS 10 ledger graph.

Before the durable provenance version existed, source links, reversal links,
and the base seal were all writable. Even a perfectly matching legacy graph
is therefore not evidence that the engine created it. Preserve the source and
journal rows, quarantine the source, and clear only the untrusted move links.
Never backfill or bless a graph from its shape.
"""

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
_PROVENANCE_VERSION = 1


def migrate(cr, version):
    if not version:
        return
    for table in ('eh_accounting_change', 'eh_subsequent_event'):
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "legacy_provenance_quarantined boolean DEFAULT false" % table
        )
        cr.execute(
            "ALTER TABLE %s ADD COLUMN IF NOT EXISTS "
            "legacy_provenance_quarantine_note text" % table
        )
    cr.execute(
        "ALTER TABLE account_move ADD COLUMN IF NOT EXISTS "
        "eh_event_provenance_version integer DEFAULT 0"
    )

    change_count = _quarantine_sources(
        cr,
        table='eh_accounting_change',
        move_field='eh_accounting_change_id',
        other_move_field='eh_subsequent_event_id',
        source_date_field='change_date',
        label='accounting-change',
    )
    event_count = _quarantine_sources(
        cr,
        table='eh_subsequent_event',
        move_field='eh_subsequent_event_id',
        other_move_field='eh_accounting_change_id',
        source_date_field='reporting_date',
        label='subsequent-event',
    )

    cr.execute(
        """
        UPDATE account_move
           SET eh_accounting_change_id = NULL,
               eh_subsequent_event_id = NULL
         WHERE (eh_accounting_change_id IS NOT NULL
                OR eh_subsequent_event_id IS NOT NULL)
           AND COALESCE(eh_event_provenance_version, 0) != %s
        """,
        (_PROVENANCE_VERSION,),
    )
    cleared = cr.rowcount
    env = api.Environment(cr, SUPERUSER_ID, {})
    stamped_change_count = _quarantine_bad_stamped_histories(
        cr, env['eh.accounting.change'].search([]), 'change',
    )
    stamped_event_count = _quarantine_bad_stamped_histories(
        cr, env['eh.subsequent.event'].search([]), 'event',
    )
    change_count += stamped_change_count
    event_count += stamped_event_count
    if change_count or event_count or cleared:
        _logger.warning(
            "Events provenance audit quarantined %d accounting change(s) "
            "and %d subsequent event(s), and cleared %d unstamped legacy "
            "move link(s). No legacy graph was trusted from shape.",
            change_count, event_count, cleared,
        )


def _quarantine_sources(
        cr, table, move_field, other_move_field, source_date_field, label):
    note = (
        "Fail-closed provenance audit: this %s carries posted/source-linked "
        "journal evidence without the immutable engine version stamp, or "
        "its stamped primary/link topology is invalid. The source and "
        "journal rows were preserved; ledger transitions are blocked "
        "pending review. No legacy graph was trusted from shape." % label
    )
    cr.execute(
        """
        UPDATE {table} AS source
           SET legacy_provenance_quarantined = TRUE,
               legacy_provenance_quarantine_note = %s
         WHERE (
                source.state = 'posted'
                OR source.move_id IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM account_move AS linked
                     WHERE linked.{move_field} = source.id
                )
           )
           AND NOT (
                source.state = 'reversed'
                AND source.legacy_remediation_move_id IS NOT NULL
           )
           AND (
                EXISTS (
                    SELECT 1 FROM account_move AS linked
                     WHERE linked.{move_field} = source.id
                       AND COALESCE(
                           linked.eh_event_provenance_version, 0
                       ) != %s
                )
                OR (
                    source.state = 'posted'
                    AND source.move_id IS NULL
                )
                OR (
                    source.move_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM account_move AS primary_move
                         WHERE primary_move.id = source.move_id
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM account_move AS primary_move
                     WHERE primary_move.id = source.move_id
                       AND (
                            COALESCE(
                                primary_move.eh_event_provenance_version, 0
                            ) != %s
                            OR primary_move.{move_field} IS DISTINCT FROM
                               source.id
                            OR primary_move.{other_move_field} IS NOT NULL
                            OR primary_move.reversed_entry_id IS NOT NULL
                            OR primary_move.state != 'posted'
                            OR primary_move.eh_sealed IS NOT TRUE
                            OR primary_move.company_id IS DISTINCT FROM
                               source.company_id
                            OR primary_move.journal_id IS DISTINCT FROM
                               source.journal_id
                            OR primary_move.date IS DISTINCT FROM
                               source.{source_date_field}
                       )
                )
           )
        """.format(
            table=table,
            move_field=move_field,
            other_move_field=other_move_field,
            source_date_field=source_date_field,
        ),
        (note, _PROVENANCE_VERSION, _PROVENANCE_VERSION),
    )
    return cr.rowcount


def _quarantine_bad_stamped_histories(cr, sources, kind):
    invalid = []
    for source in sources:
        if source.legacy_provenance_quarantined:
            continue
        try:
            _validate_stamped_history(source, kind)
        except Exception as exc:
            _logger.warning(
                "Stamped %s source %s failed exact current/history "
                "validation: %s", kind, source.id, exc,
            )
            invalid.append(source.id)
    if not invalid:
        return 0
    table = (
        'eh_accounting_change'
        if kind == 'change'
        else 'eh_subsequent_event'
    )
    cr.execute(
        "UPDATE %s SET legacy_provenance_quarantined = TRUE, "
        "legacy_provenance_quarantine_note = %%s WHERE id IN %%s" % table,
        (
            "Fail-closed stamped-history audit: the current primary or an "
            "older original/reversal pair did not retain exact sealed "
            "engine provenance. Evidence was preserved and ledger "
            "transitions are blocked pending review.",
            tuple(invalid),
        ),
    )
    return len(invalid)


def _validate_stamped_history(source, kind):
    """Accept a current primary plus zero or more exact historical pairs."""
    if (
        source.state == 'reversed'
        and source.legacy_remediation_move_id
        and source.move_id
    ):
        source._eh_validate_legacy_remediation(
            source.move_id, source.legacy_remediation_move_id,
        )
        return
    move_field = (
        'eh_accounting_change_id'
        if kind == 'change'
        else 'eh_subsequent_event_id'
    )
    other_field = (
        'eh_subsequent_event_id'
        if kind == 'change'
        else 'eh_accounting_change_id'
    )
    linked = source.env['account.move'].search([
        (move_field, '=', source.id),
    ])
    current = source.move_id
    if source.state == 'posted':
        if not current:
            raise ValueError("posted source lacks its current primary")
        if kind == 'change':
            source._eh_validate_restatement_move(current)
        else:
            source._eh_validate_adjusting_move(current)
    elif current:
        raise ValueError("non-posted source retains a current primary")

    historical = linked - current
    originals = historical.filtered(lambda move: not move.reversed_entry_id)
    reversals = historical - originals
    if len(originals) != len(reversals):
        raise ValueError("historical entries are not original/reversal pairs")
    for original in originals:
        matches = reversals.filtered(
            lambda move: move.reversed_entry_id == original,
        )
        if len(matches) != 1:
            raise ValueError("historical original lacks one exact reversal")
        reversal = matches.ensure_one()
        for move in original | reversal:
            if (
                move.state != 'posted'
                or not move.eh_sealed
                or move.eh_event_provenance_version
                   != _PROVENANCE_VERSION
                or move.company_id != source.company_id
                or move[move_field] != source
                or move[other_field]
            ):
                raise ValueError("historical pair has invalid provenance")
        if original.reversed_entry_id:
            raise ValueError("historical original is itself a reversal")
        if reversal.journal_id != original.journal_id:
            raise ValueError("historical pair changed journals")
        reverse_signature = sorted(
            (account_id, credit, debit)
            for account_id, debit, credit
            in source._eh_move_signature(original)
        )
        if source._eh_move_signature(reversal) != reverse_signature:
            raise ValueError("historical reversal does not exactly mirror")
    if reversals - originals._eh_reversal_moves():
        raise ValueError("historical reversal points outside this history")
