# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Headless-browser verification for the financial dashboard."""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_dashboard', 'post_install', '-at_install')
class TestDashboardTour(HttpCase):

    def test_dashboard_menu_snapshot_refresh_tour(self):
        admin = self.env.ref('base.user_admin')
        admin.group_ids |= self.env.ref('eh_account_base.group_eh_user')

        self.start_tour('/odoo', 'eh_dashboard_test_tour', login='admin')

        dashboard = self.env['eh.account.dashboard'].sudo().search([
            ('user_id', '=', admin.id),
            ('company_id', '=', admin.company_id.id),
        ])
        self.assertEqual(len(dashboard), 1)
        self.assertEqual(dashboard.user_id, admin)
        self.assertEqual(dashboard.company_id, admin.company_id)
