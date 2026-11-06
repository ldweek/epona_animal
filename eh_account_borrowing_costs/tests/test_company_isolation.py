# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Company-isolation regression for the borrowing-cost child models.

The parent eh.borrowing.cost has a company record rule, but a direct search on
the eh.borrowing.cost.line / eh.borrowing.cost.suspension children carries no
implicit multi-company filter. Without their own global ir.rule an accounting
user belonging only to Company A could read (and, given CRUD access, rewrite)
another company's IAS 23 capitalisation base through the child models. These
tests exercise the isolation as a genuine non-superuser: the test env is
superuser, for which record rules are correctly a no-op.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_borrowing_costs', 'post_install', '-at_install')
class TestBorrowingCostCompanyIsolation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Company A is the seeded cls.company; add a second tenant.
        cls.company_b = cls.env['res.company'].create({'name': 'BC Company B'})

        # A borrowing cost in each company, each with a dated-expenditure line
        # and a suspension span (the two child models under test).
        cls.bc_a = cls.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'Plant A',
            'company_id': cls.company.id,
            'expenditure_line_ids': [(0, 0, {
                'date': '2026-01-10', 'amount': 1000.0, 'label': 'A line'})],
            'suspension_line_ids': [(0, 0, {
                'date_start': '2026-03-01', 'date_end': '2026-03-10',
                'label': 'A suspension'})],
        })
        cls.bc_b = cls.env['eh.borrowing.cost'].create({
            'name': '/', 'qualifying_asset': 'Plant B',
            'company_id': cls.company_b.id,
            'expenditure_line_ids': [(0, 0, {
                'date': '2026-01-10', 'amount': 9999.0, 'label': 'B line'})],
            'suspension_line_ids': [(0, 0, {
                'date_start': '2026-03-01', 'date_end': '2026-03-10',
                'label': 'B suspension'})],
        })
        cls.line_a = cls.bc_a.expenditure_line_ids
        cls.line_b = cls.bc_b.expenditure_line_ids
        cls.susp_a = cls.bc_a.suspension_line_ids
        cls.susp_b = cls.bc_b.suspension_line_ids

        # A plain accounting user assigned to Company A only. Restrict to a
        # single company (singular company_id) so its allowed companies are
        # exactly [A] and the record rule filters Company B out.
        cls.user_a = cls.env['res.users'].create({
            'name': 'BC User A',
            'login': 'bc_iso_user_a',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def test_line_read_is_company_scoped(self):
        Line = self.env['eh.borrowing.cost.line'].with_user(self.user_a)
        visible = Line.search([])
        self.assertIn(self.line_a, visible,
                      "User must see its own company's expenditure lines.")
        self.assertNotIn(
            self.line_b, visible,
            "Company B's expenditure lines must not leak to a Company A user.")

    def test_suspension_read_is_company_scoped(self):
        Susp = self.env['eh.borrowing.cost.suspension'].with_user(self.user_a)
        visible = Susp.search([])
        self.assertIn(self.susp_a, visible,
                      "User must see its own company's suspension spans.")
        self.assertNotIn(
            self.susp_b, visible,
            "Company B's suspension spans must not leak to a Company A user.")

    def test_cannot_write_other_company_line(self):
        # The record rule hides Company B's line from a Company A user, so a
        # direct write to it is refused rather than silently distorting B's
        # weighted-average base.
        with self.assertRaises(AccessError):
            self.line_b.with_user(self.user_a).write({'amount': 1.0})
        # Untouched.
        self.assertEqual(self.line_b.amount, 9999.0)

    def test_cannot_write_other_company_suspension(self):
        with self.assertRaises(AccessError):
            self.susp_b.with_user(self.user_a).write(
                {'date_end': '2026-03-20'})
