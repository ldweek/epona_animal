# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the state machine is enforced in the ORM, not just the UI.

A plain interactive user must not be able to RPC-write ``state`` straight to
a posted value, skipping ``action_classify`` / ``action_sell`` and the
journal entry they post. The ``eh.workflow.guard`` mixin blocks any direct
write to a guarded field unless it originates from the record's own action.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_held_for_sale', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.asset = cls._ensure_account(
            cls.env, '1700', 'Assets Held for Sale', 'asset_current')
        cls.impairment = cls._ensure_account(
            cls.env, '5170', 'Held-for-sale Impairment', 'expense')
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        # A non-superuser, non-manager internal user. The test env itself runs
        # as superuser, for which the guard is deliberately inert, so the
        # guarded write must be attempted through with_user(this user).
        cls.plain_user = cls.env['res.users'].create({
            'name': 'HFS Plain User',
            'login': 'eh_hfs_plain_user',
            'email': 'eh_hfs_plain_user@example.com',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.eh_user = cls.env['res.users'].create({
            'name': 'HFS Accounting User',
            'login': 'eh_hfs_accounting_user',
            'email': 'eh_hfs_accounting_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.core_account_manager = cls.env['res.users'].create({
            'name': 'Core Accounting Manager without HFS access',
            'login': 'eh_hfs_core_account_manager',
            'email': 'eh_hfs_core_account_manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('account.group_account_manager').id,
            ])],
        })

    def _item(self, **extra):
        vals = {
            'name': '/',
            'carrying_amount': 1000.0,
            'fair_value_less_costs': 800.0,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(extra)
        return self.env['eh.held.for.sale'].create(vals)

    def _group(self):
        return self.env['eh.disposal.group'].create({
            'name': '/',
            'fair_value_less_costs': 800.0,
            'impairment_account_id': self.impairment.id,
            'asset_account_id': self.asset.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Member A',
                'carrying_amount': 1000.0,
                'account_id': self.asset.id,
            })],
        })

    def test_held_for_sale_state_write_blocked_for_plain_user(self):
        item = self.env['eh.held.for.sale'].create({
            'name': '/',
            'carrying_amount': 1000.0,
            'fair_value_less_costs': 800.0,
            'asset_account_id': self.asset.id,
            'impairment_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(item.state, 'draft')
        # RPC-writing the state to 'held' would skip the write-down journal
        # entry that action_classify posts. The guard must refuse it.
        with self.assertRaises(AccessError):
            item.with_user(self.plain_user).write({'state': 'held'})
        # And the value must not have moved.
        self.assertEqual(item.state, 'draft')

    def test_disposal_group_state_write_blocked_for_plain_user(self):
        group = self.env['eh.disposal.group'].create({
            'name': '/',
            'fair_value_less_costs': 800.0,
            'impairment_account_id': self.impairment.id,
            'asset_account_id': self.asset.id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(group.state, 'draft')
        with self.assertRaises(AccessError):
            group.with_user(self.plain_user).write({'state': 'sold'})
        self.assertEqual(group.state, 'draft')

    def test_classified_single_basis_and_pause_marker_are_immutable(self):
        item = self._item()
        item.action_classify()
        with self.assertRaises(UserError):
            item.with_user(self.eh_user).write({
                'classification_date': '2026-01-01',
            })
        with self.assertRaises(AccessError):
            item.with_user(self.eh_user).write({
                'asset_paused_by_hfs': True,
            })

    def test_classified_group_and_member_evidence_are_immutable(self):
        group = self._group()
        group.action_classify()
        line = group.line_ids
        with self.assertRaises(UserError):
            line.with_user(self.eh_user).write({
                'asset_paused_by_group': True,
            })
        with self.assertRaises(UserError):
            line.with_user(self.eh_user).write({
                'account_id': self.impairment.id,
            })
        with self.assertRaises(UserError):
            group.with_user(self.eh_user).write({
                'classification_date': '2026-01-01',
            })

    def test_eh_user_can_update_scope_without_chart_write_access(self):
        """Validating an HFS record needs read, not configuration-write,
        access to the referenced discontinued-operation accounts."""
        item = self._item(
            is_discontinued=True,
            discontinued_pl_account_ids=[(6, 0, self.impairment.ids)],
        )
        move = self.post_balanced_move([
            {'account': self.impairment, 'debit': 10.0},
            {'account': self.account_cash, 'credit': 10.0},
        ], date='2026-01-01')
        candidate = move.line_ids.filtered(
            lambda line: line.account_id == self.impairment)
        item.discontinued_candidate_move_line_ids = [(6, 0, candidate.ids)]
        item.action_tag_discontinued()

        self.assertFalse(
            self.env['account.account'].with_user(
                self.eh_user).check_access_rights('write', raise_exception=False)
        )
        item.with_user(self.eh_user).write({
            'discontinued_candidate_move_line_ids': [(6, 0, candidate.ids)],
        })
        self.assertEqual(item.discontinued_candidate_move_line_ids, candidate)

        group = self._group()
        group.line_ids.with_user(self.eh_user).write({
            'pl_account_id': self.impairment.id,
        })
        self.assertEqual(group.line_ids.pl_account_id, self.impairment)

    def test_move_provenance_is_immutable(self):
        item = self._item()
        item.action_classify()
        move = item.move_ids[:1]
        self.assertTrue(move)
        with self.assertRaises(AccessError):
            move.write({'eh_held_for_sale_id': False})

    def test_core_accountant_without_hfs_acl_can_create_ordinary_bill(self):
        """HFS evidence guards must not break standard accounting flows."""
        user = self.core_account_manager
        self.assertFalse(user.has_group('eh_account_base.group_eh_user'))
        bill = self.env['account.move'].with_user(user).with_company(
            self.company,
        ).create({
            'move_type': 'in_invoice',
            'company_id': self.company.id,
            'partner_id': self.partner_a.id,
            'journal_id': self.journal_purchase.id,
            'invoice_date': '2026-01-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Ordinary vendor charge',
                'account_id': self.account_expense.id,
                'quantity': 1.0,
                'price_unit': 25.0,
            })],
        })
        self.assertTrue(bill.invoice_line_ids)

    def test_core_accountant_cannot_edit_frozen_discontinued_line(self):
        """Narrow lookup elevation must preserve frozen-line enforcement."""
        item = self._item(
            is_discontinued=True,
            discontinued_pl_account_ids=[(6, 0, self.impairment.ids)],
        )
        move = self.post_balanced_move([
            {'account': self.impairment, 'debit': 10.0},
            {'account': self.account_cash, 'credit': 10.0},
        ], date='2026-01-01')
        candidate = move.line_ids.filtered(
            lambda line: line.account_id == self.impairment,
        )
        item.discontinued_candidate_move_line_ids = [(6, 0, candidate.ids)]
        item.action_tag_discontinued()

        with self.assertRaisesRegex(
                UserError, 'frozen discontinued-operation'):
            candidate.with_user(self.core_account_manager).write({
                'account_id': self.account_cash.id,
            })

    def test_foreign_journal_is_rejected_before_action(self):
        foreign = self.env['res.company'].create({'name': 'HFS Foreign Co'})
        foreign_journal = self.env['account.journal'].create({
            'name': 'HFS Foreign Miscellaneous',
            'code': 'HFX',
            'type': 'general',
            'company_id': foreign.id,
        })
        item = self._item()
        with self.assertRaises(ValidationError):
            item.write({'journal_id': foreign_journal.id})

    def test_actions_take_row_locks(self):
        item = self._item()
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            item.action_classify()
        self.assertTrue(any(
            'eh_held_for_sale' in query and 'FOR UPDATE' in query
            for query in seen
        ))

    def test_member_create_rechecks_group_state_after_lock(self):
        group = self._group()
        line = group.line_ids
        line.unlink()
        model_class = group.__class__
        real_lock = model_class._eh_lock_for_transition

        def _classify_while_waiting(records):
            result = real_lock(records)
            if group.id in records.ids:
                self.env.cr.execute(
                    "UPDATE eh_disposal_group SET state = 'held' "
                    "WHERE id = %s", (group.id,))
                records.invalidate_recordset(['state'])
            return result

        with self.assertRaises(UserError):
            with self.env.cr.savepoint(), patch.object(
                    model_class, '_eh_lock_for_transition',
                    _classify_while_waiting):
                self.env['eh.disposal.group.line'].create({
                    'group_id': group.id,
                    'name': 'Concurrent late member',
                    'carrying_amount': 1.0,
                    'account_id': self.asset.id,
                })
