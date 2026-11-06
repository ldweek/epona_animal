# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: the recurring-template state machine cannot be
driven by a direct RPC/ORM write.

The systemic defect this closes: a state machine enforced only in the UI (a
readonly statusbar) is not protected, because a draft's state is not frozen. A
low-privilege user could RPC ``write({'state': 'active'})`` straight past
``action_activate`` (skipping its line-present check), or
``write({'state': 'finished'})`` to relabel the record. The shared
``eh.workflow.guard`` blocks every write to the guarded ``state`` field unless
it originates from one of the record's own actions (which run under sudo).
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_recurring_invoices', 'post_install', '-at_install')
class TestRecurringWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['eh.recurring.invoice.template']
        cls.sale_journal = cls.env['account.journal'].search(
            [('company_id', '=', cls.company.id), ('type', '=', 'sale')],
            limit=1,
        )
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Sales',
                'code': 'SALE',
                'type': 'sale',
                'company_id': cls.company.id,
            })
        cls.partner = cls.env['res.partner'].create({'name': 'Recurring Cust'})
        # A non-superuser operational user: has read/write ACL on the template
        # (group_eh_user) but is NOT the superuser, so the guard fires.
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Recurring Operator',
                'login': 'eh_recurring_operator',
                'company_id': cls.env.company.id,
                'company_ids': [(6, 0, cls.env.company.ids)],
                'group_ids': [(6, 0, [
                    cls.env.ref('eh_account_base.group_eh_user').id,
                ])],
            })
        except Exception:
            cls.plain_user = False

    def _draft_template(self):
        return self.Template.create({
            'name': 'Monthly support',
            'code': 'guard_monthly',
            'partner_id': self.partner.id,
            'journal_id': self.sale_journal.id,
            'line_ids': [(0, 0, {
                'name': 'Support fee',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })

    def test_direct_state_write_refused_for_plain_user(self):
        """A plain user cannot RPC the draft straight to 'active', skipping
        action_activate's line-present check."""
        if not self.plain_user:
            self.skipTest("No non-superuser user could be created in this env.")
        rec = self._draft_template()
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'active'})
        # State is untouched; the workflow was not bypassed.
        self.assertEqual(rec.state, 'draft')

    def test_direct_state_write_to_finished_refused(self):
        """Relabelling to the terminal state directly is refused too."""
        if not self.plain_user:
            self.skipTest("No non-superuser user could be created in this env.")
        rec = self._draft_template()
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'finished'})
        self.assertEqual(rec.state, 'draft')

    def test_action_path_still_works(self):
        """The sanctioned action still moves the state (guard is scoped to
        direct writes, not the record's own methods)."""
        rec = self._draft_template()
        rec.action_activate()
        self.assertEqual(rec.state, 'active')

    def test_line_edit_takes_same_parent_lock_as_generation(self):
        rec = self._draft_template()
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            rec.line_ids.write({'price_unit': 125.0})
        self.assertTrue(any(
            'eh_recurring_invoice_template' in query
            and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_generated_period_provenance_is_server_owned(self):
        rec = self._draft_template()
        rec.action_generate_now()
        move = rec.last_generated_move_id
        with self.assertRaises(AccessError):
            move.write({'eh_recurring_period_end': move.invoice_date})
