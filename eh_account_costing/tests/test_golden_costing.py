# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden worked examples for eh_account_costing.

Every expected amount is hand-derived from the inputs stated in the test,
derivation in a comment; nothing is read back from the engine under test.

Engine conventions asserted here (read from models/variance_run.py and
models/contribution_report.py):

* ADVERSE POSITIVE, FAVOURABLE NEGATIVE on every variance.
* Each variance amount is rounded to company currency (2dp) at the step
  shown in the module docstring; the per-element price-type and
  quantity-type variances telescope, so the variance lines always sum
  exactly to total actual cost minus total standard cost absorbed.
* CVP ratios are stored rounded to 4 decimals at each step (documented
  stored-rounded convention); money amounts to 2dp.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_costing', 'post_install', '-at_install')
class TestGoldenCosting(EhGoldenTestCase):
    """Standard costing golden set: the full two-way variance
    decomposition, the posting / analysis-only split, the CVP block and
    the ledger revenue pickup."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.acc_price = cls._ensure_account(
            cls.env, '5810', 'Price Variance', 'expense')
        cls.acc_usage = cls._ensure_account(
            cls.env, '5811', 'Usage Variance', 'expense')
        cls.acc_rate = cls._ensure_account(
            cls.env, '5812', 'Rate Variance', 'expense')
        cls.acc_efficiency = cls._ensure_account(
            cls.env, '5813', 'Efficiency Variance', 'expense')
        cls.acc_spend = cls._ensure_account(
            cls.env, '5814', 'Spend Variance', 'expense')
        cls.acc_volume = cls._ensure_account(
            cls.env, '5815', 'Volume Variance', 'expense')
        cls.acc_absorption = cls._ensure_account(
            cls.env, '5809', 'Absorption Clearing', 'expense')

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _standard_card(self, **kw):
        """The golden standard card:

        material 2 kg  x  5.00 = 10.00 / unit
        labour   0.5 h x 20.00 = 10.00 / unit
        VOH      0.5 h x  4.00 =  2.00 / unit
        FOH      1     x 10.00 = 10.00 / unit
        normal capacity 1,000 units -> FOH budget 10,000.00
        std cost / unit 32.00, variable 22.00, fixed 10.00
        """
        vals = {
            'item_name': 'Golden Widget',
            'normal_capacity': 1000.0,
            'line_ids': [
                (0, 0, {'element': 'material', 'uom_name': 'kg',
                        'std_qty': 2.0, 'std_price': 5.0}),
                (0, 0, {'element': 'labour', 'uom_name': 'hr',
                        'std_qty': 0.5, 'std_price': 20.0}),
                (0, 0, {'element': 'variable_overhead', 'uom_name': 'hr',
                        'std_qty': 0.5, 'std_price': 4.0}),
                (0, 0, {'element': 'fixed_overhead',
                        'std_qty': 1.0, 'std_price': 10.0}),
            ],
        }
        vals.update(kw)
        card = self.env['eh.cost.card'].create(vals)
        card.action_activate()
        return card

    def _golden_actual(self, card):
        """The golden period actuals:

        output 900 units; material 1,900 kg costing 9,880.00 (5.20/kg);
        labour 480 h costing 9,120.00 (19.00/h); VOH driver 480 h costing
        2,050.00; FOH 9,800.00.
        """
        return self.env['eh.cost.actual'].create({
            'card_id': card.id,
            'period_start': '2026-01-01', 'period_end': '2026-01-31',
            'units_produced': 900.0,
            'line_ids': [
                (0, 0, {'element': 'material',
                        'actual_qty_total': 1900.0,
                        'actual_cost_total': 9880.0}),
                (0, 0, {'element': 'labour',
                        'actual_qty_total': 480.0,
                        'actual_cost_total': 9120.0}),
                (0, 0, {'element': 'variable_overhead',
                        'actual_qty_total': 480.0,
                        'actual_cost_total': 2050.0}),
                (0, 0, {'element': 'fixed_overhead',
                        'actual_qty_total': 0.0,
                        'actual_cost_total': 9800.0}),
            ],
        })

    def _run(self, actuals, **kw):
        vals = {
            'period_start': '2026-01-01', 'period_end': '2026-01-31',
            'actual_ids': [(6, 0, actuals.ids)],
        }
        vals.update(kw)
        return self.env['eh.cost.variance.run'].create(vals)

    def _posting_accounts(self):
        return {
            'post_variances': True,
            'journal_id': self.journal_misc.id,
            'price_variance_account_id': self.acc_price.id,
            'usage_variance_account_id': self.acc_usage.id,
            'rate_variance_account_id': self.acc_rate.id,
            'efficiency_variance_account_id': self.acc_efficiency.id,
            'spend_variance_account_id': self.acc_spend.id,
            'volume_variance_account_id': self.acc_volume.id,
            'absorption_account_id': self.acc_absorption.id,
        }

    def _line_amount(self, run, element, kind):
        line = run.line_ids.filtered(
            lambda l: l.element == element and l.kind == kind)
        self.assertEqual(
            len(line), 1,
            'expected exactly one %s/%s line, got %s' % (
                element, kind, len(line)))
        return line.amount

    # ------------------------------------------------------------------
    # golden 1: the full two-way variance set
    # ------------------------------------------------------------------

    def test_golden_full_variance_set(self):
        """Standard card (per unit): material 2 kg x 5.00, labour 0.5 h x
        20.00, VOH 0.5 h x 4.00, FOH rate 10.00 with budget 10,000 for
        1,000 units. Actual output 900 units; material 1,900 kg costing
        9,880 (5.20/kg); labour 480 h costing 9,120 (19.00/h); VOH 2,050;
        FOH 9,800.

        Hand derivation (adverse positive, favourable negative):

        material price      = 9,880 - 5.00 x 1,900        =    380.00 A
                              (= (5.20 - 5.00) x 1,900)
        material usage      = (1,900 - 2 x 900) x 5.00    =    500.00 A
        labour rate         = 9,120 - 20.00 x 480         =   -480.00 F
                              (= (19.00 - 20.00) x 480)
        labour efficiency   = (480 - 0.5 x 900) x 20.00   =    600.00 A
        VOH spend           = 2,050 - 480 x 4.00          =    130.00 A
        VOH efficiency      = (480 - 450) x 4.00          =    120.00 A
        FOH spend           = 9,800 - 10,000              =   -200.00 F
        FOH volume          = 10,000 - 10.00 x 900        =  1,000.00 A

        Reconciliation: total actual 9,880 + 9,120 + 2,050 + 9,800 =
        30,850; standard absorbed 900 x (10 + 10 + 2 + 10) = 28,800;
        total variance 2,050.00 A = sum of the eight lines exactly.
        """
        card = self._standard_card()
        self.assertAlmostEqual(card.std_cost_unit, 32.0, places=4)
        self.assertAlmostEqual(card.std_variable_cost_unit, 22.0, places=4)
        self.assertAlmostEqual(card.std_fixed_cost_unit, 10.0, places=4)
        self.assertAlmostEqual(card.budget_fixed_overhead, 10000.0, places=2)

        run = self._run(self._golden_actual(card))
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertEqual(len(run.line_ids), 8)

        expected = [
            ('material', 'price', 380.0),
            ('material', 'usage', 500.0),
            ('labour', 'rate', -480.0),
            ('labour', 'efficiency', 600.0),
            ('variable_overhead', 'spend', 130.0),
            ('variable_overhead', 'efficiency', 120.0),
            ('fixed_overhead', 'spend', -200.0),
            ('fixed_overhead', 'volume', 1000.0),
        ]
        for element, kind, amount in expected:
            got = self._line_amount(run, element, kind)
            self.assertAlmostEqual(
                got, amount, places=2,
                msg='%s %s variance: got %s, expected %s' % (
                    element, kind, got, amount))

        self.assertAlmostEqual(run.total_actual_cost, 30850.0, places=2)
        self.assertAlmostEqual(run.total_absorbed_cost, 28800.0, places=2)
        self.assertAlmostEqual(run.total_variance, 2050.0, places=2)
        self.assertAlmostEqual(
            sum(run.line_ids.mapped('amount')), 2050.0, places=2)
        # Favourable flags follow the sign convention.
        self.assertEqual(
            set(run.line_ids.filtered('is_favourable').mapped('kind')),
            {'rate', 'spend'})

    # ------------------------------------------------------------------
    # golden 2: posting the variance set
    # ------------------------------------------------------------------

    def test_golden_variance_posting(self):
        """Same numbers as golden 1 with posting enabled. The entry
        aggregates per kind (net adverse = debit, net favourable =
        credit); efficiency nets labour 600 A + VOH 120 A = 720 A, spend
        nets VOH 130 A + FOH -200 F = -70 F.

        Dr price variance         380.00
        Dr usage variance         500.00
        Cr rate variance                     480.00
        Dr efficiency variance    720.00
        Cr spend variance                     70.00
        Dr volume variance      1,000.00
        Cr absorption (under-absorption)   2,050.00
        (debits 2,600.00 = credits 2,600.00)
        """
        card = self._standard_card()
        run = self._run(self._golden_actual(card), **self._posting_accounts())
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertEqual(len(run.move_ids), 1)
        move = run.move_ids
        self.assertMoveLines(move, [
            (self.acc_price, 380.0, 0.0),
            (self.acc_usage, 500.0, 0.0),
            (self.acc_rate, 0.0, 480.0),
            (self.acc_efficiency, 720.0, 0.0),
            (self.acc_spend, 0.0, 70.0),
            (self.acc_volume, 1000.0, 0.0),
            (self.acc_absorption, 0.0, 2050.0),
        ])
        self.assertBalanced(move)
        self.assertTrue(move.eh_sealed)
        self.assertEqual(move.state, 'posted')
        # The posted run is frozen: measurement inputs cannot be re-keyed.
        with self.assertRaises(UserError):
            run.period_start = '2026-02-01'
        with self.assertRaises(UserError):
            run.post_variances = False
        # Its engine lines are frozen too.
        with self.assertRaises(UserError):
            run.line_ids[0].amount = 999.0
        # The actuals feeding it are frozen.
        actual = run.actual_ids
        with self.assertRaises(UserError):
            actual.units_produced = 1000.0
        with self.assertRaises(UserError):
            actual.line_ids[0].actual_cost_total = 1.0
        # And the run cannot be deleted or re-keyed to another state.
        with self.assertRaises(UserError):
            run.unlink()
        with self.assertRaises(UserError):
            run.state = 'draft'

    def test_post_recomputes_live_actuals_under_source_lock(self):
        """A computed analysis cannot post stale values after actual edits."""
        card = self._standard_card()
        actual = self._golden_actual(card)
        run = self._run(actual, **self._posting_accounts())
        run.action_compute()
        stale_total = run.total_actual_cost

        material = actual.line_ids.filtered(
            lambda line: line.element == 'material')
        material.actual_cost_total += 100.0
        self.assertEqual(run.total_actual_cost, stale_total)

        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertEqual(run.total_actual_cost, stale_total + 100.0)
        self.assertEqual(
            sum(run.line_ids.mapped('amount')),
            run.total_variance,
        )
        self.assertBalanced(run.move_ids)

    def test_posted_variance_has_controlled_exact_date_reversal(self):
        card = self._standard_card()
        actual = self._golden_actual(card)
        run = self._run(actual, **self._posting_accounts())
        run.action_compute()
        run.action_post()
        original = run.move_ids

        run.reversal_date = run.period_end
        run.action_reverse()

        reversal = run.reversal_move_id
        self.assertEqual(run.state, 'reversed')
        self.assertEqual(len(run.move_ids), 2)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertEqual(reversal.date, run.period_end)
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(reversal.eh_sealed)
        self.assertEqual(
            sum((original | reversal).line_ids.mapped('balance')),
            0.0,
        )
        # Reversal releases source actuals for a corrected successor run;
        # immutable original and counter-entry remain linked to this run.
        actual.line_ids[0].actual_cost_total += 1.0
        with self.assertRaises(UserError):
            run.action_cancel()

    def test_golden_analysis_mode_posts_nothing(self):
        """Analysis-only mode is the default: post_variances is False, so
        Post is refused and nothing reaches the ledger even with every
        account configured."""
        card = self._standard_card()
        vals = self._posting_accounts()
        vals.pop('post_variances')  # stays at the default False
        run = self._run(self._golden_actual(card), **vals)
        run.action_compute()
        self.assertFalse(run.post_variances)
        with self.assertRaises(UserError):
            run.action_post()
        self.assertFalse(run.move_ids)
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(
            self.posted_balance(self.acc_absorption), 0.0, places=2)

    # ------------------------------------------------------------------
    # golden 3: CVP block
    # ------------------------------------------------------------------

    def test_golden_cvp(self):
        """CVP: fixed costs 120,000, price 50.00, unit variable cost
        30.00, sales 8,500 units, target profit 40,000.

        revenue            = 8,500 x 50.00        = 425,000.00
        variable cost      = 8,500 x 30.00        = 255,000.00
        contribution       = 425,000 - 255,000    = 170,000.00
        unit CM            = 170,000 / 8,500      = 20.0000
        CM ratio           = 170,000 / 425,000    = 40.0000 %
        operating income   = 170,000 - 120,000    = 50,000.00
        break-even units   = 120,000 / 20         = 6,000.0000
        break-even revenue = 120,000 x 100 / 40   = 300,000.00
        margin of safety   = 125,000 / 425,000    = 29.4118 %  (4dp)
        target units       = 160,000 / 20         = 8,000.0000
        DOL                = 170,000 / 50,000     = 3.4000
        """
        card = self.env['eh.cost.card'].create({
            'item_name': 'CVP Widget',
            'line_ids': [(0, 0, {
                'element': 'material', 'std_qty': 1.0, 'std_price': 30.0})],
        })
        card.action_activate()
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01', 'period_end': '2026-03-31',
            'fixed_costs': 120000.0,
            'target_profit': 40000.0,
            'line_ids': [(0, 0, {
                'card_id': card.id, 'units_sold': 8500.0,
                'revenue_source': 'manual', 'revenue': 425000.0})],
        })
        line = report.line_ids
        self.assertAlmostEqual(line.variable_cost, 255000.0, places=2)
        self.assertAlmostEqual(line.contribution, 170000.0, places=2)
        self.assertAlmostEqual(line.cm_ratio_pct, 40.0, places=4)
        self.assertAlmostEqual(report.total_revenue, 425000.0, places=2)
        self.assertAlmostEqual(
            report.total_variable_cost, 255000.0, places=2)
        self.assertAlmostEqual(
            report.total_contribution, 170000.0, places=2)
        self.assertAlmostEqual(report.cm_ratio_pct, 40.0, places=4)
        self.assertAlmostEqual(report.unit_cm, 20.0, places=4)
        self.assertAlmostEqual(report.operating_income, 50000.0, places=2)
        self.assertAlmostEqual(report.breakeven_units, 6000.0, places=4)
        self.assertAlmostEqual(
            report.breakeven_revenue, 300000.0, places=2)
        # 125,000 / 425,000 x 100 = 29.41176... -> 29.4118 at the stored
        # 4dp convention.
        self.assertAlmostEqual(
            report.margin_of_safety_pct, 29.4118, places=4)
        self.assertAlmostEqual(
            report.target_profit_units, 8000.0, places=4)
        self.assertAlmostEqual(report.operating_leverage, 3.4, places=4)

    # ------------------------------------------------------------------
    # golden 4: revenue picked up from the posted ledger
    # ------------------------------------------------------------------

    def test_golden_contribution_from_ledger(self):
        """Two posted invoice lines for the product inside the period
        (10 x 500.00 = 5,000.00 and 5 x 440.00 = 2,200.00) sum to
        7,200.00; a third invoice outside the period is excluded. With
        15 units sold at a standard variable cost of 30.00/unit:

        revenue      = 7,200.00 (ledger)
        variable     = 15 x 30.00 = 450.00
        contribution = 7,200 - 450 = 6,750.00
        """
        product = self.env['product.product'].create({
            'name': 'Ledger Widget',
            'type': 'consu',
            'property_account_income_id': self.account_revenue.id,
        })
        card = self.env['eh.cost.card'].create({
            'product_id': product.id,
            'line_ids': [(0, 0, {
                'element': 'material', 'std_qty': 1.0, 'std_price': 30.0})],
        })
        card.action_activate()

        def invoice(day, lines):
            move = self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': self.partner_a.id,
                'invoice_date': day,
                'date': day,
                'invoice_line_ids': [(0, 0, {
                    'product_id': product.id,
                    'quantity': qty,
                    'price_unit': price,
                    'account_id': self.account_revenue.id,
                    'tax_ids': [(5, 0, 0)],
                }) for qty, price in lines],
            })
            move.action_post()
            return move

        invoice('2026-02-10', [(10.0, 500.0), (5.0, 440.0)])
        invoice('2026-05-15', [(3.0, 999.0)])  # outside the period

        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01', 'period_end': '2026-03-31',
            'fixed_costs': 1000.0,
            'line_ids': [(0, 0, {
                'card_id': card.id, 'units_sold': 15.0,
                'revenue_source': 'ledger'})],
        })
        report.action_fetch_ledger_revenue()
        line = report.line_ids
        self.assertAlmostEqual(line.revenue, 7200.0, places=2)
        self.assertAlmostEqual(line.variable_cost, 450.0, places=2)
        self.assertAlmostEqual(line.contribution, 6750.0, places=2)
        self.assertAlmostEqual(report.total_revenue, 7200.0, places=2)
        # Done freezes the report and its lines.
        report.action_done()
        with self.assertRaises(UserError):
            report.fixed_costs = 2000.0
        with self.assertRaises(UserError):
            line.units_sold = 20.0
        with self.assertRaises(UserError):
            report.action_fetch_ledger_revenue()

    # ------------------------------------------------------------------
    # guardrails
    # ------------------------------------------------------------------

    def test_golden_one_active_card_per_product(self):
        """One active card per product and company: a raw state write on a
        second card is refused; the Activate action supersedes the old
        card instead."""
        product = self.env['product.product'].create(
            {'name': 'Single Standard Widget'})
        card1 = self._standard_card(
            product_id=product.id, item_name=False)
        card2 = self.env['eh.cost.card'].create({
            'product_id': product.id,
            'line_ids': [(0, 0, {
                'element': 'material', 'std_qty': 2.0, 'std_price': 5.5})],
        })
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            card2.write({'state': 'active'})
        self.env.invalidate_all()
        self.assertEqual(card2.state, 'draft')
        card2.action_activate()
        self.assertEqual(card2.state, 'active')
        self.assertEqual(card1.state, 'superseded')
        # An activated card's standards are frozen; a revision is a new
        # card.
        with self.assertRaises(UserError):
            card2.normal_capacity = 500.0
        with self.assertRaises(UserError):
            card2.line_ids[0].std_price = 6.0
        with self.assertRaises(UserError):
            card2.line_ids.create({
                'card_id': card2.id, 'element': 'labour',
                'std_qty': 1.0, 'std_price': 1.0})
        with self.assertRaises(UserError):
            card2.line_ids.unlink()

    def test_golden_variance_lines_engine_only(self):
        """Variance lines are engine output: a manual line, edit or
        deletion is refused so the reconciliation identity cannot be
        broken by hand."""
        card = self._standard_card()
        run = self._run(self._golden_actual(card))
        run.action_compute()
        with self.assertRaises(UserError):
            self.env['eh.cost.variance.line'].create({
                'run_id': run.id, 'actual_id': run.actual_ids.id,
                'name': 'Hand-keyed', 'element': 'material',
                'kind': 'price', 'amount': 123.0})
        with self.assertRaises(UserError):
            run.line_ids[0].amount = 1.0
        with self.assertRaises(UserError):
            run.line_ids[0].unlink()
        # Reset clears the decomposition through the engine path.
        run.action_reset_to_draft()
        self.assertFalse(run.line_ids)
        self.assertEqual(run.state, 'draft')
        self.assertAlmostEqual(run.total_variance, 0.0, places=2)
