# -*- encoding: utf-8 -*-
"""Classify historical engine moves for the IAS 37 roll-forward report."""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE account_move move
           SET eh_provision_event = CASE
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Unwinding of discount%%'
               ) THEN 'unwind'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Provision remeasurement (increase)%%'
               ) THEN 'remeasure_increase'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Provision remeasurement (decrease)%%'
               ) THEN 'remeasure_decrease'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Provision utilised%%'
               ) THEN 'utilisation'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Provision reversed%%'
               ) THEN 'reversal'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Asset recognition reversed%%'
               ) THEN 'asset_reversal'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Asset recognised%%'
               ) THEN 'asset_recognition'
               WHEN EXISTS (
                   SELECT 1 FROM account_move_line line
                    WHERE line.move_id = move.id
                      AND line.name ILIKE 'Reimbursement asset%%'
               ) THEN 'reimbursement'
               ELSE 'recognition'
           END
          FROM eh_provision provision
         WHERE move.eh_provision_id = provision.id
           AND move.eh_provision_event IS NULL
           AND move.eh_sealed IS TRUE
           AND move.state = 'posted'
    """)
