# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 9 impairment worked examples for eh_account_ecl.

Each test is a hand-computed worked example in the shape of the IFRS 9
staging and measurement guidance (numbers only, recomputed by hand from the
inputs stated in the test). The exact journal entries and reconciliation
lines the engine produces are asserted against literal amounts; nothing is
read back from the engine under test to build an expected value.

Standard portfolio used by the staging cases (stated once, reused):

* EAD 1,000.00, LGD 60%, 12-month PD 2%, lifetime PD 8%, no discounting.
* Stage 1 ECL = 1,000 x 60% x 2%  = 12.00 (12-month PD).
* Stage 2/3 ECL = 1,000 x 60% x 8% = 48.00 (lifetime PD).

Engine conventions exercised here (read from models/ecl_run.py):

* The stage engine runs on every compute of a general-approach run. The
  bucket's days_from is its days-past-due signal; > 30 DPD presumes SICR
  (Stage 2, IFRS 9.5.5.11), > 90 DPD presumes default (Stage 3,
  IFRS 9.B5.5.37). Qualitative sicr / credit_impaired flags override
  upward; the low-credit-risk exemption (IFRS 9.5.5.10) holds Stage 1
  against the 30-DPD backstop only. POCI never re-stages and always
  measures lifetime ECL (IFRS 9.5.5.13).
* A transfer log entry carries the allowance the exposure held in the
  prior posted run; it feeds the IFRS 7.35H reconciliation, whose stage
  identity is closing = opening + in - out + remeasurement - write-offs.
* Write-offs consume the recognised allowance (Dr allowance /
  Cr receivable, IFRS 9.5.4.4) and can never exceed it.
"""

from datetime import date

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_ecl', 'post_install', '-at_install')
class TestGoldenIfrs9(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.impairment = cls._ensure_account(
            cls.env, '5290', 'Impairment Loss', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1290', 'Loss Allowance', 'asset_current')

    _date_seq = 0

    @classmethod
    def _next_date(cls):
        # One ECL run is allowed per company and reporting date (unique
        # constraint), so every run gets its own date, strictly ascending
        # in creation order so roll-forward chains stay deterministic.
        cls._date_seq += 1
        return '2026-%02d-%02d' % (
            1 + (cls._date_seq - 1) // 28, 1 + (cls._date_seq - 1) % 28)

    def _run(self, approach='simplified', opening=None, buckets=None,
             scenarios=None, reporting_date=None, **extra):
        run_date = reporting_date or self._next_date()
        vals = {
            'reporting_date': run_date,
            'reversal_date': run_date,
            'measurement_approach': approach,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in (buckets or [])],
        }
        if opening is not None:
            vals['opening_allowance'] = opening
        if scenarios:
            vals['scenario_ids'] = [(0, 0, s) for s in scenarios]
        vals.update(extra)
        return self.env['eh.ecl.run'].create(vals)

    def _portfolio(self, name, days_from, days_to, **extra):
        """Standard staging portfolio bucket (see module docstring)."""
        vals = {
            'name': name, 'days_from': days_from, 'days_to': days_to,
            'loss_rate': 0.0, 'gross_carrying': 1000.0,
            'exposure_at_default': 1000.0,
            'pd_12m': 2.0, 'pd_lifetime': 8.0, 'lgd': 60.0,
        }
        vals.update(extra)
        return vals

    def _invoice(self, partner, amount, due, invoice_date='2026-01-01'):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': invoice_date,
            'invoice_date_due': due,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale', 'quantity': 1, 'price_unit': amount,
                'account_id': self.account_revenue.id})],
        })
        inv.action_post()
        return inv

    def _receivable_line(self, invoice):
        return invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')

    # ------------------------------------------------------------------
    # staging engine
    # ------------------------------------------------------------------

    def test_golden_backstop30_transfer_and_movement(self):
        """Two-period staging case (IFRS 9.5.5.11 backstop).

        Period 1: exposure current (0 DPD) -> Stage 1.
            ECL = 1,000 x 60% x 2% (12m PD) = 12.00.
            Opening 0 -> movement 12.00: Dr impairment 12 / Cr allowance 12.
        Period 2: same exposure now 45 DPD -> 30-DPD backstop -> Stage 2,
        with a 1 -> 2 transfer logged at the 12.00 allowance it carried.
            ECL = 1,000 x 60% x 8% (lifetime PD) = 48.00.
            Opening rolls 12.00 -> movement 36.00: Dr 36 / Cr 36.
        Reconciliation of period 2 (IFRS 7.35H):
            Stage 1: opening 12, out 12, remeasure 0 -> closing 0.
            Stage 2: opening 0, in 12, remeasure 36 -> closing 48.
        """
        run1 = self._run(approach='general', opening=0.0,
                         buckets=[self._portfolio('PORT', 0, 30)])
        run1.action_compute()
        bucket1 = run1.bucket_ids
        self.assertEqual(bucket1.stage, '1')
        self.assertFalse(run1.transfer_ids)
        self.assertAlmostEqual(bucket1.ecl_effective, 12.0, places=2)
        self.assertAlmostEqual(run1.closing_allowance, 12.0, places=2)
        run1.action_post()
        self.assertMoveLines(run1.move_id, [
            (self.impairment, 12.0, 0.0),
            (self.allowance, 0.0, 12.0),
        ])

        # Same exposure, one period later, now 45 days past due. No
        # opening keyed: it must roll forward from run1's closing (12.00).
        run2 = self._run(approach='general',
                         buckets=[self._portfolio('PORT', 45, 60)])
        self.assertAlmostEqual(run2.opening_allowance, 12.0, places=2)
        run2.action_compute()
        bucket2 = run2.bucket_ids
        self.assertEqual(bucket2.stage, '2')
        self.assertAlmostEqual(bucket2.ecl_effective, 48.0, places=2)
        transfer = run2.transfer_ids
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer.from_stage, '1')
        self.assertEqual(transfer.to_stage, '2')
        self.assertEqual(transfer.reason, 'backstop_30')
        self.assertAlmostEqual(transfer.amount, 12.0, places=2)
        # Movement = closing 48.00 - opening 12.00 = 36.00.
        self.assertAlmostEqual(run2.movement, 36.0, places=2)
        run2.action_post()
        self.assertMoveLines(run2.move_id, [
            (self.impairment, 36.0, 0.0),
            (self.allowance, 0.0, 36.0),
        ])
        recon = {l.stage: l for l in run2.recon_ids}
        s1, s2 = recon['1'], recon['2']
        self.assertAlmostEqual(s1.opening, 12.0, places=2)
        self.assertAlmostEqual(s1.transfers_out, 12.0, places=2)
        self.assertAlmostEqual(s1.transfers_in, 0.0, places=2)
        self.assertAlmostEqual(s1.remeasurement, 0.0, places=2)
        self.assertAlmostEqual(s1.closing, 0.0, places=2)
        self.assertAlmostEqual(s2.opening, 0.0, places=2)
        self.assertAlmostEqual(s2.transfers_in, 12.0, places=2)
        self.assertAlmostEqual(s2.remeasurement, 36.0, places=2)
        self.assertAlmostEqual(s2.closing, 48.0, places=2)

    def test_golden_backstop90_default_presumption(self):
        """100 DPD breaches the 90-DPD backstop (IFRS 9.B5.5.37): Stage 3,
        lifetime ECL = 1,000 x 60% x 8% = 48.00, reason backstop_90."""
        run = self._run(approach='general', opening=0.0,
                        buckets=[self._portfolio('OLD', 100, 0)])
        run.action_compute()
        self.assertEqual(run.bucket_ids.stage, '3')
        self.assertAlmostEqual(run.closing_allowance, 48.0, places=2)
        transfer = run.transfer_ids
        self.assertEqual(transfer.from_stage, '1')
        self.assertEqual(transfer.to_stage, '3')
        self.assertEqual(transfer.reason, 'backstop_90')

    def test_golden_low_credit_risk_exemption(self):
        """45 DPD would trip the 30-DPD backstop, but the low-credit-risk
        exemption (IFRS 9.5.5.10) holds Stage 1: ECL stays at the 12-month
        figure 1,000 x 60% x 2% = 12.00 and no transfer is logged."""
        run = self._run(approach='general', opening=0.0, buckets=[
            self._portfolio('LCR', 45, 60, low_credit_risk=True)])
        run.action_compute()
        self.assertEqual(run.bucket_ids.stage, '1')
        self.assertAlmostEqual(run.closing_allowance, 12.0, places=2)
        self.assertFalse(run.transfer_ids)

    def test_golden_sicr_qualitative_flag(self):
        """Only 10 DPD, but the manual SICR flag moves the exposure to
        Stage 2 (IFRS 9.5.5.9): lifetime ECL 48.00, reason sicr_flag."""
        run = self._run(approach='general', opening=0.0, buckets=[
            self._portfolio('SICR', 10, 20, sicr=True)])
        run.action_compute()
        self.assertEqual(run.bucket_ids.stage, '2')
        self.assertAlmostEqual(run.closing_allowance, 48.0, places=2)
        transfer = run.transfer_ids
        self.assertEqual((transfer.from_stage, transfer.to_stage), ('1', '2'))
        self.assertEqual(transfer.reason, 'sicr_flag')

    def test_golden_credit_impaired_flag(self):
        """Only 10 DPD, but the credit-impaired flag forces Stage 3:
        lifetime ECL 48.00; the qualitative override wins over ageing.

        The impaired move is a Stage-3 transfer driven by qualitative
        impairment, not by the SICR flag (which is a Stage-2 driver), so the
        transfer trail records reason 'credit_impaired', distinct from the
        'sicr_flag' reason a genuine SICR move carries (IFRS 9.B5.5.37).
        """
        run = self._run(approach='general', opening=0.0, buckets=[
            self._portfolio('IMP', 10, 20, credit_impaired=True)])
        run.action_compute()
        self.assertEqual(run.bucket_ids.stage, '3')
        self.assertAlmostEqual(run.closing_allowance, 48.0, places=2)
        transfer = run.transfer_ids
        self.assertEqual((transfer.from_stage, transfer.to_stage), ('1', '3'))
        self.assertEqual(transfer.reason, 'credit_impaired')

    def test_golden_poci_pinned_to_lifetime(self):
        """POCI (IFRS 9.5.5.13): lifetime PD even while labelled Stage 1,
        excluded from re-staging, own reconciliation line.

        ECL = 1,000 x 60% x 8% (lifetime, never the 2% 12-month PD)
            = 48.00.
        """
        run = self._run(approach='general', opening=0.0, buckets=[
            self._portfolio('POCI', 0, 30, poci=True)])
        # Measured lifetime before any staging pass.
        self.assertAlmostEqual(run.bucket_ids.ecl_general, 48.0, places=2)
        run.action_compute()
        # The engine leaves the stage untouched and logs the pinning.
        self.assertEqual(run.bucket_ids.stage, '1')
        transfer = run.transfer_ids
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer.reason, 'poci')
        self.assertEqual(transfer.from_stage, transfer.to_stage)
        run.action_post()
        recon = {l.stage: l for l in run.recon_ids}
        self.assertAlmostEqual(recon['poci'].closing, 48.0, places=2)
        self.assertAlmostEqual(recon['1'].closing, 0.0, places=2)

    # ------------------------------------------------------------------
    # approach exclusivity
    # ------------------------------------------------------------------

    def test_general_ignores_matrix_loss_rate(self):
        """A general run measures from EAD/LGD/PD only: with a 25% matrix
        rate also keyed, the closing is 48.00 (lifetime), not 250.00."""
        run = self._run(approach='general', opening=0.0, buckets=[
            self._portfolio('X', 45, 60, loss_rate=25.0)])
        run.action_compute()
        bucket = run.bucket_ids
        self.assertAlmostEqual(bucket.ecl_effective, bucket.ecl_general,
                               places=2)
        self.assertAlmostEqual(run.closing_allowance, 48.0, places=2)

    def test_stage_engine_refused_on_simplified(self):
        run = self._run(buckets=[{
            'name': 'S', 'days_from': 0, 'days_to': 0, 'loss_rate': 5.0}])
        with self.assertRaises(UserError):
            run.action_apply_stage_engine()

    # ------------------------------------------------------------------
    # forward-looking scenarios
    # ------------------------------------------------------------------

    def test_golden_scenario_weighting(self):
        """Probability-weighted ECL over three scenarios (IFRS 9.5.5.17a).

        Stage 2 exposure: EAD 10,000, LGD 40%, lifetime PD 20%.
        Scenarios: Down 0.3 x PD factor 0.8, Base 0.4 x 1.0, Up 0.3 x 1.3.
            weighted PD = 0.3 x 16% + 0.4 x 20% + 0.3 x 26%
                        = 4.8% + 8.0% + 7.8% = 20.6%
            ECL = 10,000 x 40% x 20.6% = 824.00
        (single-scenario base would be 10,000 x 40% x 20% = 800.00).
        """
        run = self._run(
            approach='general', opening=0.0,
            buckets=[{
                'name': 'S2', 'days_from': 45, 'days_to': 60, 'stage': '2',
                'loss_rate': 0.0, 'exposure_at_default': 10000.0,
                'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0}],
            scenarios=[
                {'name': 'Down', 'weight': 0.3, 'pd_factor': 0.8},
                {'name': 'Base', 'weight': 0.4, 'pd_factor': 1.0},
                {'name': 'Up', 'weight': 0.3, 'pd_factor': 1.3},
            ])
        run.action_compute()
        self.assertAlmostEqual(run.closing_allowance, 824.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.impairment, 824.0, 0.0),
            (self.allowance, 0.0, 824.0),
        ])

    def test_golden_scenario_pd_cap(self):
        """A macro factor cannot push PD past certainty: lifetime PD 90%
        x factor 1.3 caps at 100%, so ECL = 1,000 x 100% x 100% =
        1,000.00, never more than the exposure."""
        run = self._run(
            approach='general', opening=0.0,
            buckets=[{
                'name': 'CAP', 'days_from': 100, 'days_to': 0, 'stage': '3',
                'loss_rate': 0.0, 'exposure_at_default': 1000.0,
                'lgd': 100.0, 'pd_12m': 5.0, 'pd_lifetime': 90.0}],
            scenarios=[{'name': 'Severe', 'weight': 1.0, 'pd_factor': 1.3}])
        run.action_compute()
        self.assertAlmostEqual(run.closing_allowance, 1000.0, places=2)

    def test_scenario_weights_rebalance_then_validate_as_one_set(self):
        run = self._run(
            approach='general', opening=0.0,
            buckets=[self._portfolio('W', 0, 30)],
            scenarios=[
                {'name': 'A', 'weight': 0.5},
                {'name': 'B', 'weight': 0.4},
            ])
        with self.assertRaisesRegex(UserError, 'sum to 0.9'):
            run.action_compute()
        # A set-level compute check permits the ordinary sequential edit that
        # rebalances existing rows; no per-row constraint traps the midpoint.
        run.scenario_ids.filtered(lambda scenario: scenario.name == 'A').write({
            'weight': 0.6,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')

    def test_scenarios_frozen_after_post(self):
        run = self._run(
            approach='general', opening=0.0,
            buckets=[self._portfolio('FZ', 45, 60)],
            scenarios=[{'name': 'Base', 'weight': 1.0, 'pd_factor': 1.0}])
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.scenario_ids.write({'pd_factor': 2.0})
        with self.assertRaises(UserError):
            run.scenario_ids.unlink()

    # ------------------------------------------------------------------
    # EIR discounting from maturity
    # ------------------------------------------------------------------

    def test_golden_eir_maturity_discounting(self):
        """IFRS 9.5.5.17(b): discount at the EIR over the actual term.

        Reporting 2026-06-30, maturity 2028-06-29 -> 730 days / 365
        = 2.0 years at EIR 10%:
            PV factor = 1 / 1.1^2 = 1 / 1.21
            ECL = (1,000 x 25%) / 1.21 = 250 / 1.21 = 206.61
        A second band with no maturity date falls back to the manual
        2 periods and lands on the same 206.61; run closing = 413.22.
        """
        run = self._run(reporting_date='2026-06-30', opening=0.0, buckets=[
            {'name': 'MAT', 'days_from': 91, 'days_to': 120,
             'loss_rate': 25.0, 'gross_carrying': 1000.0,
             'discount_rate': 10.0, 'maturity_date': date(2028, 6, 29)},
            {'name': 'MAN', 'days_from': 121, 'days_to': 0,
             'loss_rate': 25.0, 'gross_carrying': 1000.0,
             'discount_rate': 10.0, 'periods_to_recovery': 2},
        ])
        by_name = {b.name: b for b in run.bucket_ids}
        self.assertAlmostEqual(
            by_name['MAT'].periods_effective, 2.0, places=4)
        self.assertAlmostEqual(by_name['MAT'].ecl, 206.61, places=2)
        self.assertAlmostEqual(
            by_name['MAN'].periods_effective, 2.0, places=4)
        self.assertAlmostEqual(by_name['MAN'].ecl, 206.61, places=2)
        self.assertAlmostEqual(run.closing_allowance, 413.22, places=2)

    # ------------------------------------------------------------------
    # EAD population from the ledger
    # ------------------------------------------------------------------

    def test_golden_populate_aging(self):
        """Two open invoices land in the right ageing bands.

        Reporting 2026-06-30. Invoice 500 due 2026-05-21 is 40 days past
        due -> 31-90 band; invoice 800 due 2026-03-22 is 100 days past
        due -> 91+ band. Closing = 500 x 5% + 800 x 25% = 25 + 200
        = 225.00.
        """
        self._invoice(self.partner_a, 500.0, '2026-05-21')
        self._invoice(self.partner_b, 800.0, '2026-03-22')
        run = self._run(reporting_date='2026-06-30', opening=0.0, buckets=[
            {'name': 'Current', 'days_from': 0, 'days_to': 30,
             'loss_rate': 1.0},
            {'name': '31-90', 'days_from': 31, 'days_to': 90,
             'loss_rate': 5.0},
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0},
        ])
        run.action_populate_from_receivables()
        by_name = {b.name: b for b in run.bucket_ids}
        self.assertAlmostEqual(by_name['Current'].gross_carrying, 0.0,
                               places=2)
        self.assertAlmostEqual(by_name['31-90'].gross_carrying, 500.0,
                               places=2)
        self.assertAlmostEqual(by_name['91+'].gross_carrying, 800.0,
                               places=2)
        self.assertAlmostEqual(run.closing_allowance, 225.0, places=2)
        # No segments configured: nothing auto-created.
        self.assertEqual(set(run.bucket_ids.mapped('origin')), {'manual'})

    def test_golden_populate_segments_and_ead(self):
        """Segmented population under the general approach.

        Segment 'AU customers' captures partner_a (country AU); partner_b
        (country NZ) matches no segment and stays in the unsegmented
        template band. Both invoices are 40 days past due at 2026-06-30:
        500 (partner_a) -> '31-90 / AU customers', 800 (partner_b) ->
        '31-90'. Under the general approach the open residual is both the
        gross carrying amount and the EAD, so with LGD 60% and lifetime
        PD 8% on the Stage 2 band:
            AU band ECL   = 500 x 60% x 8% = 24.00
            rest band ECL = 800 x 60% x 8% = 38.40
            closing       = 62.40
        Repopulating must replace the auto buckets, not duplicate them.
        """
        self.partner_a.country_id = self.env.ref('base.au')
        self.partner_b.country_id = self.env.ref('base.nz')
        segment = self.env['eh.ecl.segment'].create({
            'name': 'AU customers',
            'country_ids': [(6, 0, [self.env.ref('base.au').id])],
        })
        self._invoice(self.partner_a, 500.0, '2026-05-21')
        self._invoice(self.partner_b, 800.0, '2026-05-21')
        template = {
            'loss_rate': 0.0, 'pd_12m': 2.0, 'pd_lifetime': 8.0,
            'lgd': 60.0}
        run = self._run(
            approach='general', reporting_date='2026-06-30', opening=0.0,
            buckets=[
                dict(template, name='Current', days_from=0, days_to=30,
                     stage='1'),
                dict(template, name='31-90', days_from=31, days_to=90,
                     stage='2'),
                dict(template, name='91+', days_from=91, days_to=0,
                     stage='3'),
            ])
        run.action_populate_from_receivables()
        # 3 manual templates + 3 auto segment bands.
        self.assertEqual(len(run.bucket_ids), 6)
        auto = run.bucket_ids.filtered(lambda b: b.origin == 'auto')
        self.assertEqual(len(auto), 3)
        self.assertEqual(auto.mapped('segment_id'), segment)
        au_band = auto.filtered(lambda b: b.days_from == 31)
        self.assertEqual(au_band.name, '31-90 / AU customers')
        # Template band parameters cloned onto the segment band.
        self.assertAlmostEqual(au_band.pd_lifetime, 8.0, places=3)
        self.assertEqual(au_band.stage, '2')
        self.assertAlmostEqual(au_band.gross_carrying, 500.0, places=2)
        self.assertAlmostEqual(au_band.exposure_at_default, 500.0, places=2)
        rest_band = run.bucket_ids.filtered(
            lambda b: not b.segment_id and b.days_from == 31)
        self.assertAlmostEqual(rest_band.gross_carrying, 800.0, places=2)
        self.assertAlmostEqual(rest_band.exposure_at_default, 800.0,
                               places=2)
        self.assertAlmostEqual(run.closing_allowance, 62.40, places=2)
        # Idempotent repopulation: same shape, same amounts.
        run.action_populate_from_receivables()
        self.assertEqual(len(run.bucket_ids), 6)
        self.assertAlmostEqual(run.closing_allowance, 62.40, places=2)
        self.assertAlmostEqual(
            sum(run.bucket_ids.mapped('gross_carrying')), 1300.0, places=2)

    # ------------------------------------------------------------------
    # write-off integration
    # ------------------------------------------------------------------

    def test_golden_writeoff_flow(self):
        """Write-off consumes the allowance and feeds the reconciliation.

        Run at 2026-06-30: 91+ band 1,000 x 25% -> closing allowance
        250.00, posted (Dr impairment 250 / Cr allowance 250).
        Write-off of a 100.00 receivable (full) then 60.00 of a second
        100.00 receivable (partial):
            each posts Dr allowance / Cr receivable;
            reconciliation Stage 3: opening 0, remeasure 250,
            write-offs 160 -> closing 90.00;
            allowance ledger = 250 credit - 160 debit = 90.00;
            a later run rolls its opening at 90.00 and ties to ledger.
        Guards: a 200.00 write-off is refused once only 150.00 of
        allowance remains; a 50.00 write-off is refused against a line
        whose residual is only 40.00.
        """
        inv1 = self._invoice(self.partner_a, 100.0, '2026-04-01')
        inv2 = self._invoice(self.partner_b, 100.0, '2026-04-01')
        inv3 = self._invoice(self.partner_a, 300.0, '2026-04-01')
        run = self._run(reporting_date='2026-06-30', opening=0.0, buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0}])
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.impairment, 250.0, 0.0),
            (self.allowance, 0.0, 250.0),
        ])

        line1 = self._receivable_line(inv1)
        wo1 = self.env['eh.ecl.writeoff'].create({
            'run_id': run.id, 'move_line_id': line1.id,
            'amount': 100.0, 'stage': '3', 'date': '2026-07-01'})
        wo1.action_post_writeoff()
        # Dr allowance 100 / Cr receivable 100 (IFRS 9.5.4.4). The credit
        # hits the same receivable account the invoice line sits on.
        self.assertMoveLines(wo1.move_id, [
            (self.allowance, 100.0, 0.0),
            (line1.account_id, 0.0, 100.0),
        ])
        # Full write-off reconciles the receivable line.
        self.assertAlmostEqual(line1.amount_residual, 0.0, places=2)
        self.assertTrue(line1.reconciled)

        # Guard: remaining allowance is 250 - 100 = 150; 200 must refuse.
        line3 = self._receivable_line(inv3)
        blocked = self.env['eh.ecl.writeoff'].create({
            'run_id': run.id, 'move_line_id': line3.id,
            'amount': 200.0, 'stage': '3', 'date': '2026-07-01'})
        with self.assertRaises(UserError):
            blocked.action_post_writeoff()

        # Partial write-off: 60 of the 100 residual.
        line2 = self._receivable_line(inv2)
        wo2 = self.env['eh.ecl.writeoff'].create({
            'run_id': run.id, 'move_line_id': line2.id,
            'amount': 60.0, 'stage': '3', 'date': '2026-07-01'})
        wo2.action_post_writeoff()
        self.assertAlmostEqual(line2.amount_residual, 40.0, places=2)
        self.assertFalse(line2.reconciled)

        # Guard: 50 exceeds the 40 residual left on the line.
        over = self.env['eh.ecl.writeoff'].create({
            'run_id': run.id, 'move_line_id': line2.id,
            'amount': 50.0, 'stage': '3', 'date': '2026-07-01'})
        with self.assertRaises(UserError):
            over.action_post_writeoff()

        # Reconciliation: closing 250 measured - 160 written off = 90.
        recon = {l.stage: l for l in run.recon_ids}
        s3 = recon['3']
        self.assertAlmostEqual(s3.opening, 0.0, places=2)
        self.assertAlmostEqual(s3.remeasurement, 250.0, places=2)
        self.assertAlmostEqual(s3.writeoffs, 160.0, places=2)
        self.assertAlmostEqual(s3.closing, 90.0, places=2)

        # Ledger: allowance carries 250 credit - 160 debit = 90 credit.
        self.assertAlmostEqual(
            self.posted_balance(self.allowance), -90.0, places=2)

        # A posted write-off is part of the trail: frozen and undeletable.
        with self.assertRaises(UserError):
            wo1.unlink()
        with self.assertRaises(UserError):
            wo1.write({'amount': 10.0})

        # Reversing the source allowance while write-offs remain posted would
        # debit the allowance back to nil after those write-offs already
        # debited it, driving the ledger below zero. Dependency is fail-closed.
        with self.assertRaises(UserError):
            run.action_reverse()

        # Roll-forward: the next run opens at closing net of write-offs
        # (90.00) and ties to the allowance ledger.
        later = self._run(reporting_date='2026-07-31', buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0}])
        self.assertAlmostEqual(later.opening_allowance, 90.0, places=2)
        self.assertAlmostEqual(later.ledger_allowance, 90.0, places=2)
        self.assertTrue(later.opening_ties_out)
        at_run_date = run.eh_deferred_tax_temp_diffs(date(2026, 6, 30))
        self.assertEqual(len(at_run_date), 1)
        self.assertAlmostEqual(at_run_date[0]['tax_base'], 250.0, places=2)
        deferred_tax_rows = run.eh_deferred_tax_temp_diffs(date(2026, 7, 31))
        self.assertEqual(len(deferred_tax_rows), 1)
        self.assertAlmostEqual(
            deferred_tax_rows[0]['tax_base'], 90.0, places=2,
        )

    def test_late_writeoff_and_prior_reversal_fail_behind_successor(self):
        invoice = self._invoice(self.partner_a, 100.0, '2026-04-01')
        first = self._run(reporting_date='2026-06-30', opening=0.0, buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0},
        ])
        first.action_compute()
        first.action_post()
        too_early = self.env['eh.ecl.writeoff'].create({
            'run_id': first.id,
            'move_line_id': self._receivable_line(invoice).id,
            'amount': 50.0,
            'stage': '3',
            'date': '2026-06-29',
        })
        with self.assertRaisesRegex(UserError, 'dated before'):
            too_early.action_post_writeoff()
        self.assertEqual(too_early.state, 'draft')
        second = self._run(reporting_date='2026-07-31', opening=250.0, buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1200.0},
        ])
        second.action_compute()
        second.action_post()

        writeoff = self.env['eh.ecl.writeoff'].create({
            'run_id': first.id,
            'move_line_id': self._receivable_line(invoice).id,
            'amount': 50.0,
            'stage': '3',
            'date': '2026-08-01',
        })
        with self.assertRaisesRegex(UserError, 'newest-first'):
            writeoff.action_post_writeoff()
        self.assertEqual(writeoff.state, 'draft')
        self.assertFalse(writeoff.move_id)
        with self.assertRaisesRegex(UserError, 'newest-first'):
            first.action_reverse()

        # Unwind the active chain in order. The older allowance can then
        # accept the write-off, while its own reversal remains blocked by that
        # immutable dependent entry.
        second.action_reverse()
        writeoff.action_post_writeoff()
        self.assertEqual(writeoff.state, 'posted')
        with self.assertRaisesRegex(UserError, 'posted write-off'):
            first.action_reverse()

    def test_backdated_run_cannot_post_behind_active_successor(self):
        later = self._run(reporting_date='2026-09-30', opening=0.0, buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 1000.0},
        ])
        later.action_compute()
        later.action_post()
        earlier = self._run(reporting_date='2026-08-31', opening=0.0, buckets=[
            {'name': '91+', 'days_from': 91, 'days_to': 0,
             'loss_rate': 25.0, 'stage': '3', 'gross_carrying': 800.0},
        ])
        earlier.action_compute()
        with self.assertRaisesRegex(UserError, 'newest-first'):
            earlier.action_post()
        self.assertEqual(earlier.state, 'computed')
        self.assertFalse(earlier.move_id)

    # ------------------------------------------------------------------
    # audit-trail ownership guards
    # ------------------------------------------------------------------

    def test_transfer_and_recon_are_engine_owned(self):
        run = self._run(approach='general', opening=0.0,
                        buckets=[self._portfolio('G', 45, 60)])
        run.action_compute()
        with self.assertRaises(UserError):
            self.env['eh.ecl.transfer'].create({
                'run_id': run.id, 'from_stage': '1', 'to_stage': '3',
                'reason': 'sicr_flag'})
        with self.assertRaises(UserError):
            run.transfer_ids.write({'reason': 'cure'})
        run.action_post()
        with self.assertRaises(UserError):
            self.env['eh.ecl.recon'].create({
                'run_id': run.id, 'stage': '1', 'closing': 999.0})
        with self.assertRaises(UserError):
            run.recon_ids[0].write({'closing': 999.0})
