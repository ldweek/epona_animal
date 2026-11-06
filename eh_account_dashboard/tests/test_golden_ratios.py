# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden and pairwise tests for the dashboard financial-ratio engine.

Golden case: a hand-built ledger on a dedicated fresh company (so no
other module's postings can leak into cumulative balances) with every
expected ratio derived by hand in the comments, asserted exact to 2dp
against the payload. Conventions under test (documented in
models/dashboard_ratios.py):

* point-in-time ratios read cumulative posted balances at
  period_date_to;
* average balances = (opening + closing) / 2 with opening at the day
  before period_date_from;
* days in period are inclusive of both endpoints;
* EBIT = net income + interest expense + income tax expense;
* inventory / interest / income-tax accounts resolve by the documented
  name heuristics (plus the IAS 7 interest-paid tag when present).

Zero-denominator guards, prior-period deltas, warn-threshold statuses
and the pairwise no-crash matrix (period x company currency x missing
data) are covered alongside.
"""

from datetime import date, timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


def _activate_currency(env, code):
    currency = env.ref('base.' + code)
    if not currency.active:
        currency.sudo().write({'active': True})
    return currency


def _make_company(env, name, currency_code):
    """Fresh company: guarantees the cumulative-balance ratios see
    exactly the ledger the test seeds and nothing else."""
    currency = _activate_currency(env, currency_code)
    company = env['res.company'].sudo().create({
        'name': name,
        'currency_id': currency.id,
    })
    env.user.sudo().write({'company_ids': [(4, company.id)]})
    return company


def _make_account(env, company, code, name, account_type):
    Account = env['account.account'].sudo().with_company(company)
    vals = {'code': code, 'name': name, 'account_type': account_type}
    # account.account went multi-company (company_ids m2m) in Odoo 18;
    # earlier series carry a single company_id.
    if 'company_ids' in Account._fields:
        vals['company_ids'] = [(6, 0, [company.id])]
    else:
        vals['company_id'] = company.id
    if account_type in ('asset_receivable', 'liability_payable'):
        vals['reconcile'] = True
    return Account.create(vals)


def _make_journal(env, company, code):
    return env['account.journal'].sudo().create({
        'name': 'Ratio %s' % code,
        'code': code,
        'type': 'general',
        'company_id': company.id,
    })


def _post(env, company, journal, date_, lines):
    """Create and post one balanced entry. lines: (account, debit,
    credit) triples in company currency."""
    move = env['account.move'].sudo().with_company(company).create({
        'move_type': 'entry',
        'journal_id': journal.id,
        'company_id': company.id,
        'date': date_,
        'line_ids': [(0, 0, {
            'account_id': account.id,
            'debit': debit,
            'credit': credit,
            'name': '/',
        }) for account, debit, credit in lines],
    })
    move.action_post()
    return move


def _flatten(payload):
    """{ratio key: payload entry} across every category."""
    return {
        entry['key']: entry
        for category in payload['categories']
        for entry in category['ratios']
    }


@tagged('eh_golden', 'eh_account_dashboard', 'post_install', '-at_install')
class TestGoldenRatioEngine(EhGoldenTestCase):
    """Hand-derived golden ledger, exact-2dp ratio parity.

    Golden ledger (fresh company, USD, period 2025-01-01..2025-12-31,
    365 days inclusive):

    Opening balance sheet posted 2024-12-30 (all balance-sheet lines,
    no P&L, so the prior window carries zero revenue):

        Cash 5,000 / AR 60,000 / Inventory 20,000 / Prepayments 70,000
        / Plant 345,000 = 500,000 assets
        AP 40,000 / Accrued 60,000 / Loan (non-current) 100,000 /
        Share capital 300,000 = 500,000 credit side

    2025 activity (posted 2025-06-15):

        DR AR 730,000        CR Revenue 730,000
        DR Cash 730,000      CR AR 730,000        (collections)
        DR Inventory 385,000 CR AP 385,000        (purchases)
        DR COGS 365,000      CR Inventory 365,000
        DR AP 385,000        CR Cash 385,000      (payments)
        DR Opex 285,000      CR Cash 285,000
        DR Interest 20,000   CR Cash 20,000
        DR Income tax 15,000 CR Cash 15,000

    Closing balance sheet at 2025-12-31 (derivation):
        Cash  = 5,000 + 730,000 - 385,000 - 285,000 - 20,000 - 15,000
              = 30,000
        AR    = 60,000 + 730,000 - 730,000 = 60,000
        Inv   = 20,000 + 385,000 - 365,000 = 40,000
        Prepayments 70,000 (unchanged), Plant 345,000 (unchanged)
        Current assets = 30,000 + 60,000 + 40,000 + 70,000 = 200,000
        AP    = 40,000 + 385,000 - 385,000 = 40,000
        Current liabilities = 40,000 + 60,000 = 100,000
        Non-current liabilities = 100,000; share capital = 300,000
        Net income = 730,000 - (365,000 + 285,000 + 20,000 + 15,000)
                   = 45,000
        Economic equity before year-end close = 300,000 + 45,000 = 345,000
        EBIT = 45,000 + 20,000 + 15,000 = 80,000
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Dashboard = cls.env['eh.account.dashboard']
        cls.gold_company = _make_company(
            cls.env, 'EH Ratio Golden Co', 'USD')
        cls.gold_journal = _make_journal(cls.env, cls.gold_company, 'RGLD')
        c, env = cls.gold_company, cls.env
        cls.a_cash = _make_account(
            env, c, '1000', 'Cash at Bank', 'asset_cash')
        cls.a_ar = _make_account(
            env, c, '1100', 'Trade Receivables', 'asset_receivable')
        # Name chosen to hit the documented inventory heuristic.
        cls.a_inventory = _make_account(
            env, c, '1300', 'Inventory on Hand', 'asset_current')
        # Name chosen to MISS the inventory heuristic.
        cls.a_prepaid = _make_account(
            env, c, '1400', 'Prepayments and Deposits', 'asset_prepayments')
        cls.a_fixed = _make_account(
            env, c, '1500', 'Plant and Equipment', 'asset_fixed')
        cls.a_ap = _make_account(
            env, c, '2100', 'Trade Payables', 'liability_payable')
        cls.a_accrued = _make_account(
            env, c, '2200', 'Accrued Liabilities', 'liability_current')
        cls.a_loan = _make_account(
            env, c, '2500', 'Long Term Borrowings', 'liability_non_current')
        cls.a_equity = _make_account(
            env, c, '3000', 'Share Capital', 'equity')
        cls.a_revenue = _make_account(
            env, c, '4000', 'Sales Revenue', 'income')
        cls.a_cogs = _make_account(
            env, c, '5000', 'Cost of Goods Sold', 'expense_direct_cost')
        cls.a_opex = _make_account(
            env, c, '6000', 'Operating Expenses', 'expense')
        # Names chosen to hit the interest / income-tax heuristics.
        cls.a_interest = _make_account(
            env, c, '6900', 'Interest Expense', 'expense')
        cls.a_tax = _make_account(
            env, c, '6300', 'Income Tax Expense', 'expense')

        journal = cls.gold_journal
        _post(env, c, journal, date(2024, 12, 30), [
            (cls.a_cash, 5000.0, 0.0),
            (cls.a_ar, 60000.0, 0.0),
            (cls.a_inventory, 20000.0, 0.0),
            (cls.a_prepaid, 70000.0, 0.0),
            (cls.a_fixed, 345000.0, 0.0),
            (cls.a_ap, 0.0, 40000.0),
            (cls.a_accrued, 0.0, 60000.0),
            (cls.a_loan, 0.0, 100000.0),
            (cls.a_equity, 0.0, 300000.0),
        ])
        mid = date(2025, 6, 15)
        _post(env, c, journal, mid, [
            (cls.a_ar, 730000.0, 0.0), (cls.a_revenue, 0.0, 730000.0)])
        _post(env, c, journal, mid, [
            (cls.a_cash, 730000.0, 0.0), (cls.a_ar, 0.0, 730000.0)])
        _post(env, c, journal, mid, [
            (cls.a_inventory, 385000.0, 0.0), (cls.a_ap, 0.0, 385000.0)])
        _post(env, c, journal, mid, [
            (cls.a_cogs, 365000.0, 0.0), (cls.a_inventory, 0.0, 365000.0)])
        _post(env, c, journal, mid, [
            (cls.a_ap, 385000.0, 0.0), (cls.a_cash, 0.0, 385000.0)])
        _post(env, c, journal, mid, [
            (cls.a_opex, 285000.0, 0.0), (cls.a_cash, 0.0, 285000.0)])
        _post(env, c, journal, mid, [
            (cls.a_interest, 20000.0, 0.0), (cls.a_cash, 0.0, 20000.0)])
        _post(env, c, journal, mid, [
            (cls.a_tax, 15000.0, 0.0), (cls.a_cash, 0.0, 15000.0)])

        cls.dash = cls.Dashboard.create({
            'name': 'Golden ratio dashboard',
            'company_id': c.id,
            'period_mode': 'custom',
            'period_date_from': date(2025, 1, 1),
            'period_date_to': date(2025, 12, 31),
            'posted_only': True,
        })

    def _payload(self):
        return self.dash._eh_ratio_payload()

    # ---- golden values ----

    def test_golden_ratio_values(self):
        """Every ratio exact to 2dp against the hand derivation.

        Liquidity (closing balances at 2025-12-31):
          current = 200,000 / 100,000                     = 2.00
          quick   = (200,000 - 40,000 - 70,000) / 100,000 = 0.90
          cash    = 30,000 / 100,000                      = 0.30
        Efficiency (averages over 365 days):
          avg AR  = (60,000 + 60,000) / 2  = 60,000
            DSO   = 60,000 / (730,000 / 365) = 60,000 / 2,000 = 30.00
          avg AP  = (40,000 + 40,000) / 2  = 40,000
            DPO   = 40,000 / (365,000 / 365) = 40,000 / 1,000 = 40.00
          avg Inv = (20,000 + 40,000) / 2  = 30,000
            DIO   = 30,000 / (365,000 / 365) = 30.00
          CCC     = 30 + 30 - 40 = 20.00
        Profitability:
          gross margin = (730,000 - 365,000) / 730,000 * 100 = 50.00
          net margin   = 45,000 / 730,000 * 100 = 6.1643... -> 6.16
          avg assets   = (500,000 + 545,000) / 2 = 522,500
            ROA        = 45,000 / 522,500 * 100 = 8.6124... -> 8.61
          avg economic equity = (300,000 + 345,000) / 2 = 322,500
            ROE        = 45,000 / 322,500 * 100 = 13.95
          avg capital employed = 322,500 + 100,000 = 422,500
            ROCE       = 80,000 / 422,500 * 100 = 18.93
        Leverage (closing):
          debt-to-equity = (100,000 + 100,000) / 345,000
                         = 0.5797... -> 0.58
          interest cover = 80,000 / 20,000 = 4.00
        """
        payload = self._payload()
        self.assertTrue(payload['available'])
        self.assertEqual(payload['window']['days'], 365)
        flat = _flatten(payload)
        expected = {
            'current_ratio': 2.00,
            'quick_ratio': 0.90,
            'cash_ratio': 0.30,
            'dso': 30.00,
            'dpo': 40.00,
            'dio': 30.00,
            'ccc': 20.00,
            'gross_margin_pct': 50.00,
            'net_margin_pct': 6.16,
            'roa': 8.61,
            'roe': 13.95,
            'roce': 18.93,
            'debt_to_equity': 0.58,
            'interest_cover': 4.00,
        }
        for key, exp in expected.items():
            entry = flat[key]
            self.assertIsNotNone(
                entry['value'],
                'ratio %s unexpectedly null (note=%s)' % (
                    key, entry['note']))
            self.assertAlmostEqual(
                entry['value'], exp, places=2,
                msg='ratio %s: got %s, expected %s' % (
                    key, entry['value'], exp))
        # No denominator fell back or failed on the golden ledger, so
        # the only permissible note is none at all.
        for key in expected:
            self.assertFalse(
                flat[key]['note'],
                'ratio %s should carry no note on the golden ledger, '
                'got %r' % (key, flat[key]['note']))

    def test_golden_flags(self):
        payload = self._payload()
        self.assertTrue(payload['flags']['inventory_detected'])
        # No account carries the IAS 7 tag in the fresh company, so the
        # interest set must resolve via the documented name heuristic.
        self.assertEqual(payload['flags']['interest_source'], 'heuristic')
        self.assertEqual(payload['flags']['tax_source'], 'heuristic')

    def test_interest_tag_source_preferred(self):
        """Tagging the interest account with the IAS 7 interest-paid
        tag flips the source to 'tag' without changing the cover."""
        tag = self.env.ref(
            'eh_account_dynamic_reports.account_tag_interest_paid',
            raise_if_not_found=False)
        if tag is None:
            self.skipTest('IAS 7 interest tag not available')
        self.a_interest.sudo().write({'tag_ids': [(4, tag.id)]})
        payload = self._payload()
        self.assertEqual(payload['flags']['interest_source'], 'tag')
        flat = _flatten(payload)
        self.assertAlmostEqual(
            flat['interest_cover']['value'], 4.00, places=2,
            msg='interest cover must stay 4.00 under the tag source')

    # ---- statuses / thresholds ----

    def test_statuses_with_default_thresholds(self):
        """Defaults: current<1, quick<0.8, cash<0.2, cover<2,
        net margin<0 warn-below; D/E>2, CCC>90 warn-above. The golden
        ledger sits on the healthy side of every threshold."""
        flat = _flatten(self._payload())
        for key in ('current_ratio', 'quick_ratio', 'cash_ratio',
                    'interest_cover', 'net_margin_pct',
                    'debt_to_equity', 'ccc'):
            self.assertEqual(
                flat[key]['status'], 'ok',
                'ratio %s should be ok at default thresholds, got %s '
                '(value %s)' % (key, flat[key]['status'],
                                flat[key]['value']))
        for key in ('dso', 'dpo', 'dio', 'gross_margin_pct',
                    'roa', 'roe', 'roce'):
            self.assertEqual(
                flat[key]['status'], 'info',
                'ratio %s carries no threshold and should be info, '
                'got %s' % (key, flat[key]['status']))

    def test_threshold_flip_drives_warn_status(self):
        company = self.gold_company.sudo()
        company.write({
            # current ratio 2.00 < 2.5 -> warn (below rule)
            'eh_ratio_warn_current': 2.5,
            # interest cover 4.00 < 5.0 -> warn (below rule)
            'eh_ratio_warn_interest_cover': 5.0,
            # debt-to-equity 0.58 > 0.5 -> warn (above rule)
            'eh_ratio_warn_debt_equity': 0.5,
        })
        flat = _flatten(self._payload())
        self.assertEqual(flat['current_ratio']['status'], 'warn')
        self.assertEqual(flat['interest_cover']['status'], 'warn')
        self.assertEqual(flat['debt_to_equity']['status'], 'warn')
        # Untouched thresholds stay ok.
        self.assertEqual(flat['quick_ratio']['status'], 'ok')

    # ---- prior period ----

    def test_prior_period_deltas_golden(self):
        """Prior window = same length immediately before: 365 days
        ending 2024-12-31, so 2024-01-02..2024-12-31. Only the opening
        balance move (2024-12-30, no P&L lines) falls inside it.

        Prior closing balances at 2024-12-31:
          current assets = 5,000 + 60,000 + 20,000 + 70,000 = 155,000
          current liabilities = 100,000
          prior current ratio = 1.55
            delta = (2.00 - 1.55) / 1.55 * 100 = 29.0322... -> 29.03
          prior quick = (155,000 - 20,000 - 70,000) / 100,000 = 0.65
            delta = (0.90 - 0.65) / 0.65 * 100 = 38.4615... -> 38.46
          prior cash ratio = 5,000 / 100,000 = 0.05
            delta = (0.30 - 0.05) / 0.05 * 100 = 500.00
          prior debt-to-equity = 200,000 / 300,000 = 0.67; current
            economic-equity denominator includes 45,000 unclosed profit,
            so the delta is -13.04 percent.
          prior revenue = 0 -> prior DSO is None -> delta is None.
        """
        payload = self._payload()
        self.assertEqual(payload['window']['prior_from'], '2024-01-02')
        self.assertEqual(payload['window']['prior_to'], '2024-12-31')
        flat = _flatten(payload)

        self.assertAlmostEqual(flat['current_ratio']['prior'], 1.55,
                               places=2)
        self.assertAlmostEqual(flat['current_ratio']['delta_pct'], 29.03,
                               places=2)
        self.assertAlmostEqual(flat['quick_ratio']['prior'], 0.65,
                               places=2)
        self.assertAlmostEqual(flat['quick_ratio']['delta_pct'], 38.46,
                               places=2)
        self.assertAlmostEqual(flat['cash_ratio']['prior'], 0.05,
                               places=2)
        self.assertAlmostEqual(flat['cash_ratio']['delta_pct'], 500.00,
                               places=2)
        self.assertAlmostEqual(flat['debt_to_equity']['delta_pct'], -13.04,
                               places=2)
        self.assertIsNone(
            flat['dso']['prior'],
            'prior window has no revenue, prior DSO must be null')
        self.assertIsNone(
            flat['dso']['delta_pct'],
            'no baseline: DSO delta must be null, got %s' % (
                flat['dso']['delta_pct'],))

    # ---- snapshot integration ----

    def test_snapshot_includes_ratios(self):
        snap = self.dash.get_dashboard_snapshot()
        self.assertIn('ratios', snap)
        self.assertTrue(snap['ratios']['available'])
        flat = _flatten(snap['ratios'])
        self.assertAlmostEqual(flat['current_ratio']['value'], 2.00,
                               places=2)

    # ---- zero-denominator guards ----

    def test_zero_denominators_null_marked(self):
        """Empty ledger: every ratio must be None with a note and
        status 'na'; the snapshot path must not raise."""
        company = _make_company(self.env, 'EH Ratio Empty Co', 'USD')
        dash = self.Dashboard.create({
            'name': 'Empty ratio dashboard',
            'company_id': company.id,
            'period_mode': 'ytd',
        })
        snap = dash.get_dashboard_snapshot()
        payload = snap['ratios']
        self.assertTrue(payload['available'])
        for category in payload['categories']:
            for entry in category['ratios']:
                key = entry['key']
                self.assertIsNone(
                    entry['value'],
                    'ratio %s should be null on an empty ledger, got '
                    '%s' % (key, entry['value']))
                self.assertTrue(
                    entry['note'],
                    'ratio %s should carry a denominator note' % key)
                self.assertEqual(
                    entry['status'], 'na',
                    'ratio %s should have status na, got %s' % (
                        key, entry['status']))
                self.assertIsNone(
                    entry['delta_pct'],
                    'ratio %s should carry no delta without a '
                    'baseline' % key)


@tagged('eh_golden', 'eh_account_dashboard', 'post_install', '-at_install')
class TestRatioPairwise(EhGoldenTestCase):
    """Pairwise matrix: period preset x company currency x missing
    data. Asserts the engine never crashes and null-marks exactly the
    ratios whose inputs are absent."""

    AXES = {
        'period': ['mtd', 'qtd', 'ytd'],
        'currency': ['USD', 'EUR'],
        'missing': ['inventory', 'interest'],
    }

    def test_pairwise_no_crash_and_null_marking(self):
        env = self.env
        Dashboard = env['eh.account.dashboard']
        today = fields.Date.context_today(env['res.users'])
        opening_day = today - timedelta(days=400)
        cases = pairwise_cases(self.AXES)
        self.assertTrue(cases)
        for idx, case in enumerate(cases):
            with self.subTest(case=case):
                company = _make_company(
                    env, 'EH Ratio PW %02d' % idx, case['currency'])
                journal = _make_journal(env, company, 'RP%02d' % idx)
                cash = _make_account(
                    env, company, '1000', 'Cash at Bank', 'asset_cash')
                ar = _make_account(
                    env, company, '1100', 'Trade Receivables',
                    'asset_receivable')
                ap = _make_account(
                    env, company, '2100', 'Trade Payables',
                    'liability_payable')
                equity = _make_account(
                    env, company, '3000', 'Share Capital', 'equity')
                revenue = _make_account(
                    env, company, '4000', 'Sales Revenue', 'income')
                cogs = _make_account(
                    env, company, '5000', 'Cost of Goods Sold',
                    'expense_direct_cost')

                # Opening equity injection well before any window.
                _post(env, company, journal, opening_day, [
                    (cash, 10000.0, 0.0), (equity, 0.0, 10000.0)])
                # Current-window activity posted today so it lands in
                # every period preset (mtd, qtd, ytd all end today).
                _post(env, company, journal, today, [
                    (ar, 12000.0, 0.0), (revenue, 0.0, 12000.0)])
                _post(env, company, journal, today, [
                    (cogs, 6000.0, 0.0), (ap, 0.0, 6000.0)])
                if case['missing'] != 'inventory':
                    inventory = _make_account(
                        env, company, '1300', 'Inventory on Hand',
                        'asset_current')
                    _post(env, company, journal, today, [
                        (inventory, 3000.0, 0.0), (cash, 0.0, 3000.0)])
                if case['missing'] != 'interest':
                    interest = _make_account(
                        env, company, '6900', 'Interest Expense',
                        'expense')
                    _post(env, company, journal, today, [
                        (interest, 500.0, 0.0), (cash, 0.0, 500.0)])

                dash = Dashboard.create({
                    'name': 'PW dashboard %02d' % idx,
                    'company_id': company.id,
                    'period_mode': case['period'],
                })
                # Full snapshot path: must not raise on any axis mix.
                snap = dash.get_dashboard_snapshot()
                payload = snap['ratios']
                self.assertTrue(
                    payload['available'], 'case %s: no payload' % case)
                flat = _flatten(payload)

                # Data present on every case computes.
                self.assertIsNotNone(
                    flat['current_ratio']['value'],
                    'case %s: current ratio should compute' % case)
                self.assertIsNotNone(
                    flat['dso']['value'],
                    'case %s: DSO should compute' % case)

                if case['missing'] == 'inventory':
                    self.assertIsNone(
                        flat['dio']['value'],
                        'case %s: DIO must null-mark without '
                        'inventory' % case)
                    self.assertTrue(
                        flat['dio']['note'],
                        'case %s: DIO must carry a note' % case)
                    self.assertEqual(flat['dio']['status'], 'na')
                    self.assertIsNone(
                        flat['ccc']['value'],
                        'case %s: CCC requires DIO' % case)
                    self.assertFalse(
                        payload['flags']['inventory_detected'])
                else:
                    self.assertIsNotNone(
                        flat['dio']['value'],
                        'case %s: DIO should compute (note=%s)' % (
                            case, flat['dio']['note']))
                    self.assertIsNotNone(
                        flat['ccc']['value'],
                        'case %s: CCC should compute' % case)

                if case['missing'] == 'interest':
                    self.assertIsNone(
                        flat['interest_cover']['value'],
                        'case %s: cover must null-mark without '
                        'interest' % case)
                    self.assertTrue(
                        flat['interest_cover']['note'],
                        'case %s: cover must carry a note' % case)
                    self.assertEqual(
                        flat['interest_cover']['status'], 'na')
                    self.assertEqual(
                        payload['flags']['interest_source'], 'none')
                else:
                    self.assertIsNotNone(
                        flat['interest_cover']['value'],
                        'case %s: cover should compute' % case)
