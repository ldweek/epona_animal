# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Headless-browser proof for the functional Collections onboarding tour."""

from odoo.tests import HttpCase, tagged


@tagged('eh_tour', 'eh_account_collections', 'post_install', '-at_install')
class TestCollectionsTour(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env['res.partner'].create({
            'name': 'Collections Tour Customer',
            'email': 'collections-tour@example.com',
        })
        cls.case = cls.env['eh.collections.case'].create({
            'partner_id': partner.id,
            'days_overdue_max': 45,
            'total_overdue_amount': 1250.0,
        })

    def test_collections_onboarding_tour_navigates_real_controls(self):
        self.start_tour(
            '/odoo/action-eh_account_collections.action_eh_collections_case',
            'eh_collections_setup_tour',
            login='admin',
        )
