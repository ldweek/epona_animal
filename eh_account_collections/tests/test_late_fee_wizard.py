# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Late-fee wizard tax tests.

A late-fee invoice is a tax invoice. In a GST-registered jurisdiction it
must carry output tax on the fee line; issuing it with no tax understates
output GST. These tests pin that the wizard applies the company's default
sale tax to the fee line, that an explicit tax selection wins, and that the
byte-identical no-tax fallback holds when the company configures no default.
"""

from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestLateFeeWizardTax(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.Wizard = cls.env['eh.collections.late_fee.wizard']
        cls.Tax = cls.env['account.tax']
        # account.tax.country_id is NOT NULL on Odoo 16, and posting refuses a
        # tax whose country differs from the company fiscal country. Resolve a
        # single fiscal country, pin it on the company when blank, and stamp it
        # on every test tax.
        au_us = cls.env.ref('base.us')
        if not cls.company.country_id:
            cls.company.sudo().country_id = au_us.id
        cls._fiscal_country = (
            cls.company.account_fiscal_country_id
            or cls.company.country_id or au_us)
        # A 10% output (sale) tax standing in for GST.
        cls.gst = cls._make_sale_tax('GST 10% (test)', 10.0)

    @classmethod
    def _make_sale_tax(cls, name, amount):
        vals = {
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        }
        if 'country_id' in cls.Tax._fields:
            vals['country_id'] = cls._fiscal_country.id
        return cls.Tax.create(vals)

    def _make_case(self, total=1000.0, days=45, seed_ledger=True):
        if seed_ledger:
            self._post_overdue_receivable(total, days)
        return self.Case.create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'total_overdue_amount': total,
            'oldest_overdue_date': date.today() - timedelta(days=days),
            'days_overdue_max': days,
        })

    def _make_wizard(self, case, **overrides):
        vals = {
            'case_id': case.id,
            'fee_mode': 'flat',
            'flat_amount': 50.0,
            'income_account_id': self.account_revenue.id,
            'journal_id': self.journal_sale.id,
        }
        vals.update(overrides)
        return self.Wizard.create(vals)

    def _post_overdue_receivable(self, amount, days_overdue):
        today = date.today()
        return self.post_balanced_move([
            {
                'account': self.account_receivable,
                'debit': amount,
                'partner': self.partner_a,
                'date_maturity': today - timedelta(days=days_overdue),
            },
            {'account': self.account_revenue, 'credit': amount},
        ], date=today - timedelta(days=days_overdue + 1))

    def test_percent_fee_weights_each_overdue_item_by_its_own_age(self):
        self._post_overdue_receivable(100.0, 60)
        self._post_overdue_receivable(900.0, 10)
        case = self._make_case(
            total=1000.0,
            days=60,
            seed_ledger=False,
        )
        wizard = self._make_wizard(
            case,
            fee_mode='percent',
            percent_per_month=1.0,
            today=date.today(),
        )
        # (100 * 60 + 900 * 10) exposure-days * 1% / 30 = 5.00.
        # Old logic charged all 1,000 for 60 days = 20.00.
        self.assertEqual(wizard.overdue_amount, 1000.0)
        self.assertEqual(wizard.days_overdue, 60)
        self.assertEqual(wizard.computed_fee, 5.0)

    def test_percent_fee_revalidates_live_ledger_when_issued(self):
        self._post_overdue_receivable(100.0, 30)
        case = self._make_case(total=100.0, days=30, seed_ledger=False)
        wizard = self._make_wizard(
            case,
            fee_mode='percent',
            percent_per_month=1.0,
            today=date.today(),
        )
        self.assertEqual(wizard.computed_fee, 1.0)

        # Change the ledger after the preview was cached. Issue must use the
        # authoritative 300 exposure, not the stale 100 preview.
        self._post_overdue_receivable(200.0, 30)
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(wizard.overdue_amount, 300.0)
        self.assertEqual(wizard.computed_fee, 3.0)
        self.assertEqual(move.invoice_line_ids.price_unit, 3.0)

    def test_default_sale_tax_applied_to_fee_line(self):
        """With a company default sale tax set, the fee line carries it.

        This FAILS before the fix: the line was created with no tax_ids,
        so a GST-registered issuer produced a tax invoice with zero output
        tax.
        """
        self.company.account_sale_tax_id = self.gst
        case = self._make_case()
        wizard = self._make_wizard(case)
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        fee_line = move.invoice_line_ids
        self.assertEqual(
            fee_line.tax_ids, self.gst,
            "Late-fee line must carry the company default sale tax so a "
            "GST-registered issuer charges output tax.",
        )
        # And the move actually accrues output tax on that base.
        self.assertGreater(
            move.amount_tax, 0.0,
            "A late-fee tax invoice must show non-zero output tax.",
        )

    def test_explicit_tax_selection_wins(self):
        other = self._make_sale_tax('GST 15% (test)', 15.0)
        self.company.account_sale_tax_id = self.gst
        case = self._make_case()
        wizard = self._make_wizard(case, tax_ids=[(6, 0, other.ids)])
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        self.assertEqual(
            move.invoice_line_ids.tax_ids, other,
            "An explicit tax selection must override the company default.",
        )

    def test_no_default_tax_keeps_line_untaxed(self):
        """Byte-identical fallback: no company default sale tax means no tax.

        Pre-existing behaviour where the jurisdiction is not GST-registered
        (or the company simply has no default sale tax) must be unchanged.
        """
        self.company.account_sale_tax_id = False
        case = self._make_case()
        wizard = self._make_wizard(case)
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        self.assertFalse(
            move.invoice_line_ids.tax_ids,
            "With no default sale tax and no explicit selection, the fee "
            "line must stay untaxed.",
        )

    def test_issue_is_idempotent_and_writes_invoice_linked_action(self):
        case = self._make_case()
        first = self._make_wizard(case)
        result = first.action_apply()
        move = self.env['account.move'].browse(result['res_id'])
        evidence = case.action_ids.filtered(
            lambda action: action.action_type == 'late_fee',
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence.late_fee_move_id, move)
        self.assertEqual(evidence.user_id, self.env.user)
        self.assertTrue(evidence.late_fee_key)

        with self.assertRaisesRegex(AccessError, 'issuance wizard'):
            self.env['eh.collections.action'].with_user(
                self.env.ref('base.user_admin'),
            ).create({
                'case_id': case.id,
                'action_type': 'late_fee',
                'summary': 'Forged evidence',
                'late_fee_move_id': move.id,
                'late_fee_key': 'forged-key',
            })

        move_count = self.env['account.move'].search_count([
            ('move_type', '=', 'out_invoice'),
            ('partner_id', '=', case.partner_id.id),
        ])
        duplicate = self._make_wizard(case)
        with self.assertRaisesRegex(UserError, 'already been issued'):
            duplicate.action_apply()
        self.assertEqual(
            self.env['account.move'].search_count([
                ('move_type', '=', 'out_invoice'),
                ('partner_id', '=', case.partner_id.id),
            ]),
            move_count,
        )
        self.assertEqual(len(case.action_ids.filtered(
            lambda action: action.action_type == 'late_fee',
        )), 1)

    # -- Cross-version blank-income fallback (self-audit finding) ------------

    def _income_account_in(self, company, code):
        """Create an income account scoped to `company`, version-aware."""
        Account = self.env['account.account']
        multi = 'company_ids' in Account._fields
        vals = {
            'code': code,
            'name': 'Fallback Income %s' % code,
            'account_type': 'income',
        }
        if multi:
            vals['company_ids'] = [(6, 0, company.ids)]
        else:
            vals['company_id'] = company.id
        return Account.create(vals)

    def test_blank_income_fallback_resolves_and_posts(self):
        """Leaving income_account_id blank must resolve a company income
        account via the fallback search and post the fee.

        This is the branch the rest of the suite never exercises. Before the
        cross-version fix the fallback search used a bare
        ('company_ids', '=', ...) leaf, which raises "Invalid field
        'company_ids' on model 'account.account'" on Odoo 16/17 (the field is
        single-company there). On a backported 16/17 run this test drives the
        version-safe else-branch and passes; pre-fix it aborted the wizard's
        primary button.
        """
        case = self._make_case()
        # No income_account_id -> exercise the fallback search.
        wizard = self.Wizard.create({
            'case_id': case.id,
            'fee_mode': 'flat',
            'flat_amount': 50.0,
            'journal_id': self.journal_sale.id,
        })
        self.assertFalse(wizard.income_account_id)
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        income = move.invoice_line_ids.account_id
        self.assertTrue(income, "Fallback must resolve an income account.")
        self.assertIn(
            income.account_type, ('income', 'income_other'),
            "Fallback must resolve an income-type account.",
        )

    def test_blank_income_fallback_stays_company_scoped(self):
        """The version-safe leaf must still honour company isolation: the
        fallback must never pick another company's income account.

        A distractor income account is created in a second company with a
        code that sorts BEFORE the wizard company's own income account, so an
        UNSCOPED search (limit=1, default order) would return the distractor.
        The scoped leaf must keep the pick inside the wizard's company on
        every series (company_ids on 18/19, company_id on 16/17).
        """
        other_company = self.env['res.company'].create({
            'name': 'Other Co (late-fee scope test)',
        })
        distractor = self._income_account_in(other_company, '0001')
        case = self._make_case()
        wizard = self.Wizard.create({
            'case_id': case.id,
            'fee_mode': 'flat',
            'flat_amount': 50.0,
            'journal_id': self.journal_sale.id,
        })
        action = wizard.action_apply()
        move = self.env['account.move'].browse(action['res_id'])
        income = move.invoice_line_ids.account_id
        self.assertNotEqual(
            income, distractor,
            "Fallback must not pick another company's income account; the "
            "company-scope leaf must survive the version guard.",
        )
        Account = self.env['account.account']
        if 'company_ids' in Account._fields:
            self.assertIn(
                self.company, income.company_ids,
                "Resolved income account must be scoped to the wizard "
                "company.",
            )
        else:
            self.assertEqual(
                income.company_id, self.company,
                "Resolved income account must be scoped to the wizard "
                "company.",
            )
