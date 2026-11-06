# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Property and pairwise tests for the IFRS 9 stage engine, cure
probation, scenario weighting and the IFRS 7.35H reconciliation.

These complement the hand-computed golden cases in test_golden_ifrs9.py:
they sweep the interaction axes (stage x movement kind, probation length,
random scenario sets) and assert engine invariants:

* the reconciliation identity per stage:
  closing = opening + transfers in - transfers out + remeasurement
            - write-offs, with opening continuous from the prior posted
  run's closing;
* cure probation: a downgrade only applies after probation_runs
  consecutive runs below the staged risk level, and any run back at the
  staged level resets the streak;
* scenario weighting: the probability-weighted ECL never exceeds the
  exposure (PD and LGD capped at 100%) and never goes negative.
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_ecl', 'post_install', '-at_install')
class TestPropertyIfrs9(EhGoldenTestCase):

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
        # Unique (company, reporting_date) per run; strictly ascending so
        # each run's engine-selected prior is the previously posted one.
        cls._date_seq += 1
        return '2026-%02d-%02d' % (
            1 + (cls._date_seq - 1) // 28, 1 + (cls._date_seq - 1) % 28)

    def _run(self, approach='simplified', opening=0.0, buckets=None,
             scenarios=None, **extra):
        vals = {
            'reporting_date': self._next_date(),
            'measurement_approach': approach,
            'opening_allowance': opening,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, b) for b in (buckets or [])],
        }
        if scenarios:
            vals['scenario_ids'] = [(0, 0, s) for s in scenarios]
        vals.update(extra)
        return self.env['eh.ecl.run'].create(vals)

    def _portfolio(self, name, days_from, days_to, **extra):
        # Standard staging portfolio: Stage 1 ECL = 1000 x 60% x 2% = 12,
        # Stage 2/3 ECL = 1000 x 60% x 8% = 48.
        vals = {
            'name': name, 'days_from': days_from, 'days_to': days_to,
            'loss_rate': 0.0, 'gross_carrying': 1000.0,
            'exposure_at_default': 1000.0,
            'pd_12m': 2.0, 'pd_lifetime': 8.0, 'lgd': 60.0,
        }
        vals.update(extra)
        return vals

    def _invoice_line(self, partner, amount, due):
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-01-01',
            'invoice_date_due': due,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sale', 'quantity': 1, 'price_unit': amount,
                'account_id': self.account_revenue.id})],
        })
        inv.action_post()
        return inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable')

    # ------------------------------------------------------------------
    # cure probation
    # ------------------------------------------------------------------

    def test_cure_probation_property(self):
        """For probation N in 1..3: after a Stage 2 backstop staging, an
        exposure back at 0 DPD stays Stage 2 (lifetime ECL 48.00) for the
        first N-1 clean runs and cures to Stage 1 (12-month ECL 12.00) on
        exactly the Nth, logging one 2 -> 1 transfer with reason cure at
        the 48.00 allowance it carried."""
        for probation in (1, 2, 3):
            name = 'CURE%d' % probation
            staged = self._run(
                approach='general', probation_runs=probation,
                buckets=[self._portfolio(name, 45, 60)])
            staged.action_compute()
            self.assertEqual(staged.bucket_ids.stage, '2',
                             'probation %d: backstop staging' % probation)
            staged.action_post()
            for clean_run in range(1, probation + 1):
                run = self._run(
                    approach='general', probation_runs=probation,
                    buckets=[self._portfolio(name, 0, 30)])
                run.action_compute()
                bucket = run.bucket_ids
                label = 'probation %d, clean run %d' % (
                    probation, clean_run)
                if clean_run < probation:
                    # Below threshold but still on probation: holds
                    # Stage 2, streak counts up, no transfer logged.
                    self.assertEqual(bucket.stage, '2', label)
                    self.assertEqual(bucket.cure_streak, clean_run, label)
                    self.assertFalse(run.transfer_ids, label)
                    self.assertAlmostEqual(
                        bucket.ecl_effective, 48.0, places=2, msg=label)
                else:
                    # Probation served: cures back to Stage 1.
                    self.assertEqual(bucket.stage, '1', label)
                    self.assertEqual(bucket.cure_streak, 0, label)
                    transfer = run.transfer_ids
                    self.assertEqual(len(transfer), 1, label)
                    self.assertEqual(
                        (transfer.from_stage, transfer.to_stage),
                        ('2', '1'), label)
                    self.assertEqual(transfer.reason, 'cure', label)
                    self.assertAlmostEqual(
                        transfer.amount, 48.0, places=2, msg=label)
                    self.assertAlmostEqual(
                        bucket.ecl_effective, 12.0, places=2, msg=label)
                run.action_post()

    def test_cure_streak_resets_on_relapse(self):
        """Probation 2: clean run (streak 1), relapse to 45 DPD (streak
        resets), clean run again -> streak restarts at 1 and the exposure
        must still be Stage 2, not cured."""
        name = 'CUREINT'
        chain = [
            (45, '2', 0),   # staged by the 30-DPD backstop
            (0, '2', 1),    # clean once: probation, streak 1
            (45, '2', 0),   # relapse: streak resets
            (0, '2', 1),    # clean once again: streak restarts at 1
        ]
        for days_from, expected_stage, expected_streak in chain:
            run = self._run(
                approach='general', probation_runs=2,
                buckets=[self._portfolio(name, days_from, days_from + 15)])
            run.action_compute()
            bucket = run.bucket_ids
            label = 'dpd %d' % days_from
            self.assertEqual(bucket.stage, expected_stage, label)
            self.assertEqual(bucket.cure_streak, expected_streak, label)
            run.action_post()

    # ------------------------------------------------------------------
    # reconciliation ties (IFRS 7.35H)
    # ------------------------------------------------------------------

    RECON_AXES = {
        'stage': ['1', '2', '3', 'poci'],
        'movement': ['flat', 'grow', 'shrink', 'writeoff'],
    }

    def test_recon_ties_pairwise(self):
        """Every stage x movement pair: the per-stage identity holds
        (closing = opening + in - out + remeasure - write-offs), opening
        is continuous from the prior posted run's closing, and closing
        equals the measured allowance net of write-offs."""
        gross_by_movement = {
            'flat': 1000.0, 'grow': 1500.0,
            'shrink': 600.0, 'writeoff': 1000.0,
        }
        for case in pairwise_cases(self.RECON_AXES):
            label = repr(case)
            stage_key = case['stage']
            base = {'name': 'RB', 'days_from': 0, 'days_to': 0,
                    'loss_rate': 20.0}
            if stage_key == 'poci':
                base.update(stage='3', poci=True)
            else:
                base['stage'] = stage_key
            prior = self._run(buckets=[dict(base, gross_carrying=1000.0)])
            prior.action_compute()
            prior.action_post()
            current = self._run(buckets=[dict(
                base, gross_carrying=gross_by_movement[case['movement']])])
            current.action_compute()
            current.action_post()
            written_off = 0.0
            if case['movement'] == 'writeoff':
                line = self._invoice_line(
                    self.partner_a, 60.0, '2026-01-15')
                writeoff = self.env['eh.ecl.writeoff'].create({
                    'run_id': current.id, 'move_line_id': line.id,
                    'amount': 50.0, 'stage': stage_key,
                    'date': '2026-12-30'})
                writeoff.action_post_writeoff()
                written_off = 50.0
            measured = dict.fromkeys(('1', '2', '3', 'poci'), 0.0)
            for bucket in current.bucket_ids:
                measured[bucket._recon_stage()] += bucket.ecl_effective
            prior_recon = {
                l.stage: l for l in current._prior_run().recon_ids}
            self.assertEqual(len(current.recon_ids), 4, label)
            for recon_line in current.recon_ids:
                stage = recon_line.stage
                # Identity per stage.
                self.assertAlmostEqual(
                    recon_line.closing,
                    recon_line.opening + recon_line.transfers_in
                    - recon_line.transfers_out + recon_line.remeasurement
                    - recon_line.writeoffs,
                    places=2, msg='identity broken for %s / %s' % (
                        stage, label))
                # Opening continuity from the engine-selected prior run.
                self.assertAlmostEqual(
                    recon_line.opening, prior_recon[stage].closing,
                    places=2, msg='opening not continuous for %s / %s' % (
                        stage, label))
                # Closing = measured allowance net of write-offs.
                expected_wo = written_off if stage == stage_key else 0.0
                self.assertAlmostEqual(
                    recon_line.closing, measured[stage] - expected_wo,
                    places=2, msg='closing wrong for %s / %s' % (
                        stage, label))
            if case['movement'] == 'writeoff':
                by_stage = {l.stage: l for l in current.recon_ids}
                self.assertAlmostEqual(
                    by_stage[stage_key].writeoffs, 50.0, places=2,
                    msg=label)

    # ------------------------------------------------------------------
    # scenario weighting invariants
    # ------------------------------------------------------------------

    def test_scenario_weighting_seeded_trials(self):
        """Randomized scenario sets (seeded): the probability-weighted
        general ECL equals the independently recomputed weighted sum and
        stays inside [0, EAD] because PD and LGD cap at 100%."""
        rng = self.seeded_rng(20260706)
        for trial in range(8):
            count = rng.randint(2, 4)
            raws = [rng.uniform(0.1, 1.0) for _ in range(count)]
            total = sum(raws)
            # The weight field stores 4 decimal places; build the oracle on
            # the SAME stored values or the comparison drifts by up to
            # 0.00005 x EAD per scenario. Last weight closes the sum to 1.
            weights = [round(raw / total, 4) for raw in raws[:-1]]
            weights.append(round(1.0 - sum(weights), 4))
            scenarios = []
            for i, weight in enumerate(weights):
                scenarios.append({
                    'name': 'S%d-%d' % (trial, i),
                    'weight': weight,
                    'pd_factor': round(rng.uniform(0.0, 2.0), 4),
                    'lgd_factor': round(rng.uniform(0.0, 2.0), 4),
                })
            ead = round(rng.uniform(0.0, 1e6), 2)
            lgd = round(rng.uniform(0.0, 100.0), 3)
            pd_12m = round(rng.uniform(0.0, 100.0), 3)
            pd_lifetime = round(rng.uniform(0.0, 100.0), 3)
            stage = rng.choice(['1', '2', '3'])
            poci = rng.random() < 0.3
            run = self._run(
                approach='general',
                buckets=[{
                    'name': 'T%d' % trial, 'days_from': 0, 'days_to': 0,
                    'loss_rate': 0.0, 'stage': stage, 'poci': poci,
                    'exposure_at_default': ead, 'lgd': lgd,
                    'pd_12m': pd_12m, 'pd_lifetime': pd_lifetime}],
                scenarios=scenarios)
            bucket = run.bucket_ids
            pd = pd_lifetime if (poci or stage != '1') else pd_12m
            expected = 0.0
            for scenario in scenarios:
                pd_eff = min(pd * scenario['pd_factor'], 100.0)
                lgd_eff = min(lgd * scenario['lgd_factor'], 100.0)
                expected += (scenario['weight'] * ead
                             * lgd_eff / 100.0 * pd_eff / 100.0)
            label = 'trial %d (seed 20260706)' % trial
            # The engine rounds once to company currency; allow exactly
            # that half-cent, nothing more.
            self.assertAlmostEqual(
                bucket.ecl_general, expected, delta=0.006, msg=label)
            self.assertGreaterEqual(bucket.ecl_general, -0.005, label)
            self.assertLessEqual(bucket.ecl_general, ead + 0.005, label)

    def test_single_neutral_scenario_matches_implicit(self):
        """A single scenario with weight 1 and factors 1 must reproduce
        the no-scenario measurement exactly."""
        bucket_vals = {
            'name': 'EQ', 'days_from': 0, 'days_to': 0, 'loss_rate': 0.0,
            'stage': '2', 'exposure_at_default': 12345.67, 'lgd': 37.5,
            'pd_12m': 4.2, 'pd_lifetime': 17.3}
        without = self._run(approach='general', buckets=[dict(bucket_vals)])
        with_neutral = self._run(
            approach='general', buckets=[dict(bucket_vals)],
            scenarios=[{'name': 'Base', 'weight': 1.0,
                        'pd_factor': 1.0, 'lgd_factor': 1.0}])
        self.assertAlmostEqual(
            without.bucket_ids.ecl_general,
            with_neutral.bucket_ids.ecl_general, places=2)
