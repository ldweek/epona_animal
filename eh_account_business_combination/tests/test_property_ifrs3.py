# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 3 measurement-period and contingent-consideration property tests.

Pairwise scenario matrix over the measurement-period adjustment axes
(restated line kind, direction, NCI basis, deferred tax) with a
hand-recomputed goodwill oracle per case, plus the guardrail suite: the
blocked states IFRS 3 prescribes (proportionate NCI deltas, goodwill
flipping negative, out-of-state applies, cross-combination lines,
frozen-after-recognition line creation).
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_business_combination', 'post_install',
        '-at_install')
class TestPropertyIfrs3(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.goodwill_acc = cls._ensure_account(
            cls.env, '1800', 'Goodwill', 'asset_non_current')
        cls.clearing = cls._ensure_account(
            cls.env, '1801', 'Acquisition Clearing', 'asset_current')
        cls.gain = cls._ensure_account(
            cls.env, '4800', 'Bargain Purchase Gain', 'income_other')
        cls.nci_equity = cls._ensure_account(
            cls.env, '3800', 'Non-controlling Interest', 'equity')
        cls.ppa_ppe = cls._ensure_account(
            cls.env, '1600', 'Property Plant Equipment', 'asset_fixed')
        cls.ppa_payable = cls._ensure_account(
            cls.env, '2600', 'Assumed Payables', 'liability_current')
        cls.dtl_acc = cls._ensure_account(
            cls.env, '2700', 'Deferred Tax Liability', 'liability_non_current')
        cls.contingent_liab = cls._ensure_account(
            cls.env, '2800', 'Contingent Consideration Liability',
            'liability_non_current')
        cls.contingent_pnl = cls._ensure_account(
            cls.env, '4840', 'Contingent Consideration Remeasurement',
            'income_other')

    def _combination(self, asset_lines, **vals):
        base = {
            'name': '/', 'acquiree_name': 'Target Ltd',
            'goodwill_account_id': self.goodwill_acc.id,
            'clearing_account_id': self.clearing.id,
            'gain_account_id': self.gain.id,
            'nci_account_id': self.nci_equity.id,
            'deferred_tax_account_id': self.dtl_acc.id,
            'journal_id': self.journal_misc.id,
            'asset_line_ids': [
                (0, 0, {
                    'name': account.name, 'account_id': account.id,
                    'fair_value': fair_value, 'is_liability': is_liability,
                    'tax_base': tax_base,
                }) for account, fair_value, is_liability, tax_base
                in asset_lines],
        }
        base.update(vals)
        return self.env['eh.business.combination'].create(base)

    # ------------------------------------------------------------------
    # pairwise matrix: measurement-period adjustment engine
    # ------------------------------------------------------------------
    def test_mp_adjustment_pairwise(self):
        """Every axis pair of the restatement engine against a hand oracle.

        Fixture per case: PPE at FV 1000 (tax base 900), assumed payables
        at FV 400 (tax base 400), so identifiable net assets 600 and a
        fair-value step-up of 100; consideration 700. The oracle recomputes
        each goodwill component from first principles (IFRS 3.32, 3.19,
        IAS 12.66) with the revised line and asserts the stored amounts and
        the restatement entry agree with it.
        """
        axes = {
            'target': ['asset', 'liability'],
            'direction': ['up', 'down'],
            'basis': ['fair_value', 'proportionate'],
            'tax': [0.0, 30.0],
        }
        for case in pairwise_cases(axes):
            with self.subTest(**case):
                vals = {
                    'consideration_transferred': 700.0,
                    'nci_measurement': case['basis'],
                    'tax_rate': case['tax'],
                }
                if case['basis'] == 'proportionate':
                    vals['nci_pct'] = 25.0
                else:
                    vals['nci_amount'] = 60.0
                c = self._combination(
                    [(self.ppa_ppe, 1000.0, False, 900.0),
                     (self.ppa_payable, 400.0, True, 400.0)],
                    **vals)
                c.action_recognise_ppa()
                goodwill_before = c.goodwill

                delta = 50.0 if case['direction'] == 'up' else -50.0
                is_liability_target = case['target'] == 'liability'
                target = c.asset_line_ids.filtered(
                    lambda l: l.is_liability == is_liability_target)
                revised = target.fair_value + delta
                adj = self.env['eh.bizcombo.adjustment'].create({
                    'combination_id': c.id,
                    'name': 'Pairwise %s' % case,
                    'line_ids': [(0, 0, {
                        'asset_line_id': target.id,
                        'revised_fair_value': revised,
                    })],
                })
                adj.action_apply()

                # Hand oracle, recomputed from first principles.
                ppe_new = 1000.0 + (0.0 if is_liability_target else delta)
                pay_new = 400.0 + (delta if is_liability_target else 0.0)
                fina_new = ppe_new - pay_new
                step_new = (ppe_new - 900.0) - (pay_new - 400.0)
                dtl_new = round(step_new * case['tax'] / 100.0, 2)
                if case['basis'] == 'proportionate':
                    # IFRS 3.19: proportionate NCI is a share of the recognised
                    # net assets, net of the IAS 12.19 deferred tax on the
                    # step-up (so it drops to nil tax when the rate is nil).
                    nci_new = round((fina_new - dtl_new) * 0.25, 2)
                else:
                    nci_new = 60.0
                goodwill_new = 700.0 + nci_new - fina_new + dtl_new

                self.assertAlmostEqual(target.fair_value, revised, places=2)
                self.assertAlmostEqual(
                    c.identifiable_net_assets, fina_new, places=2)
                self.assertAlmostEqual(c.deferred_tax, dtl_new, places=2)
                self.assertAlmostEqual(c.nci_amount, nci_new, places=2)
                self.assertAlmostEqual(c.goodwill, goodwill_new, places=2)
                self.assertAlmostEqual(
                    adj.goodwill_delta, goodwill_new - goodwill_before,
                    places=2)
                self.assertBalanced(adj.move_id)
                # The restated line leg carries exactly the fair-value delta
                # on the correct side (Dr an asset up / Cr an asset down;
                # mirrored for a liability).
                target_lines = adj.move_id.line_ids.filtered(
                    lambda l: l.account_id == target.account_id)
                self.assertEqual(len(target_lines), 1)
                debit_side = (delta > 0) != is_liability_target
                self.assertAlmostEqual(
                    target_lines.debit if debit_side else target_lines.credit,
                    abs(delta), places=2)

    # ------------------------------------------------------------------
    # measurement-period guardrails
    # ------------------------------------------------------------------
    def _recognised_combination(self, **vals):
        """PPE 1200 - payables 300 = 900; consideration 800 + NCI 220
        - 900 = goodwill 120; recognised."""
        base = {
            'consideration_transferred': 800.0,
            'nci_measurement': 'fair_value', 'nci_amount': 220.0,
        }
        base.update(vals)
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            **base)
        c.action_recognise_ppa()
        return c

    def test_mp_adjustment_scalar_deltas(self):
        """Goodwill-affecting scalar deltas without line restatements.

        Base goodwill 120 (800 + 220 - 900). Consideration +50 and
        fair-value NCI +30 restate goodwill to 850 + 250 - 900 = 200:
        Dr goodwill 80 / Cr clearing 50 / Cr NCI 30.
        """
        c = self._recognised_combination()
        self.assertAlmostEqual(c.goodwill, 120.0, places=2)
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id,
            'name': 'Contingent consideration true-up and NCI valuation',
            'consideration_delta': 50.0,
            'nci_delta': 30.0,
        })
        adj.action_apply()
        self.assertMoveLines(adj.move_id, [
            (self.goodwill_acc, 80.0, 0.0),
            (self.clearing, 0.0, 50.0),
            (self.nci_equity, 0.0, 30.0),
        ])
        self.assertAlmostEqual(c.consideration_transferred, 850.0, places=2)
        self.assertAlmostEqual(c.nci_amount, 250.0, places=2)
        self.assertAlmostEqual(c.goodwill, 200.0, places=2)
        self.assertAlmostEqual(adj.goodwill_delta, 80.0, places=2)

    def test_mp_adjustment_nci_delta_blocked_on_proportionate(self):
        # IFRS 3.19: proportionate NCI is measured from net assets, so a
        # manual NCI delta is contradictory and blocked.
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='proportionate', nci_pct=25.0)
        c.action_recognise_ppa()
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id, 'name': 'Manual NCI', 'nci_delta': 30.0})
        with self.assertRaises(UserError):
            adj.action_apply()

    def test_mp_adjustment_flip_to_bargain_blocked(self):
        # Restating net assets up by 250 would drive goodwill to
        # 800 + 220 - 1150 = -130: below nil is blocked explicitly.
        c = self._recognised_combination()
        ppe_line = c.asset_line_ids.filtered(lambda l: not l.is_liability)
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id, 'name': 'Bargain flip',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1450.0,
            })],
        })
        with self.assertRaises(UserError):
            adj.action_apply()

    def test_mp_adjustment_requires_recognised(self):
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0)],
            consideration_transferred=800.0)
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id, 'name': 'Too early',
            'consideration_delta': 50.0})
        with self.assertRaises(UserError):
            adj.action_apply()

    def test_mp_adjustment_line_must_match_combination(self):
        c1 = self._recognised_combination()
        c2 = self._recognised_combination()
        foreign_line = c2.asset_line_ids.filtered(lambda l: not l.is_liability)
        with self.assertRaises(UserError):
            self.env['eh.bizcombo.adjustment'].create({
                'combination_id': c1.id, 'name': 'Cross wiring',
                'line_ids': [(0, 0, {
                    'asset_line_id': foreign_line.id,
                    'revised_fair_value': 1250.0,
                })],
            })

    def test_mp_adjustment_duplicate_line_blocked(self):
        c = self._recognised_combination()
        ppe_line = c.asset_line_ids.filtered(lambda l: not l.is_liability)
        with self.assertRaises(UserError):
            self.env['eh.bizcombo.adjustment'].create({
                'combination_id': c.id, 'name': 'Duplicate target',
                'line_ids': [
                    (0, 0, {'asset_line_id': ppe_line.id,
                            'revised_fair_value': 1250.0}),
                    (0, 0, {'asset_line_id': ppe_line.id,
                            'revised_fair_value': 1300.0}),
                ],
            })

    def test_asset_line_create_blocked_on_recognised(self):
        # Frozen-after-post integrity: a new identifiable line would shift
        # stored goodwill on a posted combination without a move.
        c = self._recognised_combination()
        with self.assertRaises(UserError):
            self.env['eh.business.combination.asset'].create({
                'combination_id': c.id, 'name': 'Late line',
                'account_id': self.ppa_ppe.id, 'fair_value': 10.0,
            })

    # ------------------------------------------------------------------
    # contingent consideration guardrails
    # ------------------------------------------------------------------
    def test_remeasure_requires_recognised(self):
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0)],
            consideration_transferred=800.0,
            contingent_consideration_initial_fv=100.0,
            contingent_account_id=self.contingent_liab.id,
            contingent_pnl_account_id=self.contingent_pnl.id)
        rm = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': c.id, 'new_fair_value': 140.0})
        with self.assertRaises(UserError):
            rm.action_apply()

    def test_remeasure_zero_delta_blocked(self):
        c = self._recognised_combination(
            contingent_consideration_initial_fv=100.0,
            contingent_account_id=self.contingent_liab.id,
            contingent_pnl_account_id=self.contingent_pnl.id)
        rm = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': c.id, 'new_fair_value': 100.0})
        with self.assertRaises(UserError):
            rm.action_apply()
