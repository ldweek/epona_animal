# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the state machine must be enforced at the ORM write layer.

The transition buttons are UI-only affordances; without the eh.workflow.guard
mixin a plain user with model write access could RPC ``write({'state':
'measured'})`` straight past ``action_remeasure`` and its journal entry. These
tests assert the guarded fields refuse a direct write from a non-superuser.
"""

from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_fair_value', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.fv_asset = cls._ensure_account(
            cls.env, '1600', 'Investments at FV', 'asset_current')
        cls.fv_gain = cls._ensure_account(
            cls.env, '4600', 'Fair Value Gain/Loss', 'income_other')
        cls.fv_oci = cls._ensure_account(
            cls.env, '3600', 'FVOCI Reserve', 'equity')
        # A plain accounting user: has read/write ACL on the model
        # (group_eh_user) but is NOT a manager. So a raw write is permitted by
        # the access rules and only the workflow guard can block it, which is
        # exactly the bypass vector under test.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'FV Plain User',
            'login': 'fv_plain@test',
            'email': 'fv_plain@test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.manager_user = cls.env['res.users'].create({
            'name': 'FV Manager User',
            'login': 'fv_manager@test',
            'email': 'fv_manager@test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _item(self, **vals):
        base = {
            'name': '/', 'nature': 'financial_asset', 'level': '1',
            'prior_carrying': 1000.0, 'fair_value': 1200.0, 'routing': 'pl',
            'measurement_date': '2026-03-31',
            'balance_sheet_account_id': self.fv_asset.id,
            'gain_loss_account_id': self.fv_gain.id,
            'oci_account_id': self.fv_oci.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.fair.value.item'].create(base)

    def test_direct_state_write_blocked_for_plain_user(self):
        # A draft item's state is not frozen by the measured-guard, so without
        # the workflow guard a plain user could jump it to 'measured' and skip
        # the remeasurement posting entirely.
        item = self._item()
        self.assertEqual(item.state, 'draft')
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'state': 'measured'})
        # The state did not move.
        self.assertEqual(item.state, 'draft')

    def test_direct_recycled_write_blocked_for_plain_user(self):
        # 'recycled' is guarded too: flipping it would make action_recycle /
        # action_derecognise believe the OCI reserve was already settled.
        item = self._item()
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'recycled': True})

    def test_legitimate_action_still_posts(self):
        # Exercise a real non-superuser manager. Stored classification outputs
        # are action-owned, while a direct actor write remains guarded.
        item = self._item()
        item.with_user(self.manager_user).action_remeasure()
        item.invalidate_recordset(['state'])
        self.assertEqual(item.state, 'measured')

    def test_manager_can_reset_then_derecognise_on_explicit_date(self):
        # Cover the successful manager-only lifecycle with a real actor, not
        # the TransactionCase superuser used by most accounting examples.
        item = self._item()
        manager_item = item.with_user(self.manager_user)
        manager_item.action_remeasure()
        manager_item.action_reset_to_draft()
        item.invalidate_recordset(['state', 'disposal_date'])
        self.assertEqual(item.state, 'draft')
        self.assertFalse(item.disposal_date)

        manager_item.write({
            'fair_value': 1250.0,
            'measurement_date': '2026-04-30',
        })
        manager_item.action_remeasure()
        manager_item.write({
            'disposal_proceeds': 1250.0,
            'disposal_date': '2026-04-30',
        })
        manager_item.action_derecognise()
        item.invalidate_recordset([
            'state', 'disposal_date', 'derecognition_date', 'move_ids',
        ])
        self.assertEqual(item.state, 'derecognised')
        self.assertEqual(item.derecognition_date,
                         fields.Date.to_date('2026-04-30'))
        disposal_moves = item.move_ids.filtered(
            lambda move: move.date == fields.Date.to_date('2026-04-30')
        )
        self.assertTrue(disposal_moves)
        self.assertTrue(all(
            move.create_uid == self.manager_user for move in item.move_ids
        ))

    def test_manager_can_cancel_unused_item(self):
        item = self._item()
        item.with_user(self.manager_user).action_cancel()
        item.invalidate_recordset(['state'])
        self.assertEqual(item.state, 'cancelled')

    def test_manager_action_closes_rollforward_outputs(self):
        item = self._item(level='3', fair_value=1000.0)
        line = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': item.measurement_date,
            'period_end': item.measurement_date,
            'opening_balance': 1000.0,
        })
        line.with_user(self.manager_user).action_close()
        line.invalidate_recordset(['state', 'closing_balance'])
        self.assertEqual(line.state, 'closed')
        self.assertAlmostEqual(line.closing_balance, 1000.0, places=2)
        line.with_user(self.manager_user).action_reopen()
        line.invalidate_recordset(['state'])
        self.assertEqual(line.state, 'draft')

    def test_move_provenance_rejects_forged_internal_context(self):
        item = self._item()
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': item.measurement_date,
            'line_ids': [],
        })
        with self.assertRaisesRegex(AccessError, 'server-owned'):
            move.with_user(self.manager_user).with_context(
                eh_fair_value_move_link_internal=True,
            ).write({'eh_fair_value_item_id': item.id})

    def test_rollforward_direct_close_blocked_for_plain_user(self):
        item = self._item(level='3')
        line = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': item.measurement_date,
            'period_end': item.measurement_date,
            'opening_balance': 0.0,
        })
        self.assertEqual(line.state, 'draft')
        with self.assertRaises(AccessError):
            line.with_user(self.plain_user).write({'state': 'closed'})
        self.assertEqual(line.state, 'draft')

    def test_cancel_takes_same_item_row_lock_as_remeasurement(self):
        item = self._item()
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            item.action_cancel()

        self.assertEqual(item.state, 'cancelled')
        self.assertTrue(any(
            'eh_fair_value_item' in query and 'FOR UPDATE' in query
            for query in queries
        ))
