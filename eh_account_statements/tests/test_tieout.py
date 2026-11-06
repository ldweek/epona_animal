# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cross-statement tie-out control tests.

One posted-ledger truth, four statement figures: P&L net profit, SoCI profit
for the period, SoCE profit movement, and the balance sheet current-year
earnings movement must all agree for a period. These tests exercise the
eh.statement.tieout control and the SoCE conditional-blocking profit gate.
"""

from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_statements', 'integration', 'post_install', '-at_install')
class TestStatementTieout(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # action_check / action_reset on the tie-out and action_confirm on
        # the statements are manager-gated; the acting test user must be an
        # EH Accounting Manager for the positive paths.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        cls.group_user = cls.env.ref('eh_account_base.group_eh_user')
        cls.user_plain = cls.env['res.users'].create({
            'name': 'Tieout Plain User',
            'login': 'eh_tieout_plain@test',
            'email': 'eh_tieout_plain@test',
            'group_ids': [(6, 0, [cls.group_user.id])],
        })

    def _post_pl_move(self, revenue, expense, date):
        """Post revenue (credit) vs expense (debit), cash balancing."""
        return self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': revenue - expense},
                {'account': self.account_expense, 'debit': expense},
                {'account': self.account_revenue, 'credit': revenue},
            ],
            date=date,
        )

    def _create_tieout(self):
        return self.env['eh.statement.tieout'].create({
            'date_from': '2026-01-01', 'date_to': '2026-12-31',
        })

    def test_tieout_all_statements_agree(self):
        # Net profit = 40000 - 15000 = 25000, posted within the period.
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        # SoCI derives its profit from the same ledger.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soci.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soci.profit_for_period, 25000.0, places=2)
        # SoCE carries the matching profit movement on its components.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings', 'profit': 25000.0}),
            ],
        })
        tieout = self._create_tieout()
        tieout.action_check()
        self.assertEqual(tieout.state, 'checked')
        self.assertAlmostEqual(tieout.pl_net_profit, 25000.0, places=2)
        self.assertAlmostEqual(tieout.soci_profit, 25000.0, places=2)
        self.assertAlmostEqual(tieout.soce_profit_movement, 25000.0, places=2)
        self.assertAlmostEqual(
            tieout.bs_current_year_earnings_delta, 25000.0, places=2)
        # Both sources were found and recorded, nothing marked NA.
        self.assertEqual(tieout.soci_id, soci)
        self.assertEqual(tieout.soce_id, soce)
        self.assertTrue(tieout.soci_applicable)
        self.assertTrue(tieout.soce_applicable)
        self.assertTrue(tieout.soci_tied)
        self.assertTrue(tieout.soce_tied)
        self.assertTrue(tieout.bs_tied)
        self.assertTrue(tieout.all_tied)

    def test_soce_confirm_blocks_and_tieout_reports_untied(self):
        # Ledger net profit is 25000 but the SoCE claims 30000 was taken to
        # equity while its reported profit (derived from the ledger) is
        # 25000: reported_profit is set and does not tie, so the SoCE
        # confirm gate must now BLOCK (it used to be advisory only).
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings', 'profit': 30000.0}),
            ],
        })
        soce.action_derive_profit_from_ledger()
        self.assertAlmostEqual(soce.reported_profit, 25000.0, places=2)
        self.assertFalse(soce.profit_ties)
        with self.assertRaises(UserError):
            soce.action_confirm()
        self.assertEqual(soce.state, 'draft')
        # The tie-out reports the SoCE pair untied with a 5000 residual;
        # there is no SoCI for the period, so that pair is marked not
        # applicable and the absence is recorded in the source note.
        tieout = self._create_tieout()
        tieout.action_check()
        self.assertFalse(tieout.soce_tied)
        self.assertAlmostEqual(tieout.soce_residual, 5000.0, places=2)
        self.assertFalse(tieout.all_tied)
        self.assertFalse(tieout.soci_applicable)
        self.assertTrue(tieout.soci_tied)
        self.assertIn('not applicable', tieout.source_note)
        # Fixing the component profit lets the SoCE confirm again.
        soce.line_ids[0].profit = 25000.0
        self.assertTrue(soce.profit_ties)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_soce_confirm_stays_advisory_without_reported_profit(self):
        # No reported profit entered: the gate must not block, preserving
        # the previous advisory-only behaviour.
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'line_ids': [
                (0, 0, {'component': 'retained_earnings', 'profit': 30000.0}),
            ],
        })
        self.assertFalse(soce.profit_ties)
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_tieout_bs_delta_equals_pl_net_profit(self):
        # Prior-period P&L activity must NOT leak into the current-year
        # earnings movement: the balance sheet delta snapshots the ledger
        # aggregate at both period ends, so only the in-period profit
        # remains. Prior year: net 10000. Period: net 25000.
        self._post_pl_move(30000.0, 20000.0, '2025-06-30')
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        tieout = self._create_tieout()
        tieout.action_check()
        self.assertAlmostEqual(tieout.pl_net_profit, 25000.0, places=2)
        self.assertAlmostEqual(
            tieout.bs_current_year_earnings_delta, tieout.pl_net_profit,
            places=2)
        self.assertAlmostEqual(tieout.bs_residual, 0.0, places=2)
        self.assertTrue(tieout.bs_tied)

    def test_balance_sheet_leg_is_an_independent_stock_query(self):
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        tieout = self._create_tieout()

        def mismatching_balance_sheet_stock(record, date_to):
            return 20000.0 if str(date_to) == '2026-12-31' else 0.0

        with patch.object(
            type(tieout), '_balance_sheet_cye_at', autospec=True,
            side_effect=mismatching_balance_sheet_stock,
        ):
            tieout.action_check()
        self.assertAlmostEqual(tieout.pl_net_profit, 25000.0, places=2)
        self.assertAlmostEqual(
            tieout.bs_current_year_earnings_delta, 20000.0, places=2)
        self.assertAlmostEqual(tieout.bs_residual, -5000.0, places=2)
        self.assertFalse(tieout.bs_tied)
        self.assertFalse(tieout.all_tied)

    def test_tieout_without_primary_statements_is_not_signed_off(self):
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        tieout = self._create_tieout()
        tieout.action_check()
        self.assertFalse(tieout.soci_applicable)
        self.assertFalse(tieout.soce_applicable)
        self.assertTrue(tieout.bs_tied)
        self.assertFalse(
            tieout.all_tied,
            "Internal ledger arithmetic cannot replace missing statements.")

    def test_primary_statement_period_sources_are_database_unique(self):
        vals = {
            'period_start': '2031-01-01',
            'period_end': '2031-12-31',
        }
        self.env['eh.soci'].create(vals)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.soci'].create(vals)
        self.env['eh.soce'].create(vals)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.soce'].create(vals)

    def test_tieout_frozen_after_check(self):
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        tieout = self._create_tieout()
        tieout.action_check()
        self.assertEqual(tieout.state, 'checked')
        # Snapshot figures and period bounds are frozen once checked.
        with self.assertRaises(UserError):
            tieout.pl_net_profit = 1.0
        with self.assertRaises(UserError):
            tieout.date_from = '2026-02-01'
        # Re-running the check on a frozen record is refused.
        with self.assertRaises(UserError):
            tieout.action_check()
        # Free-text notes stay editable on a frozen control.
        tieout.notes = 'Reviewed.'
        self.assertEqual(tieout.notes, 'Reviewed.')
        # A manager reset unfreezes the record so it can be edited and
        # checked again.
        tieout.action_reset()
        self.assertEqual(tieout.state, 'draft')
        tieout.date_from = '2026-02-01'
        self.assertEqual(str(tieout.date_from), '2026-02-01')

    def test_tieout_freeze_guard_scoped_to_frozen_records(self):
        # A batch write over [checked, draft] touching a frozen field must
        # block only because of the checked record, and the error must name
        # the frozen record, not the draft one. A pure-draft batch touching
        # the same field must write through untouched.
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        checked = self._create_tieout()
        checked.action_check()
        self.assertEqual(checked.state, 'checked')
        draft = self._create_tieout()
        self.assertEqual(draft.state, 'draft')
        batch = checked + draft
        with self.assertRaises(UserError) as cm:
            batch.write({'date_from': '2026-03-01'})
        message = str(cm.exception)
        self.assertIn(checked.name, message)
        self.assertNotIn(draft.name, message)
        # The draft record on its own accepts the frozen-field write because
        # nothing in its recordset is checked.
        draft.write({'date_from': '2026-03-01'})
        self.assertEqual(str(draft.date_from), '2026-03-01')

    def test_tieout_check_requires_manager(self):
        # action_check and action_reset are manager-gated.
        self._post_pl_move(40000.0, 15000.0, '2026-06-30')
        tieout = self._create_tieout()
        with self.assertRaises(AccessError):
            tieout.with_user(self.user_plain).action_check()
        self.assertEqual(tieout.state, 'draft')
        tieout.action_check()
        with self.assertRaises(AccessError):
            tieout.with_user(self.user_plain).action_reset()
        self.assertEqual(tieout.state, 'checked')
