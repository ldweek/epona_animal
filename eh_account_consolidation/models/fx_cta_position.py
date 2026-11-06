# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Freeze CTA-position identity once consolidation consumes it."""

from odoo import _, models
from odoo.exceptions import UserError


_CONSOLIDATION_IDENTITY_FIELDS = frozenset({
    'name', 'company_id', 'cta_account_id',
    'foreign_operation_partner_id', 'foreign_operation_company_id',
    'operation_currency_id', 'journal_id',
    'gain_account_id', 'loss_account_id',
})


class EhFxCtaPosition(models.Model):
    _inherit = 'eh.fx.cta.position'

    def _eh_assert_not_consumed_by_consolidation(self):
        """Fail closed on any committed settled consolidation reference.

        Every consolidation member-link and run transition locks this same CTA
        position row after its entity/run locks.  The position writer takes the
        position lock before this read.  Therefore a concurrent compute either
        commits first (and is observed below) or waits and consumes the updated
        identity; there is no allow-check/use race and no read-rule elevation.
        """
        position_ids = tuple(sorted(self.ids))
        if not position_ids:
            return self
        self.env.cr.execute(
            """
            WITH referenced_entity AS (
                SELECT member.entity_id
                  FROM eh_consol_member AS member
                 WHERE member.cta_position_id IN %s
                UNION
                SELECT run.entity_id
                  FROM eh_consol_run_line AS line
                  JOIN eh_consol_run AS run ON run.id = line.run_id
                 WHERE line.cta_position_id IN %s
            )
            SELECT 1
              FROM eh_consol_run AS run
             WHERE run.entity_id IN (
                       SELECT entity_id FROM referenced_entity
                   )
               AND (
                      run.state IN ('computed', 'reviewed', 'closed')
                   OR run.move_id IS NOT NULL
                   OR run.reversal_move_id IS NOT NULL
                   OR run.legacy_evidence_quarantined
                   OR EXISTS (
                        SELECT 1
                          FROM eh_consol_run_move_history_rel AS history
                         WHERE history.run_id = run.id
                   )
                   OR EXISTS (
                        SELECT 1
                          FROM eh_consol_run_line AS output
                         WHERE output.run_id = run.id
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
             LIMIT 1
            """,
            (position_ids, position_ids),
        )
        if self.env.cr.fetchone():
            raise UserError(_(
                "A CTA position linked to settled consolidation evidence has "
                "an immutable company, foreign-operation identity and "
                "account routing. Create a new position for a changed basis."
            ))
        return self

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        if set(vals) & _CONSOLIDATION_IDENTITY_FIELDS:
            # Match consolidation/Base global order: company, then position.
            self._eh_lock_for_transition(
                extra_company_ids=(vals.get('company_id'),),
            )
            self._eh_assert_not_consumed_by_consolidation()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        self._eh_assert_not_consumed_by_consolidation()
        return super().unlink()
