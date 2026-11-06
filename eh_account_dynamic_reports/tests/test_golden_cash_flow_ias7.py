# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IAS 7 golden tests for the Cash Flow Statement (Phase 6).

Hand-derived worked examples for:

* the real indirect method (profit before tax start, tag-resolved non-cash
  add-backs, working-capital deltas) and its exact tie to the direct
  method's operating total on the same ledger;
* the IAS 7.31-35 disclosed flows (interest paid / received, dividends
  paid / received, income taxes paid) extracted from tagged journal items,
  including the tax-authority fallback;
* the IAS 7.31 presentation policy (options override > company field >
  default) flipping section placement without changing net change;
* the IAS 7.43 non-cash transaction register memo section.

Plus a property layer: seeded random posting mixes built from tag-complete
templates, asserting the indirect operating total equals the direct
operating total within currency rounding on every draw. The tie is the
invariant; a failure message carries the seed and the template trace so
the exact ledger replays.

Convention notes (golden rule 3): amounts are exact-2dp; income posts as
credit (negative balance); the report's sign convention is cash inflow
positive, outflow negative; working-capital delta rows present the cash
effect (an increase in payables is a positive row).
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


class CashFlowIas7Case(EhGoldenTestCase):
    """Shared fixtures for the IAS 7 cash flow golden / property tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.cash_flow']
        # Non-current / P&L fixtures beyond the common chart.
        cls.account_fixed_asset = cls._ensure_account(
            cls.env, '1500', 'Equipment', 'asset_fixed')
        cls.account_accum_dep = cls._ensure_account(
            cls.env, '1590', 'Accumulated Depreciation', 'asset_fixed')
        cls.account_accum_imp = cls._ensure_account(
            cls.env, '1591', 'Accumulated Impairment', 'asset_fixed')
        cls.account_investment = cls._ensure_account(
            cls.env, '1700', 'Equity Investments', 'asset_non_current')
        cls.account_lt_deposit = cls._ensure_account(
            cls.env, '1800', 'Long Term Deposits', 'asset_non_current')
        cls.account_lt_loan = cls._ensure_account(
            cls.env, '2500', 'Long Term Loan', 'liability_non_current')
        cls.account_prov_liab = cls._ensure_account(
            cls.env, '2600', 'Provision Make Good', 'liability_non_current')
        cls.account_tax_payable = cls._ensure_account(
            cls.env, '2205', 'Income Tax Payable', 'liability_current')
        cls.account_dividends = cls._ensure_account(
            cls.env, '3100', 'Dividends Declared', 'equity')
        cls.account_dep_expense = cls._ensure_account(
            cls.env, '5100', 'Depreciation Expense', 'expense_depreciation')
        cls.account_fx_loss = cls._ensure_account(
            cls.env, '6600', 'Unrealised FX Loss', 'expense')
        cls.account_prov_expense = cls._ensure_account(
            cls.env, '6700', 'Provision Expense', 'expense')
        cls.account_imp_expense = cls._ensure_account(
            cls.env, '6800', 'Impairment Loss', 'expense')
        cls.account_int_expense = cls._ensure_account(
            cls.env, '6900', 'Interest Expense', 'expense')
        cls.account_tax_expense = cls._ensure_account(
            cls.env, '6300', 'Income Tax Expense', 'expense')
        cls.account_fv_gain = cls._ensure_account(
            cls.env, '7300', 'Fair Value Gain', 'income_other')

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    # ---- helpers ----

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, date=fields.Date.from_string(date_str))

    def _tag(self, account, tag_xmlid):
        tag = self.env.ref('eh_account_dynamic_reports.%s' % tag_xmlid)
        account.sudo().write({'tag_ids': [(4, tag.id)]})

    def _compute(self, **overrides):
        return self.handler.compute(dict(self.options, **overrides))

    @staticmethod
    def _line(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    def _amount(self, result, line_id):
        line = self._line(result, line_id)
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None


@tagged('eh_golden', 'eh_account_dynamic_reports',
        'post_install', '-at_install')
class TestCashFlowIndirectGolden(CashFlowIas7Case):

    def test_indirect_ties_direct_golden(self):
        # Seed (all in-period):
        #   cash sale          Dr Cash 1000 / Cr Revenue 1000
        #   expense on credit  Dr Expense 400 / Cr Payables 400
        #   depreciation       Dr Depreciation Expense 100
        #                      / Cr Accumulated Depreciation 100 (tagged)
        #
        # Direct derivation: the only cash-affecting entry is the sale, so
        # operating = 1000.00, investing = financing = 0.
        #
        # Indirect derivation:
        #   profit before tax  = 1000 - 400 - 100          =  500.00
        #   + depreciation add-back (non-current movement) =  100.00
        #   + increase in payables                         =  400.00
        #   = cash generated from operations               = 1000.00 (exact)
        self._tag(self.account_dep_expense, 'account_tag_noncash_depreciation')
        self._post([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], '2026-03-10')
        self._post([
            {'account': self.account_expense, 'debit': 400.0},
            {'account': self.account_payable, 'credit': 400.0,
             'partner': self.partner_a},
        ], '2026-04-05')
        self._post([
            {'account': self.account_dep_expense, 'debit': 100.0},
            {'account': self.account_accum_dep, 'credit': 100.0},
        ], '2026-06-30')

        direct = self._compute()
        indirect = self._compute(cash_flow_method='indirect')

        self.assertEqual(
            direct['totals']['operating'], 1000.00,
            "direct operating expected 1000.00, got %s"
            % direct['totals'])
        self.assertEqual(
            indirect['totals']['operating'], 1000.00,
            "indirect operating expected 1000.00, got %s"
            % indirect['totals'])
        # The tie, stated explicitly as well.
        self.assertEqual(
            direct['totals']['operating'],
            indirect['totals']['operating'],
            "indirect operating must tie to direct: %s vs %s"
            % (direct['totals'], indirect['totals']))
        # Row-level derivation.
        self.assertEqual(self._amount(indirect, 'indirect-pbt'), 500.00)
        self.assertEqual(
            self._amount(indirect, 'indirect-noncash-depreciation'), 100.00)
        self.assertEqual(
            self._amount(indirect, 'indirect-wc-liability_payable'), 400.00)
        self.assertEqual(self._amount(indirect, 'indirect-cgo'), 1000.00)
        # Identity block agrees between the methods.
        self.assertEqual(
            direct['totals']['net_change_in_cash'],
            indirect['totals']['net_change_in_cash'])
        self.assertEqual(indirect['totals']['balance_check'], 0.00)
        self.assertEqual(direct['totals']['balance_check'], 0.00)

    def test_tagged_provision_to_current_liability_has_no_addback(self):
        # A provision charged to a CURRENT liability self-corrects through
        # the working-capital delta and must produce NO add-back, even when
        # the expense account carries the provisions tag - otherwise the
        # charge would be counted twice and the tie would break.
        #   Dr Provision Expense 80 / Cr Other Current Liability 80
        # Direct: no cash entry, operating = 0.
        # Indirect: PBT -80, provisions add-back 0 (no non-current
        # movement), increase in other current liabilities +80 -> 0.
        self._tag(self.account_prov_expense, 'account_tag_noncash_provisions')
        current_liab = self._ensure_account(
            self.env, '2210', 'Accrued Liabilities', 'liability_current')
        self._post([
            {'account': self.account_prov_expense, 'debit': 80.0},
            {'account': current_liab, 'credit': 80.0},
        ])
        direct = self._compute()
        indirect = self._compute(cash_flow_method='indirect')
        self.assertEqual(direct['totals']['operating'], 0.00)
        self.assertEqual(
            indirect['totals']['operating'], 0.00,
            "provision to current liability double-counted: %s"
            % indirect['totals'])
        self.assertEqual(self._amount(indirect, 'indirect-pbt'), -80.00)
        self.assertIsNone(
            self._line(indirect, 'indirect-noncash-provisions'),
            "zero add-back row must stay hidden")
        self.assertEqual(
            self._amount(indirect, 'indirect-wc-liability_current'), 80.00)

    def test_provision_to_noncurrent_liability_adds_back(self):
        # The same charge against a NON-current liability is invisible to
        # both profit-adjusted cash and the working-capital deltas, so the
        # tagged add-back must carry it:
        #   Dr Provision Expense 80 / Cr Provision (non-current) 80
        # Indirect: PBT -80, provisions add-back +80 -> operating 0 = direct.
        self._tag(self.account_prov_expense, 'account_tag_noncash_provisions')
        self._post([
            {'account': self.account_prov_expense, 'debit': 80.0},
            {'account': self.account_prov_liab, 'credit': 80.0},
        ])
        direct = self._compute()
        indirect = self._compute(cash_flow_method='indirect')
        self.assertEqual(direct['totals']['operating'], 0.00)
        self.assertEqual(indirect['totals']['operating'], 0.00)
        self.assertEqual(
            self._amount(indirect, 'indirect-noncash-provisions'), 80.00)


@tagged('eh_golden', 'eh_account_dynamic_reports',
        'post_install', '-at_install')
class TestCashFlowDisclosuresGolden(CashFlowIas7Case):

    def _seed_interest_and_tax(self):
        """Baseline sale 1000; interest paid 50 in cash off a tagged
        expense account; income tax accrued 120 to a tagged payable and
        settled in full. Company maps the tax expense account (the
        profit-before-tax bridge)."""
        self._tag(self.account_int_expense, 'account_tag_interest_paid')
        self._tag(self.account_tax_payable, 'account_tag_income_tax_paid')
        self.company.sudo().eh_pnl_tax_expense_account_ids = [
            (6, 0, self.account_tax_expense.ids)]
        self._post([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], '2026-02-10')
        self._post([
            {'account': self.account_int_expense, 'debit': 50.0},
            {'account': self.account_cash, 'credit': 50.0},
        ], '2026-05-20')
        self._post([
            {'account': self.account_tax_expense, 'debit': 120.0},
            {'account': self.account_tax_payable, 'credit': 120.0},
        ], '2026-06-30')
        self._post([
            {'account': self.account_tax_payable, 'debit': 120.0},
            {'account': self.account_cash, 'credit': 120.0},
        ], '2026-07-15')

    def test_ias7_31_35_disclosed_lines_direct(self):
        # Derivation (direct): cash flows are +1000 sale, -50 interest,
        # -120 tax settlement. The tagged flows leave their account-type
        # buckets and surface on dedicated lines:
        #   Interest paid      -50.00
        #   Income taxes paid -120.00
        #   operating total = 1000 - 50 - 120 = 830.00
        self._seed_interest_and_tax()
        result = self._compute()
        interest_line = self._line(result, 'disclosure-interest_paid')
        tax_line = self._line(result, 'disclosure-income_tax_paid')
        self.assertIsNotNone(interest_line, result['totals'])
        self.assertIsNotNone(tax_line, result['totals'])
        self.assertEqual(
            self._amount(result, 'disclosure-interest_paid'), -50.00)
        self.assertEqual(
            self._amount(result, 'disclosure-income_tax_paid'), -120.00)
        self.assertEqual(interest_line['meta']['section_id'], 'operating')
        self.assertEqual(tax_line['meta']['section_id'], 'operating')
        self.assertEqual(result['totals']['operating'], 830.00)
        self.assertEqual(result['totals']['disclosures'], {
            'interest_paid': -50.00, 'income_tax_paid': -120.00,
        })
        # Re-attribution: the expense bucket carried only the disclosed
        # interest, so no residual Expenses row may remain.
        self.assertIsNone(
            self._line(result, 'section-operating-line-expense'))
        self.assertIsNone(
            self._line(result, 'section-operating-line-liability_current'))

    def test_ias7_31_35_disclosed_lines_indirect_tie(self):
        # Indirect derivation on the same ledger:
        #   net income        = 1000 - 50 - 120 = 830
        #   profit before tax = 830 + 120       = 950.00
        #   + finance costs added back          =  50.00
        #   (tax payable excluded from deltas)
        #   = cash generated from operations    = 1000.00
        #   - interest paid                     =  50.00
        #   - income taxes paid                 = 120.00
        #   = net cash from operating           = 830.00 = direct
        self._seed_interest_and_tax()
        result = self._compute(cash_flow_method='indirect')
        self.assertEqual(self._amount(result, 'indirect-pbt'), 950.00)
        self.assertEqual(
            self._amount(result, 'indirect-adj-finance-costs'), 50.00)
        self.assertEqual(self._amount(result, 'indirect-cgo'), 1000.00)
        self.assertEqual(
            self._amount(result, 'disclosure-interest_paid'), -50.00)
        self.assertEqual(
            self._amount(result, 'disclosure-income_tax_paid'), -120.00)
        self.assertEqual(result['totals']['operating'], 830.00)
        self.assertEqual(
            result['totals']['cash_generated_from_operations'], 1000.00)
        # The tax-payable movement must NOT also appear as a delta row.
        self.assertIsNone(
            self._line(result, 'indirect-wc-liability_current'))
        direct = self._compute()
        self.assertEqual(
            direct['totals']['operating'], result['totals']['operating'],
            "indirect must tie to direct: %s vs %s"
            % (direct['totals'], result['totals']))

    def _seed_tax_authority_fixture(self):
        """Create an untagged tax-authority payable named as a tax
        repartition target, returning the account. Tax fixtures on a
        demo-less company need an explicit fiscal country and tax group
        (same provisioning as the BAS tests)."""
        authority_payable = self._ensure_account(
            self.env, '2206', 'Tax Authority Payable', 'liability_current')
        us = self.env.ref('base.us')
        if not self.company.country_id:
            self.company.sudo().country_id = us.id
        fiscal_country = (
            self.company.account_fiscal_country_id
            or self.company.country_id or us)
        Group = self.env['account.tax.group']
        group_has_company = 'company_id' in Group._fields
        group = Group.search(
            [('company_id', '=', self.company.id)]
            if group_has_company else [], limit=1)
        if not group:
            group_vals = {'name': 'EH IAS7 Test Group'}
            if group_has_company:
                group_vals['company_id'] = self.company.id
            group = Group.create(group_vals)
        tax = self.env['account.tax'].create({
            'name': 'EH IAS7 Fallback Tax',
            'amount': 15.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'tax_group_id': group.id,
            'country_id': fiscal_country.id,
        })
        # Read the computed invoice/refund pair, never the plain
        # repartition_line_ids one2many: the stored compute that creates
        # the repartition rows runs lazily at flush, so the plain
        # one2many reads back empty right after create() and the account
        # write below would land on no records.
        repartition = (tax.invoice_repartition_line_ids
                       | tax.refund_repartition_line_ids)
        repartition.filtered(
            lambda l: l.repartition_type == 'tax',
        ).write({'account_id': authority_payable.id})
        return authority_payable

    def test_income_tax_paid_falls_back_to_tax_repartition_accounts(self):
        # No account carries the EH Income Tax Paid tag; the settlement
        # goes against an account named as a tax repartition target. The
        # fallback is strictly OPT-IN (an untagged book must render
        # byte-identically by default) and, opted in, measures settlement
        # lines only: the remittance is disclosed (-80.00) while the
        # VAT-shaped accrual credited inside the cash sale stays in its
        # bucket and can never render a positive "taxes paid".
        authority_payable = self._seed_tax_authority_fixture()
        # Remittance: Dr Tax Authority Payable 80 / Cr Cash 80.
        self._post([
            {'account': authority_payable, 'debit': 80.0},
            {'account': self.account_cash, 'credit': 80.0},
        ])
        # Cash sale with tax accrued INSIDE the cash-affecting entry (the
        # POS-session-closing / bank-journal cash-sale shape).
        self._post([
            {'account': self.account_cash, 'debit': 115.0},
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': authority_payable, 'credit': 15.0},
        ], '2026-06-20')

        # Default state: fallback off, no disclosure row, no payload key.
        result = self._compute()
        self.assertIsNone(
            self._line(result, 'disclosure-income_tax_paid'),
            result['totals'])
        self.assertNotIn('disclosures', result['totals'])

        # Opted in per render: only the settlement is disclosed; the
        # +15 accrual stays in Other Current Liabilities. Operating =
        # 100 (sale ex tax) + 15 (accrual) - 80 (remitted) = 35.00.
        result = self._compute(cf_income_tax_fallback=True)
        self.assertEqual(
            self._amount(result, 'disclosure-income_tax_paid'), -80.00,
            result['totals'])
        self.assertEqual(
            result['totals']['disclosures']['income_tax_paid'], -80.00)
        self.assertEqual(
            self._amount(
                result, 'section-operating-line-liability_current'),
            15.00)
        self.assertEqual(result['totals']['operating'], 35.00)

        # Opted in on the company flag: same measurement; the explicit
        # render option still switches it off for one render.
        self.company.sudo().eh_cf_tax_fallback = True
        result = self._compute()
        self.assertEqual(
            self._amount(result, 'disclosure-income_tax_paid'), -80.00)
        result = self._compute(cf_income_tax_fallback=False)
        self.assertIsNone(
            self._line(result, 'disclosure-income_tax_paid'))

    def test_income_tax_fallback_keeps_indirect_tied_to_direct(self):
        # VAT-shaped ledger with the fallback opted in: cash sale 110
        # incl 10 accrued straight to the authority payable, then remit
        # 10. Direct operating = 110 - 10 = 100.00. The indirect method
        # deliberately ignores the fallback (no disclosed line; the
        # payable stays inside the working-capital deltas) so its
        # operating total ties exactly: PBT 100 + payable delta 0 = 100.
        authority_payable = self._seed_tax_authority_fixture()
        self._post([
            {'account': self.account_cash, 'debit': 110.0},
            {'account': self.account_revenue, 'credit': 100.0},
            {'account': authority_payable, 'credit': 10.0},
        ])
        self._post([
            {'account': authority_payable, 'debit': 10.0},
            {'account': self.account_cash, 'credit': 10.0},
        ], '2026-06-25')

        direct = self._compute(cf_income_tax_fallback=True)
        self.assertEqual(direct['totals']['operating'], 100.00)
        self.assertEqual(
            self._amount(direct, 'disclosure-income_tax_paid'), -10.00)

        indirect = self._compute(
            cash_flow_method='indirect', cf_income_tax_fallback=True)
        self.assertIsNone(
            self._line(indirect, 'disclosure-income_tax_paid'))
        self.assertNotIn('disclosures', indirect['totals'])
        self.assertEqual(self._amount(indirect, 'indirect-pbt'), 100.00)
        self.assertEqual(
            indirect['totals']['operating'],
            direct['totals']['operating'],
            "indirect must tie to direct under the fallback: %s vs %s"
            % (indirect['totals'], direct['totals']))

    def test_policy_option_flips_interest_paid_to_financing(self):
        # Seed: sale 1000, interest paid 50 (tagged). Default policy keeps
        # interest paid in operating (op 950 / fin 0); the IAS 7.31 option
        # moves the LINE to financing (op 1000 / fin -50). Net change is
        # presentation-invariant at 950 either way.
        self._tag(self.account_int_expense, 'account_tag_interest_paid')
        self._post([
            {'account': self.account_cash, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ])
        self._post([
            {'account': self.account_int_expense, 'debit': 50.0},
            {'account': self.account_cash, 'credit': 50.0},
        ], '2026-06-20')

        default = self._compute()
        self.assertEqual(
            self._line(default, 'disclosure-interest_paid')
            ['meta']['section_id'], 'operating')
        self.assertEqual(default['totals']['operating'], 950.00)
        self.assertEqual(default['totals']['financing'], 0.00)

        flipped = self._compute(cf_interest_paid_section='financing')
        line = self._line(flipped, 'disclosure-interest_paid')
        self.assertIsNotNone(line, flipped['totals'])
        self.assertEqual(line['meta']['section_id'], 'financing')
        self.assertEqual(flipped['totals']['operating'], 1000.00)
        self.assertEqual(flipped['totals']['financing'], -50.00)
        self.assertEqual(
            default['totals']['net_change_in_cash'],
            flipped['totals']['net_change_in_cash'])
        self.assertEqual(flipped['totals']['net_change_in_cash'], 950.00)

        # Indirect under the same flip: operating keeps the finance-cost
        # add-back but no deduction line -> 1000; financing carries -50.
        flipped_indirect = self._compute(
            cash_flow_method='indirect',
            cf_interest_paid_section='financing')
        self.assertEqual(flipped_indirect['totals']['operating'], 1000.00)
        self.assertEqual(flipped_indirect['totals']['financing'], -50.00)

        # Company policy field drives the default when no option is set;
        # an explicit option wins over the company field.
        self.company.sudo().eh_cf_interest_paid_section = 'financing'
        by_company = self._compute()
        self.assertEqual(
            self._line(by_company, 'disclosure-interest_paid')
            ['meta']['section_id'], 'financing')
        overridden = self._compute(cf_interest_paid_section='operating')
        self.assertEqual(
            self._line(overridden, 'disclosure-interest_paid')
            ['meta']['section_id'], 'operating')

    def test_policy_option_flips_dividends_paid_to_operating(self):
        # Dividends paid 200 off a tagged equity account: financing by
        # default (IAS 7.34), operating under the alternative policy.
        self._tag(self.account_dividends, 'account_tag_dividends_paid')
        self._post([
            {'account': self.account_dividends, 'debit': 200.0},
            {'account': self.account_cash, 'credit': 200.0},
        ])
        default = self._compute()
        line = self._line(default, 'disclosure-dividends_paid')
        self.assertIsNotNone(line, default['totals'])
        self.assertEqual(line['meta']['section_id'], 'financing')
        self.assertEqual(
            self._amount(default, 'disclosure-dividends_paid'), -200.00)
        self.assertEqual(default['totals']['financing'], -200.00)
        self.assertEqual(default['totals']['operating'], 0.00)
        # The equity bucket must not still carry the disclosed flow.
        self.assertIsNone(
            self._line(default, 'section-financing-line-equity'))

        flipped = self._compute(cf_dividends_paid_section='operating')
        self.assertEqual(
            self._line(flipped, 'disclosure-dividends_paid')
            ['meta']['section_id'], 'operating')
        self.assertEqual(flipped['totals']['operating'], -200.00)
        self.assertEqual(flipped['totals']['financing'], 0.00)
        self.assertEqual(
            default['totals']['net_change_in_cash'],
            flipped['totals']['net_change_in_cash'])

    def test_interest_received_extracted_and_placed(self):
        # Interest received 70 in cash off a tagged income account:
        # disclosed at +70.00, operating by default, investing under the
        # IAS 7.33 alternative (via the company policy field).
        interest_income = self._ensure_account(
            self.env, '7200', 'Interest Income', 'income_other')
        self._tag(interest_income, 'account_tag_interest_received')
        self._post([
            {'account': self.account_cash, 'debit': 70.0},
            {'account': interest_income, 'credit': 70.0},
        ])
        default = self._compute()
        self.assertEqual(
            self._amount(default, 'disclosure-interest_received'), 70.00)
        self.assertEqual(
            self._line(default, 'disclosure-interest_received')
            ['meta']['section_id'], 'operating')
        self.assertEqual(default['totals']['operating'], 70.00)

        self.company.sudo().eh_cf_interest_received_section = 'investing'
        moved = self._compute()
        self.assertEqual(
            self._line(moved, 'disclosure-interest_received')
            ['meta']['section_id'], 'investing')
        self.assertEqual(moved['totals']['operating'], 0.00)
        self.assertEqual(moved['totals']['investing'], 70.00)
        self.assertEqual(
            moved['totals']['net_change_in_cash'],
            default['totals']['net_change_in_cash'])


@tagged('eh_golden', 'eh_account_dynamic_reports',
        'post_install', '-at_install')
class TestCashFlowNoncashRegisterGolden(CashFlowIas7Case):

    def test_noncash_register_listed_as_memo_section(self):
        # Register: lease 5000 + debt conversion 2000 in period, plus an
        # out-of-period entry that must not appear. Memo only: net change
        # stays untouched at 0 (no cash postings at all).
        Register = self.env['eh.noncash.transaction']
        lease = Register.create({
            'name': 'Racking acquired under 5-year lease',
            'date': '2026-05-01', 'amount': 5000.0, 'kind': 'lease',
            'company_id': self.company.id,
        })
        conversion = Register.create({
            'name': 'Convertible note converted to shares',
            'date': '2026-08-15', 'amount': 2000.0,
            'kind': 'debt_conversion', 'company_id': self.company.id,
        })
        Register.create({
            'name': 'Prior-year swap', 'date': '2025-11-30',
            'amount': 999.0, 'kind': 'other',
            'company_id': self.company.id,
        })
        result = self._compute()
        header = self._line(result, 'section-noncash_register-header')
        self.assertIsNotNone(
            header, [l['id'] for l in result['lines']])
        self.assertEqual(
            self._amount(result, 'noncash-%s' % lease.id), 5000.00)
        self.assertEqual(
            self._amount(result, 'noncash-%s' % conversion.id), 2000.00)
        self.assertEqual(
            self._amount(result, 'section-noncash_register-total'), 7000.00)
        self.assertEqual(result['totals']['noncash_register'], 7000.00)
        self.assertEqual(result['totals']['net_change_in_cash'], 0.00)
        # Memo section sits after the cash identity block.
        line_ids = [l['id'] for l in result['lines']]
        self.assertLess(
            line_ids.index('cash_balance_check'),
            line_ids.index('section-noncash_register-header'))
        # Kind is exposed for the viewer / export caption.
        self.assertEqual(
            self._line(result, 'noncash-%s' % lease.id)
            ['meta']['noncash_kind'], 'lease')

    def test_noncash_register_absent_outside_period(self):
        self.env['eh.noncash.transaction'].create({
            'name': 'Prior-year swap', 'date': '2025-11-30',
            'amount': 999.0, 'kind': 'other',
            'company_id': self.company.id,
        })
        result = self._compute()
        self.assertIsNone(
            self._line(result, 'section-noncash_register-header'))
        self.assertNotIn('noncash_register', result['totals'])

    def test_auto_tag_action_tags_depreciation_accounts(self):
        # The install-time helper (also a server action) must stamp the
        # depreciation tag on expense_depreciation accounts, additively
        # and idempotently.
        tag = self.env.ref(
            'eh_account_dynamic_reports.account_tag_noncash_depreciation')
        self.assertNotIn(tag, self.account_dep_expense.tag_ids)
        self.env['eh.noncash.transaction'].action_eh_ias7_auto_tag()
        self.assertIn(tag, self.account_dep_expense.tag_ids)
        # Second run: no failure, tag still present exactly once.
        self.env['eh.noncash.transaction'].action_eh_ias7_auto_tag()
        self.assertEqual(
            self.account_dep_expense.tag_ids.filtered(
                lambda t: t == tag), tag)


@tagged('eh_golden', 'eh_account_dynamic_reports',
        'post_install', '-at_install')
class TestCashFlowIndirectTieProperty(CashFlowIas7Case):
    """Seeded random posting mixes: indirect == direct, every time.

    Templates are tag-complete (every non-cash P&L charge against a
    non-current account is tagged, accrual clearing accounts for the
    disclosed flows are tagged), which is exactly the documented
    requirement for the tie. Amounts are exact-2dp draws; the assertion
    tolerance is one cent per side of the comparison.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env_ref = cls.env.ref
        tag_writes = (
            (cls.account_dep_expense, 'account_tag_noncash_depreciation'),
            (cls.account_imp_expense, 'account_tag_noncash_impairment'),
            (cls.account_prov_expense, 'account_tag_noncash_provisions'),
            (cls.account_fx_loss, 'account_tag_noncash_fx'),
            (cls.account_fv_gain, 'account_tag_noncash_fair_value'),
            (cls.account_int_expense, 'account_tag_interest_paid'),
            (cls.account_tax_payable, 'account_tag_income_tax_paid'),
            (cls.account_dividends, 'account_tag_dividends_paid'),
        )
        for account, xmlid in tag_writes:
            tag = env_ref('eh_account_dynamic_reports.%s' % xmlid)
            account.sudo().write({'tag_ids': [(4, tag.id)]})
        cls.company.sudo().eh_pnl_tax_expense_account_ids = [
            (6, 0, cls.account_tax_expense.ids)]

    def _random_amount(self, rng):
        # Exact 2dp in [1.00, 5000.00].
        return rng.randrange(100, 500000) / 100.0

    def _random_date(self, rng):
        day = rng.randrange(0, 350)
        base = fields.Date.from_string('2026-01-05')
        return fields.Date.to_string(
            fields.Date.add(base, days=day))

    def _templates(self):
        """name -> callable(rng, amount, date) posting one template."""
        def cash_sale(rng, amount, date):
            self._post([
                {'account': self.account_cash, 'debit': amount},
                {'account': self.account_revenue, 'credit': amount},
            ], date)

        def credit_sale_partial_collection(rng, amount, date):
            self._post([
                {'account': self.account_receivable, 'debit': amount,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'credit': amount},
            ], date)
            fraction = rng.choice((0.0, 0.5, 1.0))
            collected = round(amount * fraction, 2)
            if collected:
                self._post([
                    {'account': self.account_cash, 'debit': collected},
                    {'account': self.account_receivable,
                     'credit': collected, 'partner': self.partner_a},
                ], date)

        def cash_expense(rng, amount, date):
            self._post([
                {'account': self.account_expense, 'debit': amount},
                {'account': self.account_cash, 'credit': amount},
            ], date)

        def credit_expense_partial_payment(rng, amount, date):
            self._post([
                {'account': self.account_expense, 'debit': amount},
                {'account': self.account_payable, 'credit': amount,
                 'partner': self.partner_b},
            ], date)
            fraction = rng.choice((0.0, 0.5, 1.0))
            paid = round(amount * fraction, 2)
            if paid:
                self._post([
                    {'account': self.account_payable, 'debit': paid,
                     'partner': self.partner_b},
                    {'account': self.account_cash, 'credit': paid},
                ], date)

        def depreciation(rng, amount, date):
            self._post([
                {'account': self.account_dep_expense, 'debit': amount},
                {'account': self.account_accum_dep, 'credit': amount},
            ], date)

        def impairment(rng, amount, date):
            self._post([
                {'account': self.account_imp_expense, 'debit': amount},
                {'account': self.account_accum_imp, 'credit': amount},
            ], date)

        def provision_noncurrent(rng, amount, date):
            self._post([
                {'account': self.account_prov_expense, 'debit': amount},
                {'account': self.account_prov_liab, 'credit': amount},
            ], date)

        def fair_value_gain(rng, amount, date):
            self._post([
                {'account': self.account_investment, 'debit': amount},
                {'account': self.account_fv_gain, 'credit': amount},
            ], date)

        def unrealised_fx_loss(rng, amount, date):
            self._post([
                {'account': self.account_fx_loss, 'debit': amount},
                {'account': self.account_lt_deposit, 'credit': amount},
            ], date)

        def asset_purchase_cash(rng, amount, date):
            self._post([
                {'account': self.account_fixed_asset, 'debit': amount},
                {'account': self.account_cash, 'credit': amount},
            ], date)

        def loan_draw(rng, amount, date):
            self._post([
                {'account': self.account_cash, 'debit': amount},
                {'account': self.account_lt_loan, 'credit': amount},
            ], date)

        def equity_injection(rng, amount, date):
            self._post([
                {'account': self.account_cash, 'debit': amount},
                {'account': self.account_equity, 'credit': amount},
            ], date)

        def interest_paid_cash(rng, amount, date):
            self._post([
                {'account': self.account_int_expense, 'debit': amount},
                {'account': self.account_cash, 'credit': amount},
            ], date)

        def tax_accrue_and_partially_pay(rng, amount, date):
            self._post([
                {'account': self.account_tax_expense, 'debit': amount},
                {'account': self.account_tax_payable, 'credit': amount},
            ], date)
            fraction = rng.choice((0.0, 0.5, 1.0))
            paid = round(amount * fraction, 2)
            if paid:
                self._post([
                    {'account': self.account_tax_payable, 'debit': paid},
                    {'account': self.account_cash, 'credit': paid},
                ], date)

        def dividends_paid_cash(rng, amount, date):
            self._post([
                {'account': self.account_dividends, 'debit': amount},
                {'account': self.account_cash, 'credit': amount},
            ], date)

        return [
            ('cash_sale', cash_sale),
            ('credit_sale_partial_collection',
             credit_sale_partial_collection),
            ('cash_expense', cash_expense),
            ('credit_expense_partial_payment',
             credit_expense_partial_payment),
            ('depreciation', depreciation),
            ('impairment', impairment),
            ('provision_noncurrent', provision_noncurrent),
            ('fair_value_gain', fair_value_gain),
            ('unrealised_fx_loss', unrealised_fx_loss),
            ('asset_purchase_cash', asset_purchase_cash),
            ('loan_draw', loan_draw),
            ('equity_injection', equity_injection),
            ('interest_paid_cash', interest_paid_cash),
            ('tax_accrue_and_partially_pay',
             tax_accrue_and_partially_pay),
            ('dividends_paid_cash', dividends_paid_cash),
        ]

    def test_indirect_equals_direct_on_random_mixes(self):
        templates = self._templates()
        for seed in range(5):
            rng = self.seeded_rng(20260705 + seed)
            trace = []
            for _draw in range(rng.randint(8, 14)):
                name, template = rng.choice(templates)
                amount = self._random_amount(rng)
                date = self._random_date(rng)
                trace.append((name, amount, date))
                template(rng, amount, date)
            direct = self._compute()
            indirect = self._compute(cash_flow_method='indirect')
            context = (
                "seed=%s trace=%s\ndirect=%s\nindirect=%s"
                % (seed, trace, direct['totals'], indirect['totals'])
            )
            for key in ('operating', 'investing', 'financing',
                        'net_change_in_cash'):
                self.assertLessEqual(
                    abs(direct['totals'][key] - indirect['totals'][key]),
                    0.02,
                    "indirect %s diverges from direct: %s"
                    % (key, context))
            # Both methods must satisfy the cash identity on this ledger.
            self.assertLessEqual(
                abs(direct['totals']['balance_check']), 0.02, context)
            self.assertLessEqual(
                abs(indirect['totals']['balance_check']), 0.02, context)
