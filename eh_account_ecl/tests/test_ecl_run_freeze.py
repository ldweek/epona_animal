# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Defense-in-depth: the run-level ORM freeze on a posted ECL run.

The ir.model.access.csv pass drops perm_unlink for a plain EH user on the
posted-figure master (eh.ecl.run) and its bucket child; the write()/unlink()
overrides on the run are the second, ORM-layer line of defence that holds even
for a manager or a raw ORM call. This proves the freeze without disturbing the
normal post / reverse flow.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'integration', 'post_install', '-at_install')
class TestEclRunFreeze(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5291', 'Impairment Loss FZ', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1291', 'Loss Allowance FZ', 'asset_current')

    def _posted_run(self):
        run = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'reversal_date': '2026-06-30',
            'opening_allowance': 0.0,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, {
                'name': '90+', 'days_from': 91, 'days_to': 0,
                'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0,
            })],
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        return run

    def test_posted_run_input_frozen_at_write_layer(self):
        """A measurement / input field cannot be written once posted."""
        run = self._posted_run()
        with self.assertRaises(UserError):
            run.write({'reporting_date': '2026-09-30'})
        with self.assertRaises(UserError):
            run.write({'loss_allowance_account_id': self.impairment.id})

    def test_posted_run_cannot_be_unlinked(self):
        run = self._posted_run()
        with self.assertRaises(UserError):
            run.unlink()

    def test_post_then_reverse_flow_still_works(self):
        """The state-only transition writes carry no frozen field and pass."""
        run = self._posted_run()
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
        # A reversed run is still frozen for input edits and deletes.
        with self.assertRaises(UserError):
            run.write({'reporting_date': '2026-12-31'})
        with self.assertRaises(UserError):
            run.unlink()

    def test_draft_run_stays_editable(self):
        """The freeze must not touch a draft run: inputs and delete stay open."""
        run = self.env['eh.ecl.run'].create({
            'reporting_date': '2026-06-30',
            'loss_allowance_account_id': self.allowance.id,
            'impairment_expense_account_id': self.impairment.id,
            'journal_id': self.journal_misc.id,
        })
        run.write({'reporting_date': '2026-07-31'})
        self.assertEqual(str(run.reporting_date), '2026-07-31')
        run.unlink()
        self.assertFalse(run.exists())
