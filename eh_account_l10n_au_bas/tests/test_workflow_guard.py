# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Workflow-guard regression: eh.bas.run state (and the lodgement stamps)
may only change through the record's own actions, which run under sudo.

A plain user with write access to eh.bas.run must not be able to skip
action_compute / action_mark_lodged (and their audit trail) by RPC-writing
write({'state': 'lodged'}) directly. The eh.workflow.guard mixin refuses
such a write for any non-superuser. The test env runs as SUPERUSER, so the
bypass is exercised through with_user(a plain, non-super user).
"""

from datetime import date

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_l10n_au_bas', 'post_install', '-at_install')
class BasRunWorkflowGuardTest(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        au = cls.env.ref('base.au')
        cls.env.company.sudo().write({
            'country_id': au.id,
            'account_fiscal_country_id': au.id,
        })
        # A plain, non-superuser operator who legitimately has write access
        # to eh.bas.run (group_eh_user grants write) but must still be barred
        # from RPC-writing the guarded state field.
        group = cls.env.ref('eh_account_base.group_eh_user', raise_if_not_found=False)
        cls.plain_user = False
        if group:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'BAS Plain User',
                'login': 'bas_plain_user_guard',
                'group_ids': [(6, 0, group.ids)],
                'company_id': cls.env.company.id,
                'company_ids': [(6, 0, cls.env.company.ids)],
            })

    def _new_run(self, quarter):
        # A fresh draft run per test so state mutation in one test does not
        # leak into another (the unique(company, fy_label, quarter) constraint
        # forces a distinct quarter per record).
        return self.env['eh.bas.run'].create({
            'company_id': self.env.company.id,
            'quarter': quarter,
            'name': 'BAS guard %s' % quarter,
        })

    def test_direct_state_write_is_blocked_for_plain_user(self):
        # A plain user RPC-writing state straight to a posted (lodged) value
        # must be refused by the guard, skipping action_compute / its audit.
        if not self.plain_user:
            self.skipTest("No eh_account_base.group_eh_user available in env")
        doc = self._new_run('q1')
        self.assertEqual(doc.state, 'draft')
        with self.assertRaises(AccessError):
            doc.with_user(self.plain_user).write({'state': 'lodged'})

    def test_action_still_advances_state_as_su(self):
        # The sanctioned action path (which runs under sudo) must still move
        # the state normally: the guard blocks only direct writes.
        doc = self._new_run('q2')
        doc.action_compute()
        self.assertEqual(doc.state, 'computed')
