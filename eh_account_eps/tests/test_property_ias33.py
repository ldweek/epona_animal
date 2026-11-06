# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise + property tests for the IAS 33 EPS engine.

The oracle for the weighted-average engine is an independent reference
implementation of the stated convention (inclusive day-count; each movement
runs to the day before the next; a restatement event multiplies every
movement recorded before its date; without events the scalar factor scales
the whole average). Invariants: diluted EPS never exceeds basic on the
control number (IAS 33.44), the continuing/discontinued split always ties
(IAS 33.66-68), and the restatement alias equals the product of the event
factors (IAS 33.64).
"""

from datetime import date, timedelta

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

AXES = {
    'movements': ['constant', 'mid_issue'],
    'events': ['none', 'split_sep', 'bonus_mar_split_sep'],
    'potential': ['none', 'options_scalar', 'options_obs', 'convertible'],
    'split': ['none', 'used'],
}

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 12, 31)

MOVEMENTS = {
    'constant': [(date(2026, 1, 1), 1000000.0)],
    'mid_issue': [(date(2026, 1, 1), 1000000.0),
                  (date(2026, 7, 1), 1200000.0)],
}
EVENTS = {
    'none': [],
    'split_sep': [(date(2026, 9, 1), 'split', 2.0)],
    'bonus_mar_split_sep': [(date(2026, 3, 15), 'bonus', 1.25),
                            (date(2026, 9, 1), 'split', 2.0)],
}


def ref_weighted_average(period_start, period_end, movements, events,
                         scalar=1.0):
    """Independent reference of the engine's weighted-average convention."""
    total_days = (period_end - period_start).days + 1
    movements = sorted(movements)
    weighted = 0.0
    for idx, (mdate, shares) in enumerate(movements):
        seg_start = max(mdate, period_start)
        if idx + 1 < len(movements):
            seg_end = movements[idx + 1][0] - timedelta(days=1)
        else:
            seg_end = period_end
        seg_end = min(seg_end, period_end)
        if seg_end < seg_start:
            continue
        days = (seg_end - seg_start).days + 1
        for edate, factor in events:
            if edate > mdate:
                shares *= factor
        weighted += shares * days
    wa = weighted / total_days
    if not events:
        wa *= scalar
    return wa


@tagged('eh_golden', 'eh_account_eps', 'post_install', '-at_install')
class TestPropertyIas33(EhGoldenTestCase):
    """Scenario-matrix and seeded-random invariants for the EPS engine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})

    def _build_run(self, case):
        run = self.env['eh.eps.run'].create({
            'period_start': PERIOD_START,
            'period_end': PERIOD_END,
            'net_profit': 500000.0,
            'profit_continuing':
                380000.0 if case['split'] == 'used' else 0.0,
            'profit_discontinued':
                120000.0 if case['split'] == 'used' else 0.0,
        })
        for mdate, shares in MOVEMENTS[case['movements']]:
            self.env['eh.eps.share.movement'].create({
                'run_id': run.id, 'effective_date': mdate,
                'shares_outstanding': shares})
        for edate, kind, factor in EVENTS[case['events']]:
            self.env['eh.eps.restatement.event'].create({
                'run_id': run.id, 'date': edate,
                'kind': kind, 'factor': factor})
        if case['potential'] == 'options_scalar':
            self.env['eh.eps.potential'].create({
                'run_id': run.id, 'name': 'Options',
                'instrument_type': 'options',
                'potential_shares': 100000.0,
                'share_count_date': PERIOD_START,
                'exercise_price': 4.0, 'average_market_price': 5.0})
        elif case['potential'] == 'options_obs':
            self.env['eh.eps.potential'].create({
                'run_id': run.id, 'name': 'Options (observed)',
                'instrument_type': 'options',
                'potential_shares': 100000.0,
                'share_count_date': PERIOD_START,
                'exercise_price': 4.0,
                'observation_ids': [
                    (0, 0, {'date': '2026-03-31', 'price': 4.8}),
                    (0, 0, {'date': '2026-06-30', 'price': 5.0}),
                    (0, 0, {'date': '2026-09-30', 'price': 5.2}),
                ]})
        elif case['potential'] == 'convertible':
            self.env['eh.eps.potential'].create({
                'run_id': run.id, 'name': 'Convertible',
                'instrument_type': 'convertible_bond',
                'potential_shares': 200000.0,
                'share_count_date': PERIOD_START,
                'earnings_adjustment': 30000.0})
        return run

    def test_pairwise_engine_invariants(self):
        for i, case in enumerate(pairwise_cases(AXES)):
            run = self._build_run(case)
            # Weighted average matches the independent reference (stored
            # at 2dp, so compare against the rounded reference).
            ref = round(ref_weighted_average(
                PERIOD_START, PERIOD_END,
                MOVEMENTS[case['movements']],
                [(d, f) for d, _k, f in EVENTS[case['events']]]), 2)
            self.assertAlmostEqual(
                run.weighted_avg_shares, ref, places=2,
                msg='case %s %s: weighted average' % (i, case))
            # Alias = product of event factors (IAS 33.64) when events.
            if case['events'] != 'none':
                product = 1.0
                for _d, _k, f in EVENTS[case['events']]:
                    product *= f
                self.assertAlmostEqual(
                    run.restatement_factor, product, places=6,
                    msg='case %s %s: alias factor' % (i, case))
            # Split always ties on the basic side (IAS 33.66-68).
            if case['split'] == 'used':
                self.assertAlmostEqual(
                    run.basic_eps_continuing + run.basic_eps_discontinued,
                    run.basic_eps, places=6,
                    msg='case %s %s: basic split tie' % (i, case))
            run.action_compute()
            # IAS 33.44: never dilute upwards on the control number.
            if case['split'] == 'used':
                self.assertLessEqual(
                    run.diluted_eps_continuing,
                    run.basic_eps_continuing + 1e-9,
                    'case %s %s: diluted continuing above basic'
                    % (i, case))
                self.assertAlmostEqual(
                    run.diluted_eps_continuing
                    + run.diluted_eps_discontinued,
                    run.diluted_eps, places=6,
                    msg='case %s %s: diluted split tie' % (i, case))
            else:
                self.assertLessEqual(
                    run.diluted_eps, run.basic_eps + 1e-9,
                    'case %s %s: diluted above basic' % (i, case))
            # The denominator never shrinks under dilution.
            self.assertGreaterEqual(
                run.diluted_shares, run.weighted_avg_shares - 0.01,
                'case %s %s: diluted shares below WA' % (i, case))

    def test_property_random_restatement_equivalence(self):
        """Seeded random movements/events: engine weighted average always
        matches the independent reference implementation."""
        rng = self.seeded_rng(33)
        for trial in range(15):
            n_moves = rng.randint(1, 4)
            move_days = sorted(rng.sample(range(0, 360), n_moves))
            movements = []
            shares = float(rng.randint(100, 2000) * 1000)
            for offset in move_days:
                movements.append(
                    (PERIOD_START + timedelta(days=offset), shares))
                shares += float(rng.randint(1, 500) * 1000)
            # Make the first movement open the period so every day is
            # covered (the module's convention needs an opening balance).
            movements[0] = (PERIOD_START, movements[0][1])
            n_events = rng.randint(0, 3)
            events = []
            for _ in range(n_events):
                edate = PERIOD_START + timedelta(
                    days=rng.randint(1, 364))
                if rng.random() < 0.7:
                    kind = rng.choice(['bonus', 'split'])
                    factor = 1.0 + rng.randint(1, 300) / 100.0
                else:
                    kind = 'consolidation'
                    factor = rng.randint(10, 90) / 100.0
                events.append((edate, kind, factor))
            run = self.env['eh.eps.run'].create({
                'period_start': PERIOD_START,
                'period_end': PERIOD_END,
                'net_profit': 500000.0})
            for mdate, count in movements:
                self.env['eh.eps.share.movement'].create({
                    'run_id': run.id, 'effective_date': mdate,
                    'shares_outstanding': count})
            for edate, kind, factor in events:
                self.env['eh.eps.restatement.event'].create({
                    'run_id': run.id, 'date': edate,
                    'kind': kind, 'factor': factor})
            ref = round(ref_weighted_average(
                PERIOD_START, PERIOD_END, movements,
                [(d, f) for d, _k, f in events]), 2)
            self.assertAlmostEqual(
                run.weighted_avg_shares, ref, delta=0.011,
                msg='trial %s: movements=%s events=%s'
                    % (trial, movements, events))
            self.assertGreaterEqual(
                run.weighted_avg_shares, 0.0, 'trial %s' % trial)
