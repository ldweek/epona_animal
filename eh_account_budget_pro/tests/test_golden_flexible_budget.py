# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden worked examples for the flexible-budget engine.

Every expected amount is hand-derived from the inputs stated in the test;
the derivation sits in a comment next to the assertion. Nothing is read
back from the engine to build an expectation.

Conventions under test (as implemented by eh.budget.line._compute_flex,
models/budget.py):

* activity ratio = actual activity / budgeted activity (1.0 when no
  activity data, so unflexed budgets behave exactly as before).
* flexed = fixed component + variable component x ratio.
* volume variance = flexed - budgeted; flexed variance is derived as
  static - volume, so static = flexed variance + volume variance holds
  by construction.
* flexed quantity = budgeted qty x ratio. Price variance is taken at
  ACTUAL quantity ((actual price - budgeted price) x actual qty);
  efficiency variance at BUDGETED price ((actual qty - flexed qty) x
  budgeted price). With consistent data (posted actual = actual qty x
  actual price, budget = budgeted qty x budgeted price) the two sum
  exactly to the flexed variance.
* Sign: positive = adverse on expense lines (actual beyond allowance).
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_budget_pro', 'post_install', '-at_install')
class TestGoldenFlexibleBudget(EhGoldenTestCase):
    """Flexed budget, three-way tie, and the revenue-proxy driver."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.flex_opex = cls._ensure_account(
            cls.env, '5310', 'Flex Semi Opex', 'expense')

    def _fy_budget(self, code, lines, activities=None):
        return self.Budget.create({
            'code': code,
            'name': code,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, line) for line in lines],
            'activity_period_ids': [(0, 0, act) for act in (activities or [])],
        })

    def test_semi_variable_flex_and_three_way_tie(self):
        """Golden: fixed 10,000 + variable 5/unit over 1,000 budgeted units.

        Inputs: budgeted amount 15,000 (fixed portion 10,000, variable
        5,000 = 1,000 units x 5), budgeted activity 1,000 units, actual
        activity 1,200 units, posted actual cost 17,500.

        Hand derivation:
          ratio           = 1,200 / 1,000                  = 1.2
          flexed          = 10,000 + 5,000 x 1.2           = 16,000.00
          flexed variance = 17,500 - 16,000                =  1,500.00 adverse
          volume variance = 16,000 - 15,000                =  1,000.00
          static variance = 17,500 - 15,000                =  2,500.00
          tie: 2,500 = 1,500 + 1,000 exactly.
        """
        budget = self._fy_budget(
            'golden_flex_semi',
            lines=[{
                'account_id': self.flex_opex.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 15000.0,
                'behaviour': 'semi_variable',
                'fixed_portion': 10000.0,
            }],
            activities=[{
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 1000.0,
                'actual_activity': 1200.0,
            }],
        )
        self.post_balanced_move(
            [
                {'account': self.flex_opex, 'debit': 17500.0},
                {'account': self.account_cash, 'credit': 17500.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        line = budget.line_ids
        self.assertAlmostEqual(
            line.budgeted_activity, 1000.0, places=4,
            msg="budgeted activity must come from the register")
        self.assertAlmostEqual(
            line.actual_activity, 1200.0, places=4,
            msg="actual activity must come from the register")
        self.assertAlmostEqual(
            line.activity_ratio, 1.2, places=4,
            msg="ratio = 1,200 / 1,000")
        self.assertAlmostEqual(
            line.actual_amount, 17500.0, places=2,
            msg="posted actual must be 17,500")
        # flexed = 10,000 fixed + 5,000 variable x 1.2 = 16,000
        self.assertAlmostEqual(
            line.flexed_amount, 16000.0, places=2,
            msg="flexed = 10,000 + 5,000 x 1.2")
        # flexed variance = 17,500 - 16,000 = 1,500 adverse
        self.assertAlmostEqual(
            line.flexed_variance, 1500.0, places=2,
            msg="flexed variance = actual 17,500 - flexed 16,000")
        # volume variance = 16,000 - 15,000 = 1,000
        self.assertAlmostEqual(
            line.volume_variance, 1000.0, places=2,
            msg="volume variance = flexed 16,000 - budget 15,000")
        # static = 17,500 - 15,000 = 2,500
        self.assertAlmostEqual(
            line.variance_amount, 2500.0, places=2,
            msg="static variance = actual 17,500 - budget 15,000")
        # the three-way tie, asserted against the literal decomposition
        self.assertAlmostEqual(
            line.variance_amount,
            line.flexed_variance + line.volume_variance, places=2,
            msg="static must equal flexed variance + volume variance")
        # budget-level rollups over the single line
        self.assertAlmostEqual(
            budget.total_flexed, 16000.0, places=2,
            msg="budget total flexed must roll up the line")
        self.assertAlmostEqual(
            budget.total_flexed_variance, 1500.0, places=2,
            msg="budget total flexed variance must roll up the line")
        self.assertAlmostEqual(
            budget.total_volume_variance, 1000.0, places=2,
            msg="budget total volume variance must roll up the line")

    def test_fixed_line_never_flexes(self):
        """A fixed line ignores activity: flexed = budgeted, volume 0,
        flexed variance = static variance (the historical behaviour)."""
        budget = self._fy_budget(
            'golden_flex_fixed',
            lines=[{
                'account_id': self.flex_opex.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 9000.0,
                'behaviour': 'fixed',
            }],
            activities=[{
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 1000.0,
                'actual_activity': 1300.0,
            }],
        )
        self.post_balanced_move(
            [
                {'account': self.flex_opex, 'debit': 9600.0},
                {'account': self.account_cash, 'credit': 9600.0},
            ],
            date=fields.Date.from_string('2026-03-10'),
        )
        line = budget.line_ids
        self.assertAlmostEqual(
            line.flexed_amount, 9000.0, places=2,
            msg="fixed line: flexed = budgeted even at 130% activity")
        self.assertAlmostEqual(
            line.volume_variance, 0.0, places=2,
            msg="fixed line: volume variance is zero")
        # static = flexed variance = 9,600 - 9,000 = 600
        self.assertAlmostEqual(
            line.flexed_variance, 600.0, places=2,
            msg="fixed line: flexed variance equals the static variance")
        self.assertAlmostEqual(
            line.variance_amount, 600.0, places=2,
            msg="static variance = 9,600 - 9,000")

    def test_revenue_line_proxy_driver(self):
        """Golden: variable cost line driven by a revenue-line proxy.

        Inputs: revenue line budgeted 10,000 with 12,000 posted revenue
        (credit, sign-normalised to +12,000); variable cost line
        budgeted 4,000 with 5,000 posted cost.

        Hand derivation:
          ratio           = 12,000 / 10,000        = 1.2
          flexed          = 4,000 x 1.2            = 4,800.00
          flexed variance = 5,000 - 4,800          =   200.00 adverse
          volume variance = 4,800 - 4,000          =   800.00
          static variance = 5,000 - 4,000          = 1,000.00 = 200 + 800.
        """
        budget = self._fy_budget(
            'golden_flex_proxy',
            lines=[
                {
                    'account_id': self.account_revenue.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'budgeted_amount': 10000.0,
                    'behaviour': 'fixed',
                },
                {
                    'account_id': self.flex_opex.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'budgeted_amount': 4000.0,
                    'behaviour': 'variable',
                },
            ],
        )
        revenue_line = budget.line_ids.filtered(
            lambda l: l.account_id == self.account_revenue)
        cost_line = budget.line_ids - revenue_line
        cost_line.write({
            'driver': 'revenue_line',
            'driver_line_id': revenue_line.id,
        })
        self.post_balanced_move(
            [
                {'account': self.account_revenue, 'credit': 12000.0},
                {'account': self.account_cash, 'debit': 12000.0},
            ],
            date=fields.Date.from_string('2026-04-15'),
        )
        self.post_balanced_move(
            [
                {'account': self.flex_opex, 'debit': 5000.0},
                {'account': self.account_cash, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-05-15'),
        )
        self.assertAlmostEqual(
            cost_line.budgeted_activity, 10000.0, places=2,
            msg="proxy budgeted activity = driver line budgeted amount")
        self.assertAlmostEqual(
            cost_line.actual_activity, 12000.0, places=2,
            msg="proxy actual activity = driver line normalised actual")
        self.assertAlmostEqual(
            cost_line.flexed_amount, 4800.0, places=2,
            msg="flexed = 4,000 x 1.2")
        self.assertAlmostEqual(
            cost_line.flexed_variance, 200.0, places=2,
            msg="flexed variance = 5,000 - 4,800")
        self.assertAlmostEqual(
            cost_line.volume_variance, 800.0, places=2,
            msg="volume variance = 4,800 - 4,000")
        self.assertAlmostEqual(
            cost_line.variance_amount, 1000.0, places=2,
            msg="static variance = 5,000 - 4,000")
        self.assertAlmostEqual(
            cost_line.variance_amount,
            cost_line.flexed_variance + cost_line.volume_variance, places=2,
            msg="three-way tie must hold under the proxy driver")


@tagged('eh_golden', 'eh_account_budget_pro', 'post_install', '-at_install')
class TestGoldenPriceEfficiencySplit(EhGoldenTestCase):
    """Price / efficiency decomposition of the flexed variance."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.materials = cls._ensure_account(
            cls.env, '5320', 'Flex Materials', 'expense')

    def test_price_and_efficiency_tie_to_flexed_variance(self):
        """Golden: budget 1,000 units at 5.00; actual 1,150 at 5.30;
        flexed quantity allowance 1,200.

        Convention: flexed qty = budgeted qty x activity ratio; price
        variance at ACTUAL qty; efficiency variance at BUDGETED price
        against the flexed qty allowance.

        Inputs: budgeted amount 5,000 (= 1,000 x 5.00), budgeted
        activity 1,000, actual activity 1,200 (ratio 1.2), actual
        1,150 units at 5.30, posted actual cost 6,095 (= 1,150 x 5.30).

        Hand derivation:
          flexed qty      = 1,000 x 1.2               = 1,200
          flexed amount   = 5,000 x 1.2               = 6,000.00
          price variance  = (5.30 - 5.00) x 1,150     =   345.00 adverse
          efficiency var  = (1,150 - 1,200) x 5.00    =  -250.00 favourable
          flexed variance = 6,095 - 6,000             =    95.00 adverse
          tie: 345 - 250 = 95 exactly; residual 0.
        """
        budget = self.Budget.create({
            'code': 'golden_split',
            'name': 'golden_split',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.materials.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 5000.0,
                'behaviour': 'variable',
                'budgeted_qty': 1000.0,
                'budgeted_unit_price': 5.0,
                'actual_qty': 1150.0,
                'actual_unit_price': 5.3,
            })],
            'activity_period_ids': [(0, 0, {
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 1000.0,
                'actual_activity': 1200.0,
            })],
        })
        self.post_balanced_move(
            [
                {'account': self.materials, 'debit': 6095.0},
                {'account': self.account_cash, 'credit': 6095.0},
            ],
            date=fields.Date.from_string('2026-05-20'),
        )
        line = budget.line_ids
        self.assertAlmostEqual(
            line.flexed_qty, 1200.0, places=4,
            msg="flexed qty = 1,000 budgeted units x ratio 1.2")
        self.assertAlmostEqual(
            line.flexed_amount, 6000.0, places=2,
            msg="flexed = 5,000 x 1.2")
        self.assertAlmostEqual(
            line.price_variance, 345.0, places=2,
            msg="price variance = 0.30 x 1,150 actual units")
        self.assertAlmostEqual(
            line.efficiency_variance, -250.0, places=2,
            msg="efficiency = (1,150 - 1,200) x 5.00, favourable")
        self.assertAlmostEqual(
            line.flexed_variance, 95.0, places=2,
            msg="flexed variance = 6,095 - 6,000")
        self.assertAlmostEqual(
            line.price_variance + line.efficiency_variance,
            line.flexed_variance, places=2,
            msg="price + efficiency must tie to the flexed variance")
        self.assertAlmostEqual(
            line.split_residual, 0.0, places=2,
            msg="consistent data leaves no split residual")
        # three-way tie still holds on top of the split
        # static = 6,095 - 5,000 = 1,095; volume = 6,000 - 5,000 = 1,000
        self.assertAlmostEqual(
            line.variance_amount, 1095.0, places=2,
            msg="static variance = 6,095 - 5,000")
        self.assertAlmostEqual(
            line.volume_variance, 1000.0, places=2,
            msg="volume variance = 6,000 - 5,000")
        self.assertAlmostEqual(
            line.variance_amount,
            line.flexed_variance + line.volume_variance, places=2,
            msg="static = flexed variance + volume variance")

    def test_split_zero_without_quantity_data(self):
        """No quantity data: the split stays zero and only the flex
        decomposition reports."""
        budget = self.Budget.create({
            'code': 'golden_split_none',
            'name': 'golden_split_none',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.materials.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 5000.0,
                'behaviour': 'variable',
            })],
            'activity_period_ids': [(0, 0, {
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 1000.0,
                'actual_activity': 1200.0,
            })],
        })
        line = budget.line_ids
        self.assertAlmostEqual(
            line.price_variance, 0.0, places=2,
            msg="no qty data: price variance must stay zero")
        self.assertAlmostEqual(
            line.efficiency_variance, 0.0, places=2,
            msg="no qty data: efficiency variance must stay zero")
        self.assertAlmostEqual(
            line.split_residual, 0.0, places=2,
            msg="no qty data: no residual")
        self.assertAlmostEqual(
            line.flexed_amount, 6000.0, places=2,
            msg="flex still applies without qty data: 5,000 x 1.2")


@tagged('eh_golden', 'eh_account_budget_pro', 'post_install', '-at_install')
class TestGoldenReforecast(EhGoldenTestCase):
    """Rolling reforecast: baseline intact, revision stored, variance vs
    revision differs from variance vs baseline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.refc_opex = cls._ensure_account(
            cls.env, '5330', 'Reforecast Opex', 'expense')

    def _monthly_budget(self, code):
        """FY2026 budget: 12 monthly lines of 1,250 on refc_opex."""
        months = self.Budget._months_in_range(
            fields.Date.from_string('2026-01-01'),
            fields.Date.from_string('2026-12-31'),
        )
        return self.Budget.create({
            'code': code,
            'name': code,
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
            'line_ids': [(0, 0, {
                'account_id': self.refc_opex.id,
                'period_from': m_from,
                'period_to': m_to,
                'budgeted_amount': 1250.0,
            }) for (m_from, m_to) in months],
        })

    @freeze_time('2026-07-15')
    def test_reforecast_snapshot_and_variance_basis(self):
        """Golden: six elapsed months of trending actuals, six remaining
        months re-projected by linear trend.

        Posted actuals (15th of each month): Jan 1,000; Feb 1,100;
        Mar 1,200; Apr 1,300; May 1,400; Jun 1,500. Frozen today is
        2026-07-15, so Jan-Jun are elapsed (period fully before today)
        and Jul-Dec remain.

        Projection derivation: the trailing history window is the 24
        months ending 2026-06-30; after dropping leading all-zero
        months the series is [1000, 1100, 1200, 1300, 1400, 1500]
        (6 points -> linear trend). OLS on indices 0..5 gives slope 100,
        intercept 1,000 exactly, so indices 6..11 project to 1,600,
        1,700, 1,800, 1,900, 2,000, 2,100.
        """
        budget = self._monthly_budget('golden_refc')
        budget.action_confirm()
        for month, amount in ((1, 1000.0), (2, 1100.0), (3, 1200.0),
                              (4, 1300.0), (5, 1400.0), (6, 1500.0)):
            self.post_balanced_move(
                [
                    {'account': self.refc_opex, 'debit': amount},
                    {'account': self.account_cash, 'credit': amount},
                ],
                date=fields.Date.from_string('2026-%02d-15' % month),
            )
        baseline_amounts = budget.line_ids.mapped('budgeted_amount')

        budget.action_reforecast()

        # -- baseline untouched --
        self.assertEqual(
            len(budget.line_ids), 12,
            "reforecast must not add or remove baseline lines")
        self.assertEqual(
            budget.line_ids.mapped('budgeted_amount'), baseline_amounts,
            "reforecast must not change baseline amounts")
        self.assertEqual(
            budget.state, 'confirmed',
            "reforecast must not move the budget state")

        # -- revision stored --
        self.assertEqual(
            budget.revision_count, 1, "one revision after one reforecast")
        revision = budget.active_revision_id
        self.assertEqual(
            revision.revision_date,
            fields.Date.from_string('2026-07-15'),
            "revision must be stamped with the reforecast date")
        self.assertEqual(
            len(revision.line_ids), 12,
            "snapshot must carry one row per budget month")
        # elapsed months at actuals: Jan..Jun = 1,000..1,500
        expected = {
            1: (1000.0, 'actual'), 2: (1100.0, 'actual'),
            3: (1200.0, 'actual'), 4: (1300.0, 'actual'),
            5: (1400.0, 'actual'), 6: (1500.0, 'actual'),
            # remaining months by linear trend: slope 100, intercept
            # 1,000, indices 6..11 -> 1,600..2,100
            7: (1600.0, 'linear_trend'), 8: (1700.0, 'linear_trend'),
            9: (1800.0, 'linear_trend'), 10: (1900.0, 'linear_trend'),
            11: (2000.0, 'linear_trend'), 12: (2100.0, 'linear_trend'),
        }
        for rev_line in revision.line_ids:
            month = rev_line.period_from.month
            exp_amount, exp_source = expected[month]
            self.assertAlmostEqual(
                rev_line.amount, exp_amount, places=2,
                msg="revision amount for 2026-%02d" % month)
            self.assertEqual(
                rev_line.source, exp_source,
                "revision source for 2026-%02d" % month)
        self.assertAlmostEqual(
            revision.total_amount,
            # 1,000+...+1,500 = 7,500 actual; 1,600+...+2,100 = 11,100
            18600.0, places=2,
            msg="revision total = 7,500 actuals + 11,100 projected")

        # -- variance vs revision differs from variance vs baseline --
        jan = budget.line_ids.filtered(
            lambda l: l.period_from.month == 1)
        jul = budget.line_ids.filtered(
            lambda l: l.period_from.month == 7)
        # Jan: actual 1,000 vs baseline 1,250 -> -250; vs revision
        # 1,000 -> 0 (the revision absorbed the actual).
        self.assertAlmostEqual(
            jan.variance_amount, -250.0, places=2,
            msg="Jan baseline variance = 1,000 - 1,250")
        self.assertAlmostEqual(
            jan.revision_amount, 1000.0, places=2,
            msg="Jan revision amount is the snapshot actual")
        self.assertAlmostEqual(
            jan.revision_variance, 0.0, places=2,
            msg="Jan variance vs revision = 1,000 - 1,000")
        # Jul: no actual yet; baseline variance -1,250, revision
        # variance -1,600 (the reforecast expects more spend).
        self.assertAlmostEqual(
            jul.variance_amount, -1250.0, places=2,
            msg="Jul baseline variance = 0 - 1,250")
        self.assertAlmostEqual(
            jul.revision_amount, 1600.0, places=2,
            msg="Jul revision amount is the projected 1,600")
        self.assertAlmostEqual(
            jul.revision_variance, -1600.0, places=2,
            msg="Jul variance vs revision = 0 - 1,600")

        # -- a second reforecast stacks, never overwrites --
        budget.action_reforecast()
        self.assertEqual(
            budget.revision_count, 2,
            "second reforecast must add a second revision")
        self.assertNotEqual(
            budget.active_revision_id, revision,
            "latest revision must supersede for reporting")
        self.assertTrue(
            revision.exists(),
            "the first revision must remain as history")

    def test_reforecast_requires_confirmed_state(self):
        budget = self._monthly_budget('golden_refc_draft')
        with self.assertRaises(UserError, msg=(
                "draft budgets have no locked baseline to revise")):
            budget.action_reforecast()

    def test_reforecast_requires_lines(self):
        budget = self.Budget.create({
            'code': 'golden_refc_nolines',
            'name': 'golden_refc_nolines',
            'date_from': fields.Date.from_string('2026-01-01'),
            'date_to': fields.Date.from_string('2026-12-31'),
        })
        # force past the confirm gate to hit the reforecast guard
        budget.state = 'confirmed'
        with self.assertRaises(UserError, msg=(
                "a budget without lines has nothing to reforecast")):
            budget.action_reforecast()
