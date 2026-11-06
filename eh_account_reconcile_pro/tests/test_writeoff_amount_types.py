# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Write-off amount types on reconciliation rules (CE parity)."""

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_reconcile_pro.tests.common import (
    EhReconcileIntegrationTestCase,
)


@tagged('eh_account_reconcile_pro', 'integration', 'post_install',
        '-at_install')
class TestWriteoffAmountTypes(EhReconcileIntegrationTestCase):

    def _rule(self, code, amount_type, amount):
        return self.env['eh.reconciliation.rule'].create({
            'name': code,
            'code': code,
            'rule_type': 'write_off',
            'writeoff_account_id': self.account_expense.id,
            'writeoff_amount_type': amount_type,
            'writeoff_amount': amount,
        })

    def test_residual_returns_full_amount(self):
        rule = self._rule('wo_residual', 'residual', 0.0)
        line = self.make_statement_line(100.0)
        self.assertAlmostEqual(
            rule.compute_writeoff_amount(line), 100.0, places=2)

    def test_fixed_amount(self):
        rule = self._rule('wo_fixed', 'fixed', 25.0)
        line = self.make_statement_line(100.0)
        self.assertAlmostEqual(
            rule.compute_writeoff_amount(line), 25.0, places=2)

    def test_fixed_amount_capped_at_statement(self):
        rule = self._rule('wo_fixed_big', 'fixed', 150.0)
        line = self.make_statement_line(100.0)
        self.assertAlmostEqual(
            rule.compute_writeoff_amount(line), 100.0, places=2)

    def test_percentage(self):
        rule = self._rule('wo_pct', 'percentage', 10.0)
        line = self.make_statement_line(100.0)
        self.assertAlmostEqual(
            rule.compute_writeoff_amount(line), 10.0, places=2)

    def test_fixed_requires_positive_amount(self):
        with self.assertRaises(ValidationError):
            self._rule('wo_zero', 'fixed', 0.0)

    def test_percentage_capped_at_100(self):
        with self.assertRaises(ValidationError):
            self._rule('wo_over', 'percentage', 150.0)
