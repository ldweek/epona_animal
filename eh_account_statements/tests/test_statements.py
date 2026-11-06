# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 1 primary statement tests."""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_statements.models import presentation


@tagged('eh_account_statements', 'integration', 'post_install', '-at_install')
class TestStatements(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # action_confirm / action_set_to_draft on both primary statements are
        # manager-gated (segregation of duties). The acting test user must be
        # an EH Accounting Manager for the positive-path tests to confirm.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        # A plain user with no manager rights, used for the negative gate
        # tests. group_eh_user grants read access without the manager gate.
        cls.group_user = cls.env.ref('eh_account_base.group_eh_user')
        cls.group_auditor = cls.env.ref('eh_account_base.group_eh_auditor')
        cls.user_plain = cls.env['res.users'].create({
            'name': 'Statements Plain User',
            'login': 'eh_stmt_plain@test',
            'email': 'eh_stmt_plain@test',
            'group_ids': [(6, 0, [cls.group_user.id])],
        })
        cls.user_manager = cls.env['res.users'].create({
            'name': 'Statements Manager User',
            'login': 'eh_stmt_manager@test',
            'email': 'eh_stmt_manager@test',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [cls.group_manager.id])],
        })
        cls.user_auditor = cls.env['res.users'].create({
            'name': 'Statements Auditor User',
            'login': 'eh_stmt_auditor@test',
            'email': 'eh_stmt_auditor@test',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [cls.group_auditor.id])],
        })

    def test_soce_closing_reconciles(self):
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 100000.0, 'issue_of_shares': 20000.0}),
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0,
                        'dividends': 10000.0}),
                (0, 0, {'component': 'revaluation_reserve',
                        'opening_balance': 0.0, 'oci_movement': 5000.0}),
            ],
        })
        # Share capital 100000 + 20000 = 120000.
        sc = soce.line_ids.filtered(lambda l: l.component == 'share_capital')
        self.assertAlmostEqual(sc.closing_balance, 120000.0, places=2)
        # Retained 50000 + 30000 - 10000 = 70000.
        re = soce.line_ids.filtered(
            lambda l: l.component == 'retained_earnings')
        self.assertAlmostEqual(re.closing_balance, 70000.0, places=2)
        # Totals: opening 150000, closing 120000 + 70000 + 5000 = 195000.
        self.assertAlmostEqual(soce.total_opening, 150000.0, places=2)
        self.assertAlmostEqual(soce.total_closing, 195000.0, places=2)
        self.assertAlmostEqual(soce.total_profit, 30000.0, places=2)
        self.assertAlmostEqual(soce.total_dividends, 10000.0, places=2)

    def test_soci_total_comprehensive_income(self):
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
                (0, 0, {'name': 'Hedge', 'oci_type': 'cashflow_hedge',
                        'amount': -5000.0, 'will_reclassify': True}),
            ],
        })
        self.assertAlmostEqual(soci.oci_no_reclassify, 15000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, -5000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 10000.0, places=2)
        self.assertAlmostEqual(soci.total_comprehensive_income, 110000.0,
                               places=2)

    def test_ias1_82_line_items_oci_tax_and_associates(self):
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 900.0,
            'pl_revenue': 1000.0,
            'pl_interest_revenue_eir': 100.0,
            'pl_derecognition_gain_loss': 20.0,
            'pl_finance_costs': -50.0,
            'pl_impairment_losses': -30.0,
            'pl_share_associates_jv': 40.0,
            'pl_reclassification_amortised_cost': 5.0,
            'pl_reclassification_fvoci': 3.0,
            'pl_tax_expense': -200.0,
            'pl_discontinued_operations': 12.0,
            'line_ids': [
                (0, 0, {
                    'name': 'Associate OCI that may be reclassified',
                    'oci_type': 'associates_reclassify',
                    'amount': 100.0,
                    'tax_amount': -25.0,
                }),
                (0, 0, {
                    'name': 'Associate OCI that will not be reclassified',
                    'oci_type': 'associates_no_reclassify',
                    'amount': 40.0,
                    'tax_amount': -10.0,
                }),
            ],
        })
        self.assertAlmostEqual(soci.pl_line_items_total, 900.0, places=2)
        self.assertAlmostEqual(soci.pl_line_items_residual, 0.0, places=2)
        recyclable = soci.line_ids.filtered(
            lambda line: line.oci_type == 'associates_reclassify')
        non_recyclable = soci.line_ids - recyclable
        self.assertTrue(recyclable.will_reclassify)
        self.assertFalse(non_recyclable.will_reclassify)
        self.assertAlmostEqual(recyclable.net_amount, 75.0, places=2)
        self.assertAlmostEqual(non_recyclable.net_amount, 30.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, 75.0, places=2)
        self.assertAlmostEqual(soci.oci_no_reclassify, 30.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 105.0, places=2)

    def test_soce_ias1_106_106a_107_and_annual_comparatives(self):
        prior_soce = self.env['eh.soce'].create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
        })
        current_soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'comparative_annual_id': prior_soce.id,
            'dividends_per_share': 0.25,
            'dividends_disclosure_note': 'Ordinary shares: 0.25 per share.',
            'line_ids': [(0, 0, {
                'component': 'retained_earnings',
                'opening_balance': 1000.0,
                'retrospective_adjustment': -50.0,
                'profit': 300.0,
                'oci_movement': 50.0,
                'oci_revaluation': 40.0,
                'oci_translation': 10.0,
                'dividends': 100.0,
            })],
        })
        line = current_soce.line_ids
        self.assertEqual(current_soce.comparative_annual_id, prior_soce)
        self.assertAlmostEqual(line.restated_opening_balance, 950.0, places=2)
        self.assertAlmostEqual(line.oci_analysis_total, 50.0, places=2)
        self.assertAlmostEqual(line.oci_analysis_residual, 0.0, places=2)
        self.assertAlmostEqual(current_soce.total_restated_opening, 950.0,
                               places=2)
        self.assertAlmostEqual(line.closing_balance, 1200.0, places=2)
        self.assertAlmostEqual(current_soce.dividends_per_share, 0.25,
                               places=2)

        prior_soci = self.env['eh.soci'].create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
        })
        current_soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'comparative_annual_id': prior_soci.id,
        })
        self.assertEqual(current_soci.comparative_annual_id, prior_soci)

    def test_soci_attribution_residual(self):
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_owners': 90000.0,
            'attributable_to_nci': 10000.0,
            'line_ids': [],
        })
        # 100000 profit, no OCI; attribution 90000 + 10000 = 100000, residual 0.
        self.assertAlmostEqual(soci.total_comprehensive_income, 100000.0,
                               places=2)
        self.assertAlmostEqual(soci.attribution_residual, 0.0, places=2)

    def test_confirm(self):
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31'})
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soci_confirm_blocks_when_attribution_untied(self):
        # Attribution amounts set but they do not sum to total comprehensive
        # income: residual != 0, so confirm must raise.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_owners': 80000.0,
            'attributable_to_nci': 10000.0,
        })
        self.assertFalse(soci.attribution_tied)
        self.assertAlmostEqual(soci.attribution_residual, 10000.0, places=2)
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # A tied statement (residual 0) confirms without error.
        soci.attributable_to_owners = 90000.0
        self.assertTrue(soci.attribution_tied)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_soci_derive_profit_from_ledger(self):
        # Post a P&L move: 40000 revenue (credit) vs 15000 expense (debit),
        # cash balancing. Net profit = 40000 - 15000 = 25000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soci.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soci.profit_for_period, 25000.0, places=2)

    def test_primary_statement_ledger_scope_includes_every_branch(self):
        if (
            'parent_id' not in self.env['res.company']._fields
            or not hasattr(self.company, '_accessible_branches')
        ):
            self.skipTest('Accounting branches are unavailable on this series.')

        branch = self._create_accounting_branch({
            'name': 'IAS 1 Statement Branch',
            'parent_id': self.company.id,
        })
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        branch_journal = self._ensure_journal(
            branch_env, branch, 'general', 'IAS1B', 'IAS 1 Branch Journal',
        )
        if 'company_ids' in branch_env['account.account']._fields:
            branch_revenue = self.account_revenue
            branch_cash = self.account_cash
        else:
            branch_revenue = self._ensure_account(
                branch_env, 'IAS1R', 'IAS 1 Branch Revenue', 'income',
            )
            branch_cash = self._ensure_account(
                branch_env, 'IAS1C', 'IAS 1 Branch Cash', 'asset_cash',
            )
        move = branch_env['account.move'].create({
            'move_type': 'entry',
            'company_id': branch.id,
            'journal_id': branch_journal.id,
            'date': '2026-06-30',
            'line_ids': [
                (0, 0, {
                    'name': 'Branch revenue',
                    'account_id': branch_revenue.id,
                    'credit': 700.0,
                }),
                (0, 0, {
                    'name': 'Branch cash',
                    'account_id': branch_cash.id,
                    'debit': 700.0,
                }),
            ],
        })
        move.action_post()

        statement_env = branch_env['eh.soci'].with_company(self.company).env
        soci = statement_env['eh.soci'].create({
            'company_id': self.company.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
        })
        soce = statement_env['eh.soce'].create({
            'company_id': self.company.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
        })
        tieout = statement_env['eh.statement.tieout'].create({
            'company_id': self.company.id,
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        })

        self.assertAlmostEqual(
            soci._pl_ledger_profit_at('2026-01-01', '2026-12-31'),
            700.0, places=2,
        )
        self.assertTrue(soci._has_ledger_pl_at(
            '2026-01-01', '2026-12-31'))
        self.assertAlmostEqual(
            soce._pl_net_profit_at('2026-01-01', '2026-12-31'),
            700.0, places=2,
        )
        self.assertAlmostEqual(
            soce._equity_balance_at('2026-12-31'), 700.0, places=2,
        )
        self.assertAlmostEqual(
            tieout._pl_ledger_aggregate('2026-01-01', '2026-12-31'),
            700.0, places=2,
        )

    def test_profit_derivations_exclude_year_end_entries(self):
        """A same-day close must not erase the closed year's reported P&L."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        close = self.post_balanced_move(
            [
                {'account': self.account_revenue, 'debit': 40000.0},
                {'account': self.account_expense, 'credit': 15000.0},
                {'account': self.account_equity, 'credit': 25000.0},
            ],
            date='2026-12-31',
        )
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        with patch.object(
            type(soci), '_eh_year_end_entry_move_ids',
            lambda record: close.ids,
        ):
            self.assertAlmostEqual(
                soci._pl_ledger_profit_at('2026-01-01', '2026-12-31'),
                25000.0, places=2)
        with patch.object(
            type(soce), '_eh_year_end_entry_move_ids',
            lambda record: close.ids,
        ):
            self.assertAlmostEqual(
                soce._pl_net_profit_at('2026-01-01', '2026-12-31'),
                25000.0, places=2)

    def test_soce_profit_ties_and_derivation(self):
        # Post a P&L move dated within 2026: revenue 40000 (credit), expense
        # 15000 (debit), cash balancing. Net profit = 40000 - 15000 = 25000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings', 'profit': 25000.0}),
            ],
        })
        # Before derivation reported_profit is 0, so components (25000) do not
        # tie to reported (0).
        self.assertAlmostEqual(soce.total_profit, 25000.0, places=2)
        self.assertFalse(soce.profit_ties)
        # Deriving reported_profit from the ledger yields 25000 and ties.
        soce.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soce.reported_profit, 25000.0, places=2)
        self.assertAlmostEqual(soce.profit_movement_tie_out, 0.0, places=2)
        self.assertTrue(soce.profit_ties)
        # The component movement is tied, so the blocking control confirms.
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_confirm_snapshots_derivable_profit_without_manual_step(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {
                'component': 'retained_earnings',
                'profit': 25000.0,
            })],
        })
        self.assertFalse(soce.reported_profit)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        self.assertAlmostEqual(soce.reported_profit, 25000.0, places=2)
        self.assertTrue(soce.profit_ties)

    def test_soce_confirm_blocks_when_closing_disagrees_with_ledger(self):
        # A ledger equity figure IS derivable (a posted equity move exists),
        # so the closing tie-out is now blocking, not advisory. Post 200000
        # to owner equity at period end: ledger closing equity = 200000. The
        # worksheet claims a 150000 closing, which disagrees, so confirm must
        # raise and the statement stays draft.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': self.account_equity, 'credit': 200000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 150000.0}),
            ],
        })
        self.assertTrue(soce.ledger_derivable)
        self.assertAlmostEqual(soce.ledger_closing, 200000.0, places=2)
        self.assertAlmostEqual(soce.total_closing, 150000.0, places=2)
        self.assertFalse(soce.tied)
        with self.assertRaises(UserError):
            soce.action_confirm()
        self.assertEqual(soce.state, 'draft')
        # Reconciling the worksheet to the ledger (closing 200000) lets it
        # confirm.
        soce.line_ids[0].opening_balance = 200000.0
        self.assertTrue(soce.tied)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_ledger_equity_includes_unallocated_profit(self):
        """Open-year closing equity includes current P&L before formal close."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': self.account_equity, 'credit': 200000.0},
            ],
            date='2026-01-01',
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {
                    'component': 'share_capital',
                    'opening_balance': 200000.0,
                }),
                (0, 0, {
                    'component': 'retained_earnings',
                    'profit': 25000.0,
                }),
            ],
        })
        self.assertTrue(soce.ledger_derivable)
        self.assertAlmostEqual(soce.ledger_closing, 225000.0, places=2)
        self.assertAlmostEqual(soce.total_closing, 225000.0, places=2)
        self.assertTrue(soce.tied)

    def test_soce_confirm_advisory_when_ledger_not_derivable(self):
        # No equity postings exist, so no ledger closing figure is derivable:
        # the closing tie-out stays advisory. A worksheet with a non-zero
        # closing that would "disagree" with the nil ledger read must still
        # confirm, and a chatter warning is posted instead of blocking.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'opening_balance': 150000.0}),
            ],
        })
        self.assertFalse(soce.ledger_derivable)
        self.assertFalse(soce.tied)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_derive_from_ledger(self):
        # Post equity: credit 200000 to owner equity, debit cash. Equity is
        # credit-positive, so ledger equity = 200000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': self.account_equity, 'credit': 200000.0},
            ],
            date='2025-12-31',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital'}),
                (0, 0, {'component': 'retained_earnings'}),
            ],
        })
        soce.action_derive_from_ledger()
        # No per-component account mapping, so the equity opening (200000,
        # dated 2025-12-31 <= period_start-1) lands on the first line; the
        # second line is zeroed (backward-compatible path).
        self.assertAlmostEqual(soce.total_opening, 200000.0, places=2)
        first = soce.line_ids[0]
        self.assertAlmostEqual(first.opening_balance, 200000.0, places=2)

    def test_soce_derive_opening_lands_on_correct_component(self):
        # DEFECT (B): the derive helper used to dump ALL opening equity onto
        # line[0], so every per-component opening roll-forward was individually
        # wrong even though the header total tied. With a per-component account
        # mapping, each component must receive the opening balance of exactly
        # its own equity accounts.
        account_share = self._ensure_account(
            self.env, '3010', 'Share Capital', 'equity')
        account_retained = self._ensure_account(
            self.env, '3020', 'Retained Earnings', 'equity')
        # Opening balances at 2025-12-31: share capital 150000, retained 50000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 200000.0},
                {'account': account_share, 'credit': 150000.0},
                {'account': account_retained, 'credit': 50000.0},
            ],
            date='2025-12-31',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'share_capital',
                        'equity_account_ids': [(6, 0, [account_share.id])]}),
                (0, 0, {'component': 'retained_earnings',
                        'equity_account_ids': [(6, 0, [account_retained.id])]}),
            ],
        })
        soce.action_derive_from_ledger()
        share_line = soce.line_ids.filtered(
            lambda l: l.component == 'share_capital')
        retained_line = soce.line_ids.filtered(
            lambda l: l.component == 'retained_earnings')
        # Each component gets ITS OWN opening, not the whole 200000 on line[0].
        self.assertAlmostEqual(share_line.opening_balance, 150000.0, places=2)
        self.assertAlmostEqual(
            retained_line.opening_balance, 50000.0, places=2)
        # Header total still equals the ledger figure.
        self.assertAlmostEqual(soce.total_opening, 200000.0, places=2)

    def test_soce_rejects_duplicate_equity_account_mapping(self):
        account = self._ensure_account(
            self.env, '3035', 'Unique SoCE Mapping', 'equity')
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {
                'component': 'share_capital',
                'equity_account_ids': [(6, 0, account.ids)],
            })],
        })
        with self.assertRaisesRegex(UserError, 'mapped to both'):
            self.env['eh.soce.line'].create({
                'soce_id': soce.id,
                'component': 'retained_earnings',
                'equity_account_ids': [(6, 0, account.ids)],
            })

    def test_confirmed_soce_keeps_signed_ledger_snapshot(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 100.0},
                {'account': self.account_equity, 'credit': 100.0},
            ],
            date='2026-01-31',
        )
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {
                'component': 'share_capital',
                'opening_balance': 100.0,
            })],
        })
        soce.action_confirm()
        self.assertAlmostEqual(soce.ledger_closing, 100.0, places=2)
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 50.0},
                {'account': self.account_equity, 'credit': 50.0},
            ],
            date='2026-06-30',
        )
        soce.invalidate_recordset(['ledger_closing', 'closing_tie_out'])
        self.assertAlmostEqual(
            soce.ledger_closing, 100.0, places=2,
            msg="Signed statement must retain its confirmation snapshot.")

    def test_auditor_can_read_statement_headers_and_lines(self):
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {'component': 'share_capital'})],
        })
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {
                'name': 'Auditor OCI',
                'oci_type': 'other',
            })],
        })
        auditor_soce = soce.with_user(self.user_auditor)
        auditor_soci = soci.with_user(self.user_auditor)
        self.assertEqual(auditor_soce.read(['name'])[0]['name'], soce.name)
        self.assertEqual(
            auditor_soce.line_ids.read(['component'])[0]['component'],
            'share_capital')
        self.assertEqual(auditor_soci.read(['name'])[0]['name'], soci.name)
        self.assertEqual(
            auditor_soci.line_ids.read(['name'])[0]['name'], 'Auditor OCI')

    def test_soci_confirm_blocks_when_profit_disagrees_with_ledger(self):
        # DEFECT (A): a mis-keyed profit used to confirm freely because the
        # SoCI confirm never tied profit_for_period to the ledger. A ledger
        # profit figure IS derivable here (posted P&L exists), so the gate
        # must now BLOCK when the reported profit disagrees.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        # Ledger net profit = 25000, but the statement was mis-keyed to 30000.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 30000.0,
        })
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Correcting the profit to match the ledger lets it confirm.
        soci.profit_for_period = 25000.0
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_soci_confirm_advisory_when_ledger_not_derivable(self):
        # Advisory-safe: with NO posted P&L in the period the ledger profit is
        # not derivable, so a hand-keyed profit must still confirm (existing
        # flows on empty-ledger companies are preserved).
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_search_views_period_sequences_state_copy_and_currency_format(self):
        Soci = self.env['eh.soci']
        Soce = self.env['eh.soce']
        Tieout = self.env['eh.statement.tieout']
        self.assertFalse(Soci._fields['state'].copy)
        self.assertFalse(Soce._fields['state'].copy)
        self.assertFalse(Tieout._fields['state'].copy)

        soci = Soci.create({
            'period_start': '2024-01-01', 'period_end': '2024-12-31',
        })
        soce = Soce.create({
            'period_start': '2024-01-01', 'period_end': '2024-12-31',
        })
        tieout = Tieout.create({
            'date_from': '2024-01-01', 'date_to': '2024-12-31',
        })
        self.assertIn('/2024/', soci.name)
        self.assertIn('/2024/', soce.name)
        self.assertIn('/2024/', tieout.name)

        soci_search = self.env.ref(
            'eh_account_statements.view_eh_soci_search')
        soce_search = self.env.ref(
            'eh_account_statements.view_eh_soce_search')
        self.assertIn('period_end', soci_search.arch_db)
        self.assertIn('group_company', soce_search.arch_db)
        self.assertEqual(
            self.env.ref('eh_account_statements.action_eh_soci').search_view_id,
            soci_search,
        )
        self.assertEqual(
            self.env.ref('eh_account_statements.action_eh_soce').search_view_id,
            soce_search,
        )

        currency = self.env['res.currency'].search(
            [('rounding', '=', 0.001)], limit=1)
        if not currency:
            currency = self.env['res.currency'].create({
                'name': 'XTS', 'symbol': 'XTS', 'rounding': 0.001,
            })
        # Prove the reusable formatter honors a three-decimal currency with a
        # lightweight statement in a temporary three-decimal company.
        company = self.env['res.company'].create({
            'name': 'Statements Three Decimal Company',
            'currency_id': currency.id,
        })
        self.env.user.write({'company_ids': [(4, company.id)]})
        three_decimal = Soci.with_company(company).create({
            'company_id': company.id,
            'period_start': '2024-01-01', 'period_end': '2024-12-31',
        })
        formatted = presentation.format_statement_amount(
            three_decimal, 1234.567)
        self.assertIn('567', formatted)

    # ---- manager gate (segregation of duties, IAS 1) ----

    def test_soci_confirm_requires_manager(self):
        # A non-manager can neither confirm nor silently reopen a signed
        # statement of comprehensive income; a manager can do both.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        with self.assertRaises(AccessError):
            soci.with_user(self.user_plain).action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Real non-superuser manager confirms guarded stored totals.
        soci.with_user(self.user_manager).action_confirm()
        soci.invalidate_recordset(['state'])
        self.assertEqual(soci.state, 'confirmed')
        # Non-manager cannot reopen a confirmed statement.
        with self.assertRaises(AccessError):
            soci.with_user(self.user_plain).action_set_to_draft()
        self.assertEqual(soci.state, 'confirmed')
        # Manager can reopen.
        soci.with_user(self.user_manager).action_set_to_draft()
        soci.invalidate_recordset(['state'])
        self.assertEqual(soci.state, 'draft')

    def test_soce_confirm_requires_manager(self):
        # Same gate on the statement of changes in equity.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [(0, 0, {
                'component': 'retained_earnings',
                'opening_balance': 0.0,
            })],
        })
        with self.assertRaises(AccessError):
            soce.with_user(self.user_plain).action_confirm()
        self.assertEqual(soce.state, 'draft')
        soce.with_user(self.user_manager).action_confirm()
        soce.invalidate_recordset(['state'])
        self.assertEqual(soce.state, 'confirmed')
        with self.assertRaises(AccessError):
            soce.with_user(self.user_plain).action_set_to_draft()
        self.assertEqual(soce.state, 'confirmed')
        soce.with_user(self.user_manager).action_set_to_draft()
        soce.invalidate_recordset(['state'])
        self.assertEqual(soce.state, 'draft')

    # ---- freeze-after-confirm (IAS 1.106-108) ----

    def test_soci_confirmed_figures_frozen(self):
        # A confirmed statement of comprehensive income must not have its
        # figures edited in place; only a set-to-draft unlocks it.
        # The single OCI line names no source account, so its recycling
        # section is not tag-derived (IAS 1.82A): this test exercises the
        # freeze mechanics, not the recycling gate, so it clears that gate
        # with the OCI recycling override + reason.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'oci_tag_override': True,
            'oci_tag_override_reason': 'Freeze-mechanics fixture only.',
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
            ],
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        # Editing the header profit figure is blocked.
        with self.assertRaises(UserError):
            soci.profit_for_period = 250000.0
        self.assertAlmostEqual(soci.profit_for_period, 100000.0, places=2)
        # Editing an OCI line amount is blocked too.
        with self.assertRaises(UserError):
            soci.line_ids[0].amount = 99999.0
        self.assertAlmostEqual(soci.line_ids[0].amount, 15000.0, places=2)
        # Deleting an OCI line is blocked.
        with self.assertRaises(UserError):
            soci.line_ids[0].unlink()
        # Deleting the confirmed statement is blocked.
        with self.assertRaises(UserError):
            soci.unlink()
        # After a manager set-to-draft the figures are editable again.
        soci.action_set_to_draft()
        soci.profit_for_period = 250000.0
        self.assertAlmostEqual(soci.profit_for_period, 250000.0, places=2)

    def test_soci_combined_state_and_figure_write_is_blocked(self):
        # The freeze must not be bypassable by flipping state to draft AND
        # editing a frozen figure in the SAME raw write.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        with self.assertRaises(UserError):
            soci.write({'state': 'draft', 'profit_for_period': 250000.0})
        self.assertEqual(soci.state, 'confirmed')
        self.assertAlmostEqual(soci.profit_for_period, 100000.0, places=2)

    def test_non_manager_cannot_reopen_confirmed_statement_by_raw_write(self):
        # Reopening a confirmed statement (state -> draft) is manager-gated
        # even via a raw ORM write, not just via action_set_to_draft.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.action_confirm()
        with self.assertRaises(UserError):
            soci.with_user(self.user_plain).write({'state': 'draft'})
        self.assertEqual(soci.state, 'confirmed')

    def test_soce_confirmed_figures_frozen(self):
        # A confirmed statement of changes in equity must not have its
        # figures edited in place; only a set-to-draft unlocks it.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0}),
            ],
        })
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        # Editing a line input figure is blocked.
        with self.assertRaises(UserError):
            soce.line_ids[0].profit = 99999.0
        self.assertAlmostEqual(soce.line_ids[0].profit, 30000.0, places=2)
        # Editing the header reported_profit is blocked.
        with self.assertRaises(UserError):
            soce.reported_profit = 12345.0
        # Deleting a line is blocked.
        with self.assertRaises(UserError):
            soce.line_ids[0].unlink()
        # Deleting the confirmed statement is blocked.
        with self.assertRaises(UserError):
            soce.unlink()
        # After a manager set-to-draft the figures are editable again.
        soce.action_set_to_draft()
        soce.line_ids[0].profit = 99999.0
        self.assertAlmostEqual(soce.line_ids[0].profit, 99999.0, places=2)

    def test_soci_confirmed_line_create_append_blocked(self):
        # A direct create() that appends an OCI line to a CONFIRMED statement
        # would recompute totals and silently move the parent figures, so it
        # must be blocked just like write()/unlink() on the line.
        # The OCI line names no source account, so it clears the IAS 1.82A
        # recycling completeness gate with the override (this test exercises
        # the confirmed-append freeze, not the recycling gate).
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'oci_tag_override': True,
            'oci_tag_override_reason': 'Freeze-mechanics fixture only.',
            'line_ids': [
                (0, 0, {'name': 'Revaluation', 'oci_type': 'revaluation',
                        'amount': 15000.0, 'will_reclassify': False}),
            ],
        })
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        before = soci.total_comprehensive_income
        with self.assertRaises(UserError):
            self.env['eh.soci.line'].create({
                'soci_id': soci.id, 'name': 'Injected',
                'oci_type': 'other', 'amount': 88888.0,
            })
        self.assertEqual(len(soci.line_ids), 1)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, before, places=2)
        # After a manager set-to-draft the append is allowed again.
        soci.action_set_to_draft()
        self.env['eh.soci.line'].create({
            'soci_id': soci.id, 'name': 'Allowed',
            'oci_type': 'other', 'amount': 5000.0,
        })
        self.assertEqual(len(soci.line_ids), 2)

    def test_soce_confirmed_line_create_append_blocked(self):
        # A direct create() that appends a line to a CONFIRMED statement of
        # changes in equity would recompute totals and silently move the parent
        # figures, so it must be blocked just like write()/unlink().
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings',
                        'opening_balance': 50000.0, 'profit': 30000.0}),
            ],
        })
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')
        before = soce.total_closing
        with self.assertRaises(UserError):
            self.env['eh.soce.line'].create({
                'soce_id': soce.id, 'component': 'share_capital',
                'opening_balance': 77777.0,
            })
        self.assertEqual(len(soce.line_ids), 1)
        self.assertAlmostEqual(soce.total_closing, before, places=2)
        # After a manager set-to-draft the append is allowed again.
        soce.action_set_to_draft()
        self.env['eh.soce.line'].create({
            'soce_id': soce.id, 'component': 'share_capital',
            'opening_balance': 1000.0,
        })
        self.assertEqual(len(soce.line_ids), 2)
