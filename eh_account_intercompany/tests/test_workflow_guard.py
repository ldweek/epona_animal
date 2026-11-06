# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression guard: the elimination batch state machine cannot be
skipped by a direct RPC/ORM write.

The defect this closes: a state machine enforced only in the UI (a
readonly statusbar and a write-guard that blocks LEAVING a posted batch)
still lets any user with model write access RPC-write
``write({'state': 'posted'})`` straight past ``action_post`` and its
sealed elimination journal entry. eh.ic.elimination.batch now inherits
eh.workflow.guard, so 'state' may only change through the record's own
actions (which run as su); a direct non-superuser write is refused.

The test runner env is SUPERUSER (env.su True), which would sail past the
guard, so the negative path MUST run as a non-superuser (with_user). A
manager user is used so the model-level ACL grants write and the failure
we observe is the workflow guard itself, not a plain access-rights denial.
"""

import importlib.util
from pathlib import Path

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_intercompany', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Guard Sister B',
            'currency_id': cls.company_a.currency_id.id,
        })
        cls.company_elim = cls.env['res.company'].create({
            'name': 'Guard Consolidation Workspace',
            'currency_id': cls.company_a.currency_id.id,
            'eh_ic_consolidation_only': True,
        })

        # A draft batch, created as superuser (the sanctioned path).
        cls.doc = cls.env['eh.ic.elimination.batch'].create({
            'company_a_id': cls.company_a.id,
            'company_b_id': cls.company_b.id,
            'period_from': '2026-01-01',
            'period_to': '2026-01-31',
            'elimination_company_id': cls.company_elim.id,
        })

        # A non-superuser manager: model ACL grants write, so a direct
        # state write reaching the ORM proves the WORKFLOW guard blocks it,
        # not a coarse access-rights denial. Odoo 19 res.users uses
        # group_ids (not groups_id).
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'Guard Manager',
                'login': 'eh_guard_manager',
                'company_id': cls.company_a.id,
                'group_ids': [(4, cls.env.ref('base.group_user').id),
                              (4, cls.env.ref(
                                  'eh_account_base.group_eh_manager').id)],
            })
            # Grant both sister companies via attribute assignment rather than
            # a company_ids create-command: the 16/17 backport rewrites a
            # 'company_ids': [(6, 0, [...])] create-val (it assumes the single
            # -company account.account form) and mangles a two-element list.
            # res.users.company_ids exists on 16-19, so this attribute write is
            # valid on every version and is left untouched by the transform.
            cls.user.company_ids = (
                cls.company_a + cls.company_b + cls.company_elim)
        except Exception:  # noqa: BLE001
            cls.user = False

    def test_direct_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC past action_post into 'posted'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.doc.state, 'draft')
        with self.assertRaises(AccessError):
            self.doc.with_user(self.user).write({'state': 'posted'})
        # The forged transition did not take: still draft.
        self.assertEqual(self.doc.state, 'draft')

    def test_sudo_state_write_passes(self):
        """The sanctioned server path (su) writes state normally."""
        self.assertEqual(self.doc.state, 'draft')
        self.doc.sudo().write({'state': 'computed'})
        self.assertEqual(self.doc.state, 'computed')

    def test_member_legal_entity_cannot_be_elimination_workspace(self):
        with self.assertRaisesRegex(
                ValidationError, 'distinct entity marked Consolidation-only'):
            self.env['eh.ic.elimination.batch'].create({
                'company_a_id': self.company_a.id,
                'company_b_id': self.company_b.id,
                'period_from': '2026-02-01',
                'period_to': '2026-02-28',
                'elimination_company_id': self.company_a.id,
            })

    def test_upgrade_quarantines_legacy_destination_and_stock_fraction(self):
        line = self.env['eh.ic.unrealised.line'].sudo().with_context(
            eh_ic_elim_engine=True,
        ).create({
            'batch_id': self.doc.id,
            'quantity': 2.0,
            'margin': 100.0,
            'remaining_fraction': 0.5,
            'inventory_evidence_date': self.doc.period_to,
            'inventory_evidence_reference': 'UNPROVABLE-LEGACY-QUANT',
            'inventory_evidence_reviewed': True,
        })
        self.env.cr.execute(
            "UPDATE eh_ic_elimination_batch "
            "SET elimination_company_id = %s, state = 'computed' "
            "WHERE id = %s",
            (self.company_a.id, self.doc.id),
        )
        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.0' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_ic_migration_1_3_0', str(migration_path))
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.1.2.6')

        self.doc.invalidate_recordset()
        line.invalidate_recordset()
        self.assertTrue(self.doc.statutory_ledger_quarantined)
        self.assertIn(
            'Historical journals were preserved',
            self.doc.statutory_ledger_quarantine_note,
        )
        self.assertFalse(line.inventory_evidence_reviewed)
        self.assertFalse(line.inventory_evidence_date)
        self.assertFalse(line.inventory_evidence_reference)
