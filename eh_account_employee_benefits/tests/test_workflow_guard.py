# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the workflow state machines are enforced by eh.workflow.guard
(sudo provenance), not merely by a readonly widget.

Each guarded model exposes a ``state`` a plain user must NOT be able to
advance by a direct RPC ``write`` - that would skip the record's own action
(the manager check, the account validation, the sealed journal entry, the
activation/closing gates). The guard blocks any non-superuser write to a
guarded field; the sanctioned actions run under sudo and are unaffected.

The test env runs as SUPERUSER, so every negative assertion is made through
``with_user(a plain user)`` - as the superuser the guard is (correctly) a
no-op.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_employee_benefits', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain internal user with no elevated accounting rights. On Odoo 19
        # res.users uses group_ids (not groups_id).
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'EH Benefit Plain User',
                'login': 'eh_benefit_plain_user',
                'group_ids': [
                    (6, 0, [
                        cls.env.ref('base.group_user').id,
                        cls.env.ref('eh_account_base.group_eh_user').id,
                    ])],
            })
        except Exception:  # pragma: no cover - hardened env may forbid it
            cls.user = None

        cls.plan = cls.env['eh.benefit.plan'].create({
            'name': 'Guard Test DB Plan',
        })
        cls.valuation = cls.env['eh.benefit.valuation'].create({
            'plan_id': cls.plan.id,
            'period_end': '2026-12-31',
        })
        cls.accrual = cls.env['eh.benefit.dc.accrual'].create({
            'period_date': '2026-12-31',
            'amount': 1000.0,
        })

    def _assert_state_write_blocked(self, record, target_state):
        """A non-superuser direct write of the guarded state must be refused."""
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        self.assertEqual(record.state, 'draft')
        with self.assertRaises(AccessError):
            record.with_user(self.user).write({'state': target_state})
        # Nothing moved: the record is still draft.
        record.invalidate_recordset(['state'])
        self.assertEqual(record.state, 'draft')

    def test_plan_state_write_blocked(self):
        self._assert_state_write_blocked(self.plan, 'active')

    def test_valuation_state_write_blocked(self):
        self._assert_state_write_blocked(self.valuation, 'posted')

    def test_accrual_state_write_blocked(self):
        self._assert_state_write_blocked(self.accrual, 'posted')

    def test_keyed_closing_is_writable_but_engine_outputs_are_not(self):
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        # Closing figures are documented actuary inputs with a server-side
        # rollforward-tie constraint. Workflow guard must not make their form
        # widgets fake-editable.
        self.valuation.with_user(self.user).write({
            'closing_dbo': self.valuation.closing_dbo,
            'closing_assets': self.valuation.closing_assets,
        })
        with self.assertRaises(AccessError):
            self.valuation.with_user(self.user).write({
                'net_liability': 999999.0,
            })
        with self.assertRaises(AccessError):
            self.valuation.with_user(self.user).write({
                'company_id': self.env.company.id,
            })
        with self.assertRaises(AccessError):
            self.accrual.with_user(self.user).write({
                'currency_id': self.env.company.currency_id.id,
            })

    def test_truthy_context_cannot_forge_move_provenance(self):
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.user).with_context(
                eh_benefit_move_capability=True,
            ).create({
                'move_type': 'entry',
                'company_id': self.env.company.id,
                'date': '2026-12-31',
                'eh_benefit_valuation_id': self.valuation.id,
            })

    def test_foreign_plan_id_cannot_be_guessed_on_child_create(self):
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        foreign_company = self.env['res.company'].create({
            'name': 'EH Benefit Hidden Company',
        })
        foreign_plan = self.env['eh.benefit.plan'].sudo().create({
            'name': 'EH Benefit Hidden Plan',
            'company_id': foreign_company.id,
        })
        with self.assertRaises(AccessError):
            self.env['eh.benefit.valuation'].with_user(self.user).create({
                'plan_id': foreign_plan.id,
                'period_end': '2027-12-31',
            })

    def test_child_write_locks_parent_before_freeze_decision(self):
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            self.valuation.write({'notes': 'lock regression'})
        plan_lock = next(
            i for i, query in enumerate(queries)
            if 'eh_benefit_plan' in query and 'FOR UPDATE' in query
        )
        valuation_lock = next(
            i for i, query in enumerate(queries)
            if 'eh_benefit_valuation' in query and 'FOR UPDATE' in query
        )
        self.assertLess(plan_lock, valuation_lock)

    def test_sudo_action_path_still_writes_state(self):
        # Positive path: the sanctioned action (which runs under sudo) moves
        # state past the guard. Proves the guard blocks only the RPC bypass,
        # not the record's own workflow.
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager'
        )
        self.plan.action_activate()
        self.assertEqual(self.plan.state, 'active')
