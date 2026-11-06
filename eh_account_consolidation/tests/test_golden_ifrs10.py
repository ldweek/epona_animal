# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 10 / IAS 28 / IFRS 3 / IAS 21.48 consolidation mechanics.

Hand-computed worked examples for the Phase 2 consolidation automation:
proportional scaling (translate first, scale second), mandatory equity-method
configuration with idempotent IAS 28 pick-up and the IAS 28.1A fair value
option, the automatic IFRS 3 investment elimination (goodwill, bargain
purchase, historical-rate translation of acquisition equity), the fair-value
NCI measurement basis, member-disposal CTA recycling, and the IFRS 10
B87/B92-93 policy and reporting-date guards.

Rate convention (identical to test_golden_ias21.py): rate records live on the
presentation currency with company_id = the subsidiary company, so a rate of
R means 1 subsidiary-currency unit buys R presentation units, and the engine
MULTIPLIES subsidiary balances by that factor (closing factor for B/S,
day-weighted average for P&L).

FX fixture used by the FX classes below:

    2026-01-01 .. 2026-01-05: 1 sub unit = 0.80 pres units
    2026-01-06 onwards:       1 sub unit = 0.90 pres units
    period 2026-01-01 .. 2026-01-10
    closing = 0.90; average = (5 x 0.80 + 5 x 0.90) / 10 = 0.85

Flat-rate classes pin a single 1.0 rate so elimination arithmetic is a clean
scalar with no CTA noise.
"""

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase

from .common import (
    add_acquisition_component,
    make_account as _make_account,
)


class _GoldenConsolBase(EhGoldenTestCase):
    """Shared parent-company scaffolding.

    Subclasses set RATES = [(day, rate), ...] on the presentation currency
    (quoted as presentation units per 1 subsidiary unit, booked against the
    subsidiary company per the module's rate recipe).
    """

    PERIOD_FROM = '2026-01-01'
    PERIOD_TO = '2026-01-10'
    BOOK_DATE = '2026-01-05'
    CCY_NAME = 'TGX'
    RATES = [('2026-01-01', 1.0)]

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': self.CCY_NAME, 'symbol': 'X',
            'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        for day, rate in self.RATES:
            self._set_rate(self.pres_ccy, day, rate)
        self.parent_company = self.env['res.company'].create({
            'name': 'Golden Consol Parent %s' % self.CCY_NAME,
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        self.manager_group = self.env.ref('eh_account_base.group_eh_manager')
        # Consolidated-chart accounts on the parent company.
        self.cta_account = _make_account(
            self.env, self.parent_company, '3900',
            'Currency Translation Reserve', 'equity')
        self.nci_account = _make_account(
            self.env, self.parent_company, '3200',
            'Non-Controlling Interest', 'equity')
        self.re_account = _make_account(
            self.env, self.parent_company, '3100',
            'Consolidated Retained Earnings', 'equity_unaffected')
        self.investment_account = _make_account(
            self.env, self.parent_company, '1500',
            'Investment in Sub', 'asset_non_current')
        self.equity_elim_account = _make_account(
            self.env, self.parent_company, '3150',
            'Pre-Acq Equity Elimination', 'equity')
        self.goodwill_account = _make_account(
            self.env, self.parent_company, '1600',
            'Goodwill', 'asset_non_current')
        self.sop_account = _make_account(
            self.env, self.parent_company, '4100',
            'Share of Profit of Associates', 'income')
        self.fx_gain_account = _make_account(
            self.env, self.parent_company, '7100',
            'FX Recycling Gain', 'income_other')
        self.fx_loss_account = _make_account(
            self.env, self.parent_company, '7200',
            'FX Recycling Loss', 'expense')
        self.bargain_gain_account = _make_account(
            self.env, self.parent_company, '4900',
            'Bargain Purchase Gain', 'income_other')
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Golden Consol Group %s' % self.CCY_NAME,
            'code': 'golden_consol_%s' % self.CCY_NAME.lower(),
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'cta_account_id': self.cta_account.id,
            'nci_account_id': self.nci_account.id,
            'parent_retained_earnings_account_id': self.re_account.id,
            'cta_gain_account_id': self.fx_gain_account.id,
            'cta_loss_account_id': self.fx_loss_account.id,
            'bargain_purchase_gain_account_id': self.bargain_gain_account.id,
        })

    def _member(self, **vals):
        base = {
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        }
        base.update(vals)
        member = self.env['eh.consol.member'].create(base)
        if (
            member.investment_account_id
            and member.investment_amount > 0.0
            and member.acquisition_equity > 0.0
        ):
            add_acquisition_component(
                member,
                self.account_equity,
                member.acquisition_equity,
            )
        return member

    def _run(self, compute=True):
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': self.PERIOD_FROM,
            'period_to': self.PERIOD_TO,
        })
        for member in self.entity.member_ids.filtered(
            lambda candidate: candidate.method == 'equity'
            and not candidate.fv_option
            and candidate.functional_currency_id != self.pres_ccy
        ):
            self.env['eh.consol.equity.rollforward'].create({
                'run_id': run.id,
                'member_id': member.id,
                'foreign_exchange_confirmed': True,
                'basis': 'Golden fixture: explicit zero IAS 21 adjustment.',
            })
        if compute:
            run.action_compute()
        return run

    def assertRunBalances(self, run):
        total = sum(run.line_ids.mapped('amount'))
        self.assertTrue(
            float_is_zero(
                total, precision_rounding=self.pres_ccy.rounding),
            "the consolidation run must net to zero, got %r" % total)


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenProportional(_GoldenConsolBase):
    """Proportional method: ownership share applied to EVERY balance, AFTER
    translation, and NCI is structurally impossible."""

    CCY_NAME = 'TGP'
    RATES = [('2026-01-01', 0.80), ('2026-01-06', 0.90)]

    def test_golden_proportional_translates_then_scales(self):
        # 75%-owned proportional member. Books (subsidiary currency, Odoo
        # signed balances, debit positive):
        #   cash    1,000 debit  -> +1,000
        #   revenue 1,000 credit -> -1,000
        #
        # Rates: closing 0.90 (B/S), day-weighted average 0.85 (P&L).
        # Proportional scaling is applied AFTER translation:
        #
        #   cash    = +1,000 x 0.90 x 0.75 = +675.00
        #   revenue = -1,000 x 0.85 x 0.75 = -637.50
        #
        # Member residual = 675.00 - 637.50 = +37.50, so the member-tagged
        # CTA plug is -37.50 and the run nets to zero.
        member = self._member(method='proportional', ownership_pct=75.0)
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )
        run = self._run()
        sub_lines = run.line_ids.filtered(
            lambda l: l.kind == 'subsidiary_balance')
        self.assertEqual(len(sub_lines), 2)
        cash_line = sub_lines.filtered(
            lambda l: l.account_id == self.account_cash)
        revenue_line = sub_lines.filtered(
            lambda l: l.account_id == self.account_revenue)
        self.assertAlmostEqual(cash_line.amount, 675.00, places=2)
        self.assertAlmostEqual(revenue_line.amount, -637.50, places=2)
        # NCI is blocked for proportional members: no NCI line whatsoever.
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'nci'),
            "the proportional method must never carve NCI")
        # The member-tagged CTA plug balances the scaled residual.
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertEqual(len(cta_lines), 1)
        self.assertEqual(cta_lines.member_id, member)
        self.assertAlmostEqual(cta_lines.amount, -37.50, places=2)
        self.assertRunBalances(run)

    def test_golden_proportional_nci_account_blocked(self):
        with self.assertRaises(ValidationError):
            self._member(
                method='proportional', ownership_pct=60.0,
                nci_account_id=self.nci_account.id,
            )

    def test_golden_proportional_fair_value_basis_blocked(self):
        with self.assertRaises(ValidationError):
            self._member(
                method='proportional', ownership_pct=60.0,
                nci_basis='fair_value', nci_fair_value=100.0,
                acquisition_equity=500.0,
            )


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenEquityMethod(_GoldenConsolBase):
    """IAS 28: mandatory configuration, idempotent pick-up, IAS 28.1A fair
    value option. Flat 1.0 rate so the share arithmetic is a clean scalar."""

    CCY_NAME = 'TGE'
    RATES = [('2026-01-01', 1.0)]

    def _seed_profit(self, amount=1000.0):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': amount},
                {'account': self.account_revenue, 'credit': amount},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    def test_golden_equity_pickup_idempotent_across_recompute(self):
        # 40% associate, period profit 1,000 at rate 1.0.
        #   share = 0.40 x 1,000 = 400.00
        #   Dr investment +400.00 / Cr share of profit -400.00
        # Recompute (reset to draft + compute) must rebuild EXACTLY the same
        # two legs: the pick-up is keyed to the run and member and is
        # dropped with the run lines, never doubled.
        self.env.user.group_ids |= self.manager_group
        self._member(
            method='equity', ownership_pct=40.0,
            investment_account_id=self.investment_account.id,
            share_of_profit_account_id=self.sop_account.id,
        )
        self._seed_profit(1000.0)
        run = self._run()
        for _cycle in range(2):
            pickup = run.line_ids.filtered(
                lambda l: l.kind == 'equity_pickup')
            self.assertEqual(
                len(pickup), 2,
                "exactly two pick-up legs per compute, never doubled")
            inv_leg = pickup.filtered(
                lambda l: l.account_id == self.investment_account)
            sop_leg = pickup.filtered(
                lambda l: l.account_id == self.sop_account)
            self.assertAlmostEqual(inv_leg.amount, 400.00, places=2)
            self.assertAlmostEqual(sop_leg.amount, -400.00, places=2)
            self.assertRunBalances(run)
            run.action_reset_to_draft()
            run.action_compute()

    def test_golden_equity_carrying_rollforward_across_periods(self):
        """IAS 28 carrying value is cumulative and includes distributions,
        OCI and a documented IAS 21 foreign-associate adjustment."""
        self.env.user.group_ids |= self.manager_group
        dividend_income = _make_account(
            self.env, self.parent_company, '4200',
            'Associate Dividend Income', 'income_other',
        )
        associate_oci = _make_account(
            self.env, self.parent_company, '3300',
            'Associate OCI Reserve', 'equity',
        )
        member = self._member(
            method='equity', ownership_pct=40.0,
            investment_account_id=self.investment_account.id,
            share_of_profit_account_id=self.sop_account.id,
        )
        self._seed_profit(1000.0)
        first = self._run()
        first.action_review()

        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 500.0},
                {'account': self.account_revenue, 'credit': 500.0},
            ],
            date=fields.Date.from_string('2027-01-05'),
        )
        second = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2027-01-01',
            'period_to': '2027-01-10',
        })
        self.env['eh.consol.equity.rollforward'].create({
            'run_id': second.id,
            'member_id': member.id,
            'dividends_received': 100.0,
            'dividend_income_account_id': dividend_income.id,
            'oci_share': 40.0,
            'oci_account_id': associate_oci.id,
            'foreign_exchange_adjustment': 10.0,
            'foreign_exchange_confirmed': True,
            'basis': 'Board pack: dividend 100, OCI 40 and FX reserve 10.',
        })
        second.action_compute()

        pickup = second.line_ids.filtered(
            lambda line: line.kind == 'equity_pickup'
            and line.member_id == member
        )
        by_account = {}
        for line in pickup:
            by_account[line.account_id] = (
                by_account.get(line.account_id, 0.0) + line.amount
            )
        self.assertEqual(by_account, {
            self.investment_account: 550.0,
            self.re_account: -400.0,
            self.sop_account: -200.0,
            dividend_income: 100.0,
            associate_oci: -40.0,
            self.cta_account: -10.0,
        })
        self.assertEqual(len(pickup), 10)
        self.assertEqual(sum(pickup.mapped('amount')), 0.0)
        self.assertRunBalances(second)

    def test_golden_equity_missing_config_blocks_compute(self):
        # No investment / share-of-profit account: IAS 28 equity accounting
        # is mandatory, so the compute is refused, never silently skipped.
        self._member(method='equity', ownership_pct=40.0)
        self._seed_profit(1000.0)
        run = self._run(compute=False)
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids)

    def test_golden_equity_partial_config_blocks_compute(self):
        # Only one of the two accounts set still blocks.
        self._member(
            method='equity', ownership_pct=40.0,
            investment_account_id=self.investment_account.id,
        )
        self._seed_profit(1000.0)
        run = self._run(compute=False)
        with self.assertRaises(UserError):
            run.action_compute()

    def test_golden_fv_option_books_disclosure_not_pickup(self):
        # IAS 28.1A fair value option: no pick-up configuration required, no
        # pick-up booked; a zero-amount memo disclosure line records the
        # election instead.
        member = self._member(
            method='equity', ownership_pct=40.0, fv_option=True,
        )
        self._seed_profit(1000.0)
        run = self._run()
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'equity_pickup'),
            "no equity pick-up under the fair value option")
        disclosure = run.line_ids.filtered(lambda l: l.kind == 'disclosure')
        self.assertEqual(len(disclosure), 1)
        self.assertEqual(disclosure.member_id, member)
        self.assertAlmostEqual(disclosure.amount, 0.00, places=2)
        self.assertIn('28.1A', disclosure.notes)
        self.assertRunBalances(run)

    def test_golden_fv_option_requires_equity_method(self):
        with self.assertRaises(ValidationError):
            self._member(method='full', fv_option=True)


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenInvestmentElimination(_GoldenConsolBase):
    """IFRS 3 automatic investment elimination: goodwill, bargain purchase,
    historical-rate translation, and the entity-level off switch. Flat 1.0
    rate."""

    CCY_NAME = 'TGI'
    RATES = [('2026-01-01', 1.0)]

    def _seed_equity(self, amount=600.0):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': amount},
                {'account': self.account_equity, 'credit': amount},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    def _configured_member(self, **vals):
        base = {
            'method': 'full',
            'ownership_pct': 100.0,
            'investment_account_id': self.investment_account.id,
            'equity_elimination_account_id': self.equity_elim_account.id,
            'goodwill_account_id': self.goodwill_account.id,
            'nci_account_id': self.nci_account.id,
        }
        base.update(vals)
        return self._member(**base)

    def test_golden_goodwill_recognised(self):
        # 100% acquisition. Investment I = 800, acquisition-date equity
        # A = 600 (presentation currency; no historical rate).
        #
        #   Dr acquisition share-capital component +600.00
        #   Cr investment                 -800.00
        #   Cr NCI (1-1) x 600            -0.00
        #   Dr goodwill  I - o x A = 800 - 600 = +200.00  (kind 'goodwill')
        #
        # Legs sum to zero: 600 - 800 - 0 + 200 = 0.
        self._configured_member(
            investment_amount=800.0, acquisition_equity=600.0)
        self._seed_equity(600.0)
        run = self._run()
        equity_leg = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.member_id
            and l.account_id == self.account_equity)
        inv_leg = run.line_ids.filtered(
            lambda l: l.account_id == self.investment_account)
        gw_leg = run.line_ids.filtered(
            lambda l: l.account_id == self.goodwill_account)
        self.assertAlmostEqual(equity_leg.amount, 600.00, places=2)
        self.assertAlmostEqual(inv_leg.amount, -800.00, places=2)
        self.assertAlmostEqual(gw_leg.amount, 200.00, places=2)
        self.assertEqual(gw_leg.kind, 'goodwill',
                         "the goodwill residual is tagged by kind")
        self.assertFalse(
            run.line_ids.filtered(lambda line: line.kind == 'nci'),
            "a wholly owned acquisition must not emit a zero NCI row",
        )
        self.assertRunBalances(run)

    def test_acquisition_components_remove_each_source_equity_account(self):
        second_equity = _make_account(
            self.env, self.company, '3001',
            'Acquisition Reserve', 'equity',
        )
        member = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'method': 'full',
            'ownership_pct': 100.0,
            'investment_account_id': self.investment_account.id,
            'investment_amount': 800.0,
            'acquisition_equity': 600.0,
            'goodwill_account_id': self.goodwill_account.id,
        })
        add_acquisition_component(member, self.account_equity, 400.0)
        add_acquisition_component(member, second_equity, 200.0)
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 600.0},
                {'account': self.account_equity, 'credit': 400.0},
                {'account': second_equity, 'credit': 200.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )
        run = self._run()
        for account, expected_component in (
            (self.account_equity, 400.0),
            (second_equity, 200.0),
        ):
            component = run.line_ids.filtered(
                lambda line, target=account: line.kind == 'elimination'
                and line.member_id == member
                and line.account_id == target
            )
            self.assertEqual(len(component), 1)
            self.assertEqual(component.amount, expected_component)
            self.assertEqual(sum(run.line_ids.filtered(
                lambda line, target=account: line.account_id == target
            ).mapped('amount')), 0.0)
        goodwill = run.line_ids.filtered(
            lambda line: line.kind == 'goodwill'
            and line.member_id == member
        )
        self.assertEqual(goodwill.amount, 200.0)
        self.assertRunBalances(run)

    def test_golden_bargain_purchase_credit(self):
        # 100% acquisition. Consideration I = 500 against acquisition-date
        # equity A = 600: bargain purchase of 100.
        #
        #   bargain gain = I - o x A = 500 - 600 = -100.00 (credit)
        self._configured_member(
            investment_amount=500.0, acquisition_equity=600.0)
        self._seed_equity(600.0)
        run = self._run()
        gain_leg = run.line_ids.filtered(
            lambda l: l.account_id == self.bargain_gain_account)
        self.assertAlmostEqual(gain_leg.amount, -100.00, places=2)
        self.assertEqual(gain_leg.kind, 'bargain_purchase')
        self.assertFalse(run.line_ids.filtered(
            lambda line: line.account_id == self.goodwill_account))
        self.assertRunBalances(run)

    def test_golden_historical_rate_translates_acquisition_equity(self):
        # Acquisition-date equity stated in the FUNCTIONAL currency
        # (A_func = 600) with a historical rate of 0.50 presentation units
        # per functional unit (IAS 21.23(b): non-monetary historical figure
        # at the historical rate):
        #
        #   A_pres       = 600 x 0.50 = 300.00
        #   equity leg   = +300.00
        #   goodwill leg = I - o x A_pres = 800 - 300 = +500.00
        self._configured_member(
            investment_amount=800.0, acquisition_equity=600.0,
            historical_rate=0.50,
            acquisition_date=fields.Date.from_string('2025-06-30'))
        self._seed_equity(600.0)
        run = self._run()
        equity_leg = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.member_id
            and l.account_id == self.account_equity)
        gw_leg = run.line_ids.filtered(
            lambda l: l.account_id == self.goodwill_account)
        self.assertAlmostEqual(equity_leg.amount, 300.00, places=2)
        self.assertAlmostEqual(gw_leg.amount, 500.00, places=2)
        self.assertRunBalances(run)

    def test_historical_rate_requires_acquisition_date_evidence(self):
        with self.assertRaises(ValidationError):
            self._configured_member(
                investment_amount=800.0,
                acquisition_equity=600.0,
                historical_rate=0.50,
            )

    def test_golden_flag_off_keeps_warning_behaviour(self):
        # auto_eliminate_investment off: no elimination legs are generated
        # and the diagnostic warning about the un-eliminated investment
        # returns (the pre-automation behaviour).
        self.entity.auto_eliminate_investment = False
        self._configured_member(
            investment_amount=800.0, acquisition_equity=600.0)
        self._seed_equity(600.0)
        run = self._run()
        self.assertFalse(
            run.line_ids.filtered(
                lambda l: l.account_id in (
                    self.investment_account | self.equity_elim_account
                    | self.goodwill_account)),
            "no elimination legs when the entity opts out")
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'goodwill'))
        self.assertIn(
            'not eliminated', run.consolidation_warning or '')


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenNciFairValue(_GoldenConsolBase):
    """IFRS 3.19(a) fair-value NCI basis (full goodwill). Flat 1.0 rate."""

    CCY_NAME = 'TGF'
    RATES = [('2026-01-01', 1.0)]

    def _seed_equity_and_profit(self, equity=1000.0, profit=50.0):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': equity},
                {'account': self.account_equity, 'credit': equity},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': profit},
                {'account': self.account_revenue, 'credit': profit},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    def test_golden_fair_value_nci_with_elimination(self):
        # 80%-owned member, fully configured for the IFRS 3 elimination.
        #   A (acquisition equity)     = 1,000
        #   I (investment)             = 1,000
        #   FV of NCI at acquisition   =   220
        #   post-acquisition profit    =    50
        #
        # Acquisition legs:
        #   Dr equity elimination      +1,000.00
        #   Cr investment              -1,000.00
        #   Cr NCI at fair value         -220.00
        #   Dr goodwill I + FV - A = 1,000 + 220 - 1,000 = +220.00 (full
        #     goodwill: includes the minority's 220 - 200 = 20 of goodwill)
        #
        # Post-acquisition NCI: reporting base = equity 1,000 + profit 50 =
        # 1,050; movement = 1,050 - 1,000 = 50; minority share = 20% x 50 =
        # 10, stored credit-negative -10.00.
        #
        # Total NCI = -220.00 - 10.00 = -230.00
        #           = -(FV 220 + 20% x post-acq delta 50).
        self._member(
            method='full', ownership_pct=80.0,
            investment_account_id=self.investment_account.id,
            investment_amount=1000.0,
            acquisition_equity=1000.0,
            equity_elimination_account_id=self.equity_elim_account.id,
            goodwill_account_id=self.goodwill_account.id,
            nci_account_id=self.nci_account.id,
            nci_basis='fair_value',
            nci_fair_value=220.0,
        )
        self._seed_equity_and_profit(1000.0, 50.0)
        run = self._run()
        gw_leg = run.line_ids.filtered(
            lambda l: l.account_id == self.goodwill_account)
        self.assertAlmostEqual(gw_leg.amount, 220.00, places=2)
        self.assertEqual(gw_leg.kind, 'goodwill')
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        self.assertEqual(
            len(nci_lines), 2,
            "acquisition-date FV NCI plus the post-acquisition share")
        self.assertAlmostEqual(
            sum(nci_lines.mapped('amount')), -230.00, places=2)
        self.assertAlmostEqual(run.nci_amount, -230.00, places=2)
        self.assertRunBalances(run)

    def test_golden_fair_value_nci_carve_path(self):
        # Same economics WITHOUT the elimination accounts (no investment
        # configured), so the FV basis flows through the plain carve:
        #
        #   NCI = FV + (1-o) x post-acq movement = 220 + 0.20 x 50 = 230
        #
        # stored credit-negative as a single -230.00 carve line with its
        # +230.00 retained-earnings reclass leg.
        self._member(
            method='full', ownership_pct=80.0,
            acquisition_equity=1000.0,
            nci_basis='fair_value',
            nci_fair_value=220.0,
        )
        self._seed_equity_and_profit(1000.0, 50.0)
        run = self._run()
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        self.assertEqual(len(nci_lines), 1)
        self.assertAlmostEqual(nci_lines.amount, -230.00, places=2)
        self.assertAlmostEqual(run.nci_amount, -230.00, places=2)
        reclass = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.account_id == self.re_account)
        self.assertAlmostEqual(reclass.amount, 230.00, places=2)
        self.assertRunBalances(run)

    def test_golden_fair_value_basis_requires_anchor_fields(self):
        # FV basis without the FV amount, or without the acquisition equity,
        # or at 100% ownership, is refused at configuration time.
        with self.assertRaises(ValidationError):
            self._member(
                method='full', ownership_pct=80.0,
                nci_basis='fair_value', acquisition_equity=1000.0)
        with self.assertRaises(ValidationError):
            self._member(
                method='full', ownership_pct=80.0,
                nci_basis='fair_value', nci_fair_value=220.0)
        with self.assertRaises(ValidationError):
            self._member(
                method='full', ownership_pct=100.0,
                nci_basis='fair_value', nci_fair_value=220.0,
                acquisition_equity=1000.0)


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenCtaRecycle(_GoldenConsolBase):
    """IAS 21.48 / 48A-C: member-disposal CTA treatment."""

    CCY_NAME = 'TGR'
    RATES = [('2026-01-01', 0.80), ('2026-01-06', 0.90)]

    def _seed_fx_gain_books(self):
        # cash 80,000 debit vs revenue 80,000 credit (subsidiary currency).
        #   cash    = +80,000 x 0.90 (closing) = +72,000.00
        #   revenue = -80,000 x 0.85 (average) = -68,000.00
        #   member residual +4,000.00 -> member CTA plug -4,000.00
        # i.e. an accumulated translation GAIN of 4,000 in the reserve.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 80000.0},
                {'account': self.account_revenue, 'credit': 80000.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    def test_golden_full_disposal_recycles_entire_cta(self):
        self.env.user.group_ids |= self.manager_group
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        run = self._run()
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertEqual(len(cta_lines), 1)
        self.assertEqual(cta_lines.member_id, member)
        self.assertAlmostEqual(cta_lines.amount, -4000.00, places=2)
        # Full disposal: reclass = 100% x 4,000 gain.
        #   Dr CTA reserve +4,000.00 / Cr FX recycling gain -4,000.00
        member.disposal_pct = 100.0
        member.action_dispose_member()
        recycle = run.line_ids.filtered(lambda l: l.kind == 'cta_recycle')
        self.assertEqual(len(recycle), 2)
        reserve_leg = recycle.filtered(
            lambda l: l.account_id == self.cta_account)
        gain_leg = recycle.filtered(
            lambda l: l.account_id == self.fx_gain_account)
        self.assertAlmostEqual(reserve_leg.amount, 4000.00, places=2)
        self.assertAlmostEqual(gain_leg.amount, -4000.00, places=2)
        # Remaining member CTA is zero; a second disposal has nothing left.
        self.assertAlmostEqual(
            run._eh_member_cta_balance(member), 0.00, places=2)
        with self.assertRaises(UserError):
            member.action_dispose_member()
        self.assertRunBalances(run)

    def test_disposal_event_survives_reset_and_rebuilds_exact_lines(self):
        self.env.user.group_ids |= self.manager_group
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        run = self._run()
        member.disposal_pct = 100.0
        member.action_dispose_member()
        event = run.disposal_event_ids
        self.assertEqual(len(event), 1)
        self.assertEqual(event.reclass_amount, -4000.0)
        original = run.line_ids.filtered(
            lambda line: line.disposal_event_id == event
        )
        self.assertEqual(
            sorted(original.mapped('amount')), [-4000.0, 4000.0],
        )
        with self.assertRaises(AccessError):
            event.write({'reclass_amount': -1.0})

        run.action_reset_to_draft()
        self.assertTrue(event.exists())
        self.assertFalse(run.line_ids)
        run.action_compute()
        rebuilt = run.line_ids.filtered(
            lambda line: line.disposal_event_id == event
        )
        self.assertEqual(len(rebuilt), 2)
        self.assertEqual(
            sorted(rebuilt.mapped('amount')), [-4000.0, 4000.0],
        )
        self.assertEqual(rebuilt.mapped('account_id'), (
            self.cta_account | self.fx_gain_account
        ))
        self.assertRunBalances(run)

    def test_golden_partial_disposal_retaining_control_reattributes_to_nci(self):
        self.env.user.group_ids |= self.manager_group
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        run = self._run()
        # Partial disposal retaining control: 25% x 4,000 = 1,000 moves from
        # parent CTA reserve to NCI equity. IAS 21.48C forbids P&L recycling.
        member.disposal_pct = 25.0
        member.action_dispose_member()
        recycle = run.line_ids.filtered(lambda l: l.kind == 'cta_recycle')
        reserve_leg = recycle.filtered(
            lambda l: l.account_id == self.cta_account)
        nci_leg = recycle.filtered(
            lambda l: l.account_id == self.nci_account)
        self.assertAlmostEqual(reserve_leg.amount, 1000.00, places=2)
        self.assertAlmostEqual(nci_leg.amount, -1000.00, places=2)
        self.assertFalse(recycle.filtered(
            lambda l: l.account_id == self.fx_gain_account))
        # Remaining balance drawn down to -3,000.00 (4,000 - 1,000 gain).
        self.assertAlmostEqual(
            run._eh_member_cta_balance(member), -3000.00, places=2)
        # A follow-up 100% disposal recycles exactly the remaining 3,000.
        member.disposal_pct = 100.0
        member.action_dispose_member()
        self.assertAlmostEqual(
            run._eh_member_cta_balance(member), 0.00, places=2)
        recycle = run.line_ids.filtered(lambda l: l.kind == 'cta_recycle')
        self.assertAlmostEqual(
            sum(recycle.filtered(
                lambda l: l.account_id == self.fx_gain_account,
            ).mapped('amount')), -3000.00, places=2)
        self.assertRunBalances(run)

    def test_golden_partial_disposal_losing_control_recycles_all_cta(self):
        self.env.user.group_ids |= self.manager_group
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        run = self._run()
        member.write({
            'disposal_pct': 25.0,
            'disposal_loses_control': True,
        })
        member.action_dispose_member()
        recycle = run.line_ids.filtered(lambda l: l.kind == 'cta_recycle')
        self.assertAlmostEqual(
            recycle.filtered(
                lambda l: l.account_id == self.cta_account).amount,
            4000.00, places=2)
        self.assertAlmostEqual(
            recycle.filtered(
                lambda l: l.account_id == self.fx_gain_account).amount,
            -4000.00, places=2)
        self.assertAlmostEqual(
            run._eh_member_cta_balance(member), 0.00, places=2)
        self.assertRunBalances(run)

    def test_reviewed_disposal_demotes_run_for_fresh_signoff(self):
        self.env.user.group_ids |= self.manager_group
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        run = self._run()
        run.action_review()
        self.assertTrue(run.reviewed_at)
        member.disposal_pct = 25.0
        member.action_dispose_member()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.reviewed_at)
        self.assertFalse(run.reviewed_by_id)

    def test_linked_cta_position_cannot_be_deleted_from_settled_graph(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Golden linked CTA position',
            'company_id': self.parent_company.id,
            'foreign_operation_company_id': self.company.id,
            'operation_currency_id': self.company.currency_id.id,
            'cta_account_id': self.cta_account.id,
            'gain_account_id': self.fx_gain_account.id,
            'loss_account_id': self.fx_loss_account.id,
        })
        self._member(
            method='full', ownership_pct=100.0,
            cta_position_id=position.id,
        )
        self._seed_fx_gain_books()
        run = self._run()
        self.assertTrue(run.line_ids.filtered(
            lambda line: line.cta_position_id == position,
        ))
        original_name = position.name
        with self.assertRaises(UserError):
            position.write({'name': 'Rewritten historical CTA identity'})
        with self.assertRaises(UserError):
            position.write({'company_id': self.company.id})
        self.assertEqual(position.name, original_name)
        self.assertEqual(position.company_id, self.parent_company)
        with self.assertRaises(UserError):
            position.unlink()
        self.assertTrue(position.exists())

    def test_golden_disposal_is_manager_gated(self):
        member = self._member(method='full', ownership_pct=100.0)
        self._seed_fx_gain_books()
        self._run()
        non_manager = self.env['res.users'].create({
            'name': 'Consol Clerk Dispose',
            'login': 'consol_clerk_dispose',
            'group_ids': [(6, 0, [
                self.env.ref('account.group_account_user').id,
            ])],
        })
        with self.assertRaises(UserError):
            member.with_user(non_manager).action_dispose_member()


@tagged('eh_golden', 'eh_account_consolidation', 'post_install',
        '-at_install')
class TestGoldenPolicyGuards(_GoldenConsolBase):
    """IFRS 10.B87 / B92-B93: uniform policies and the three-month
    reporting-date cap, with the audited run-level override."""

    CCY_NAME = 'TGG'
    RATES = [('2026-01-01', 1.0)]

    def _seed_books(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string(self.BOOK_DATE),
        )

    def test_golden_offset_over_three_months_blocks(self):
        self._member(method='full', reporting_date_offset_months=4)
        self._seed_books()
        run = self._run(compute=False)
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'draft')

    def test_golden_offset_of_three_months_passes(self):
        # Exactly three months is the IFRS 10.B93 boundary and is allowed.
        self._member(method='full', reporting_date_offset_months=3)
        self._seed_books()
        run = self._run()
        self.assertEqual(run.state, 'computed')

    def test_golden_policy_misalignment_blocks(self):
        self._member(method='full', policy_aligned=False)
        self._seed_books()
        run = self._run(compute=False)
        with self.assertRaises(UserError):
            run.action_compute()

    def test_golden_override_requires_reason_and_logs(self):
        self.env.user.group_ids |= self.manager_group
        self._member(method='full', reporting_date_offset_months=4)
        self._seed_books()
        run = self._run(compute=False)
        # Override without a reason is refused.
        run.override_policy_checks = True
        with self.assertRaises(UserError):
            run.action_compute()
        # Override with a reason computes and logs the exception.
        run.override_policy_reason = (
            'Conforming adjustments booked; <b>not trusted HTML</b>.')
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        bodies = ' '.join(
            run.message_ids.mapped(lambda m: str(m.body or '')))
        self.assertIn('overridden', bodies)
        self.assertIn('Conforming adjustments', bodies)
        self.assertIn('&lt;b&gt;not trusted HTML&lt;/b&gt;', bodies)
        self.assertNotIn('<b>not trusted HTML</b>', bodies)
        self.assertIn('<br', bodies)
