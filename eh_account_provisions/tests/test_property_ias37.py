# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise + property tests for the IAS 37 depth mechanics.

The onerous measure and the restructuring component totals are pure
arithmetic over the record's inputs, so the oracle is recomputed
independently in the test for every generated case:

    net cost of fulfilling = max(fulfil - benefit, 0)
    measure = min(net, penalty) when penalty > 0 else net   (IAS 37.68)

Invariants checked on every case: the measure is never negative, never
exceeds the net cost of fulfilling, and never exceeds a positive exit
penalty; excluded restructuring kinds never enter the component total
(IAS 37.81).
"""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

ONEROUS_AXES = {
    'fulfil': [0.0, 50000.0, 120000.0],
    'benefit': [0.0, 30000.0, 130000.0],
    'penalty': [0.0, 20000.0, 70000.0],
}

INCLUDED_KINDS = ('termination', 'contract_termination', 'other_direct')
EXCLUDED_KINDS = ('retraining', 'marketing', 'new_systems')


@tagged('eh_golden', 'eh_account_provisions', 'post_install', '-at_install')
class TestPropertyIas37(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2900', 'Provisions', 'liability_current')

    def _onerous(self, fulfil, benefit, penalty):
        return self.env['eh.provision'].create({
            'name': '/', 'classification': 'provision',
            'provision_type': 'onerous',
            'unavoidable_cost_fulfil': fulfil,
            'contract_benefit_expected': benefit,
            'penalty_exit': penalty,
        })

    @staticmethod
    def _oracle(fulfil, benefit, penalty):
        net = max(fulfil - benefit, 0.0)
        return min(net, penalty) if penalty else net

    def test_pairwise_onerous_measure(self):
        for case in pairwise_cases(ONEROUS_AXES):
            fulfil, benefit, penalty = (
                case['fulfil'], case['benefit'], case['penalty'])
            p = self._onerous(fulfil, benefit, penalty)
            expected = round(self._oracle(fulfil, benefit, penalty), 2)
            got = p.best_estimate
            self.assertAlmostEqual(
                got, expected, places=2,
                msg='case %s: measure %s != oracle %s' % (
                    case, got, expected))
            # Invariants (IAS 37.68).
            self.assertGreaterEqual(got, 0.0)
            self.assertLessEqual(
                got, round(max(fulfil - benefit, 0.0), 2) + 0.005)
            if penalty > 0:
                self.assertLessEqual(got, penalty + 0.005)

    def test_property_onerous_measure_seeded(self):
        rng = self.seeded_rng(3701)
        for trial in range(25):
            fulfil = round(rng.uniform(0, 1000000), 2)
            benefit = round(rng.uniform(0, 1000000), 2)
            penalty = round(rng.uniform(0, 500000), 2) \
                if rng.random() < 0.7 else 0.0
            p = self._onerous(fulfil, benefit, penalty)
            expected = round(self._oracle(fulfil, benefit, penalty), 2)
            self.assertAlmostEqual(
                p.best_estimate, expected, places=2,
                msg='trial %s (%s/%s/%s)' % (trial, fulfil, benefit, penalty))

    def test_property_restructuring_totals_seeded(self):
        rng = self.seeded_rng(3702)
        kinds = INCLUDED_KINDS + EXCLUDED_KINDS
        for trial in range(15):
            lines, included, excluded = [], 0.0, 0.0
            for i in range(rng.randint(1, 6)):
                kind = kinds[rng.randrange(len(kinds))]
                amount = round(rng.uniform(0, 100000), 2)
                if kind in INCLUDED_KINDS:
                    included += amount
                else:
                    excluded += amount
                lines.append((0, 0, {
                    'name': 'component %s' % i, 'cost_kind': kind,
                    'amount': amount}))
            p = self.env['eh.provision'].create({
                'name': '/', 'classification': 'provision',
                'provision_type': 'restructuring',
                'best_estimate': 1000.0,
                'restructuring_line_ids': lines,
            })
            self.assertAlmostEqual(
                p.restructuring_component_total, round(included, 2),
                places=2, msg='trial %s included' % trial)
            self.assertAlmostEqual(
                p.restructuring_excluded_total, round(excluded, 2),
                places=2, msg='trial %s excluded' % trial)
