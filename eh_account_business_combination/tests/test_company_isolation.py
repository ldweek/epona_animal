# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company-isolation regression for the business-combination child models.

The parent eh.business.combination (and eh.bizcombo.adjustment) each carry a
company record rule, but a direct search on the eh.business.combination.asset /
eh.bizcombo.adjustment.line children carried no implicit multi-company filter.
Without their own global ir.rule an accounting user belonging only to Company A
could read (and, given CRUD access, rewrite) another company's IFRS 3.18
purchase-price-allocation fair values and IFRS 3.45-49 measurement-period
restatement amounts through the child models. These tests exercise the
isolation as a genuine non-superuser: the test env is superuser, for which
record rules are correctly a no-op.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_business_combination', 'post_install', '-at_install')
class TestBusinessCombinationCompanyIsolation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Company A is the seeded cls.company; add a second tenant.
        cls.company_b = cls.env['res.company'].create({'name': 'BC Company B'})

        # An identifiable-asset account in each company. account.account is
        # multi-company (company_ids) in Odoo 18+ and single-company
        # (company_id) before that, so resolve the binding field at runtime to
        # stay cross-version safe and satisfy check_company on the asset line.
        Account = cls.env['account.account']
        multi = 'company_ids' in Account._fields
        cls.acct_a = cls._ensure_account(
            cls.env, '1600', 'PPE A', 'asset_fixed')
        if multi:
            cls.acct_b = Account.create({
                'code': '1600B', 'name': 'PPE B',
                'account_type': 'asset_fixed',
                'company_ids': [(6, 0, cls.company_b.ids)]})
        else:
            cls.acct_b = Account.create({
                'code': '1600B', 'name': 'PPE B',
                'account_type': 'asset_fixed',
                'company_id': cls.company_b.id})

        # A combination in each company, each carrying one identifiable-asset
        # line and one draft measurement-period adjustment with a line delta
        # (the two child models under test).
        cls.combo_a = cls.env['eh.business.combination'].create({
            'name': '/', 'acquiree_name': 'Target A',
            'company_id': cls.company.id,
            'asset_line_ids': [(0, 0, {
                'name': 'PPE A', 'account_id': cls.acct_a.id,
                'fair_value': 1000.0})]})
        cls.combo_b = cls.env['eh.business.combination'].create({
            'name': '/', 'acquiree_name': 'Target B',
            'company_id': cls.company_b.id,
            'asset_line_ids': [(0, 0, {
                'name': 'PPE B', 'account_id': cls.acct_b.id,
                'fair_value': 9999.0})]})
        cls.asset_a = cls.combo_a.asset_line_ids
        cls.asset_b = cls.combo_b.asset_line_ids

        cls.adj_a = cls.env['eh.bizcombo.adjustment'].create({
            'combination_id': cls.combo_a.id, 'name': 'New info A',
            'line_ids': [(0, 0, {
                'asset_line_id': cls.asset_a.id,
                'revised_fair_value': 1100.0})]})
        cls.adj_b = cls.env['eh.bizcombo.adjustment'].create({
            'combination_id': cls.combo_b.id, 'name': 'New info B',
            'line_ids': [(0, 0, {
                'asset_line_id': cls.asset_b.id,
                'revised_fair_value': 8888.0})]})
        cls.adjline_a = cls.adj_a.line_ids
        cls.adjline_b = cls.adj_b.line_ids

        # A plain accounting user assigned to Company A only. Restrict to a
        # single company (singular company_id) so its allowed companies are
        # exactly [A] and the record rule filters Company B out.
        cls.user_a = cls.env['res.users'].create({
            'name': 'BC User A',
            'login': 'bc_iso_user_a',
            'company_id': cls.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_asset_line_read_is_company_scoped(self):
        Asset = self.env['eh.business.combination.asset'].with_user(
            self.user_a)
        visible = Asset.search([])
        self.assertIn(self.asset_a, visible,
                      "User must see its own company's identifiable lines.")
        self.assertNotIn(
            self.asset_b, visible,
            "Company B's identifiable-asset fair values must not leak to a "
            "Company A user.")

    def test_adjustment_line_read_is_company_scoped(self):
        Line = self.env['eh.bizcombo.adjustment.line'].with_user(self.user_a)
        visible = Line.search([])
        self.assertIn(self.adjline_a, visible,
                      "User must see its own company's restatement lines.")
        self.assertNotIn(
            self.adjline_b, visible,
            "Company B's measurement-period restatement amounts must not "
            "leak to a Company A user.")

    def test_cannot_write_other_company_asset_line(self):
        # The record rule hides Company B's line from a Company A user, so a
        # direct write to it is refused rather than silently distorting B's
        # purchase-price allocation.
        with self.assertRaises(AccessError):
            self.asset_b.with_user(self.user_a).write({'fair_value': 1.0})
        self.assertEqual(self.asset_b.fair_value, 9999.0)

    def test_cannot_write_other_company_adjustment_line(self):
        with self.assertRaises(AccessError):
            self.adjline_b.with_user(self.user_a).write(
                {'revised_fair_value': 1.0})
        self.assertEqual(self.adjline_b.revised_fair_value, 8888.0)
