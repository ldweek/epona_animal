# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 23 worked examples for borrowing cost capitalisation.

Each test is a hand-computed worked example in the spirit of the standard's
illustrative material: the inputs are stated in the test, every expected
amount is derived by hand in a comment, and the posted journal entry is
asserted exactly. No expected value is read back from the engine under test.

Day-count convention of the engine (see
eh_account_borrowing_costs/models/borrowing_cost.py, _weighted_average_base):
actual-day count with inclusive business-date endpoints,

    weight = ((period_end - expenditure_date).days + 1)
             / ((period_end - period_start).days + 1)

so a full calendar year 2026-01-01 .. 2026-12-31 spans 365 days,
and expenditure dated 2026-07-01 is outstanding 184/365 of that period. In
the suspended (windowed) case the DENOMINATOR stays the full nominal period;
suspensions only remove days from the numerator (_windowed_base).
"""

from datetime import date

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_borrowing_costs', 'post_install',
        '-at_install')
class TestGoldenIas23(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.asset = cls._ensure_account(
            cls.env, '1680', 'Asset Under Construction', 'asset_non_current')
        cls.interest = cls._ensure_account(
            cls.env, '5680', 'Interest Expense', 'expense')

    def _rec(self, **vals):
        base = {
            'name': '/', 'qualifying_asset': 'New plant',
            'asset_account_id': self.asset.id,
            'borrowing_cost_account_id': self.interest.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.borrowing.cost'].create(base)

    def test_golden_weighted_average_mid_period_spend(self):
        # IAS 23.14 weighted-average accumulated expenditure, general
        # borrowings only, with a mid-period spend.
        #
        # Capitalisation period 2026-01-01 .. 2026-12-31.
        #   total_days = (2026-12-31 - 2026-01-01).days + 1 = 365
        # Expenditure:
        #   1,200,000 on 2026-01-01 -> outstanding 365/365 of the period
        #     contribution = 1,200,000 x 365/365      = 1,200,000.0000
        #     600,000 on 2026-07-01 -> outstanding (2026-12-31 - 2026-07-01)
        #     + 1 = 184 days
        #     contribution = 600,000 x 184/365        =   302,465.7534
        #   weighted-average base                     = 1,502,465.7534
        #     rounded to company currency (2dp)       = 1,502,465.75
        # Capitalisation rate 10%:
        #   general = 1,502,465.7534 x 10%            =   150,246.5753
        #     rounded                                 =   150,246.58
        # No specific borrowing, no cap -> capitalisable = 150,246.58.
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ])
        self.assertAlmostEqual(
            rec.weighted_average_base, 1502465.75, places=2)
        self.assertAlmostEqual(rec.capitalisable, 150246.58, places=2)
        # The posted entry reclassifies exactly that amount from interest
        # expense to the qualifying asset.
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        self.assertMoveLines(rec.move_id, [
            (self.asset, 150246.58, 0.0),
            (self.interest, 0.0, 150246.58),
        ])
        self.assertBalanced(rec.move_id)

    def test_golden_specific_net_of_temporary_investment_income(self):
        # IAS 23.12-13: borrowing costs on specific borrowings are
        # capitalised net of income on the temporary investment of those
        # borrowings.
        #
        #   specific borrowing cost                   = 80,000
        #   temporary investment income               = 12,000
        #   specific capitalisable = 80,000 - 12,000  = 68,000
        rec = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=12000.0)
        self.assertAlmostEqual(rec.capitalisable, 68000.0, places=2)
        rec.action_capitalise()
        self.assertMoveLines(rec.move_id, [
            (self.asset, 68000.0, 0.0),
            (self.interest, 0.0, 68000.0),
        ])
        self.assertBalanced(rec.move_id)

        # Floor at zero: income exceeding the specific cost reduces the
        # specific component to nil, never below.
        #   specific net = max(80,000 - 95,000, 0)    = 0 (not -15,000)
        rec_floor = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=95000.0)
        self.assertAlmostEqual(rec_floor.capitalisable, 0.0, places=2)

        # And the excess income must NOT spill over and erode the general
        # component (IAS 23.12: the income belongs to the specific
        # borrowing's own capitalisable amount only).
        #   specific net = max(80,000 - 95,000, 0)    = 0
        #   general      = 200,000 x 5%               = 10,000
        #   capitalisable                             = 10,000
        # A spill defect would net the -15,000 excess into the general
        # pool and give -5,000 (or a floored 0).
        rec_spill = self._rec(
            specific_borrowing_cost=80000.0,
            temporary_investment_income=95000.0,
            general_expenditure=200000.0,
            capitalisation_rate=5.0)
        self.assertAlmostEqual(rec_spill.capitalisable, 10000.0, places=2)

    def test_golden_suspension_excludes_three_months(self):
        # IAS 23.20-21: capitalisation is suspended while active development
        # is suspended. Same inputs as the weighted-average example, with
        # development suspended for three full months, 2026-03-01 to
        # 2026-06-01 (Mar 31 + Apr 30 + May 31 = 92 days).
        #
        # Engine convention (_windowed_base): the denominator remains the
        # full nominal period (365 days); the suspension removes days from
        # each line's outstanding-day numerator only.
        #
        # Active spans: (2026-01-01 .. 2026-03-01) and
        #               (2026-06-01 .. 2026-12-31).
        # Line 1: 1,200,000 dated 2026-01-01
        #   span 1 days = (2026-03-01 - 2026-01-01).days = 59
        #   span 2 days = (2026-12-31 - 2026-06-01).days + 1 = 214
        #   outstanding = 273 (= 365 - 92)
        #   contribution = 1,200,000 x 273/365        =   897,534.2466
        # Line 2: 600,000 dated 2026-07-01 (inside span 2 only)
        #   outstanding = (2026-12-31 - 2026-07-01).days + 1 = 184
        #   contribution = 600,000 x 184/365          =   302,465.7534
        # Base = (1,200,000 x 273 + 600,000 x 184) / 365
        #      = 438,000,000 / 365                    = 1,200,000.0000
        # Capitalisable = 1,200,000 x 10%             =   120,000.00
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ],
            suspension_line_ids=[
                (0, 0, {'date_start': date(2026, 3, 1),
                        'date_end': date(2026, 6, 1)}),
            ])
        self.assertTrue(rec._has_capitalisation_window())
        self.assertAlmostEqual(
            rec.weighted_average_base, 1200000.0, places=2)
        self.assertAlmostEqual(rec.capitalisable, 120000.0, places=2)
        # Strictly below the unsuspended figure (150,246.58) by the
        # suspended slice of line 1: 1,200,000 x 92/365 x 10% = 30,246.58.
        self.assertNotAlmostEqual(rec.capitalisable, 150246.58, places=2)

    def test_golden_capped_at_actual_borrowing_cost(self):
        # IAS 23.14: the amount capitalised in a period shall not exceed the
        # borrowing costs incurred in that period. Same weighted-average
        # setup as the first example:
        #   uncapped = 1,502,465.7534 x 10%           =   150,246.5753
        #     rounded                                 =   150,246.58
        # Actual borrowing cost incurred              =    50,000
        #   capitalisable = min(150,164.84, 50,000)   =    50,000 exactly
        rec = self._rec(
            capitalisation_rate=10.0,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 12, 31),
            actual_borrowing_cost=50000.0,
            expenditure_line_ids=[
                (0, 0, {'date': date(2026, 1, 1), 'amount': 1200000.0}),
                (0, 0, {'date': date(2026, 7, 1), 'amount': 600000.0}),
            ])
        self.assertAlmostEqual(rec.uncapped_amount, 150246.58, places=2)
        self.assertAlmostEqual(rec.capitalisable, 50000.0, places=2)
        # The posted entry reclassifies interest expense to the qualifying
        # asset for exactly the capped amount, not the uncapped figure.
        rec.action_capitalise()
        self.assertEqual(rec.state, 'capitalised')
        self.assertMoveLines(rec.move_id, [
            (self.asset, 50000.0, 0.0),
            (self.interest, 0.0, 50000.0),
        ])
        self.assertBalanced(rec.move_id)
