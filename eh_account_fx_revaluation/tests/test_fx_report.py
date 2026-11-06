# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation report render regression.

The two shipped bugs on the front-end PDF reports were missing render
tests: nothing exercised the QWeb template, so a KeyError or bad
expression in the template only surfaced when a user clicked Print.
This proves the FX revaluation report renders to HTML for a real run.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .compat import add_user_groups


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestFxReport(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user,
            cls.env.ref('account.group_account_manager'),
            cls.env.ref('eh_account_base.group_eh_manager'),
        )

        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

    def test_report_renders_html(self):
        """action_report_fx_revaluation renders non-empty HTML for a run.

        Seeds a minimal valid eh.fx.revaluation.run (same fields the
        sibling model tests use), adds a populated report line, renders via
        _render_qweb_html so there
        is no wkhtmltopdf dependency, and asserts the QWeb template
        produced output. A non-empty body proves the template evaluated
        without a KeyError or render failure; the stable title string
        confirms the run's page was actually emitted.
        """
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
            'reversal_date': '2026-04-07',
        })
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.env['eh.fx.revaluation.line'].sudo().create({
            'run_id': run.id,
            'account_id': self.account_receivable.id,
            'partner_id': self.partner_a.id,
            'foreign_currency_id': eur.id,
            'balance_foreign': 1234.56,
            'balance_company': 1300.0,
            'closing_rate': 1.1,
            'new_balance_company': 1358.02,
            'adjustment': 58.02,
            'nature': 'gain',
            'source_line_count': 1,
        })
        report = self.env.ref(
            'eh_account_fx_revaluation.action_report_fx_revaluation')
        html, ftype = report._render_qweb_html(report.report_name, run.ids)
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'FX Revaluation', html)
        self.assertIn(b'Revaluation lines', html)
        self.assertIn(b'EUR', html)
        self.assertIn(self.account_receivable.code.encode(), html)
        self.assertIn(self.partner_a.name.encode(), html)
        self.assertIn(b'Settled revaluation-date adjustment', html)
        self.assertIn(b'Reversal date', html)
        self.assertIn(b'2026', html)
