# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden tests for the generated disclosures.

Phase 6 of the IFRS 10/10 program: disclosure numbers come from the ledger
and the measurement engines, not from typing. Every expected amount below is
hand-derived from inputs stated in the test, with the derivation in a
comment; nothing is read back from an engine to build an expectation.

Cross-module feeds (ECL staging, IFRS 16 lease schedules, IFRS 2 period
charges, fair-value floating flags) are soft lookups, so their tests skip
cleanly when the feeding module is not installed and run in full on a suite
install.
"""

from datetime import date
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestCreditNoteEclFeed(EhGoldenTestCase):
    """IFRS 7.35A-N: the credit-risk note's staging table, allowance
    reconciliation and provision-matrix summary feed from the posted ECL
    run; a manual stage row overrides with a flagged discrepancy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.ecl_expense = cls._ensure_account(
            cls.env, '5300', 'ECL Impairment Loss', 'expense')
        cls.ecl_allowance = cls._ensure_account(
            cls.env, '1109', 'Loss Allowance', 'asset_current')

    def setUp(self):
        super().setUp()
        if 'eh.ecl.run' not in self.env:
            self.skipTest('eh_account_ecl is not installed')

    def _posted_ecl_run(self):
        """Simplified run with two hand-keyed buckets, computed and posted.

        Derivation (no discounting: discount rate 0, periods 0):
          bucket 'Current' : gross 100,000 x 2%  = ECL 2,000 (stage 1)
          bucket 'Over 90' : gross  50,000 x 10% = ECL 5,000 (stage 2)
          closing allowance 7,000; opening 0; movement 7,000.
        Reconciliation (first run, so opening/transfers/write-offs are 0):
          stage 1: remeasurement 2,000, closing 2,000
          stage 2: remeasurement 5,000, closing 5,000
        """
        today = fields.Date.context_today(self.env.user)
        run = self.env['eh.ecl.run'].create({
            'reporting_date': today,
            'measurement_approach': 'simplified',
            'journal_id': self.journal_misc.id,
            'impairment_expense_account_id': self.ecl_expense.id,
            'loss_allowance_account_id': self.ecl_allowance.id,
            'opening_allowance': 0.0,
            'bucket_ids': [
                (0, 0, {'name': 'Current', 'days_from': 0, 'days_to': 30,
                        'loss_rate': 2.0, 'stage': '1',
                        'gross_carrying': 100000.0}),
                (0, 0, {'name': 'Over 90', 'days_from': 91, 'days_to': 0,
                        'loss_rate': 10.0, 'stage': '2',
                        'gross_carrying': 50000.0}),
            ],
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted',
                         'the feeding ECL run must be posted')
        return run

    def test_credit_note_feeds_staging_recon_and_matrix(self):
        run = self._posted_ecl_run()
        today = fields.Date.context_today(self.env.user)
        note = self.env['eh.fin.credit.note'].create({
            'reporting_date': today})
        note.action_populate()

        self.assertEqual(note.ecl_run_approach, 'simplified')
        self.assertTrue(note.ecl_run_name, 'the feeding run must be stamped')
        self.assertTrue(note.ecl_source_valid)
        self.assertEqual(note.ecl_source_status, 'valid')

        # Staging table: one engine row per stage with data.
        self.assertEqual(len(note.stage_line_ids), 2,
                         'stages 1 and 2 carry data, 3 and POCI do not')
        by_stage = {l.stage: l for l in note.stage_line_ids}
        s1, s2 = by_stage['1'], by_stage['2']
        self.assertEqual(s1.origin, 'ecl')
        self.assertAlmostEqual(s1.gross_carrying, 100000.0, places=2)
        self.assertAlmostEqual(s1.allowance, 2000.0, places=2)
        # Net = 100,000 - 2,000 = 98,000.
        self.assertAlmostEqual(s1.net_carrying, 98000.0, places=2)
        self.assertFalse(s1.has_discrepancy)
        self.assertAlmostEqual(s2.gross_carrying, 50000.0, places=2)
        self.assertAlmostEqual(s2.allowance, 5000.0, places=2)
        self.assertAlmostEqual(s2.net_carrying, 45000.0, places=2)
        # Totals: 150,000 gross, 7,000 allowance, 143,000 net.
        self.assertAlmostEqual(note.total_gross, 150000.0, places=2)
        self.assertAlmostEqual(note.total_allowance, 7000.0, places=2)
        self.assertAlmostEqual(note.total_net, 143000.0, places=2)
        self.assertFalse(note.has_discrepancy)

        # A later change to the feeder invalidates the stamped evidence and
        # finalisation fails closed until the note is repopulated.
        self.env.cr.execute(
            "UPDATE eh_ecl_run "
            "SET write_date = write_date + interval '1 second' "
            "WHERE id = %s",
            (run.id,),
        )
        run.invalidate_recordset(['write_date'])
        note.invalidate_recordset([
            'ecl_source_valid', 'ecl_source_status',
        ])
        self.assertFalse(note.ecl_source_valid)
        self.assertEqual(note.ecl_source_status, 'changed')
        with self.assertRaises(UserError):
            note.action_finalise()
        note.action_populate()
        self.assertTrue(note.ecl_source_valid)

        # Reconciliation mirror: hand-derived AND equal to the run's rows.
        self.assertEqual(len(note.recon_line_ids), 2,
                         'all-zero stages are not copied')
        recon = {l.stage: l for l in note.recon_line_ids}
        for stage, closing in (('1', 2000.0), ('2', 5000.0)):
            row = recon[stage]
            self.assertAlmostEqual(row.opening, 0.0, places=2)
            self.assertAlmostEqual(row.transfers_in, 0.0, places=2)
            self.assertAlmostEqual(row.transfers_out, 0.0, places=2)
            self.assertAlmostEqual(row.remeasurement, closing, places=2)
            self.assertAlmostEqual(row.writeoffs, 0.0, places=2)
            self.assertAlmostEqual(row.closing, closing, places=2)
            source = run.recon_ids.filtered(lambda r: r.stage == stage)
            self.assertAlmostEqual(row.closing, source.closing, places=2,
                                   msg='note row must equal the run recon')

        # Provision-matrix summary for the simplified run.
        self.assertEqual(len(note.matrix_line_ids), 2)
        matrix = {l.name: l for l in note.matrix_line_ids}
        self.assertAlmostEqual(
            matrix['Current'].gross_carrying, 100000.0, places=2)
        self.assertAlmostEqual(matrix['Current'].ecl, 2000.0, places=2)
        self.assertAlmostEqual(matrix['Current'].loss_rate, 2.0, places=4)
        self.assertAlmostEqual(
            matrix['Over 90'].gross_carrying, 50000.0, places=2)
        self.assertAlmostEqual(matrix['Over 90'].ecl, 5000.0, places=2)

        # Idempotent: a second populate rebuilds, never duplicates.
        note.action_populate()
        self.assertEqual(len(note.stage_line_ids), 2)
        self.assertEqual(len(note.recon_line_ids), 2)
        self.assertEqual(len(note.matrix_line_ids), 2)
        self.assertAlmostEqual(note.total_allowance, 7000.0, places=2)

    def test_credit_note_manual_override_flags_discrepancy(self):
        """A preparer-keyed stage row wins (override) but the engine figure
        is stamped alongside and the disagreement is flagged: manual
        allowance 2,500 vs engine 2,000 -> discrepancy 500."""
        self._posted_ecl_run()
        today = fields.Date.context_today(self.env.user)
        note = self.env['eh.fin.credit.note'].create({
            'reporting_date': today})
        self.env['eh.fin.credit.stage.line'].create({
            'note_id': note.id, 'stage': '1',
            'gross_carrying': 100000.0, 'allowance': 2500.0,
        })
        note.action_populate()

        by_stage = {l.stage: l for l in note.stage_line_ids}
        s1 = by_stage['1']
        self.assertEqual(s1.origin, 'manual',
                         'the manual row must survive populate as override')
        self.assertAlmostEqual(s1.allowance, 2500.0, places=2)
        self.assertAlmostEqual(s1.engine_allowance, 2000.0, places=2)
        # Discrepancy = manual 2,500 - engine 2,000 = 500.
        self.assertAlmostEqual(s1.allowance_discrepancy, 500.0, places=2)
        self.assertTrue(s1.has_discrepancy)
        self.assertTrue(note.has_discrepancy)
        # Stage 2 stays an engine row.
        s2 = by_stage['2']
        self.assertEqual(s2.origin, 'ecl')
        self.assertAlmostEqual(s2.allowance, 5000.0, places=2)
        # Totals use the override: 2,500 + 5,000 = 7,500.
        self.assertAlmostEqual(note.total_allowance, 7500.0, places=2)
        # Idempotent with the override in place.
        note.action_populate()
        self.assertEqual(len(note.stage_line_ids), 2)
        self.assertAlmostEqual(note.total_allowance, 7500.0, places=2)

    def test_credit_note_finalise_freezes(self):
        run = self._posted_ecl_run()
        today = fields.Date.context_today(self.env.user)
        note = self.env['eh.fin.credit.note'].create({
            'reporting_date': today})
        note.action_populate()
        note.action_finalise()
        with self.assertRaises(UserError):
            note.action_populate()
        with self.assertRaises(UserError):
            note.stage_line_ids[0].allowance = 999.0
        with self.assertRaises(UserError):
            self.env['eh.fin.credit.stage.line'].create({
                'note_id': note.id, 'stage': '3', 'allowance': 1.0})
        note.action_reopen()
        note.action_populate()
        self.assertEqual(run.state, 'posted')


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestMaturityExtraction(EhGoldenTestCase):
    """IFRS 7.39: contractual undiscounted buckets extracted from open
    items and lease schedules, idempotently, with manual rows preserved."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_open_receivable_extraction_days_scheme_idempotent(self):
        """A 1,000 receivable due in 45 days lands whole in the 31-90 day
        bucket of the day-count scheme; repopulating is idempotent and a
        manual row survives it."""
        today = fields.Date.context_today(self.env.user)
        due = today + relativedelta(days=45)
        self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a, 'date_maturity': due},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date=today)

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'band_scheme': 'days',
            'include_open_items': True,
        })
        run.action_populate()

        rec_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'receivable')
        self.assertEqual(len(rec_rows), 1,
                         'zero buckets are skipped for extracted classes')
        self.assertEqual(rec_rows.band, 'd31_90',
                         '45 days out belongs in the 31-90 day bucket')
        self.assertAlmostEqual(rec_rows.undiscounted_amount, 1000.0,
                               places=2)
        self.assertEqual(rec_rows.origin, 'extracted')
        self.assertAlmostEqual(run.total_undiscounted, 0.0, places=2)
        self.assertAlmostEqual(
            run.total_receivable_inflows, 1000.0, places=2,
        )

        # A manual band row survives the repopulate; extracted rows are
        # rebuilt without duplication (the ECL-populate idempotency
        # pattern).
        self.env['eh.fin.maturity.line'].create({
            'run_id': run.id, 'band': 'gt_5y',
            'undiscounted_amount': 77.0,
        })
        run.action_populate()
        rec_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'receivable')
        manual_rows = run.line_ids.filtered(lambda l: l.origin == 'manual')
        self.assertEqual(len(rec_rows), 1, 'repopulate must not duplicate')
        self.assertAlmostEqual(rec_rows.undiscounted_amount, 1000.0,
                               places=2)
        self.assertEqual(len(manual_rows), 1,
                         'the manual row must survive the repopulate')
        self.assertAlmostEqual(manual_rows.undiscounted_amount, 77.0,
                               places=2)
        # Liability/manual outflows and receivable inflows are not netted.
        self.assertAlmostEqual(run.total_undiscounted, 77.0, places=2)
        self.assertAlmostEqual(
            run.total_receivable_inflows, 1000.0, places=2,
        )

    def test_open_receivable_extraction_contractual_scheme(self):
        """The same 45-day receivable under the default contractual scheme
        lands in the under-3-months band, so the original presentation
        remains available."""
        today = fields.Date.context_today(self.env.user)
        due = today + relativedelta(days=45)
        self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a, 'date_maturity': due},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date=today)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'include_open_items': True,
        })
        run.action_populate()
        rec_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'receivable')
        self.assertEqual(rec_rows.band, 'lt_3m')
        self.assertAlmostEqual(rec_rows.undiscounted_amount, 1000.0,
                               places=2)

    def test_open_item_aggregate_does_not_search_full_aml_recordset(self):
        today = fields.Date.context_today(self.env.user)
        due = today + relativedelta(days=45)
        self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 123.0,
             'partner': self.partner_a, 'date_maturity': due},
            {'account': self.account_revenue, 'credit': 123.0},
        ], date=today)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
        })
        MoveLine = self.env['account.move.line']
        with patch.object(
            type(MoveLine), 'search',
            side_effect=AssertionError(
                "open-item aggregate materialised account.move.line"
            ),
        ):
            flows = list(run._open_item_flows('asset_receivable'))
        self.assertIn((due, 123.0), flows)

    def test_open_item_residual_is_reconstructed_at_reporting_cutoff(self):
        """A later full reconciliation must not erase the cutoff exposure.

        Invoice 1,000; payment 400 dated before 31 March; payment 600 dated
        after it. The invoice is fully reconciled now, but its 31 March
        residual was exactly 600 and that is the maturity amount.
        """
        cutoff = date(2026, 3, 31)
        invoice = self.post_balanced_move([
            {'account': self.account_receivable, 'debit': 1000.0,
             'partner': self.partner_a,
             'date_maturity': date(2026, 6, 30)},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date=date(2026, 1, 15))
        first_payment = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 400.0},
            {'account': self.account_receivable, 'credit': 400.0,
             'partner': self.partner_a},
        ], date=date(2026, 3, 1))
        later_payment = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 600.0},
            {'account': self.account_receivable, 'credit': 600.0,
             'partner': self.partner_a},
        ], date=date(2026, 4, 30))
        invoice_line = invoice.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        first_line = first_payment.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        later_line = later_payment.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        (invoice_line | first_line).reconcile()
        (invoice_line | later_line).reconcile()
        self.assertTrue(invoice_line.reconciled)
        self.assertAlmostEqual(invoice_line.amount_residual, 0.0, places=2)

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': cutoff,
            'include_open_items': True,
        })
        run.action_populate()
        receivables = run.line_ids.filtered(
            lambda line: line.item_class == 'receivable'
        )
        self.assertAlmostEqual(
            sum(receivables.mapped('undiscounted_amount')), 600.0, places=2,
        )

    def test_legacy_maturity_rows_quarantine_and_remediation(self):
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': date(2026, 3, 31),
        })
        line = self.env['eh.fin.maturity.line'].create({
            'run_id': run.id,
            'band': 'lt_3m',
            'item_class': 'payable',
            'undiscounted_amount': 125.0,
        })
        # Reproduce a row stamped by the upgrade migration without invoking a
        # forged ORM engine context in the regression itself.
        self.env.cr.execute(
            "UPDATE eh_fin_maturity_line "
            "SET legacy_claimed_origin = 'extracted', "
            "legacy_claimed_item_class = item_class, "
            "origin = 'legacy_ambiguous', item_class = 'legacy_unknown' "
            "WHERE id = %s",
            (line.id,),
        )
        line.invalidate_recordset()
        run.invalidate_recordset()
        run._compute_legacy_ambiguous_count()
        run._compute_total()
        self.assertEqual(line.origin, 'legacy_ambiguous')
        self.assertEqual(line.item_class, 'legacy_unknown')
        self.assertEqual(line.legacy_claimed_origin, 'extracted')
        self.assertEqual(line.legacy_claimed_item_class, 'payable')
        self.assertEqual(run.legacy_ambiguous_count, 1)
        self.assertAlmostEqual(run.total_undiscounted, 0.0, places=2)
        with self.assertRaises(UserError):
            run.action_populate()
        with self.assertRaises(UserError):
            run.action_finalise()

        run.action_confirm_legacy_lines_manual()
        self.assertEqual(line.origin, 'manual')
        self.assertEqual(line.item_class, 'manual')
        self.assertEqual(line.legacy_claimed_item_class, 'payable')
        self.assertEqual(line.legacy_resolved_by_id, self.env.user)
        self.assertTrue(line.legacy_resolved_at)
        self.assertAlmostEqual(run.total_undiscounted, 125.0, places=2)

        excluded_run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': date(2026, 3, 31),
        })
        excluded = self.env['eh.fin.maturity.line'].create({
            'run_id': excluded_run.id,
            'band': 'lt_3m',
            'item_class': 'payable',
            'undiscounted_amount': 999.0,
        })
        self.env.cr.execute(
            "UPDATE eh_fin_maturity_line "
            "SET legacy_claimed_origin = 'extracted', "
            "legacy_claimed_item_class = item_class, "
            "origin = 'legacy_ambiguous', item_class = 'legacy_unknown' "
            "WHERE id = %s",
            (excluded.id,),
        )
        excluded.invalidate_recordset()
        excluded_run.invalidate_recordset()
        excluded_run._compute_legacy_ambiguous_count()
        excluded_run.action_exclude_legacy_lines()
        self.assertEqual(excluded.origin, 'legacy_excluded')
        self.assertEqual(excluded.item_class, 'legacy_unknown')
        self.assertEqual(excluded.legacy_claimed_item_class, 'payable')
        self.assertTrue(excluded.exists(), 'remediation must retain evidence')
        self.assertAlmostEqual(excluded_run.total_undiscounted, 0.0, places=2)
        excluded_run.action_populate()
        self.assertTrue(excluded.exists())

    def test_lease_schedule_extraction_buckets_exact(self):
        """A 24-month, 1,000/month lease bands its remaining contractual
        payments exactly (IFRS 16.58 -> IFRS 7.39).

        Zero-rate lease so every schedule payment is exactly 1,000 (no
        final-row interest true-up). The lease engine pays monthly in
        arrears on MONTH-END dates: commencement 2026-01-15 gives payments
        on 2026-02-28, 2026-03-31, ..., 2028-01-31 (24 rows).

        Bands relative to reporting date 2026-01-15 (contractual scheme):
          < 2026-04-15 (3m):  2026-02-28, 2026-03-31          =  2 x 1,000
          < 2027-01-15 (1y):  2026-04-30 .. 2026-12-31        =  9 x 1,000
          < 2031-01-15 (5y):  2027-01-31 .. 2028-01-31        = 13 x 1,000
        Total 24,000.
        """
        if 'eh.lease.contract' not in self.env:
            self.skipTest('eh_account_assets_pro is not installed')
        account_rou = self._ensure_account(
            self.env, '1520', 'ROU Asset', 'asset_fixed')
        account_rou_accum = self._ensure_account(
            self.env, '1530', 'ROU Accumulated Depreciation', 'asset_fixed')
        account_liability = self._ensure_account(
            self.env, '2200', 'Lease Liability', 'liability_current')
        account_interest = self._ensure_account(
            self.env, '5200', 'Interest Expense', 'expense')
        account_rou_dep = self._ensure_account(
            self.env, '5110', 'ROU Depreciation', 'expense_depreciation')
        lease = self.env['eh.lease.contract'].create({
            'name': '/',
            'reference': 'DISC-LSE-1',
            'lessor_id': self.partner_b.id,
            'commencement_date': '2026-01-15',
            'term_months': 24,
            'cadence': 'monthly',
            'payment_timing': 'arrears',
            'payment_amount': 1000.0,
            'incremental_borrowing_rate': 0.0,
            'rou_asset_account_id': account_rou.id,
            'lease_liability_account_id': account_liability.id,
            'interest_expense_account_id': account_interest.id,
            'rou_depreciation_account_id': account_rou_dep.id,
            'rou_accumulated_depreciation_account_id': account_rou_accum.id,
            'cash_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
        })
        lease.action_activate()
        self.assertEqual(lease.state, 'active')
        self.assertEqual(len(lease.schedule_line_ids), 24)

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': date(2026, 1, 15),
            'include_leases': True,
        })
        run.action_populate()
        lease_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'lease')
        bands = {l.band: l.undiscounted_amount for l in lease_rows}
        self.assertAlmostEqual(bands['lt_3m'], 2000.0, places=2,
                               msg='2 month-end payments before +3m')
        self.assertAlmostEqual(bands['3m_1y'], 9000.0, places=2,
                               msg='9 month-end payments before +1y')
        self.assertAlmostEqual(bands['1y_5y'], 13000.0, places=2,
                               msg='13 month-end payments from +1y to +2y')
        self.assertAlmostEqual(
            sum(lease_rows.mapped('undiscounted_amount')), 24000.0,
            places=2)
        # Idempotent repopulate: buckets unchanged, no duplicates.
        run.action_populate()
        lease_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'lease')
        self.assertEqual(len(lease_rows), 3)
        self.assertAlmostEqual(
            sum(lease_rows.mapped('undiscounted_amount')), 24000.0,
            places=2)

    def test_ended_lease_is_included_when_live_at_reporting_cutoff(self):
        """Current state cannot erase a lease that was live at the cutoff."""
        if 'eh.lease.contract' not in self.env:
            self.skipTest('eh_account_assets_pro is not installed')
        account_rou = self._ensure_account(
            self.env, '1521', 'Lifecycle ROU Asset', 'asset_fixed')
        account_rou_accum = self._ensure_account(
            self.env, '1531', 'Lifecycle ROU Accum', 'asset_fixed')
        account_liability = self._ensure_account(
            self.env, '2201', 'Lifecycle Lease Liability',
            'liability_current')
        account_interest = self._ensure_account(
            self.env, '5201', 'Lifecycle Interest', 'expense')
        account_rou_dep = self._ensure_account(
            self.env, '5111', 'Lifecycle ROU Depreciation',
            'expense_depreciation')
        lease = self.env['eh.lease.contract'].create({
            'name': '/',
            'reference': 'DISC-LSE-ENDED',
            'lessor_id': self.partner_b.id,
            'commencement_date': '2026-01-15',
            'term_months': 2,
            'cadence': 'monthly',
            'payment_timing': 'arrears',
            'payment_amount': 1000.0,
            'incremental_borrowing_rate': 0.0,
            'rou_asset_account_id': account_rou.id,
            'lease_liability_account_id': account_liability.id,
            'interest_expense_account_id': account_interest.id,
            'rou_depreciation_account_id': account_rou_dep.id,
            'rou_accumulated_depreciation_account_id': account_rou_accum.id,
            'cash_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
        })
        lease.action_activate()
        lease.schedule_line_ids.action_post()
        self.assertEqual(lease.state, 'ended')

        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': date(2026, 1, 15),
            'include_leases': True,
        })
        run.action_populate()
        lease_rows = run.line_ids.filtered(
            lambda line: line.item_class == 'lease'
        )
        self.assertAlmostEqual(
            sum(lease_rows.mapped('undiscounted_amount')), 2000.0, places=2,
        )
        # Idempotent even though the lease's current lifecycle is ended.
        run.action_populate()
        lease_rows = run.line_ids.filtered(
            lambda l: l.item_class == 'lease')
        self.assertEqual(len(lease_rows), 1)
        self.assertAlmostEqual(
            sum(lease_rows.mapped('undiscounted_amount')), 2000.0,
            places=2)

    def test_lease_extraction_without_module_raises(self):
        if 'eh.lease.schedule.line' in self.env:
            self.skipTest('eh_account_assets_pro IS installed here')
        today = fields.Date.context_today(self.env.user)
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'include_leases': True,
        })
        with self.assertRaises(UserError):
            run.action_populate()


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestSensitivity(EhGoldenTestCase):
    """IFRS 7.40: computed currency and interest-rate sensitivity."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_fx_shock_golden(self):
        """USD company, open EUR receivable of 10,000 at 0.9 EUR per USD,
        10% shock.

        Convention (module): the impact is the P&L effect of the foreign
        currency STRENGTHENING by the shock against the functional
        currency, applied to the net open monetary position. Derivation:
          exposure = 10,000 EUR / 0.9 = 11,111.11 USD
          impact   = 11,111.11 x 10% = +1,111.11 USD (a gain: the
          receivable is a net EUR asset, worth more when EUR strengthens;
          a 10% weakening loses the same amount).
        """
        today = fields.Date.context_today(self.env.user)
        eur = self.env.ref('base.EUR')
        self._set_rate(eur, today, 0.9)
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': today,
            'line_ids': [
                (0, 0, {
                    'name': 'FX-REC',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': eur.id,
                    'amount_currency': 10000.0,
                    'debit': 11111.11, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'FX-REV',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0, 'credit': 11111.11,
                }),
            ],
        })
        move.action_post()

        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today,
            'fx_shock_pct': 10.0,
        })
        sens.action_compute()
        fx_rows = sens.line_ids.filtered(lambda l: l.kind == 'fx')
        self.assertEqual(len(fx_rows), 1, 'one row per open currency')
        row = fx_rows
        self.assertEqual(row.shock_currency_id, eur)
        self.assertAlmostEqual(row.exposure_foreign, 10000.0, places=2)
        self.assertAlmostEqual(row.exposure, 11111.11, places=2)
        self.assertAlmostEqual(row.pnl_impact, 1111.11, places=2,
                               msg='gain when EUR strengthens 10%')
        self.assertAlmostEqual(row.oci_impact, 0.0, places=2)
        self.assertEqual(row.origin, 'computed')
        # Idempotent recompute.
        sens.action_compute()
        fx_rows = sens.line_ids.filtered(lambda l: l.kind == 'fx')
        self.assertEqual(len(fx_rows), 1)
        self.assertAlmostEqual(fx_rows.pnl_impact, 1111.11, places=2)

    def test_fx_aggregate_does_not_search_full_aml_recordset(self):
        today = fields.Date.context_today(self.env.user)
        eur = self.env.ref('base.EUR')
        self._set_rate(eur, today, 0.8)
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': today,
            'line_ids': [
                (0, 0, {
                    'name': 'FX bounded aggregate',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': eur.id,
                    'amount_currency': 800.0,
                    'debit': 1000.0,
                }),
                (0, 0, {
                    'name': 'FX bounded counterpart',
                    'account_id': self.account_revenue.id,
                    'credit': 1000.0,
                }),
            ],
        }).action_post()
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today,
        })
        MoveLine = self.env['account.move.line']
        with patch.object(
            type(MoveLine), 'search',
            side_effect=AssertionError(
                "FX aggregate materialised account.move.line"
            ),
        ):
            positions = sens._fx_open_positions()
        self.assertAlmostEqual(positions[eur], 800.0, places=2)

    def test_fx_other_monetary_accounts_are_explicitly_included(self):
        today = fields.Date.context_today(self.env.user)
        eur = self.env.ref('base.EUR')
        self._set_rate(eur, today, 0.8)
        borrowing = self._ensure_account(
            self.env, '2298', 'Foreign currency borrowing',
            'liability_current',
        )
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': today,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR borrowing',
                    'account_id': borrowing.id,
                    'currency_id': eur.id,
                    'amount_currency': -1000.0,
                    'credit': 1250.0,
                }),
                (0, 0, {
                    'name': 'Borrowing proceeds',
                    'account_id': self.account_cash.id,
                    'debit': 1250.0,
                }),
            ],
        }).action_post()
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today,
            'fx_monetary_account_ids': [(6, 0, borrowing.ids)],
        })
        sens.action_compute()
        row = sens.line_ids.filtered(
            lambda line: line.kind == 'fx'
            and line.shock_currency_id == eur
        )
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row.exposure_foreign, -1000.0, places=2)

    def test_fx_residual_is_reconstructed_at_reporting_cutoff(self):
        """Foreign residual uses only partials dated through the cutoff."""
        eur = self.env.ref('base.EUR')
        cutoff = date(2026, 3, 31)
        self._set_rate(eur, cutoff, 0.8)

        def _post_fx(day, amount_fc, company_amount, debit):
            ar_vals = {
                'name': 'FX cutoff leg',
                'account_id': self.account_receivable.id,
                'partner_id': self.partner_a.id,
                'currency_id': eur.id,
                'amount_currency': amount_fc if debit else -amount_fc,
                'debit': company_amount if debit else 0.0,
                'credit': 0.0 if debit else company_amount,
            }
            counterpart = {
                'name': 'FX cutoff counterpart',
                'account_id': (
                    self.account_revenue.id if debit else self.account_cash.id
                ),
                'debit': 0.0 if debit else company_amount,
                'credit': company_amount if debit else 0.0,
            }
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': day,
                'line_ids': [(0, 0, ar_vals), (0, 0, counterpart)],
            })
            move.action_post()
            return move.line_ids.filtered(
                lambda line: line.account_id == self.account_receivable
            )

        invoice_line = _post_fx(date(2026, 1, 15), 1000.0, 1250.0, True)
        first_payment = _post_fx(date(2026, 3, 1), 400.0, 500.0, False)
        later_payment = _post_fx(date(2026, 4, 30), 600.0, 750.0, False)
        (invoice_line | first_payment).reconcile()
        (invoice_line | later_payment).reconcile()
        self.assertTrue(invoice_line.reconciled)

        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': cutoff,
            'fx_shock_pct': 10.0,
        })
        sens.action_compute()
        row = sens.line_ids.filtered(
            lambda line: line.kind == 'fx'
            and line.shock_currency_id == eur
        )
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row.exposure_foreign, 600.0, places=2)
        self.assertAlmostEqual(row.exposure, 750.0, places=2)
        self.assertAlmostEqual(row.pnl_impact, 75.0, places=2)

    def test_ir_source_is_explicit_and_never_cross_counts_facility(self):
        """The same 50,000 facility on two registers contributes once."""
        today = fields.Date.context_today(self.env.user)
        self.env['eh.fin.risk'].create({
            'name': 'Floating facility', 'risk_category': 'market_interest',
            'carrying_amount': -50000.0, 'floating_rate': True,
            'reporting_date': today,
        })
        self.env['eh.fin.maturity.run'].create({
            'reporting_date': today,
            'instrument_ids': [(0, 0, {
                'name': 'Floating facility',
                'principal': 50000.0,
                'annual_rate': 5.0,
                'floating_rate': True,
                'maturity_date': today + relativedelta(years=2),
            })],
        })
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today,
            'ir_shock_bp': 100.0,
        })
        sens.action_compute()
        ir_rows = sens.line_ids.filtered(lambda l: l.kind == 'interest')
        self.assertEqual(sens.ir_source, 'risk_register')
        self.assertEqual(len(ir_rows), 1)
        self.assertAlmostEqual(ir_rows.exposure, -50000.0, places=2)
        self.assertAlmostEqual(ir_rows.pnl_impact, -500.0, places=2)
        self.assertAlmostEqual(sens.total_pnl_impact, -500.0, places=2)
        self.assertAlmostEqual(sens.total_oci_impact, 0.0, places=2)

        sens.ir_source = 'maturity_run'
        sens.action_compute()
        ir_rows = sens.line_ids.filtered(lambda l: l.kind == 'interest')
        self.assertEqual(len(ir_rows), 1)
        self.assertIn('borrowing', ir_rows.name.lower())
        self.assertAlmostEqual(ir_rows.exposure, -50000.0, places=2)
        self.assertAlmostEqual(ir_rows.pnl_impact, -500.0, places=2)
        self.assertAlmostEqual(sens.total_pnl_impact, -500.0, places=2)

    def test_ir_shock_fvoci_debt_cashflow_effect_routes_to_pnl(self):
        """Carrying x bp is an interest cash-flow effect, not FV movement."""
        if 'eh.fair.value.item' not in self.env:
            self.skipTest('eh_account_fair_value is not installed')
        Item = self.env['eh.fair.value.item']
        if 'floating_rate' not in Item._fields:
            self.skipTest('fair-value module carries no floating flag')
        today = fields.Date.context_today(self.env.user)
        item = Item.create({
            'name': '/',
            'nature': 'financial_asset',
            'instrument_type': 'debt',
            'business_model': 'hold_collect_sell',
            'sppi_fixed_dates': True,
            'sppi_interest_only': True,
            'sppi_no_leverage': True,
            'sppi_no_contingent_returns': True,
            'floating_rate': True,
            'measurement_date': today,
            'fair_value': 50000.0,
            'prior_carrying': 0.0,
            'balance_sheet_account_id': self.account_receivable.id,
            'oci_account_id': self._ensure_account(
                self.env, '3199', 'FVOCI sensitivity reserve', 'equity'
            ).id,
            'gain_loss_account_id': self._ensure_account(
                self.env, '7199', 'FV sensitivity gain', 'income_other'
            ).id,
            'journal_id': self.journal_misc.id,
        })
        self.assertEqual(item.ifrs9_classification, 'fvoci_debt',
                         'HTC&S + SPPI pass classifies as FVOCI-debt')
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today,
            'ir_shock_bp': 100.0,
            'ir_source': 'fair_value',
        })
        sens.action_compute()
        ir_rows = sens.line_ids.filtered(lambda l: l.kind == 'interest')
        self.assertEqual(len(ir_rows), 1)
        # 50,000 x 100bp = 500 annual interest cash flow, in P&L.
        self.assertAlmostEqual(ir_rows.oci_impact, 0.0, places=2)
        self.assertAlmostEqual(ir_rows.pnl_impact, 500.0, places=2)

    def test_ir_fair_value_rejects_draft_master_rewrite(self):
        if 'eh.fair.value.item' not in self.env:
            self.skipTest('eh_account_fair_value is not installed')
        Item = self.env['eh.fair.value.item']
        if 'floating_rate' not in Item._fields:
            self.skipTest('fair-value module carries no floating flag')
        cutoff = date(2026, 3, 31)
        item = Item.create({
            'name': '/',
            'nature': 'financial_asset',
            'instrument_type': 'debt',
            'business_model': 'hold_collect_sell',
            'sppi_fixed_dates': True,
            'sppi_interest_only': True,
            'sppi_no_leverage': True,
            'sppi_no_contingent_returns': True,
            'floating_rate': True,
            'measurement_date': cutoff,
            'fair_value': 50000.0,
            'prior_carrying': 0.0,
            'balance_sheet_account_id': self.account_receivable.id,
            'oci_account_id': self._ensure_account(
                self.env, '3198', 'FV rewrite reserve', 'equity'
            ).id,
            'gain_loss_account_id': self._ensure_account(
                self.env, '7198', 'FV rewrite gain', 'income_other'
            ).id,
            'journal_id': self.journal_misc.id,
        })
        item.action_remeasure()
        item.action_reset_to_draft()
        item.write({
            'measurement_date': date(2026, 6, 30),
            'fair_value': 999999.0,
        })
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': cutoff,
            'ir_shock_bp': 100.0,
            'ir_source': 'fair_value',
        })
        with self.assertRaises(UserError):
            sens.action_compute()

    def test_ir_fair_value_future_first_measurement_is_excluded(self):
        if 'eh.fair.value.item' not in self.env:
            self.skipTest('eh_account_fair_value is not installed')
        Item = self.env['eh.fair.value.item']
        if 'floating_rate' not in Item._fields:
            self.skipTest('fair-value module carries no floating flag')
        cutoff = date(2026, 3, 31)
        item = Item.create({
            'name': '/',
            'nature': 'financial_asset',
            'instrument_type': 'debt',
            'business_model': 'hold_collect_sell',
            'sppi_fixed_dates': True,
            'sppi_interest_only': True,
            'sppi_no_leverage': True,
            'sppi_no_contingent_returns': True,
            'floating_rate': True,
            'measurement_date': date(2026, 6, 30),
            'fair_value': 75000.0,
            'prior_carrying': 0.0,
            'balance_sheet_account_id': self.account_receivable.id,
            'oci_account_id': self._ensure_account(
                self.env, '3197', 'FV future reserve', 'equity'
            ).id,
            'gain_loss_account_id': self._ensure_account(
                self.env, '7197', 'FV future gain', 'income_other'
            ).id,
            'journal_id': self.journal_misc.id,
        })
        item.action_remeasure()
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': cutoff,
            'ir_shock_bp': 100.0,
            'ir_source': 'fair_value',
        })
        sens.action_compute()
        rows = sens.line_ids.filtered(
            lambda line: line.kind == 'interest'
            and item.name in line.name
        )
        self.assertFalse(rows)

    def test_ir_fair_value_uses_latest_posted_move_on_same_date(self):
        if 'eh.fair.value.item' not in self.env:
            self.skipTest('eh_account_fair_value is not installed')
        Item = self.env['eh.fair.value.item']
        if 'floating_rate' not in Item._fields:
            self.skipTest('fair-value module carries no floating flag')
        cutoff = date(2026, 3, 31)
        item = Item.create({
            'name': 'FV-LATEST-SAME-DATE',
            'nature': 'financial_asset',
            'floating_rate': True,
            'measurement_date': cutoff,
            'prior_carrying': 0.0,
            'fair_value': 50.0,
            'routing': 'pl',
            'balance_sheet_account_id': self.account_receivable.id,
            'gain_loss_account_id': self._ensure_account(
                self.env, '7196', 'FV latest P&L gain', 'income_other'
            ).id,
            'oci_account_id': self._ensure_account(
                self.env, '3196', 'FV latest OCI reserve', 'equity'
            ).id,
            'journal_id': self.journal_misc.id,
        })
        item.action_remeasure()
        item.action_reset_to_draft()
        # Posted history freezes instrument routing; only next-measurement
        # inputs may change after reset.
        item.write({'fair_value': 60.0})
        item.action_remeasure()

        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': cutoff,
            'ir_shock_bp': 100.0,
            'ir_source': 'fair_value',
        })
        sens.action_compute()
        row = sens.line_ids.filtered(
            lambda line: line.kind == 'interest'
            and item.name in line.name
        )
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row.exposure, 60.0, places=2)
        self.assertAlmostEqual(row.pnl_impact, 0.6, places=2)
        self.assertAlmostEqual(row.oci_impact, 0.0, places=2)

    def test_sensitivity_finalise_freezes(self):
        today = fields.Date.context_today(self.env.user)
        sens = self.env['eh.fin.sensitivity'].create({
            'reporting_date': today})
        sens.action_compute()
        sens.action_finalise()
        with self.assertRaises(UserError):
            sens.action_compute()
        with self.assertRaises(UserError):
            sens.fx_shock_pct = 5.0
        with self.assertRaises(UserError):
            sens.ir_source = 'maturity_run'
        with self.assertRaises(UserError):
            self.env['eh.fin.sensitivity.line'].create({
                'run_id': sens.id, 'kind': 'fx', 'name': 'Sneak'})
        sens.action_reopen()
        sens.action_compute()


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestMajorCustomers(EhGoldenTestCase):
    """IFRS 8.34: single external customers at or above 10% of revenue."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def _post_revenue(self, amount, partner=None, analytic=None, date=None):
        income_vals = {
            'name': 'MAJ-REV',
            'account_id': self.account_revenue.id,
            'debit': 0.0, 'credit': amount,
        }
        if partner is not None:
            income_vals['partner_id'] = partner.id
        if analytic is not None:
            income_vals['analytic_distribution'] = {str(analytic.id): 100.0}
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date or fields.Date.context_today(self.env.user),
            'line_ids': [
                (0, 0, {
                    'name': 'MAJ-REC',
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id if partner else False,
                    'debit': amount, 'credit': 0.0,
                }),
                (0, 0, income_vals),
            ],
        })
        move.action_post()
        return move

    def test_major_customer_12pct_row_8pct_none(self):
        """Ledger revenue 1,000 total: partner A 120 (12% -> major),
        partner B 80 (8% -> not disclosed), 800 partnerless. Partner A's
        revenue is analytic-tagged to the Retail segment, so the row
        attributes to Retail."""
        today = fields.Date.context_today(self.env.user)
        plan = self.env['account.analytic.plan'].create({
            'name': 'Major Customer Plan'})
        analytic = self.env['account.analytic.account'].create({
            'name': 'Retail Segment MC', 'plan_id': plan.id})
        self._post_revenue(120.0, partner=self.partner_a, analytic=analytic)
        self._post_revenue(80.0, partner=self.partner_b)
        self._post_revenue(800.0)

        report = self.env['eh.segment.report'].create({
            'period_end': today,
            'entity_revenue': 1000.0,
            'segment_ids': [
                (0, 0, {'name': 'Retail', 'revenue': 120.0,
                        'analytic_account_id': analytic.id}),
            ],
        })
        report.action_compute_major_customers()

        self.assertAlmostEqual(report.ledger_total_revenue, 1000.0,
                               places=2)
        self.assertEqual(report.major_customer_count, 1,
                         'only the 12% customer clears the 10% test')
        row = report.major_customer_line_ids
        self.assertEqual(row.partner_id, self.partner_a)
        self.assertAlmostEqual(row.revenue, 120.0, places=2)
        # 120 / 1,000 = 12%.
        self.assertAlmostEqual(row.revenue_pct, 12.0, places=4)
        self.assertEqual(row.segment_names, 'Retail',
                         'attribution through the analytic tag')
        self.assertNotIn(self.partner_b,
                         report.major_customer_line_ids.partner_id)
        # Idempotent recompute.
        report.action_compute_major_customers()
        self.assertEqual(report.major_customer_count, 1)

    def test_major_customer_exact_threshold_included(self):
        """IFRS 8.34 reads '10 per cent OR MORE': a customer at exactly
        10% (100 of 1,000) is disclosed."""
        today = fields.Date.context_today(self.env.user)
        self._post_revenue(100.0, partner=self.partner_a)
        self._post_revenue(900.0)
        report = self.env['eh.segment.report'].create({
            'period_end': today, 'entity_revenue': 1000.0,
            'segment_ids': [(0, 0, {'name': 'All', 'revenue': 1000.0})],
        })
        report.action_compute_major_customers()
        self.assertEqual(report.major_customer_count, 1)
        self.assertAlmostEqual(
            report.major_customer_line_ids.revenue_pct, 10.0, places=4)

    def test_major_customer_compute_blocked_when_finalised(self):
        today = fields.Date.context_today(self.env.user)
        report = self.env['eh.segment.report'].create({
            'period_end': today, 'entity_revenue': 100.0,
            'segment_ids': [(0, 0, {'name': 'A', 'revenue': 100.0})],
        })
        report.action_finalise()
        with self.assertRaises(UserError):
            report.action_compute_major_customers()


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestKmpCompensation(EhGoldenTestCase):
    """IAS 24.17: KMP compensation categories with the share-based figure
    prefilled from the IFRS 2 engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_kmp_prefill_from_sbp_period_charge(self):
        """One posted IFRS 2 period run: 300 options, grant-date FV 10.00,
        3-year cliff, 10% expected forfeiture, first anniversary run.
        Charge = 300 x 0.90 x 10.00 x 12/36 = 900.00 (the same derivation
        as the IFRS 2 golden suite). The prefill lands 900.00 on the
        share-based category; a manual short-term line of 100,000 makes
        the IAS 24.17 total 100,900.00."""
        if 'eh.sbp.period.run' not in self.env:
            self.skipTest('eh_account_share_based_payment is not installed')
        sbp_expense = self._ensure_account(
            self.env, '6150', 'Share-based Payment Expense', 'expense')
        sbp_reserve = self._ensure_account(
            self.env, '3150', 'SBP Equity Reserve', 'equity')
        sbp_liability = self._ensure_account(
            self.env, '2350', 'SBP Liability', 'liability_current')
        plan = self.env['eh.sbp.plan'].create({
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'vesting_months': 0,
            'expense_account_id': sbp_expense.id,
            'equity_account_id': sbp_reserve.id,
            'liability_account_id': sbp_liability.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
                'expected_forfeiture_pct': 10.0,
            })],
        })
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2027-01-01',
        })
        run.action_post()
        self.assertAlmostEqual(run.period_charge, 900.0, places=2,
                               msg='300 x 0.9 x 10 x 12/36')

        party = self.env['eh.related.party'].create({
            'name': 'Key Management', 'relationship': 'kmp',
            'is_kmp': True,
            'partner_id': self.partner_a.id,
            'reporting_date': date(2027, 6, 30),
            'compensation_date_from': date(2026, 7, 1),
        })
        party.action_prefill_share_based()
        sbp_lines = party.compensation_line_ids.filtered(
            lambda l: l.origin == 'sbp')
        self.assertEqual(len(sbp_lines), 1)
        self.assertEqual(sbp_lines.category, 'share_based')
        self.assertAlmostEqual(sbp_lines.amount, 900.0, places=2)

        self.env['eh.related.party.compensation'].create({
            'party_id': party.id, 'category': 'short_term',
            'amount': 100000.0,
        })
        # IAS 24.17 total = 100,000 short-term + 900 share-based.
        self.assertAlmostEqual(party.total_compensation, 100900.0, places=2)
        # Idempotent: prefill updates the engine line in place.
        party.action_prefill_share_based()
        sbp_lines = party.compensation_line_ids.filtered(
            lambda l: l.origin == 'sbp')
        self.assertEqual(len(sbp_lines), 1)
        self.assertAlmostEqual(sbp_lines.amount, 900.0, places=2)
        self.assertAlmostEqual(party.total_compensation, 100900.0, places=2)

    def test_kmp_sbp_partner_allocation_conserves_posted_charge(self):
        """Three equal grant contributions allocate 1.00 as .34/.33/.33.

        The cent remainder goes to the lowest (partner id, grant id), and the
        three separately-prefilled KMP registers sum exactly to the posted
        plan charge rather than each duplicating the entity-wide 1.00.
        """
        if 'eh.sbp.period.run' not in self.env:
            self.skipTest('eh_account_share_based_payment is not installed')
        sbp_expense = self._ensure_account(
            self.env, '6151', 'Allocated SBP Expense', 'expense')
        sbp_reserve = self._ensure_account(
            self.env, '3151', 'Allocated SBP Reserve', 'equity')
        partner_c = self.env['res.partner'].create({'name': 'KMP C'})
        partners = (self.partner_a | self.partner_b | partner_c).sorted('id')
        plan = self.env['eh.sbp.plan'].create({
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 0,
            'vesting_months': 1,
            'expense_account_id': sbp_expense.id,
            'equity_account_id': sbp_reserve.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [
                (0, 0, {
                    'partner_id': partner.id,
                    'instruments_granted': 1,
                    'grant_date_fair_value': 0.3333,
                })
                for partner in partners
            ],
        })
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2026-02-01',
        })
        run.action_post()
        self.assertAlmostEqual(run.period_charge, 1.0, places=2)

        parties = self.env['eh.related.party']
        for partner in partners:
            parties |= self.env['eh.related.party'].create({
                'name': 'KMP allocation %s' % partner.name,
                'relationship': 'kmp',
                'is_kmp': True,
                'partner_id': partner.id,
                'compensation_date_from': date(2026, 1, 1),
                'reporting_date': date(2026, 12, 31),
            })
        for party in parties:
            party.action_prefill_share_based()
        amounts = {
            party.partner_id.id: party.compensation_line_ids.filtered(
                lambda line: line.origin == 'sbp'
            ).amount
            for party in parties
        }
        self.assertAlmostEqual(sum(amounts.values()), 1.0, places=2)
        self.assertAlmostEqual(amounts[partners[0].id], 0.34, places=2)
        self.assertAlmostEqual(amounts[partners[1].id], 0.33, places=2)
        self.assertAlmostEqual(amounts[partners[2].id], 0.33, places=2)

    def test_kmp_sbp_non_kmp_grant_share_is_not_redistributed(self):
        """A non-KMP grantee's share stays unallocated and is flagged."""
        if 'eh.sbp.period.run' not in self.env:
            self.skipTest('eh_account_share_based_payment is not installed')
        sbp_expense = self._ensure_account(
            self.env, '6152', 'Partly Allocated SBP Expense', 'expense')
        sbp_reserve = self._ensure_account(
            self.env, '3152', 'Partly Allocated SBP Reserve', 'equity')
        plan = self.env['eh.sbp.plan'].create({
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 0,
            'vesting_months': 1,
            'expense_account_id': sbp_expense.id,
            'equity_account_id': sbp_reserve.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [
                (0, 0, {
                    'partner_id': self.partner_a.id,
                    'instruments_granted': 1,
                    'grant_date_fair_value': 0.5,
                }),
                (0, 0, {
                    'partner_id': self.partner_b.id,
                    'instruments_granted': 1,
                    'grant_date_fair_value': 0.5,
                }),
            ],
        })
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2026-02-01',
        })
        run.action_post()
        self.assertAlmostEqual(run.period_charge, 1.0, places=2)

        party = self.env['eh.related.party'].create({
            'name': 'KMP partial allocation',
            'relationship': 'kmp',
            'is_kmp': True,
            'partner_id': self.partner_a.id,
            'compensation_date_from': date(2026, 1, 1),
            'reporting_date': date(2026, 12, 31),
        })
        party.action_prefill_share_based()
        line = party.compensation_line_ids.filtered(
            lambda compensation: compensation.origin == 'sbp'
        )
        self.assertAlmostEqual(line.amount, 0.5, places=2)
        self.assertIn('Unallocated share', line.note)

    def test_kmp_sbp_fails_closed_after_grant_estimate_edit(self):
        """A posted plan has no historical grant-weight snapshot to rewrite."""
        if 'eh.sbp.period.run' not in self.env:
            self.skipTest('eh_account_share_based_payment is not installed')
        sbp_expense = self._ensure_account(
            self.env, '6153', 'Historical SBP Expense', 'expense')
        sbp_reserve = self._ensure_account(
            self.env, '3153', 'Historical SBP Reserve', 'equity')
        plan = self.env['eh.sbp.plan'].create({
            'name': '/',
            'settlement': 'equity',
            'condition_kind': 'service',
            'grant_date': '2026-01-01',
            'vesting_years': 0,
            'vesting_months': 1,
            'expense_account_id': sbp_expense.id,
            'equity_account_id': sbp_reserve.id,
            'settlement_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 10,
                'grant_date_fair_value': 1.0,
            })],
        })
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id,
            'period_end': '2026-02-01',
        })
        run.action_post()
        # This is a legitimate IFRS 2 true-up input for a later run, but it
        # means the old run can no longer be split by grantee from current
        # mutable master data.
        plan.grant_ids.write({'expected_forfeiture_pct': 10.0})
        # PostgreSQL's transaction timestamp is constant inside a SavepointCase;
        # model the later RPC transaction that would naturally advance the
        # audit timestamp in production.
        self.env.cr.execute(
            "UPDATE eh_sbp_grant "
            "SET write_date = %s::timestamp + interval '1 second' "
            "WHERE id = %s",
            (run.move_id.create_date, plan.grant_ids.id),
        )
        plan.grant_ids.invalidate_recordset(['write_date'])
        self.assertGreater(
            plan.grant_ids.write_date, run.move_id.create_date,
        )
        party = self.env['eh.related.party'].create({
            'name': 'KMP historical allocation',
            'relationship': 'kmp',
            'is_kmp': True,
            'partner_id': self.partner_a.id,
            'compensation_date_from': date(2026, 1, 1),
            'reporting_date': date(2026, 12, 31),
        })
        with self.assertRaises(UserError):
            party.action_prefill_share_based()
        self.assertFalse(party.compensation_line_ids.filtered(
            lambda compensation: compensation.origin == 'sbp'
        ))

    def test_compensation_lines_freeze_when_finalised(self):
        party = self.env['eh.related.party'].create({
            'name': 'Key Management', 'relationship': 'kmp',
            'compensation_line_ids': [(0, 0, {
                'category': 'short_term', 'amount': 50000.0})],
        })
        self.assertAlmostEqual(party.total_compensation, 50000.0, places=2)
        party.action_finalise()
        with self.assertRaises(UserError):
            party.compensation_line_ids[0].amount = 1.0
        with self.assertRaises(UserError):
            self.env['eh.related.party.compensation'].create({
                'party_id': party.id, 'category': 'termination',
                'amount': 9.0})
        if 'eh.sbp.period.run' in self.env:
            with self.assertRaises(UserError):
                party.action_prefill_share_based()
        party.action_reopen()
        party.compensation_line_ids[0].amount = 60000.0
        self.assertAlmostEqual(party.total_compensation, 60000.0, places=2)


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestEntityInterestRestrictions(EhGoldenTestCase):
    """IFRS 12.13/22 restrictions register and IFRS 12.7-9 judgements."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_restrictions_total_and_render_fields(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Ring-fenced Sub', 'interest_type': 'subsidiary',
            'ownership_pct': 80.0,
            'significant_judgements':
                'Control concluded despite 50% voting rights: board '
                'majority under the shareholder agreement.',
            'restriction_line_ids': [
                (0, 0, {'kind': 'dividend',
                        'description': 'Dividends require regulator '
                                       'approval',
                        'carrying_amount': 5000.0}),
                (0, 0, {'kind': 'regulatory',
                        'description': 'Statutory liquidity ring-fence',
                        'carrying_amount': 3000.0}),
            ],
        })
        # Total restricted = 5,000 + 3,000.
        self.assertAlmostEqual(interest.total_restricted, 8000.0, places=2)
        self.assertEqual(len(interest.restriction_line_ids), 2)
        self.assertTrue(interest.significant_judgements)

    def test_restrictions_freeze_when_finalised(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Sub Ltd', 'interest_type': 'subsidiary',
            'ownership_pct': 60.0,
            'restriction_line_ids': [
                (0, 0, {'kind': 'loan',
                        'description': 'Covenant blocks upstream loans',
                        'carrying_amount': 1000.0}),
            ],
        })
        interest.action_finalise()
        with self.assertRaises(UserError):
            interest.restriction_line_ids[0].carrying_amount = 2.0
        with self.assertRaises(UserError):
            self.env['eh.entity.interest.restriction'].create({
                'interest_id': interest.id, 'kind': 'other',
                'description': 'Sneak', 'carrying_amount': 9.0})
        with self.assertRaises(UserError):
            interest.significant_judgements = 'rewritten'
        interest.action_reopen()
        interest.restriction_line_ids[0].carrying_amount = 2000.0
        self.assertAlmostEqual(interest.total_restricted, 2000.0, places=2)


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestKmpLedgerPopulate(EhGoldenTestCase):
    """IAS 24.17: KMP compensation categories populated from posted move
    lines whose account carries a category tag. The prefill re-signs each
    line's ledger balance into its category and sums it, leaving manual and
    share-based lines untouched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        # Two category-tagged expense accounts plus one untagged expense
        # account. The tag NAME equals the IAS 24.17 category code, which is
        # the documented mapping convention.
        Tag = cls.env['account.account.tag']
        cls.tag_short = Tag.create(
            {'name': 'short_term', 'applicability': 'accounts'})
        cls.tag_post = Tag.create(
            {'name': 'post_employment', 'applicability': 'accounts'})
        cls.acc_short = cls._ensure_account(
            cls.env, '6110', 'KMP Salaries', 'expense')
        cls.acc_short.tag_ids = [(6, 0, cls.tag_short.ids)]
        cls.acc_post = cls._ensure_account(
            cls.env, '6120', 'KMP Pension', 'expense')
        cls.acc_post.tag_ids = [(6, 0, cls.tag_post.ids)]
        cls.acc_untagged = cls._ensure_account(
            cls.env, '6130', 'Office Rent', 'expense')

    def _post_kmp_expense(self, account, amount, partner, date=None):
        """Post a balanced expense entry: Dr `account` (partnered), Cr cash
        (partnerless, untagged), so only the tagged debit line is a KMP
        compensation figure for the party's contact."""
        self.post_balanced_move([
            {'account': account, 'debit': amount, 'partner': partner},
            {'account': self.account_cash, 'credit': amount},
        ], date=date or fields.Date.context_today(self.env.user))

    def test_kmp_ledger_populate_by_category(self):
        """Contact posts 100,000 to the short-term-tagged account, 12,000 to
        the post-employment-tagged account, and 5,000 to an untagged account.
        Populate routes the tagged balances to their categories (short-term
        100,000; post-employment 12,000) and skips the untagged 5,000, so the
        IAS 24.17 total is 112,000."""
        party = self.env['eh.related.party'].create({
            'name': 'CEO', 'relationship': 'kmp', 'is_kmp': True,
            'partner_id': self.partner_a.id,
        })
        self._post_kmp_expense(self.acc_short, 100000.0, self.partner_a)
        self._post_kmp_expense(self.acc_post, 12000.0, self.partner_a)
        self._post_kmp_expense(self.acc_untagged, 5000.0, self.partner_a)

        party.action_populate_kmp()
        by_cat = {l.category: l for l in party.compensation_line_ids}
        self.assertEqual(set(by_cat), {'short_term', 'post_employment'},
                         'only tagged accounts feed a category')
        self.assertEqual(by_cat['short_term'].origin, 'ledger')
        self.assertAlmostEqual(by_cat['short_term'].amount, 100000.0, places=2)
        self.assertAlmostEqual(
            by_cat['post_employment'].amount, 12000.0, places=2)
        # Total = 100,000 + 12,000 (untagged 5,000 excluded).
        self.assertAlmostEqual(party.total_compensation, 112000.0, places=2)

        # Idempotent: repopulate updates the ledger lines in place.
        party.action_populate_kmp()
        self.assertEqual(len(party.compensation_line_ids), 2)
        self.assertAlmostEqual(party.total_compensation, 112000.0, places=2)

    def test_kmp_ledger_populate_keeps_manual_and_sbp_lines(self):
        """A manually keyed termination line and (when the IFRS 2 module is
        installed) a share-based engine line both survive a ledger populate;
        the ledger prefill only ever touches origin='ledger' lines."""
        party = self.env['eh.related.party'].create({
            'name': 'CFO', 'relationship': 'kmp', 'is_kmp': True,
            'partner_id': self.partner_a.id,
        })
        self.env['eh.related.party.compensation'].create({
            'party_id': party.id, 'category': 'termination',
            'amount': 30000.0})
        self._post_kmp_expense(self.acc_short, 80000.0, self.partner_a)

        party.action_populate_kmp()
        origins = party.compensation_line_ids.mapped('origin')
        self.assertIn('manual', origins)
        self.assertIn('ledger', origins)
        ledger = party.compensation_line_ids.filtered(
            lambda l: l.origin == 'ledger')
        self.assertAlmostEqual(ledger.amount, 80000.0, places=2)
        # Total = manual termination 30,000 + ledger short-term 80,000.
        self.assertAlmostEqual(party.total_compensation, 110000.0, places=2)

    def test_kmp_populate_requires_flag_and_contact(self):
        """Populate refuses on a party that is not KMP or has no contact, and
        on a finalised register."""
        not_kmp = self.env['eh.related.party'].create({
            'name': 'Supplier', 'relationship': 'other',
            'partner_id': self.partner_a.id})
        with self.assertRaises(UserError):
            not_kmp.action_populate_kmp()
        no_contact = self.env['eh.related.party'].create({
            'name': 'Board', 'relationship': 'kmp', 'is_kmp': True})
        with self.assertRaises(UserError):
            no_contact.action_populate_kmp()
        finalised = self.env['eh.related.party'].create({
            'name': 'COO', 'relationship': 'kmp', 'is_kmp': True,
            'partner_id': self.partner_a.id})
        finalised.action_finalise()
        with self.assertRaises(UserError):
            finalised.action_populate_kmp()


@tagged('eh_golden', 'eh_account_disclosures', 'post_install', '-at_install')
class TestEntityInterestConsolidationPopulate(EhGoldenTestCase):
    """IFRS 12.12/B10-B12: the interest's NCI proportion and summarised
    financial information are pulled from the latest consolidation run's
    member and NCI lines, not keyed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def setUp(self):
        super().setUp()
        if 'eh.consol.run' not in self.env:
            self.skipTest('eh_account_consolidation is not installed')

    def _acc(self, company, code, name, account_type):
        Account = self.env['account.account'].with_company(company)
        multi = 'company_ids' in Account._fields
        field = 'company_ids' if multi else 'company_id'
        value = [(6, 0, company.ids)] if multi else company.id
        existing = Account.search(
            [('code', '=', code), (field, 'in', company.ids)], limit=1)
        if existing:
            return existing
        return Account.create({
            'code': code, 'name': name, 'account_type': account_type,
            field: value})

    def _seeded_run_80pct_sub(self):
        """Build and compute a flat-rate (1.0), 80%-owned full-method run.

        Subsidiary trial balance (self.company, presentation = functional so
        no CTA), Odoo signed amounts:
          cash          10,000 Dr  -> +10,000   (asset)
          payables       3,000 Cr  ->  -3,000   (liability)
          share capital  5,000 Cr  ->  -5,000   (equity)
          revenue        4,000 Cr  ->  -4,000   (income)
          expense        2,000 Dr  ->  +2,000   (expense)
        Books balance. Net assets = 10,000 - 3,000 = 7,000; equity 5,000 +
        result 2,000 = 7,000.

        Full method at 80% with no configured investment -> the plain NCI
        carve fires: nci_base = equity(-5,000) + P&L(-4,000+2,000=-2,000)
        = -7,000; NCI share 0.20; nci line amount = -7,000 x 0.20 = -1,400.
        """
        pres = self.company.currency_id
        parent = self.env['res.company'].create({
            'name': 'IFRS12 Parent', 'currency_id': pres.id})
        self.env.user.write({'company_ids': [(4, parent.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, parent.id]))
        cta = self._acc(parent, '3900', 'CTA Reserve', 'equity')
        nci = self._acc(parent, '3200', 'NCI', 'equity')
        re = self._acc(parent, '3100', 'Consolidated RE', 'equity_unaffected')
        entity = self.env['eh.consol.entity'].create({
            'name': 'IFRS12 Group', 'code': 'ifrs12_grp',
            'parent_company_id': parent.id,
            'presentation_currency_id': pres.id,
            'cta_account_id': cta.id, 'nci_account_id': nci.id})
        self.env['eh.consol.member'].create({
            'entity_id': entity.id, 'company_id': self.company.id,
            'ownership_pct': 80.0, 'method': 'full'})
        # Subsidiary books in self.company (the member).
        cap = self._ensure_account(
            self.env, '3050', 'Sub Share Capital', 'equity')
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 10000.0},
            {'account': self.account_payable, 'credit': 3000.0},
            {'account': cap, 'credit': 5000.0},
            {'account': self.account_revenue, 'credit': 4000.0},
            {'account': self.account_expense, 'debit': 2000.0},
        ], date=date(2026, 1, 5))
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': date(2026, 1, 1),
            'period_to': date(2026, 1, 31)})
        run.action_compute()
        return run

    def test_interest_populated_from_consolidation_run(self):
        run = self._seeded_run_80pct_sub()
        interest = self.env['eh.entity.interest'].create({
            'name': self.company.display_name,
            'interest_type': 'subsidiary',
            'company_id': run.entity_id.parent_company_id.id})
        interest.action_populate_from_consolidation()

        self.assertEqual(interest.consol_run_res_id, run.id)
        self.assertTrue(interest.consol_run_name)
        # ownership 80 -> NCI proportion 20.
        self.assertAlmostEqual(interest.ownership_pct, 80.0, places=4)
        self.assertAlmostEqual(interest.nci_pct, 20.0, places=4)
        # Summarised figures from the subsidiary_balance lines.
        self.assertAlmostEqual(interest.summarised_assets, 10000.0, places=2)
        self.assertAlmostEqual(
            interest.summarised_liabilities, 3000.0, places=2)
        self.assertAlmostEqual(interest.summarised_revenue, 4000.0, places=2)
        # Profit = revenue 4,000 - expense 2,000 = 2,000.
        self.assertAlmostEqual(interest.summarised_profit, 2000.0, places=2)
        # NCI carrying = 20% of net assets 7,000 = 1,400.
        self.assertAlmostEqual(
            interest.nci_carrying_amount, 1400.0, places=2)

        # Idempotent: a second populate overwrites the same figures.
        interest.action_populate_from_consolidation()
        self.assertAlmostEqual(interest.summarised_assets, 10000.0, places=2)
        self.assertAlmostEqual(
            interest.nci_carrying_amount, 1400.0, places=2)

    def test_populate_no_matching_member_raises(self):
        run = self._seeded_run_80pct_sub()
        interest = self.env['eh.entity.interest'].create({
            'name': 'Nonexistent Entity Ltd',
            'interest_type': 'subsidiary',
            'company_id': run.entity_id.parent_company_id.id})
        with self.assertRaises(UserError):
            interest.action_populate_from_consolidation()

    def test_populate_blocked_when_finalised(self):
        run = self._seeded_run_80pct_sub()
        interest = self.env['eh.entity.interest'].create({
            'name': self.company.display_name,
            'interest_type': 'subsidiary',
            'company_id': run.entity_id.parent_company_id.id})
        interest.action_populate_from_consolidation()
        interest.action_finalise()
        with self.assertRaises(UserError):
            interest.action_populate_from_consolidation()
