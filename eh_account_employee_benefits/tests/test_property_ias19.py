# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise + property tests for the IAS 19 ledger mechanics.

Pairwise axes (funded x actuarial gain/loss x asset ceiling x past service)
over a fixed clean-number geometry so every oracle figure is hand-derivable
with no rounding:

    opening DBO 500,000 always; rate 4% so interest cost = 20,000.
    funded, ceiling case:    opening assets 600,000 -> interest income 24,000
    funded, no-ceiling case: opening assets 400,000 -> interest income 16,000
    unfunded:                no assets, no contributions, no excess return
    current service cost 30,000; benefits paid 20,000;
    contributions 25,000 and excess return +4,000 when funded.

    closing DBO    = 500,000 + 30,000 + psc + 20,000 - 20,000 + agl
                   = 530,000 + psc + agl        (range 510,000..573,000)
    closing assets = 600,000 + 24,000 + 25,000 + 4,000 - 20,000 = 633,000
                     (ceiling geometry, ALWAYS in surplus by >= 60,000)
                   = 400,000 + 16,000 + 25,000 + 4,000 - 20,000 = 425,000
                     (no-ceiling geometry, ALWAYS in deficit)
                   = 0 (unfunded)

    axis values: agl (loss positive)  +18,000 / -12,000 / 0
                 psc                  0 / +25,000 / -8,000
                 ceiling              none / binding 10,000 / slack 1,000,000

    OCI (loss+) = agl - excess return + ceiling effect change
    P&L         = 30,000 + psc + net interest

Worked check of one full case (funded, agl +18,000, psc +25,000, binding):
    closing DBO 573,000; closing assets 633,000; surplus 60,000;
    ceiling effect 50,000; recognised asset 10,000;
    OCI = 18,000 - 4,000 + 50,000 = 64,000;
    P&L = 30,000 + 25,000 + (20,000 - 24,000) = 51,000.
    Entry: Dr service 55,000, Cr net interest 4,000, Dr OCI 64,000,
    Dr plan assets 33,000 (movement), Cr plan assets 50,000 (ceiling),
    Cr DBO 73,000, Cr contribution clearing 25,000.
    Debits 152,000 = credits 152,000.

Infeasible pairs: an unfunded plan can never be in surplus, so the model
refuses the ceiling on unfunded valuations; those generated pairs are
coerced to ceiling 'none' and the refusal is asserted separately.

The seeded property sweep recomputes the documented rounding order
independently with float_round (round each rate product, then each sum) and
posts every trial, asserting the stored figures, the P&L/OCI split and the
entry balance.
"""

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import float_round

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

AXES = {
    'funded': [True, False],
    'act_gl': ['loss', 'gain', 'zero'],
    'ceiling': ['none', 'binding', 'slack'],
    'past_service': ['none', 'grant', 'negative'],
}

ACT_GL = {'loss': 18000.0, 'gain': -12000.0, 'zero': 0.0}
PSC = {'none': 0.0, 'grant': 25000.0, 'negative': -8000.0}
CEILING = {'binding': 10000.0, 'slack': 1000000.0}


@tagged('eh_golden', 'eh_account_employee_benefits', 'post_install',
        '-at_install')
class TestPropertyIas19(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.acc_service = cls._ensure_account(
            cls.env, '6202', 'DB Service Cost (PW)', 'expense')
        cls.acc_net_interest = cls._ensure_account(
            cls.env, '6212', 'DB Net Interest (PW)', 'expense')
        cls.acc_oci = cls._ensure_account(
            cls.env, '3202', 'OCI DB Remeasurements (PW)', 'equity')
        cls.acc_dbo = cls._ensure_account(
            cls.env, '2952', 'Defined Benefit Obligation (PW)',
            'liability_non_current')
        cls.acc_assets = cls._ensure_account(
            cls.env, '1952', 'Plan Assets (PW)', 'asset_non_current')
        cls.acc_contrib = cls._ensure_account(
            cls.env, '2152', 'Pension Contribution Clearing (PW)',
            'liability_current')
        cls.acc_benefit_pay = cls._ensure_account(
            cls.env, '2153', 'Benefit Payments Clearing (PW)',
            'liability_current')

    def _plan(self, name, funded=True):
        plan = self.env['eh.benefit.plan'].create({
            'name': name,
            'funded': funded,
            'contributions_posted_elsewhere': True,
            'service_cost_account_id': self.acc_service.id,
            'net_interest_account_id': self.acc_net_interest.id,
            'oci_account_id': self.acc_oci.id,
            'dbo_account_id': self.acc_dbo.id,
            'plan_asset_account_id': self.acc_dbo.id,
            'contribution_account_id': self.acc_contrib.id,
            'benefit_payment_account_id': self.acc_benefit_pay.id,
            'journal_id': self.journal_misc.id,
        })
        plan.action_activate()
        return plan

    @staticmethod
    def _net(move, account):
        lines = move.line_ids.filtered(
            lambda l: l.account_id == account)
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    # ------------------------------------------------------------------
    # oracle: the module's documented arithmetic, recomputed independently
    # ------------------------------------------------------------------
    @staticmethod
    def _oracle(funded, agl, psc, ceiling_mode):
        odbo = 500000.0
        if funded:
            oassets = 600000.0 if ceiling_mode != 'none' else 400000.0
            contrib, excess = 25000.0, 4000.0
        else:
            oassets, contrib, excess = 0.0, 0.0, 0.0
        ic = 0.04 * odbo
        ii = 0.04 * oassets
        ni = ic - ii
        cdbo = odbo + 30000.0 + psc + ic - 20000.0 + agl
        cassets = (
            oassets + ii + contrib + excess - 20000.0 if funded else 0.0)
        net_liab = cdbo - cassets
        surplus = max(-net_liab, 0.0)
        if ceiling_mode in CEILING and surplus > 0.0:
            cap = CEILING[ceiling_mode]
            ceff = max(surplus - cap, 0.0)
            recog = min(surplus, cap)
        else:
            ceff = 0.0
            recog = surplus
        return {
            'opening_dbo': odbo, 'opening_assets': oassets,
            'contrib': contrib, 'excess': excess,
            'interest_cost': ic, 'interest_income': ii, 'net_interest': ni,
            'closing_dbo': cdbo, 'closing_assets': cassets,
            'net_liability': net_liab, 'surplus': surplus,
            'ceiling_effect': ceff, 'recognised_asset': recog,
            'oci': agl - excess + ceff,
            'pnl': 30000.0 + psc + ni,
        }

    # ------------------------------------------------------------------
    # pairwise: funded x gain/loss x ceiling x past service
    # ------------------------------------------------------------------
    def test_pairwise_balance_and_routing(self):
        for idx, case in enumerate(pairwise_cases(AXES)):
            funded = case['funded']
            agl = ACT_GL[case['act_gl']]
            psc = PSC[case['past_service']]
            # Infeasible pair: unfunded plans never hold assets, so the
            # ceiling cannot apply; coerce and assert the refusal below.
            ceiling_mode = case['ceiling'] if funded else 'none'
            o = self._oracle(funded, agl, psc, ceiling_mode)
            tag = 'case %s %s' % (idx, case)

            plan = self._plan('PW plan %s' % idx, funded=funded)
            vals = {
                'plan_id': plan.id,
                'period_end': '2026-12-31',
                'opening_dbo': o['opening_dbo'],
                'opening_assets': o['opening_assets'],
                'discount_rate': 4.0,
                'current_service_cost': 30000.0,
                'past_service_cost': psc,
                'benefits_paid': 20000.0,
                'contributions_employer': o['contrib'],
                'actuarial_gain_loss_dbo': agl,
                'return_on_assets_excess': o['excess'],
            }
            if ceiling_mode in CEILING:
                vals.update(apply_asset_ceiling=True,
                            asset_ceiling=CEILING[ceiling_mode])
            v = self.env['eh.benefit.valuation'].create(vals)

            # Stored mechanics against the independent oracle.
            for fname, key in (
                    ('interest_cost', 'interest_cost'),
                    ('interest_income', 'interest_income'),
                    ('net_interest', 'net_interest'),
                    ('closing_dbo', 'closing_dbo'),
                    ('closing_assets', 'closing_assets'),
                    ('net_liability', 'net_liability'),
                    ('ceiling_effect', 'ceiling_effect'),
                    ('recognised_asset', 'recognised_asset'),
                    ('oci_remeasurement', 'oci'),
                    ('pnl_total', 'pnl')):
                self.assertAlmostEqual(
                    v[fname], o[key], places=2,
                    msg='%s: %s %s != oracle %s' % (
                        tag, fname, v[fname], o[key]))
            # Routing invariants: remeasurements never reach P&L and the
            # recognised asset never exceeds the ceiling (IAS 19.64/.122).
            self.assertAlmostEqual(
                v.pnl_total + v.oci_remeasurement,
                o['pnl'] + o['oci'], places=2, msg=tag)
            if ceiling_mode in CEILING:
                self.assertLessEqual(
                    v.recognised_asset, CEILING[ceiling_mode] + 0.005,
                    msg='%s: recognised asset above the ceiling' % tag)

            v.action_post()
            move = v.move_id
            self.assertTrue(move.eh_sealed, msg=tag)
            self.assertBalanced(move)
            # Net movement per account = the routing contract.
            self.assertAlmostEqual(
                self._net(move, self.acc_service), 30000.0 + psc,
                places=2, msg='%s: service cost leg' % tag)
            self.assertAlmostEqual(
                self._net(move, self.acc_net_interest), o['net_interest'],
                places=2, msg='%s: net interest leg' % tag)
            self.assertAlmostEqual(
                self._net(move, self.acc_oci), o['oci'],
                places=2, msg='%s: OCI leg' % tag)
            if funded:
                self.assertAlmostEqual(
                    self._net(move, self.acc_dbo),
                    -(o['closing_dbo'] - o['opening_dbo'])
                    + (o['closing_assets'] - o['opening_assets'])
                    - o['ceiling_effect'],
                    places=2, msg='%s: net position account' % tag)
                self.assertAlmostEqual(
                    self._net(move, self.acc_contrib), -o['contrib'],
                    places=2, msg='%s: contribution leg' % tag)
                self.assertAlmostEqual(
                    self._net(move, self.acc_benefit_pay), 0.0,
                    places=2,
                    msg='%s: funded plans never touch employer cash' % tag)
            else:
                self.assertAlmostEqual(
                    self._net(move, self.acc_dbo),
                    -(o['closing_dbo'] - o['opening_dbo']),
                    places=2, msg='%s: unfunded net liability leg' % tag)
                self.assertAlmostEqual(
                    self._net(move, self.acc_benefit_pay), -20000.0,
                    places=2, msg='%s: employer pays benefits' % tag)
                self.assertAlmostEqual(
                    self._net(move, self.acc_assets), 0.0,
                    places=2, msg='%s: unfunded plan has no assets' % tag)
                self.assertAlmostEqual(
                    self._net(move, self.acc_contrib), 0.0,
                    places=2, msg='%s: unfunded plan has no fund' % tag)

    def test_unfunded_refuses_assets_and_ceiling(self):
        """The pairs coerced out of the matrix are refused by the model:
        an unfunded plan cannot key assets, contributions, excess return
        or the asset ceiling."""
        plan = self._plan('PW unfunded guard', funded=False)
        base = {'plan_id': plan.id, 'period_end': '2026-12-31',
                'opening_dbo': 100000.0, 'discount_rate': 0.0}
        with self.assertRaises(ValidationError,
                               msg='unfunded + opening assets must raise'), \
                self.env.cr.savepoint():
            self.env['eh.benefit.valuation'].create(
                dict(base, opening_assets=50000.0))
        with self.assertRaises(ValidationError,
                               msg='unfunded + contributions must raise'), \
                self.env.cr.savepoint():
            self.env['eh.benefit.valuation'].create(
                dict(base, contributions_employer=1000.0))
        with self.assertRaises(ValidationError,
                               msg='unfunded + asset ceiling must raise'), \
                self.env.cr.savepoint():
            self.env['eh.benefit.valuation'].create(
                dict(base, apply_asset_ceiling=True, asset_ceiling=1.0))

    def test_stale_draft_opening_blocked_at_post(self):
        """A draft created BEFORE the prior period posted carries a stale
        opening position; the posting gate must re-run the chain check.

        Year 1: opening DBO 100,000 / assets 50,000, rate 0, service cost
        10,000 -> closing 110,000 / 50,000. Year 2 is drafted first (its
        opening defaults to zero: no posted prior exists yet), then year 1
        posts. Posting year 2 with the stale zeros must raise; rekeying
        the opening to the chained figures posts cleanly.
        """
        plan = self._plan('PW stale-draft plan')
        v2 = self.env['eh.benefit.valuation'].create({
            'plan_id': plan.id, 'period_end': '2027-12-31',
            'discount_rate': 0.0, 'current_service_cost': 5000.0})
        v1 = self.env['eh.benefit.valuation'].create({
            'plan_id': plan.id, 'period_end': '2026-12-31',
            'opening_dbo': 100000.0, 'opening_assets': 50000.0,
            'discount_rate': 0.0, 'current_service_cost': 10000.0})
        v1.action_post()
        with self.assertRaises(ValidationError,
                               msg='stale opening must not reach the '
                                   'ledger'), \
                self.env.cr.savepoint():
            v2.action_post()
        v2.write({'opening_dbo': 110000.0, 'opening_assets': 50000.0})
        v2.action_post()
        self.assertEqual(v2.state, 'posted')
        # closing DBO = 110,000 + 5,000 service cost = 115,000.
        self.assertAlmostEqual(v2.closing_dbo, 115000.00, places=2)

    # ------------------------------------------------------------------
    # seeded property sweep: rounding-order oracle + balance on every trial
    # ------------------------------------------------------------------
    def test_property_rollforward_seeded(self):
        def r2(x):
            return float_round(x, precision_digits=2)

        rng = self.seeded_rng(1901)
        for trial in range(15):
            odbo = r2(rng.uniform(200000, 1500000))
            oassets = r2(rng.uniform(50000, 1500000))
            rate = round(rng.uniform(0.5, 8.0), 4)
            sc = r2(rng.uniform(0, 100000))
            psc = r2(rng.uniform(-20000, 50000))
            ben = r2(rng.uniform(0, 0.3 * min(odbo, oassets)))
            contrib = r2(rng.uniform(0, 80000))
            agl = r2(rng.uniform(-0.08, 0.08) * odbo)
            excess = r2(rng.uniform(-0.05, 0.05) * oassets)
            apply_c = rng.random() < 0.5
            cap = r2(rng.uniform(0, 150000)) if apply_c else 0.0

            # Oracle in the documented rounding order: rate products
            # first, then each sum, all to company currency 2dp.
            ic = r2(odbo * rate / 100.0)
            ii = r2(oassets * rate / 100.0)
            ni = r2(ic - ii)
            cdbo = r2(odbo + sc + psc + ic - ben + agl)
            cassets = r2(oassets + ii + contrib + excess - ben)
            net_liab = r2(cdbo - cassets)
            surplus = max(-net_liab, 0.0)
            if apply_c and surplus > 0.0:
                ceff = r2(max(surplus - cap, 0.0))
            else:
                ceff = 0.0
            oci = r2(agl - excess + ceff)
            pnl = r2(sc + psc + ni)

            plan = self._plan('PW seeded plan %s' % trial)
            v = self.env['eh.benefit.valuation'].create({
                'plan_id': plan.id, 'period_end': '2026-12-31',
                'opening_dbo': odbo, 'opening_assets': oassets,
                'discount_rate': rate, 'current_service_cost': sc,
                'past_service_cost': psc, 'benefits_paid': ben,
                'contributions_employer': contrib,
                'actuarial_gain_loss_dbo': agl,
                'return_on_assets_excess': excess,
                'apply_asset_ceiling': apply_c,
                'asset_ceiling': cap,
            })
            tag = 'trial %s (dbo %s assets %s rate %s)' % (
                trial, odbo, oassets, rate)
            self.assertAlmostEqual(v.closing_dbo, cdbo, places=2, msg=tag)
            self.assertAlmostEqual(
                v.closing_assets, cassets, places=2, msg=tag)
            self.assertAlmostEqual(
                v.oci_remeasurement, oci, places=2, msg=tag)
            self.assertAlmostEqual(v.pnl_total, pnl, places=2, msg=tag)
            v.action_post()
            self.assertBalanced(v.move_id)
            self.assertTrue(v.move_id.eh_sealed, msg=tag)
