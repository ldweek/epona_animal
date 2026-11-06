# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 24 / IFRS 7 / 8 / 12 disclosure register tests."""

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_disclosures', 'integration', 'post_install', '-at_install')
class TestDisclosures(TransactionCase):

    def test_related_party_totals(self):
        party = self.env['eh.related.party'].create({
            'name': 'Parent Co', 'relationship': 'parent',
            'transaction_ids': [
                (0, 0, {'transaction_type': 'sale', 'amount': 1000.0,
                        'balance': 300.0}),
                (0, 0, {'transaction_type': 'loan', 'amount': 5000.0,
                        'balance': 5000.0}),
            ],
        })
        self.assertAlmostEqual(party.total_transactions, 6000.0, places=2)
        self.assertAlmostEqual(party.outstanding_balance, 5300.0, places=2)

    def test_segment_reconciliation(self):
        rep = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'Retail', 'revenue': 600.0, 'result': 80.0}),
                (0, 0, {'name': 'Wholesale', 'revenue': 350.0, 'result': 40.0}),
            ],
        })
        self.assertAlmostEqual(rep.total_segment_revenue, 950.0, places=2)
        self.assertAlmostEqual(rep.total_segment_result, 120.0, places=2)
        # Entity 1000 - segments 950 = 50 reconciling item.
        self.assertAlmostEqual(rep.revenue_reconciliation, 50.0, places=2)

    def test_entity_interest_nci(self):
        sub = self.env['eh.entity.interest'].create({
            'name': 'Sub Ltd', 'interest_type': 'subsidiary',
            'ownership_pct': 80.0})
        self.assertAlmostEqual(sub.nci_pct, 20.0, places=2)
        assoc = self.env['eh.entity.interest'].create({
            'name': 'Assoc Ltd', 'interest_type': 'associate',
            'ownership_pct': 30.0})
        # No NCI for an associate.
        self.assertAlmostEqual(assoc.nci_pct, 0.0, places=2)

    def test_fin_risk_register(self):
        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'instrument_class': 'Receivables',
            'risk_category': 'credit', 'carrying_amount': 250000.0,
            'maturity_band': '3m_1y'})
        self.assertEqual(risk.risk_category, 'credit')
        self.assertAlmostEqual(risk.carrying_amount, 250000.0, places=2)

    def test_fin_risk_ecl_stage_2_is_lifetime(self):
        """IFRS 7.35A-N: a stage 2 exposure carries a lifetime expected
        credit loss and its staged allowance nets down the gross carrying
        amount. An existing exposure with no stage stays at gross (default
        empty / not-applicable)."""
        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'risk_category': 'credit',
            'carrying_amount': 100000.0,
            'ecl_stage': '2', 'loss_allowance': 8000.0})
        # Stage 2 -> lifetime measurement basis.
        self.assertEqual(risk.ecl_basis, 'lifetime')
        # Net carrying = gross 100000 less staged allowance 8000.
        self.assertAlmostEqual(risk.net_carrying_amount, 92000.0, places=2)

        # Stage 1 is a 12-month ECL basis.
        stage1 = self.env['eh.fin.risk'].create({
            'name': 'Performing loan', 'risk_category': 'credit',
            'carrying_amount': 50000.0, 'ecl_stage': '1',
            'loss_allowance': 500.0})
        self.assertEqual(stage1.ecl_basis, '12m')
        self.assertAlmostEqual(stage1.net_carrying_amount, 49500.0, places=2)

        # No stage set -> not applicable, no allowance, net equals gross.
        plain = self.env['eh.fin.risk'].create({
            'name': 'Narrative exposure', 'risk_category': 'liquidity',
            'carrying_amount': 7000.0})
        self.assertFalse(plain.ecl_stage)
        self.assertFalse(plain.ecl_basis)
        self.assertAlmostEqual(plain.loss_allowance, 0.0, places=2)
        self.assertAlmostEqual(plain.net_carrying_amount, 7000.0, places=2)

    def test_segment_reportable_10pct_threshold(self):
        """IFRS 8.13: a segment whose revenue, absolute result and assets are
        all below 10% of the group totals is flagged non-reportable, while a
        segment above any threshold is flagged reportable."""
        rep = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'Major', 'revenue': 900.0, 'result': 90.0,
                        'assets': 900.0}),
                (0, 0, {'name': 'Tiny', 'revenue': 50.0, 'result': 5.0,
                        'assets': 50.0}),
            ],
        })
        lines = {line.name: line for line in rep.segment_ids}
        # Combined revenue 950; Tiny 50 is 5.3% -> below 10% on every measure.
        self.assertFalse(lines['Tiny'].is_reportable)
        self.assertFalse(lines['Tiny'].reportable_reason)
        # Major dominates every measure -> reportable.
        self.assertTrue(lines['Major'].is_reportable)
        self.assertIn('revenue', lines['Major'].reportable_reason)


@tagged('eh_account_disclosures', 'integration', 'post_install', '-at_install')
class TestMaturityRun(EhAccountIntegrationTestCase):
    """Ledger-driven IFRS 7.39 contractual-maturity analysis."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )

    def _post_payable(self, amount, maturity_date, ref='MAT-TEST',
                      date=None):
        """Post a balanced misc entry that credits the payable (a financial
        liability) with a future contractual maturity, debiting expense."""
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date or fields.Date.context_today(self.env.user),
            'ref': ref,
            'line_ids': [
                (0, 0, {
                    'name': ref,
                    'account_id': self.account_expense.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': ref,
                    'account_id': self.account_payable.id,
                    'debit': 0.0,
                    'credit': amount,
                    'date_maturity': maturity_date,
                }),
            ],
        })
        move.action_post()
        return move

    def test_maturity_run_buckets_and_ties_out(self):
        today = fields.Date.context_today(self.env.user)
        future = today + relativedelta(months=6)  # lands in 3m_1y band
        self._post_payable(4000.0, future)

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()

        # One line per band.
        self.assertEqual(len(run.line_ids), 5)
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        # IFRS 7.39: the liability maturity is a POSITIVE undiscounted
        # contractual outflow in the 3m-1y band, not a negative balance.
        self.assertAlmostEqual(bands['3m_1y'], 4000.0, places=2)
        self.assertGreater(bands['3m_1y'], 0.0)
        self.assertAlmostEqual(bands['lt_3m'], 0.0, places=2)
        self.assertAlmostEqual(bands['gt_5y'], 0.0, places=2)

        # Ties out to the positive contractual amount of the posted open
        # liability lines (credit - debit magnitude).
        posted = self.env['account.move.line'].search([
            ('account_id', '=', self.account_payable.id),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
        ])
        self.assertAlmostEqual(
            run.total_undiscounted,
            sum(posted.mapped(lambda ml: ml.credit - ml.debit)), places=2)
        self.assertAlmostEqual(run.total_undiscounted, 4000.0, places=2)

    def test_maturity_run_on_demand_band(self):
        today = fields.Date.context_today(self.env.user)
        past = today - relativedelta(days=5)  # on or before -> on_demand
        self._post_payable(1500.0, past, ref='MAT-DEMAND')

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        # Positive undiscounted contractual outflow, not a negative balance.
        self.assertAlmostEqual(bands['on_demand'], 1500.0, places=2)

    def test_maturity_run_liability_is_positive(self):
        """A financial-liability maturity is reported as a positive
        undiscounted contractual amount (IFRS 7.39), never a negative
        ledger balance."""
        today = fields.Date.context_today(self.env.user)
        future = today + relativedelta(months=6)
        self._post_payable(2500.0, future, ref='MAT-POS')

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        self.assertAlmostEqual(bands['3m_1y'], 2500.0, places=2)
        self.assertGreater(bands['3m_1y'], 0.0)
        # Every populated band amount is non-negative for a pure liability.
        for line in run.line_ids:
            self.assertGreaterEqual(line.undiscounted_amount, 0.0)

    def test_ledger_path_rejects_unprovable_run_level_interest(self):
        """One estimate cannot fabricate mixed liabilities' coupon flows."""
        today = fields.Date.context_today(self.env.user)
        # A liability maturing in 6 months (lands in 3m_1y) at 10% p.a.
        maturity = today + relativedelta(months=6)
        principal = 10000.0
        self._post_payable(principal, maturity, ref='MAT-INT')

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'annual_interest_rate': 10.0,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        with self.assertRaisesRegex(UserError, 'each facility as an instrument'):
            run.action_populate()
        self.assertFalse(run.line_ids)

    def test_maturity_run_no_rate_is_principal_only(self):
        """With no interest rate (the default) the analysis is byte-identical
        to the principal-only carrying figure, so existing behaviour is
        unchanged for non-interest-bearing liabilities."""
        today = fields.Date.context_today(self.env.user)
        maturity = today + relativedelta(months=6)
        self._post_payable(10000.0, maturity, ref='MAT-NORATE')

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        self.assertAlmostEqual(bands['3m_1y'], 10000.0, places=2)
        self.assertAlmostEqual(run.total_undiscounted, 10000.0, places=2)

    def test_maturity_run_excludes_lines_after_reporting_date(self):
        """Lines posted after the as-at reporting date must not leak into
        the maturity analysis."""
        today = fields.Date.context_today(self.env.user)
        after = today + relativedelta(days=10)
        future = today + relativedelta(months=6)
        # An in-window liability (posted on the reporting date) ...
        self._post_payable(3000.0, future, ref='MAT-IN', date=today)
        # ... and one posted AFTER the reporting date, which must be excluded.
        self._post_payable(9999.0, future, ref='MAT-OUT', date=after)

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()
        # Only the in-window 3000 contributes; the after-date 9999 is gone.
        self.assertAlmostEqual(run.total_undiscounted, 3000.0, places=2)
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        self.assertAlmostEqual(bands['3m_1y'], 3000.0, places=2)

    def test_maturity_coupon_bond_includes_interest_beyond_principal(self):
        """IFRS 7.39 / B11D: a plain coupon bond bands correctly. Its interim
        coupons fall in the earlier bands and the maturity band holds the
        principal plus the final coupon, so the total undiscounted amount
        strictly exceeds the principal by the sum of the contractual
        coupons."""
        today = fields.Date.context_today(self.env.user)
        # A 2-year semi-annual 10% bond, principal 1000. Coupon per period is
        # 1000 * 10% / 2 = 50, paid at +6m, +12m, +18m and +24m (the last with
        # the principal). The +6m coupon lands in the 3-months-to-1-year band,
        # so the coupon interest bands into an earlier band than the maturity.
        maturity = today + relativedelta(years=2)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'instrument_ids': [(0, 0, {
                'name': 'Coupon bond',
                'principal': 1000.0,
                'annual_rate': 10.0,
                'coupon_frequency': 'semiannual',
                'maturity_date': maturity,
            })],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        coupon = 50.0
        # The +6m coupon bands into 3m_1y; the maturity band (1y_5y) holds the
        # remaining coupons plus the principal repayment.
        self.assertGreater(bands['3m_1y'], 0.0)
        self.assertAlmostEqual(bands['3m_1y'], coupon, places=2)
        # Every band amount is non-negative for a pure liability.
        for line in run.line_ids:
            self.assertGreaterEqual(line.undiscounted_amount, 0.0)
        # Total undiscounted = principal 1000 + four coupons of 50 = 1200.
        self.assertAlmostEqual(run.total_undiscounted, 1200.0, places=2)
        # Strictly exceeds principal by the contractual interest (200), i.e.
        # the maturity analysis includes interest beyond principal.
        self.assertGreater(run.total_undiscounted, 1000.0)
        self.assertAlmostEqual(
            run.total_undiscounted - 1000.0, 4 * coupon, places=2)

    def test_maturity_zero_coupon_instrument_is_principal_only(self):
        """A zero-coupon (rate 0) instrument has a single cash flow: the
        principal at maturity. No interest is projected."""
        today = fields.Date.context_today(self.env.user)
        maturity = today + relativedelta(months=6)  # 3m_1y band
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'instrument_ids': [(0, 0, {
                'name': 'Zero-coupon note',
                'principal': 5000.0,
                'annual_rate': 0.0,
                'coupon_frequency': 'annual',
                'maturity_date': maturity,
            })],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        self.assertAlmostEqual(bands['3m_1y'], 5000.0, places=2)
        self.assertAlmostEqual(run.total_undiscounted, 5000.0, places=2)

    def test_maturity_no_instruments_uses_ledger_path(self):
        """With no instruments listed, the run still analyses the selected
        ledger accounts, so the existing ledger-driven behaviour is
        unchanged."""
        today = fields.Date.context_today(self.env.user)
        future = today + relativedelta(months=6)
        self._post_payable(4000.0, future, ref='MAT-LEDGER')
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        run.action_populate()
        bands = {line.band: line.undiscounted_amount for line in run.line_ids}
        self.assertAlmostEqual(bands['3m_1y'], 4000.0, places=2)
        self.assertAlmostEqual(run.total_undiscounted, 4000.0, places=2)

    def test_maturity_refuses_ambiguous_instrument_and_ledger_sources(self):
        today = fields.Date.context_today(self.env.user)
        future = today + relativedelta(months=6)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
            'instrument_ids': [(0, 0, {
                'name': 'Facility also represented in ledger',
                'principal': 4000.0,
                'maturity_date': future,
            })],
        })
        with self.assertRaisesRegex(UserError, 'authoritative liability source'):
            run.action_populate()
        self.assertFalse(run.line_ids)

    def test_segments_tie_out_flag(self):
        tied = self.env['eh.segment.report'].create({
            'entity_revenue': 950.0,
            'segment_ids': [
                (0, 0, {'name': 'A', 'revenue': 600.0}),
                (0, 0, {'name': 'B', 'revenue': 350.0}),
            ],
        })
        self.assertTrue(tied.segments_tie_out)

        untied = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'A', 'revenue': 600.0}),
                (0, 0, {'name': 'B', 'revenue': 350.0}),
            ],
        })
        self.assertFalse(untied.segments_tie_out)

    # --- Ledger tie-out ---------------------------------------------------

    def _make_analytic(self, name):
        """Create an analytic account (with a plan) usable in a
        move-line analytic distribution."""
        plan = self.env['account.analytic.plan'].create({'name': name})
        return self.env['account.analytic.account'].create({
            'name': name, 'plan_id': plan.id})

    def _post_segment_revenue(self, amount, analytic, date=None):
        """Post a balanced entry recognising revenue tagged to an analytic
        account: credit income, debit receivable. The income line carries the
        analytic distribution so the segment tie-out can derive it."""
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date or fields.Date.context_today(self.env.user),
            'ref': 'SEG-REV',
            'line_ids': [
                (0, 0, {
                    'name': 'SEG-REV',
                    'account_id': self.account_receivable.id,
                    'debit': amount, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'SEG-REV',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0, 'credit': amount,
                    'analytic_distribution': {str(analytic.id): 100.0},
                }),
            ],
        })
        move.action_post()
        return move

    def test_segment_line_ledger_tie_out(self):
        """A segment line linked to an analytic account derives its revenue
        from the ledger: an entered figure that disagrees with the
        ledger-derived total shows revenue_tied=False; agreeing shows
        revenue_tied=True."""
        today = fields.Date.context_today(self.env.user)
        analytic = self._make_analytic('Retail Segment')
        # Ledger recognises 600 of revenue tagged to this segment.
        self._post_segment_revenue(600.0, analytic)

        report = self.env['eh.segment.report'].create({
            'period_end': today,
            'entity_revenue': 600.0,
            'segment_ids': [
                (0, 0, {'name': 'Retail', 'revenue': 500.0,
                        'analytic_account_id': analytic.id}),
            ],
        })
        line = report.segment_ids
        # Ledger-derived revenue is the 600 recognised, positive for income.
        self.assertAlmostEqual(line.ledger_revenue, 600.0, places=2)
        # Entered 500 disagrees with ledger 600 -> not tied, residual -100.
        self.assertAlmostEqual(line.revenue_residual, -100.0, places=2)
        self.assertFalse(line.revenue_tied)

        # Correct the entered figure to agree with the ledger.
        line.revenue = 600.0
        self.assertAlmostEqual(line.revenue_residual, 0.0, places=2)
        self.assertTrue(line.revenue_tied)

    def test_segment_line_without_analytic_is_not_applicable(self):
        """A segment line with no analytic account has no ledger counterpart,
        so it is treated as tied (not applicable) and never shows drift; the
        default behaviour of a hand-keyed segment is unchanged."""
        report = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'Narrative', 'revenue': 400.0}),
            ],
        })
        line = report.segment_ids
        self.assertFalse(line.analytic_account_id)
        self.assertAlmostEqual(line.ledger_revenue, 0.0, places=2)
        self.assertTrue(line.revenue_tied)
        self.assertTrue(line.result_tied)

    def test_fin_risk_ledger_tie_out(self):
        """A financial-risk exposure with backing accounts derives its
        carrying amount from posted balances: a disagreeing entered amount
        shows carrying_tied=False; agreeing shows carrying_tied=True."""
        today = fields.Date.context_today(self.env.user)
        # Post 250000 of trade receivables (debit receivable, credit revenue).
        self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 250000.0},
            {'account': self.account_revenue, 'credit': 250000.0},
        ], date=today)

        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'risk_category': 'credit',
            'reporting_date': today,
            'carrying_amount': 240000.0,
            'ledger_account_ids': [(6, 0, self.account_receivable.ids)],
        })
        # Ledger carrying amount is the net receivable balance (debit - credit).
        self.assertAlmostEqual(risk.ledger_carrying_amount, 250000.0, places=2)
        # Entered 240000 disagrees with ledger 250000 -> not tied.
        self.assertAlmostEqual(risk.carrying_residual, -10000.0, places=2)
        self.assertFalse(risk.carrying_tied)

        risk.carrying_amount = 250000.0
        self.assertAlmostEqual(risk.carrying_residual, 0.0, places=2)
        self.assertTrue(risk.carrying_tied)

    def test_fin_risk_without_backing_accounts_is_not_applicable(self):
        """A risk exposure with no backing accounts is treated as tied (not
        applicable); the default hand-keyed behaviour is unchanged."""
        risk = self.env['eh.fin.risk'].create({
            'name': 'Narrative exposure', 'risk_category': 'market_price',
            'carrying_amount': 5000.0})
        self.assertFalse(risk.ledger_account_ids)
        self.assertAlmostEqual(risk.ledger_carrying_amount, 0.0, places=2)
        self.assertTrue(risk.carrying_tied)

    def test_related_party_ledger_tie_out(self):
        """A related party linked to a contact ties its entered outstanding
        balance to the contact's posted receivable/payable ledger."""
        today = fields.Date.context_today(self.env.user)
        # The contact owes 3000 (debit receivable, credit revenue).
        self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 3000.0,
             'partner': self.partner_a},
            {'account': self.account_revenue, 'credit': 3000.0},
        ], date=today)

        party = self.env['eh.related.party'].create({
            'name': 'Parent Co', 'relationship': 'parent',
            'partner_id': self.partner_a.id,
            'reporting_date': today,
            'transaction_ids': [
                (0, 0, {'transaction_type': 'sale', 'amount': 3000.0,
                        'balance': 2500.0}),
            ],
        })
        # Ledger receivable balance is 3000, entered outstanding 2500 -> drift.
        self.assertAlmostEqual(party.ledger_balance, 3000.0, places=2)
        self.assertAlmostEqual(party.balance_residual, -500.0, places=2)
        self.assertFalse(party.balance_tied)

        party.transaction_ids.balance = 3000.0
        self.assertAlmostEqual(party.balance_residual, 0.0, places=2)
        self.assertTrue(party.balance_tied)

    def test_related_party_without_contact_is_not_applicable(self):
        """A related party with no linked contact has no ledger counterpart,
        so it is treated as tied; default behaviour is unchanged."""
        party = self.env['eh.related.party'].create({
            'name': 'KMP Individual', 'relationship': 'kmp',
            'transaction_ids': [
                (0, 0, {'transaction_type': 'compensation', 'amount': 100.0,
                        'balance': 100.0}),
            ],
        })
        self.assertFalse(party.partner_id)
        self.assertAlmostEqual(party.ledger_balance, 0.0, places=2)
        self.assertTrue(party.balance_tied)


@tagged('eh_account_disclosures', 'integration', 'post_install', '-at_install')
class TestDisclosureFinalisation(EhAccountIntegrationTestCase):
    """Draft/finalised lock on the run-style disclosure registers: once a
    manager finalises, the figures and child lines freeze against edit,
    delete and append; only a manager can reopen."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The acting user must be a manager to finalise / reopen.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    # --- Segment report (IFRS 8) --------------------------------------------

    def _finalised_segment(self):
        report = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'Retail', 'revenue': 600.0, 'result': 80.0}),
                (0, 0, {'name': 'Wholesale', 'revenue': 350.0}),
            ],
        })
        self.assertEqual(report.state, 'draft')
        report.action_finalise()
        self.assertEqual(report.state, 'finalised')
        return report

    def test_segment_finalise_freezes_entity_figures(self):
        report = self._finalised_segment()
        with self.assertRaises(UserError):
            report.entity_revenue = 2000.0

    def test_segment_finalise_freezes_line_write(self):
        report = self._finalised_segment()
        line = report.segment_ids[0]
        with self.assertRaises(UserError):
            line.revenue = 999.0

    def test_segment_finalise_freezes_line_unlink(self):
        report = self._finalised_segment()
        with self.assertRaises(UserError):
            report.segment_ids[0].unlink()

    def test_segment_finalise_blocks_line_append(self):
        """Create-append negative test: a new segment line cannot be added to
        a finalised report, which would otherwise silently move the totals."""
        report = self._finalised_segment()
        with self.assertRaises(UserError):
            self.env['eh.segment.line'].create({
                'report_id': report.id, 'name': 'Sneak', 'revenue': 5000.0,
            })

    def test_segment_finalise_freezes_unlink(self):
        report = self._finalised_segment()
        with self.assertRaises(UserError):
            report.unlink()

    def test_segment_reopen_by_manager_unfreezes(self):
        report = self._finalised_segment()
        report.action_reopen()
        self.assertEqual(report.state, 'draft')
        # Editing, appending and deleting lines work again once reopened.
        report.entity_revenue = 1200.0
        self.env['eh.segment.line'].create({
            'report_id': report.id, 'name': 'Added', 'revenue': 100.0})
        self.assertAlmostEqual(report.entity_revenue, 1200.0, places=2)

    def test_segment_finalise_requires_manager(self):
        report = self.env['eh.segment.report'].create({
            'entity_revenue': 500.0,
            'segment_ids': [(0, 0, {'name': 'A', 'revenue': 500.0})],
        })
        non_manager = new_test_user(
            self.env, login='eh_seg_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            report.with_user(non_manager).action_finalise()
        # And a non-manager cannot reopen a finalised report either.
        report.action_finalise()
        with self.assertRaises(UserError):
            report.with_user(non_manager).action_reopen()

    # --- Maturity run (IFRS 7.39) -------------------------------------------

    def _finalised_run(self):
        today = fields.Date.context_today(self.env.user)
        future = today + relativedelta(months=6)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'instrument_ids': [(0, 0, {
                'name': 'Note', 'principal': 1000.0,
                'maturity_date': future})],
        })
        run.action_populate()
        self.assertTrue(run.line_ids)
        self.assertEqual(run.state, 'draft')
        run.action_finalise()
        self.assertEqual(run.state, 'finalised')
        return run

    def test_maturity_finalise_freezes_inputs(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.reporting_date = fields.Date.context_today(self.env.user)
        with self.assertRaises(UserError):
            run.annual_interest_rate = 5.0

    def test_maturity_finalise_blocks_repopulate(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.action_populate()

    def test_maturity_finalise_freezes_band_write(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.line_ids[0].undiscounted_amount = 99999.0

    def test_maturity_finalise_freezes_band_unlink(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.line_ids[0].unlink()

    def test_maturity_finalise_blocks_band_append(self):
        """Create-append negative test: a band line cannot be added to a
        finalised run, which would otherwise silently move the total."""
        run = self._finalised_run()
        with self.assertRaises(UserError):
            self.env['eh.fin.maturity.line'].create({
                'run_id': run.id, 'band': 'gt_5y',
                'undiscounted_amount': 5000.0,
            })

    def test_maturity_finalise_blocks_instrument_append(self):
        """Create-append negative test: an instrument cannot be added to a
        finalised run, which would change its projected cash flows."""
        run = self._finalised_run()
        future = fields.Date.context_today(self.env.user) \
            + relativedelta(years=3)
        with self.assertRaises(UserError):
            self.env['eh.fin.maturity.instrument'].create({
                'run_id': run.id, 'name': 'Sneak', 'principal': 9000.0,
                'maturity_date': future,
            })

    def test_maturity_finalise_freezes_instrument_write(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.instrument_ids[0].principal = 8000.0

    def test_maturity_finalise_freezes_unlink(self):
        run = self._finalised_run()
        with self.assertRaises(UserError):
            run.unlink()

    def test_maturity_reopen_by_manager_unfreezes(self):
        run = self._finalised_run()
        run.action_reopen()
        self.assertEqual(run.state, 'draft')
        # Re-populate works again once reopened.
        run.action_populate()
        self.assertTrue(run.line_ids)
        run.annual_interest_rate = 5.0

    def test_maturity_finalise_requires_manager(self):
        today = fields.Date.context_today(self.env.user)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        non_manager = new_test_user(
            self.env, login='eh_mat_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            run.with_user(non_manager).action_finalise()
        run.action_finalise()
        with self.assertRaises(UserError):
            run.with_user(non_manager).action_reopen()

    # --- Related party (IAS 24) ---------------------------------------------

    def _finalised_party(self):
        party = self.env['eh.related.party'].create({
            'name': 'Parent Co', 'relationship': 'parent',
            'transaction_ids': [
                (0, 0, {'transaction_type': 'sale', 'amount': 1000.0,
                        'balance': 300.0}),
            ],
        })
        self.assertEqual(party.state, 'draft')
        party.action_finalise()
        self.assertEqual(party.state, 'finalised')
        return party

    def test_related_party_finalise_freezes_details(self):
        party = self._finalised_party()
        with self.assertRaises(UserError):
            party.relationship = 'associate'

    def test_related_party_finalise_freezes_transaction_write(self):
        party = self._finalised_party()
        with self.assertRaises(UserError):
            party.transaction_ids[0].balance = 999.0

    def test_related_party_finalise_freezes_transaction_unlink(self):
        party = self._finalised_party()
        with self.assertRaises(UserError):
            party.transaction_ids[0].unlink()

    def test_related_party_finalise_blocks_transaction_append(self):
        """Create-append negative test: a transaction cannot be added to a
        finalised party, which would otherwise silently move its totals."""
        party = self._finalised_party()
        with self.assertRaises(UserError):
            self.env['eh.related.party.transaction'].create({
                'party_id': party.id, 'transaction_type': 'loan',
                'amount': 5000.0, 'balance': 5000.0,
            })

    def test_related_party_finalise_freezes_unlink(self):
        party = self._finalised_party()
        with self.assertRaises(UserError):
            party.unlink()

    def test_related_party_reopen_by_manager_unfreezes(self):
        party = self._finalised_party()
        party.action_reopen()
        self.assertEqual(party.state, 'draft')
        # Editing and appending work again once reopened.
        party.relationship = 'associate'
        self.env['eh.related.party.transaction'].create({
            'party_id': party.id, 'transaction_type': 'loan',
            'amount': 200.0, 'balance': 200.0})
        self.assertAlmostEqual(party.outstanding_balance, 500.0, places=2)

    def test_related_party_finalise_requires_manager(self):
        party = self.env['eh.related.party'].create({
            'name': 'Assoc', 'relationship': 'associate'})
        non_manager = new_test_user(
            self.env, login='eh_rp_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            party.with_user(non_manager).action_finalise()
        party.action_finalise()
        with self.assertRaises(UserError):
            party.with_user(non_manager).action_reopen()

    # --- Interests in other entities (IFRS 12) ------------------------------

    def _finalised_interest(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Sub Ltd', 'interest_type': 'subsidiary',
            'ownership_pct': 80.0})
        self.assertEqual(interest.state, 'draft')
        interest.action_finalise()
        self.assertEqual(interest.state, 'finalised')
        return interest

    def test_interest_finalise_freezes_figures(self):
        interest = self._finalised_interest()
        with self.assertRaises(UserError):
            interest.ownership_pct = 60.0

    def test_interest_finalise_freezes_unlink(self):
        interest = self._finalised_interest()
        with self.assertRaises(UserError):
            interest.unlink()

    def test_interest_reopen_by_manager_unfreezes(self):
        interest = self._finalised_interest()
        interest.action_reopen()
        self.assertEqual(interest.state, 'draft')
        # Editing works again once reopened, and the NCI recomputes.
        interest.ownership_pct = 60.0
        self.assertAlmostEqual(interest.nci_pct, 40.0, places=2)

    def test_interest_finalise_requires_manager(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Assoc Ltd', 'interest_type': 'associate',
            'ownership_pct': 30.0})
        non_manager = new_test_user(
            self.env, login='eh_ei_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            interest.with_user(non_manager).action_finalise()
        interest.action_finalise()
        with self.assertRaises(UserError):
            interest.with_user(non_manager).action_reopen()

    # --- Financial risk exposure (IFRS 7.35H) -------------------------------

    def _finalised_fin_risk(self):
        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'risk_category': 'credit',
            'carrying_amount': 100000.0, 'ecl_stage': '2',
            'loss_allowance': 8000.0})
        # Net carrying = gross 100000 less staged allowance 8000 (IFRS 7.35H).
        self.assertAlmostEqual(risk.net_carrying_amount, 92000.0, places=2)
        self.assertEqual(risk.state, 'draft')
        risk.action_finalise()
        self.assertEqual(risk.state, 'finalised')
        return risk

    def test_fin_risk_draft_figures_editable(self):
        """While draft, the measurement / input figures remain editable and
        the net carrying amount recomputes; the lock only bites once
        finalised."""
        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'risk_category': 'credit',
            'carrying_amount': 100000.0, 'ecl_stage': '2',
            'loss_allowance': 8000.0})
        self.assertEqual(risk.state, 'draft')
        risk.loss_allowance = 12000.0
        self.assertAlmostEqual(risk.net_carrying_amount, 88000.0, places=2)

    def test_fin_risk_finalise_freezes_loss_allowance(self):
        risk = self._finalised_fin_risk()
        with self.assertRaises(UserError):
            risk.loss_allowance = 20000.0

    def test_fin_risk_finalise_freezes_ecl_stage(self):
        risk = self._finalised_fin_risk()
        with self.assertRaises(UserError):
            risk.ecl_stage = '3'

    def test_fin_risk_finalise_freezes_carrying_amount(self):
        risk = self._finalised_fin_risk()
        with self.assertRaises(UserError):
            risk.carrying_amount = 500000.0

    def test_fin_risk_finalise_freezes_backing_accounts(self):
        risk = self._finalised_fin_risk()
        with self.assertRaises(UserError):
            risk.ledger_account_ids = [(6, 0, self.account_receivable.ids)]

    def test_fin_risk_finalise_freezes_unlink(self):
        risk = self._finalised_fin_risk()
        with self.assertRaises(UserError):
            risk.unlink()

    def test_fin_risk_reopen_by_manager_unfreezes(self):
        risk = self._finalised_fin_risk()
        risk.action_reopen()
        self.assertEqual(risk.state, 'draft')
        # Editing works again once reopened, and the net carrying recomputes.
        risk.loss_allowance = 15000.0
        self.assertAlmostEqual(risk.net_carrying_amount, 85000.0, places=2)

    def test_fin_risk_finalise_requires_manager(self):
        risk = self.env['eh.fin.risk'].create({
            'name': 'Borrowings', 'risk_category': 'liquidity',
            'carrying_amount': 50000.0})
        non_manager = new_test_user(
            self.env, login='eh_fr_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            risk.with_user(non_manager).action_finalise()
        # And a non-manager cannot reopen a finalised exposure either.
        risk.action_finalise()
        with self.assertRaises(UserError):
            risk.with_user(non_manager).action_reopen()

    # --- Raw-write state-transition gate (all four registers) ---------------
    # A finalise/reopen is a manager-gated control. A plain group_eh_user must
    # not be able to flip state with a raw ORM write of {'state': ...} and so
    # sidestep action_finalise / action_reopen; a manager still can, and the
    # action methods (which write state under the internal context flag)
    # continue to work.

    def test_segment_raw_state_write_requires_manager(self):
        report = self.env['eh.segment.report'].create({
            'entity_revenue': 500.0,
            'segment_ids': [(0, 0, {'name': 'A', 'revenue': 500.0})],
        })
        non_manager = new_test_user(
            self.env, login='eh_seg_raw_user',
            groups='eh_account_base.group_eh_user')
        # A non-manager cannot finalise via a raw state write.
        with self.assertRaises(UserError):
            report.with_user(non_manager).write({'state': 'finalised'})
        self.assertEqual(report.state, 'draft')
        # A manager can (the class user is a manager); the action still works.
        report.action_finalise()
        self.assertEqual(report.state, 'finalised')
        # A non-manager cannot reopen via a raw state write either.
        with self.assertRaises(UserError):
            report.with_user(non_manager).write({'state': 'draft'})
        self.assertEqual(report.state, 'finalised')
        # A manager still reopens through the action method.
        report.action_reopen()
        self.assertEqual(report.state, 'draft')

    def test_maturity_raw_state_write_requires_manager(self):
        today = fields.Date.context_today(self.env.user)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'account_ids': [(6, 0, self.account_payable.ids)],
        })
        non_manager = new_test_user(
            self.env, login='eh_mat_raw_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            run.with_user(non_manager).write({'state': 'finalised'})
        self.assertEqual(run.state, 'draft')
        run.action_finalise()
        self.assertEqual(run.state, 'finalised')
        with self.assertRaises(UserError):
            run.with_user(non_manager).write({'state': 'draft'})
        self.assertEqual(run.state, 'finalised')
        run.action_reopen()
        self.assertEqual(run.state, 'draft')

    def test_related_party_raw_state_write_requires_manager(self):
        party = self.env['eh.related.party'].create({
            'name': 'Parent Co', 'relationship': 'parent'})
        non_manager = new_test_user(
            self.env, login='eh_rp_raw_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            party.with_user(non_manager).write({'state': 'finalised'})
        self.assertEqual(party.state, 'draft')
        party.action_finalise()
        self.assertEqual(party.state, 'finalised')
        with self.assertRaises(UserError):
            party.with_user(non_manager).write({'state': 'draft'})
        self.assertEqual(party.state, 'finalised')
        party.action_reopen()
        self.assertEqual(party.state, 'draft')

    def test_interest_raw_state_write_requires_manager(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Sub Ltd', 'interest_type': 'subsidiary',
            'ownership_pct': 80.0})
        non_manager = new_test_user(
            self.env, login='eh_ei_raw_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            interest.with_user(non_manager).write({'state': 'finalised'})
        self.assertEqual(interest.state, 'draft')
        interest.action_finalise()
        self.assertEqual(interest.state, 'finalised')
        with self.assertRaises(UserError):
            interest.with_user(non_manager).write({'state': 'draft'})
        self.assertEqual(interest.state, 'finalised')
        interest.action_reopen()
        self.assertEqual(interest.state, 'draft')

    def test_create_finalised_requires_manager(self):
        """Creating a disclosure already in the finalised state must not let a
        plain user skip the manager-gated action_finalise. A manager may still
        seed a finalised record directly."""
        non_manager = new_test_user(
            self.env, login='eh_create_fin_user',
            groups='eh_account_base.group_eh_user')
        Segment = self.env['eh.segment.report']
        with self.assertRaises(UserError):
            Segment.with_user(non_manager).create({
                'entity_revenue': 500.0, 'state': 'finalised',
                'segment_ids': [(0, 0, {'name': 'A', 'revenue': 500.0})]})
        Risk = self.env['eh.fin.risk']
        with self.assertRaises(UserError):
            Risk.with_user(non_manager).create({
                'name': 'Receivables', 'risk_category': 'credit',
                'carrying_amount': 1000.0, 'state': 'finalised'})
        # A manager can seed a finalised record.
        seeded = Risk.create({
            'name': 'Managed', 'risk_category': 'credit',
            'carrying_amount': 1000.0, 'state': 'finalised'})
        self.assertEqual(seeded.state, 'finalised')

    def test_fin_risk_raw_state_write_requires_manager(self):
        """eh.fin.risk must gate BOTH directions of the state transition. A
        plain user cannot finalise a draft exposure via a raw write (which
        would freeze the IFRS 7.35H loss allowance without manager review),
        nor reopen a finalised one; the manager-gated actions still work."""
        risk = self.env['eh.fin.risk'].create({
            'name': 'Trade receivables', 'risk_category': 'credit',
            'carrying_amount': 100000.0, 'ecl_stage': '2',
            'loss_allowance': 8000.0})
        non_manager = new_test_user(
            self.env, login='eh_fr_raw_user',
            groups='eh_account_base.group_eh_user')
        with self.assertRaises(UserError):
            risk.with_user(non_manager).write({'state': 'finalised'})
        self.assertEqual(risk.state, 'draft')
        risk.action_finalise()
        self.assertEqual(risk.state, 'finalised')
        with self.assertRaises(UserError):
            risk.with_user(non_manager).write({'state': 'draft'})
        self.assertEqual(risk.state, 'finalised')
        risk.action_reopen()
        self.assertEqual(risk.state, 'draft')
