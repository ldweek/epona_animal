# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the IAS 19 benefit plan register.

Runs the eh_employee_benefits_test_tour in a real headless Chrome via
HttpCase. The eh_tour tag keeps browser cycles out of the default
per-module runs; the matrix runner selects them with --tours (test-tags
eh_tour).
"""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_employee_benefits', 'post_install',
        '-at_install')
class TestEmployeeBenefitsTour(HttpCase):

    def test_benefit_plan_create_activate_tour(self):
        # The Heritage menus are gated to the EH groups; the seeding
        # migration grants them to existing accounting managers on install,
        # but a bare test database may not have run it for admin.
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_manager')
        before = self.env['eh.benefit.plan'].search([])
        self.start_tour('/odoo', 'eh_employee_benefits_test_tour',
                        login='admin')
        plan = self.env['eh.benefit.plan'].search([]) - before
        self.assertEqual(len(plan), 1,
                         'tour did not create the benefit plan record')
        self.assertEqual(plan.name, 'Group Pension Plan')
        self.assertEqual(plan.country_note, 'Head office jurisdiction')
        self.assertEqual(plan.state, 'active',
                         'tour did not activate the benefit plan')
        self.assertTrue(plan.funded, 'funded default should remain set')
