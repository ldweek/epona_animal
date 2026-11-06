# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 3 golden worked examples: goodwill, NCI bases, bargain purchase,
deferred tax on the fair-value step-up, step-acquisition remeasurement
(IFRS 3.42), measurement-period adjustments (IFRS 3.45-49), and contingent
consideration (IFRS 3.39-40, 58).

Each test encodes a hand-computed example derived from the IFRS 3.32
goodwill equation (consideration incl. contingent consideration + NCI +
previously held interest - fair value of identifiable net assets, IAS 12.66
for deferred tax) and asserts the exact journal entry the engine posts.
Every expected amount is a literal derived in the comment above it, never
read back from the engine.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_business_combination', 'post_install',
        '-at_install')
class TestGoldenIfrs3(EhGoldenTestCase):

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
        cls.remeasure = cls._ensure_account(
            cls.env, '4810', 'Step Acquisition Remeasurement', 'income_other')
        cls.contingent_liab = cls._ensure_account(
            cls.env, '2800', 'Contingent Consideration Liability',
            'liability_non_current')
        cls.contingent_pnl = cls._ensure_account(
            cls.env, '4840', 'Contingent Consideration Remeasurement',
            'income_other')
        cls.contingent_equity = cls._ensure_account(
            cls.env, '3810', 'Contingent Consideration Equity', 'equity')

    def _combination(self, asset_lines, **vals):
        """A draft combination with identifiable asset/liability lines.

        ``asset_lines`` is a list of (account, fair_value, is_liability,
        tax_base) tuples driving the IFRS 3.18 purchase price allocation.
        """
        base = {
            'name': '/', 'acquiree_name': 'Target Ltd',
            'goodwill_account_id': self.goodwill_acc.id,
            'clearing_account_id': self.clearing.id,
            'gain_account_id': self.gain.id,
            'nci_account_id': self.nci_equity.id,
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

    def test_golden_goodwill_nci_fair_value(self):
        """IFRS 3.32 with NCI measured at fair value (IFRS 3.19 option a).

        Identifiable net assets at FV: PPE 1200 - payables 300 = 900.
        Goodwill = consideration 800 + NCI at FV 220 - net assets 900
                 = 1020 - 900 = 120.
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='fair_value', nci_amount=220.0)
        # 1200 - 300 = 900.
        self.assertAlmostEqual(c.identifiable_net_assets, 900.0, places=2)
        # 800 + 220 - 900 = 120.
        self.assertAlmostEqual(c.goodwill, 120.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 0.0, places=2)
        c.action_recognise_ppa()
        self.assertEqual(c.state, 'recognised')
        # Dr PPE 1200; Cr payables 300; Dr goodwill 120;
        # Cr clearing (consideration) 800; Cr NCI 220.
        # Debits 1200 + 120 = 1320 = credits 300 + 800 + 220.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1200.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.goodwill_acc, 120.0, 0.0),
            (self.clearing, 0.0, 800.0),
            (self.nci_equity, 0.0, 220.0),
        ])
        self.assertBalanced(c.move_id)

    def test_golden_goodwill_nci_proportionate(self):
        """IFRS 3.19 option b: NCI at the proportionate share of net assets.

        75% acquired, so the NCI (minority) ownership is 100 - 75 = 25%.
        Identifiable net assets at FV: PPE 1100 - payables 200 = 900.
        NCI = 25% x 900 = 225 (measured, not entered).
        Goodwill = consideration 800 + NCI 225 - net assets 900
                 = 1025 - 900 = 125.
        """
        c = self._combination(
            [(self.ppa_ppe, 1100.0, False, 0.0),
             (self.ppa_payable, 200.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='proportionate', nci_pct=25.0)
        # 25% x 900 = 225.
        self.assertAlmostEqual(c.nci_amount, 225.0, places=2)
        # 800 + 225 - 900 = 125.
        self.assertAlmostEqual(c.goodwill, 125.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1100; Cr payables 200; Dr goodwill 125;
        # Cr clearing (consideration) 800; Cr NCI 225.
        # Debits 1100 + 125 = 1225 = credits 200 + 800 + 225.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1100.0, 0.0),
            (self.ppa_payable, 0.0, 200.0),
            (self.goodwill_acc, 125.0, 0.0),
            (self.clearing, 0.0, 800.0),
            (self.nci_equity, 0.0, 225.0),
        ])
        self.assertBalanced(c.move_id)

    def test_golden_bargain_purchase_gain_to_pnl(self):
        """IFRS 3.34: a negative result is a bargain purchase gain in P&L.

        Identifiable net assets at FV: PPE 1300 - payables 300 = 1000.
        NCI proportionate at 20%: 20% x 1000 = 200.
        Consideration 700 + NCI 200 - net assets 1000 = -100,
        so a bargain purchase gain of 100 to profit or loss, no goodwill.
        """
        c = self._combination(
            [(self.ppa_ppe, 1300.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=700.0,
            nci_measurement='proportionate', nci_pct=20.0)
        # 20% x 1000 = 200.
        self.assertAlmostEqual(c.nci_amount, 200.0, places=2)
        # 700 + 200 - 1000 = -100 -> gain 100, goodwill nil.
        self.assertAlmostEqual(c.goodwill, 0.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 100.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1300; Cr payables 300; Cr bargain gain 100;
        # Cr clearing (consideration) 700; Cr NCI 200.
        # Debits 1300 = credits 300 + 100 + 700 + 200. No goodwill line:
        # the exhaustive line match proves the gain went to P&L instead.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1300.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.gain, 0.0, 100.0),
            (self.clearing, 0.0, 700.0),
            (self.nci_equity, 0.0, 200.0),
        ])
        self.assertBalanced(c.move_id)
        # The gain sits in the income account (credit balance -100)
        # and the goodwill asset stays untouched.
        self.assertAlmostEqual(self.posted_balance(self.gain), -100.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.goodwill_acc), 0.0,
                               places=2)

    def test_golden_deferred_tax_on_fv_step_up(self):
        """IAS 12.19/24/.66: deferred tax on the fair-value step-up.

        Book (tax base) net assets 800: PPE 1100 tax base, payables 300.
        Fair-value uplift 200 on the PPE only: PPE FV 1300, payables FV 300
        (payables FV equals tax base, adds nothing to the step-up).
        Identifiable net assets at FV: 1300 - 300 = 1000.
        Step-up = (1300 - 1100) - (300 - 300) = 200.
        DTL = 30% x 200 = 60, so post-tax identifiable net assets are
        800 + 200 - 60 = 940.
        100% acquired (NCI nil), consideration 1000:
        Goodwill = 1000 - 940 = 60
                 (engine form: 1000 + 0 - 1000 + DTL 60 = 60).
        """
        c = self._combination(
            [(self.ppa_ppe, 1300.0, False, 1100.0),
             (self.ppa_payable, 300.0, True, 300.0)],
            consideration_transferred=1000.0,
            nci_measurement='fair_value', nci_amount=0.0,
            tax_rate=30.0, deferred_tax_account_id=self.dtl_acc.id)
        # (1300 - 1100) - (300 - 300) = 200.
        self.assertAlmostEqual(c.fair_value_step_up, 200.0, places=2)
        # 30% x 200 = 60.
        self.assertAlmostEqual(c.deferred_tax, 60.0, places=2)
        # 1000 + 0 - 1000 + 60 = 60.
        self.assertAlmostEqual(c.goodwill, 60.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1300; Cr payables 300; Cr DTL 60; Dr goodwill 60;
        # Cr clearing (consideration) 1000. No NCI line at 100% owned.
        # Debits 1300 + 60 = 1360 = credits 300 + 60 + 1000.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1300.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.dtl_acc, 0.0, 60.0),
            (self.goodwill_acc, 60.0, 0.0),
            (self.clearing, 0.0, 1000.0),
        ])
        self.assertBalanced(c.move_id)
        # DTL carried as a credit balance of 60 on the balance sheet.
        self.assertAlmostEqual(self.posted_balance(self.dtl_acc), -60.0,
                               places=2)

    # ---- IFRS 3.42 step acquisition ----

    def test_golden_step_acquisition_remeasurement_gain(self):
        """IFRS 3.42: remeasure the previously-held interest to fair value.

        Previously-held 30% interest carried at 300, acquisition-date fair
        value 380: remeasurement gain = 380 - 300 = 80, to profit or loss.
        Identifiable net assets at FV: PPE 1200 - payables 300 = 900.
        Goodwill uses the FAIR VALUE of the previously-held interest:
        consideration 700 + NCI 0 + previously held at FV 380 - 900 = 180.
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=700.0,
            nci_measurement='fair_value', nci_amount=0.0,
            previously_held_interest_fv=380.0,
            previously_held_interest_carrying=300.0,
            remeasure_gain_account_id=self.remeasure.id)
        # 380 - 300 = 80.
        self.assertAlmostEqual(c.remeasurement_gain, 80.0, places=2)
        # 700 + 0 + 380 - 900 = 180.
        self.assertAlmostEqual(c.goodwill, 180.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1200; Cr payables 300; Dr goodwill 180;
        # Cr clearing 700 consideration and 380 previously-held interest;
        # Dr clearing 80 / Cr remeasurement gain 80 (IFRS 3.42 to P&L).
        # Debits 1200 + 180 + 80 = 1460 = credits 300 + 1080 + 80.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1200.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.goodwill_acc, 180.0, 0.0),
            (self.clearing, 0.0, 700.0),
            (self.clearing, 0.0, 380.0),
            (self.clearing, 80.0, 0.0),
            (self.remeasure, 0.0, 80.0),
        ])
        self.assertBalanced(c.move_id)
        # Gain in P&L (credit 80); net clearing carries consideration 700
        # plus the 300 carrying amount actually derecognised: 1080 - 80.
        self.assertAlmostEqual(self.posted_balance(self.remeasure), -80.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.clearing), -1000.0,
                               places=2)

    def test_golden_step_acquisition_remeasurement_loss(self):
        """IFRS 3.42 with a fair value below carrying: a loss to P&L.

        Carrying 400, fair value 380: remeasurement = 380 - 400 = -20.
        Goodwill still uses the fair value: 700 + 380 - 900 = 180.
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=700.0,
            nci_measurement='fair_value', nci_amount=0.0,
            previously_held_interest_fv=380.0,
            previously_held_interest_carrying=400.0,
            remeasure_gain_account_id=self.remeasure.id)
        self.assertAlmostEqual(c.remeasurement_gain, -20.0, places=2)
        self.assertAlmostEqual(c.goodwill, 180.0, places=2)
        c.action_recognise_ppa()
        # As the gain case but the P&L leg debits 20 against a 20 credit to
        # clearing: net clearing 1080 + 20 = 1100 = consideration 700 +
        # carrying 400 derecognised.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1200.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.goodwill_acc, 180.0, 0.0),
            (self.clearing, 0.0, 700.0),
            (self.clearing, 0.0, 380.0),
            (self.remeasure, 20.0, 0.0),
            (self.clearing, 0.0, 20.0),
        ])
        self.assertBalanced(c.move_id)
        self.assertAlmostEqual(self.posted_balance(self.remeasure), 20.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.clearing), -1100.0,
                               places=2)

    # ---- IFRS 3.45-49 measurement-period adjustments ----

    def test_golden_measurement_period_adjustment(self):
        """IFRS 3.45-49: PPE appraisal finalised within the window.

        At acquisition (2026-01-15): PPE provisional 1200, payables 300, so
        identifiable net assets 900; consideration 800 + NCI at FV 220
        - 900 = goodwill 120.
        Five months later the appraisal sets PPE at 1250 (+50): net assets
        950, restated goodwill 800 + 220 - 950 = 70, so the retrospective
        entry is Dr PPE 50 / Cr goodwill 50 (IFRS 3.48).
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='fair_value', nci_amount=220.0,
            acquisition_date='2026-01-15')
        self.assertAlmostEqual(c.goodwill, 120.0, places=2)
        c.action_recognise_ppa()
        # Outer limit: 12 months after acquisition (IFRS 3.45).
        self.assertEqual(str(c.measurement_period_end), '2027-01-15')
        ppe_line = c.asset_line_ids.filtered(lambda l: not l.is_liability)
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id,
            'name': 'Independent PPE appraisal finalised',
            'date': '2026-06-15',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1250.0,
            })],
        })
        adj.action_apply()
        self.assertEqual(adj.state, 'applied')
        # Dr PPE 50 / Cr goodwill 50, exactly.
        self.assertMoveLines(adj.move_id, [
            (self.ppa_ppe, 50.0, 0.0),
            (self.goodwill_acc, 0.0, 50.0),
        ])
        self.assertBalanced(adj.move_id)
        # Recognised amounts restated: line 1250, net assets 950, goodwill
        # 70 = 120 - 50; the delta is disclosed on the adjustment.
        self.assertAlmostEqual(ppe_line.fair_value, 1250.0, places=2)
        self.assertAlmostEqual(c.identifiable_net_assets, 950.0, places=2)
        self.assertAlmostEqual(c.goodwill, 70.0, places=2)
        self.assertAlmostEqual(adj.goodwill_delta, -50.0, places=2)
        self.assertAlmostEqual(adj.line_ids.previous_fair_value, 1200.0,
                               places=2)
        # Goodwill in the ledger: 120 recognised - 50 restated = 70.
        self.assertAlmostEqual(self.posted_balance(self.goodwill_acc), 70.0,
                               places=2)
        # Frozen after apply.
        with self.assertRaises(UserError):
            adj.name = 'edited'
        with self.assertRaises(UserError):
            adj.line_ids.revised_fair_value = 1300.0
        with self.assertRaises(UserError):
            adj.unlink()
        # Month 13 (2027-02-15 > 2027-01-15) is outside the measurement
        # period: blocked at entry (IFRS 3.45).
        with self.assertRaises(UserError):
            self.env['eh.bizcombo.adjustment'].create({
                'combination_id': c.id,
                'name': 'Too late',
                'date': '2027-02-15',
            })

    def test_golden_measurement_period_close_blocks_apply(self):
        """IFRS 3.45: once the measurement period is closed, no further
        measurement-period adjustment can be applied."""
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='fair_value', nci_amount=220.0)
        c.action_recognise_ppa()
        ppe_line = c.asset_line_ids.filtered(lambda l: not l.is_liability)
        adj = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id,
            'name': 'Post-close attempt',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1250.0,
            })],
        })
        c.action_close_measurement_period()
        self.assertTrue(c.measurement_period_closed)
        with self.assertRaises(UserError):
            adj.action_apply()

    # ---- IFRS 3.39-40, 58 contingent consideration ----

    def test_measurement_period_contingent_true_up_updates_liability_base(self):
        """IFRS 3.45: new acquisition-date evidence restates right balance.

        Cash consideration stays 900. Contingent consideration rises from
        100 to 120, so adjustment is Dr goodwill 20 / Cr contingent liability
        20. Clearing must not move, and later FV remeasurement starts at 120.
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=900.0,
            nci_measurement='fair_value', nci_amount=0.0,
            acquisition_date='2026-01-15',
            contingent_consideration_initial_fv=100.0,
            contingent_classification='liability',
            contingent_account_id=self.contingent_liab.id,
            contingent_pnl_account_id=self.contingent_pnl.id,
        )
        c.action_recognise_ppa()
        adjustment = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': c.id,
            'name': 'Acquisition-date earn-out evidence finalised',
            'date': '2026-06-15',
            'consideration_delta': 20.0,
            'consideration_delta_kind': 'contingent',
        })
        adjustment.action_apply()

        self.assertMoveLines(adjustment.move_id, [
            (self.goodwill_acc, 20.0, 0.0),
            (self.contingent_liab, 0.0, 20.0),
        ])
        self.assertBalanced(adjustment.move_id)
        self.assertAlmostEqual(c.consideration_transferred, 900.0, places=2)
        self.assertAlmostEqual(
            c.contingent_consideration_initial_fv, 120.0, places=2)
        self.assertAlmostEqual(
            c.contingent_consideration_current_fv, 120.0, places=2)
        self.assertAlmostEqual(c.goodwill, 120.0, places=2)
        clearing_lines = adjustment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.clearing)
        self.assertFalse(clearing_lines)

        remeasurement = self.env[
            'eh.bizcombo.contingent.remeasure'
        ].create({
            'combination_id': c.id,
            'new_fair_value': 140.0,
        })
        remeasurement.action_apply()
        self.assertAlmostEqual(remeasurement.previous_fair_value, 120.0,
                               places=2)
        self.assertAlmostEqual(remeasurement.delta, 20.0, places=2)
        with self.assertRaises(UserError):
            adjustment.action_reverse()

        remeasurement.action_reverse()
        adjustment.action_reverse()
        self.assertAlmostEqual(
            c.contingent_consideration_initial_fv, 100.0, places=2)
        self.assertAlmostEqual(
            c.contingent_consideration_current_fv, 100.0, places=2)
        self.assertAlmostEqual(c.consideration_transferred, 900.0, places=2)

    def test_golden_contingent_consideration_liability(self):
        """IFRS 3.39/.58a: liability-classified contingent consideration.

        Identifiable net assets at FV: PPE 1200 - payables 300 = 900.
        Consideration: cash 900 + contingent at acquisition-date FV 100.
        Goodwill = 900 + 100 + NCI 0 - 900 = 100.
        Later remeasurements of the liability go to profit or loss:
        100 -> 140 is a loss of 40; 140 -> 125 is a gain of 15.  # noqa: gcclog-hardcode fixed golden fixture
        """
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=900.0,
            nci_measurement='fair_value', nci_amount=0.0,
            contingent_consideration_initial_fv=100.0,
            contingent_classification='liability',
            contingent_account_id=self.contingent_liab.id,
            contingent_pnl_account_id=self.contingent_pnl.id)
        # 900 + 100 - 900 = 100.
        self.assertAlmostEqual(c.goodwill, 100.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1200; Cr payables 300; Dr goodwill 100; Cr clearing 900
        # (cash consideration only); Cr contingent liability 100.
        # Debits 1300 = credits 300 + 900 + 100.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1200.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.goodwill_acc, 100.0, 0.0),
            (self.clearing, 0.0, 900.0),
            (self.contingent_liab, 0.0, 100.0),
        ])
        self.assertBalanced(c.move_id)
        self.assertAlmostEqual(
            c.contingent_consideration_current_fv, 100.0, places=2)
        # Remeasure up to 140: delta 40 -> Dr P&L 40 / Cr liability 40.
        rm1 = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': c.id, 'new_fair_value': 140.0})
        rm1.action_apply()
        self.assertMoveLines(rm1.move_id, [
            (self.contingent_pnl, 40.0, 0.0),
            (self.contingent_liab, 0.0, 40.0),
        ])
        self.assertAlmostEqual(rm1.previous_fair_value, 100.0, places=2)
        self.assertAlmostEqual(rm1.delta, 40.0, places=2)
        self.assertAlmostEqual(
            c.contingent_consideration_current_fv, 140.0, places=2)
        # Remeasure down to 125: delta -15 -> Dr liability 15 / Cr P&L 15.
        rm2 = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': c.id, 'new_fair_value': 125.0})
        rm2.action_apply()
        self.assertMoveLines(rm2.move_id, [
            (self.contingent_liab, 15.0, 0.0),
            (self.contingent_pnl, 0.0, 15.0),
        ])
        self.assertAlmostEqual(
            c.contingent_consideration_current_fv, 125.0, places=2)
        # Liability carried at fair value: -(100 + 40 - 15) = -125.
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_liab), -125.0, places=2)
        # P&L carries the net loss 40 - 15 = 25 (debit).
        self.assertAlmostEqual(
            self.posted_balance(self.contingent_pnl), 25.0, places=2)
        # Frozen after apply.
        with self.assertRaises(UserError):
            rm1.new_fair_value = 1.0
        with self.assertRaises(UserError):
            rm1.unlink()

    def test_golden_contingent_equity_never_remeasured(self):
        """IFRS 3.58b: equity-classified contingent consideration is not
        remeasured; a remeasurement record is blocked by constraint."""
        c = self._combination(
            [(self.ppa_ppe, 1200.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=900.0,
            nci_measurement='fair_value', nci_amount=0.0,
            contingent_consideration_initial_fv=100.0,
            contingent_classification='equity',
            contingent_account_id=self.contingent_equity.id)
        # Goodwill formula is classification-independent: 900+100-900 = 100.
        self.assertAlmostEqual(c.goodwill, 100.0, places=2)
        with self.assertRaises(UserError):
            self.env['eh.bizcombo.contingent.remeasure'].create({
                'combination_id': c.id, 'new_fair_value': 140.0})

    def test_golden_goodwill_formula_all_components(self):
        """IFRS 3.32 regression with every component populated.

        Identifiable net assets at FV: PPE 1500 - payables 300 = 1200.
        Goodwill = consideration 800 + contingent consideration 100
                 + NCI at FV 225 + previously-held interest at FV 380
                 - net assets 1200 = 305.
        No carrying amount entered for the previously-held interest, so no
        IFRS 3.42 remeasurement posts (prior-behaviour regression).
        """
        c = self._combination(
            [(self.ppa_ppe, 1500.0, False, 0.0),
             (self.ppa_payable, 300.0, True, 0.0)],
            consideration_transferred=800.0,
            nci_measurement='fair_value', nci_amount=225.0,
            previously_held_interest_fv=380.0,
            contingent_consideration_initial_fv=100.0,
            contingent_classification='liability',
            contingent_account_id=self.contingent_liab.id)
        self.assertAlmostEqual(c.goodwill, 305.0, places=2)
        self.assertAlmostEqual(c.remeasurement_gain, 0.0, places=2)
        c.action_recognise_ppa()
        # Dr PPE 1500; Cr payables 300; Dr goodwill 305; Cr clearing 800
        # consideration and 380 previously held interest; Cr contingent 100;
        # Cr NCI 225. Debits 1805 = credits 300+1180+100+225 = 1805. The
        # exhaustive match proves no remeasurement legs were posted.
        self.assertMoveLines(c.move_id, [
            (self.ppa_ppe, 1500.0, 0.0),
            (self.ppa_payable, 0.0, 300.0),
            (self.goodwill_acc, 305.0, 0.0),
            (self.clearing, 0.0, 800.0),
            (self.clearing, 0.0, 380.0),
            (self.contingent_liab, 0.0, 100.0),
            (self.nci_equity, 0.0, 225.0),
        ])
        self.assertBalanced(c.move_id)
