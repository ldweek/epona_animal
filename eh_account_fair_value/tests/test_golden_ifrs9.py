# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 9 / IFRS 13 worked examples for eh_account_fair_value.

Covers the classification engine (IFRS 9.4.1: SPPI questionnaire x business
model x instrument nature, full product since the matrix is small), the
derecognition action with atomic OCI settlement (IFRS 9.5.7.10 recycling for
FVOCI-debt, IFRS 9.B5.7.1 within-equity transfer for the equity election),
the Level 3 reconciliation tie enforcement and ledger-fed gains columns
(IFRS 13.93(e)), and the linear sensitivity engine (IFRS 13.93(h)).

Every expected amount is hand-computed from the inputs stated in the test,
with the derivation in a comment; nothing is read back from the engine under
test to build an expected value.
"""

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import full_product


@tagged('eh_golden', 'eh_account_fair_value', 'post_install', '-at_install')
class TestGoldenIfrs9(EhGoldenTestCase):

    _MEASUREMENT_DATE = '2026-04-30'
    _DERECOGNITION_DATE = '2026-04-30'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.fv_asset = cls._ensure_account(
            cls.env, '1600', 'Investments at FV', 'asset_current')
        cls.fv_gain = cls._ensure_account(
            cls.env, '4600', 'Fair Value Gain/Loss', 'income_other')
        cls.fv_oci = cls._ensure_account(
            cls.env, '3600', 'FVOCI Reserve', 'equity')
        cls.fv_retained = cls._ensure_account(
            cls.env, '3610', 'Retained Earnings', 'equity')
        cls.fv_liability = cls._ensure_account(
            cls.env, '2600', 'Liabilities at FV', 'liability_current')

    _SPPI_FLAGS = ('sppi_fixed_dates', 'sppi_interest_only',
                   'sppi_no_leverage', 'sppi_no_contingent_returns')

    def _item(self, **vals):
        # No routing in the base values: the engine must derive it.
        base = {
            'name': '/', 'nature': 'financial_asset', 'level': '1',
            'prior_carrying': 1000.0, 'fair_value': 1000.0,
            'measurement_date': self._MEASUREMENT_DATE,
            'balance_sheet_account_id': self.fv_asset.id,
            'gain_loss_account_id': self.fv_gain.id,
            'oci_account_id': self.fv_oci.id,
            'settlement_account_id': self.account_cash.id,
            'retained_earnings_account_id': self.fv_retained.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.fair.value.item'].create(base)

    def _debt(self, business_model='hold_collect_sell', sppi=True, **vals):
        flags = dict.fromkeys(self._SPPI_FLAGS, True)
        if not sppi:
            flags['sppi_no_leverage'] = False
        return self._item(instrument_type='debt',
                          business_model=business_model, **flags, **vals)

    # ------------------------------------------------------------------
    # classification engine (IFRS 9.4.1)
    # ------------------------------------------------------------------

    def test_classification_full_matrix(self):
        """Full product over instrument nature x SPPI outcome x business
        model (3 x 5 x 3 = 45 cases). Expected classification derived by
        hand from IFRS 9.4.1.1-4.1.4:

        * derivative: always FVTPL (fails SPPI by nature);
        * equity without the election: FVTPL (fails SPPI by nature);
        * debt failing any one questionnaire answer: FVTPL, whichever
          answer fails;
        * debt passing SPPI: amortised cost when hold-to-collect, FVOCI-debt
          when hold-to-collect-and-sell, FVTPL when other.

        Routing must follow the classification: FVOCI to OCI, all else P&L.
        """
        fail_map = {
            'fail_fixed_dates': 'sppi_fixed_dates',
            'fail_interest_only': 'sppi_interest_only',
            'fail_leverage': 'sppi_no_leverage',
            'fail_contingent': 'sppi_no_contingent_returns',
        }
        axes = {
            'instrument': ['debt', 'equity', 'derivative'],
            'sppi_case': ['pass'] + sorted(fail_map),
            'business_model': ['hold_to_collect', 'hold_collect_sell',
                               'other'],
        }
        for case in full_product(axes):
            with self.subTest(**case):
                flags = dict.fromkeys(self._SPPI_FLAGS, True)
                if case['sppi_case'] != 'pass':
                    flags[fail_map[case['sppi_case']]] = False
                item = self._item(
                    instrument_type=case['instrument'],
                    business_model=case['business_model'], **flags)
                expected_sppi = (case['instrument'] == 'debt'
                                 and case['sppi_case'] == 'pass')
                if case['instrument'] in ('equity', 'derivative'):
                    expected = 'fvtpl'
                elif not expected_sppi:
                    expected = 'fvtpl'
                else:
                    expected = {
                        'hold_to_collect': 'amortised_cost',
                        'hold_collect_sell': 'fvoci_debt',
                        'other': 'fvtpl',
                    }[case['business_model']]
                self.assertEqual(item.sppi_pass, expected_sppi)
                self.assertEqual(item.ifrs9_classification, expected)
                self.assertEqual(
                    item.routing,
                    'oci' if expected == 'fvoci_debt' else 'pl')

    def test_fvoci_equity_election_classification(self):
        # IFRS 9.5.7.5: a non-trading equity instrument with the irrevocable
        # election is FVOCI-equity and routes to OCI.
        item = self._item(instrument_type='equity',
                          fvoci_equity_election=True)
        self.assertEqual(item.ifrs9_classification, 'fvoci_equity')
        self.assertEqual(item.routing, 'oci')
        self.assertFalse(item.sppi_pass)

    def test_nonsense_combinations_blocked(self):
        # Election on a debt instrument (IFRS 9.5.7.5 is equity-only).
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._debt(fvoci_equity_election=True)
        # Election on an equity instrument held for trading.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._item(instrument_type='equity', fvoci_equity_election=True,
                       held_for_trading=True)
        # A financial liability classified as an equity instrument.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._item(nature='financial_liability',
                       instrument_type='equity',
                       balance_sheet_account_id=self.fv_liability.id)
        # A derivative carrying a legacy FVOCI-debt label.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._item(instrument_type='derivative',
                       fvoci_classification='fvoci_debt')
        # An instrument type on a non-financial item.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._item(nature='investment_property', instrument_type='debt')
        # A legacy label conflicting with the derived classification
        # (engine says FVTPL because the business model is other).
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._debt(business_model='other',
                       fvoci_classification='fvoci_debt')

    def test_routing_override_blocked(self):
        # Routing is derived from the classification; a raw write detaching
        # the two is refused by the constraint even in draft.
        item = self._debt(business_model='hold_collect_sell')
        self.assertEqual(item.routing, 'oci')
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            item.write({'routing': 'pl'})
        self.env.invalidate_all()
        self.assertEqual(item.routing, 'oci')

    def test_election_irrevocable_after_posting(self):
        # IFRS 9.5.7.5: the election cannot flip once an entry has been
        # posted under the elected treatment, even after a reset to draft.
        item = self._item(instrument_type='equity',
                          fvoci_equity_election=True, fair_value=1150.0)
        item.action_remeasure()
        item.action_reset_to_draft()
        with self.assertRaises(UserError):
            item.write({'fvoci_equity_election': False})

    def test_amortised_cost_not_remeasured(self):
        # IFRS 9.4.1.2: SPPI pass + hold-to-collect = amortised cost; the
        # item is not remeasured to fair value at all.
        item = self._debt(business_model='hold_to_collect',
                          fair_value=1100.0)
        self.assertEqual(item.ifrs9_classification, 'amortised_cost')
        with self.assertRaises(UserError):
            item.action_remeasure()
        self.assertFalse(item.move_ids)

    # ------------------------------------------------------------------
    # derecognition with atomic OCI settlement
    # ------------------------------------------------------------------

    def test_golden_derecognise_fvoci_debt_recycles_to_pl(self):
        """FVOCI-debt bought at 1,000, remeasured to 1,150 through OCI,
        derecognised at proceeds 1,150.

        The engine posts deltas only (prior_carrying is typed, not
        journalled), so the acquisition is seeded here:
            Dr investments 1,000 / Cr cash 1,000.
        Remeasurement: 1,150 - 1,000 = 150 gain to OCI:
            Dr investments 150 / Cr OCI reserve 150.
        Disposal at proceeds = carrying (1,150), no further gain:
            Dr cash 1,150 / Cr investments 1,150.
        Recycling (IFRS 9.5.7.10): the accumulated reserve of 150 is
        reclassified to profit or loss:
            Dr OCI reserve 150 / Cr gain 150.
        Lifetime P&L = proceeds 1,150 - cost 1,000 = 150 credit; the
        investments account nets to nil (1,000 + 150 - 1,150).
        """
        self.post_balanced_move([
            {'account': self.fv_asset, 'debit': 1000.0,
             'name': 'acquisition'},
            {'account': self.account_cash, 'credit': 1000.0,
             'name': 'acquisition'},
        ])
        item = self._debt(business_model='hold_collect_sell',
                          prior_carrying=1000.0, fair_value=1150.0)
        self.assertEqual(item.ifrs9_classification, 'fvoci_debt')
        item.action_remeasure()
        remeasure = item.move_ids
        self.assertMoveLines(remeasure, [
            (self.fv_asset, 150.0, 0.0),
            (self.fv_oci, 0.0, 150.0),
        ])
        self.assertAlmostEqual(item.oci_reserve_balance, -150.0, places=2)
        # Mirror the form-button flow: the reviewer enters both accounting
        # inputs on the item, then invokes the action without RPC arguments.
        item.write({
            'disposal_proceeds': 1150.0,
            'disposal_date': self._DERECOGNITION_DATE,
        })
        item.action_derecognise()
        new_moves = (item.move_ids - remeasure).sorted('id')
        self.assertEqual(len(new_moves), 2)
        disposal, recycle = new_moves[0], new_moves[1]
        self.assertMoveLines(disposal, [
            (self.account_cash, 1150.0, 0.0),
            (self.fv_asset, 0.0, 1150.0),
        ])
        self.assertMoveLines(recycle, [
            (self.fv_oci, 150.0, 0.0),
            (self.fv_gain, 0.0, 150.0),
        ])
        for move in item.move_ids:
            self.assertBalanced(move)
        self.assertEqual(
            set(new_moves.mapped('date')),
            {fields.Date.to_date(self._DERECOGNITION_DATE)},
        )
        self.assertEqual(item.state, 'derecognised')
        self.assertEqual(
            item.derecognition_date,
            fields.Date.to_date(self._DERECOGNITION_DATE),
        )
        self.assertTrue(item.recycled)
        self.assertAlmostEqual(self.posted_balance(self.fv_oci), 0.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_gain), -150.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_asset), 0.0,
                               places=2)
        # The closed position is frozen: figures blocked, narrative open.
        with self.assertRaises(UserError):
            item.write({'fair_value': 2000.0})
        with self.assertRaises(UserError):
            item.action_remeasure()
        with self.assertRaises(UserError):
            item.action_cancel()
        with self.assertRaises(UserError):
            item.action_derecognise()
        with self.assertRaises(UserError):
            item.unlink()
        item.notes = 'disposed'

    def test_golden_derecognise_fvoci_debt_above_carrying(self):
        """Same instrument, sold for 1,200 against a carrying of 1,150.

        Disposal: proceeds 1,200 - carrying 1,150 = 50 gain to P&L
        (IFRS 9.3.2.12): Dr cash 1,200 / Cr investments 1,150 / Cr gain 50.
        Recycling: reserve 150 to P&L: Dr OCI 150 / Cr gain 150.
        Lifetime P&L = 1,200 - 1,000 = 200 credit.
        """
        item = self._debt(business_model='hold_collect_sell',
                          prior_carrying=1000.0, fair_value=1150.0)
        item.action_remeasure()
        remeasure = item.move_ids
        item.action_derecognise(
            proceeds=1200.0, accounting_date=self._DERECOGNITION_DATE)
        disposal, recycle = (item.move_ids - remeasure).sorted('id')
        self.assertMoveLines(disposal, [
            (self.account_cash, 1200.0, 0.0),
            (self.fv_asset, 0.0, 1150.0),
            (self.fv_gain, 0.0, 50.0),
        ])
        self.assertMoveLines(recycle, [
            (self.fv_oci, 150.0, 0.0),
            (self.fv_gain, 0.0, 150.0),
        ])
        self.assertAlmostEqual(self.posted_balance(self.fv_gain), -200.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_oci), 0.0,
                               places=2)

    def test_golden_derecognise_fvoci_equity_to_retained_earnings(self):
        """FVOCI equity election: 1,000 cost, remeasured to 1,150 through
        OCI, derecognised at 1,150. The reserve of 150 transfers within
        equity to retained earnings (IFRS 9.B5.7.1); profit or loss is
        never touched, so no journal line of this item may hit the P&L
        account.

        Remeasurement: Dr investments 150 / Cr OCI 150.
        Disposal: Dr cash 1,150 / Cr investments 1,150.
        Transfer: Dr OCI 150 / Cr retained earnings 150.
        """
        item = self._item(instrument_type='equity',
                          fvoci_equity_election=True,
                          prior_carrying=1000.0, fair_value=1150.0)
        item.action_remeasure()
        remeasure = item.move_ids
        self.assertMoveLines(remeasure, [
            (self.fv_asset, 150.0, 0.0),
            (self.fv_oci, 0.0, 150.0),
        ])
        item.action_derecognise(
            proceeds=1150.0, accounting_date=self._DERECOGNITION_DATE)
        disposal, transfer = (item.move_ids - remeasure).sorted('id')
        self.assertMoveLines(disposal, [
            (self.account_cash, 1150.0, 0.0),
            (self.fv_asset, 0.0, 1150.0),
        ])
        self.assertMoveLines(transfer, [
            (self.fv_oci, 150.0, 0.0),
            (self.fv_retained, 0.0, 150.0),
        ])
        # Never through profit or loss: no line of any of the item's moves
        # touches the P&L account, and its posted balance is untouched.
        self.assertFalse(item.move_ids.line_ids.filtered(
            lambda line: line.account_id == self.fv_gain))
        self.assertAlmostEqual(self.posted_balance(self.fv_gain), 0.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_retained),
                               -150.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_oci), 0.0,
                               places=2)
        self.assertEqual(item.state, 'derecognised')
        self.assertTrue(item.recycled)

    def test_golden_fvoci_equity_final_uplift_stays_in_oci(self):
        """Equity election sold above carrying: the last uplift is a
        fair-value change and belongs in OCI, never P&L.

        Carrying 1,150 (after the 150 remeasurement), proceeds 1,250:
        uplift 1,250 - 1,150 = 100 to OCI in the disposal entry:
            Dr cash 1,250 / Cr investments 1,150 / Cr OCI 100.
        Reserve after disposal = 150 + 100 = 250, transferred to retained
        earnings: Dr OCI 250 / Cr retained earnings 250.
        Retained earnings = 250 = proceeds 1,250 - cost 1,000; P&L nil.
        """
        item = self._item(instrument_type='equity',
                          fvoci_equity_election=True,
                          prior_carrying=1000.0, fair_value=1150.0)
        item.action_remeasure()
        remeasure = item.move_ids
        item.action_derecognise(
            proceeds=1250.0, accounting_date=self._DERECOGNITION_DATE)
        disposal, transfer = (item.move_ids - remeasure).sorted('id')
        self.assertMoveLines(disposal, [
            (self.account_cash, 1250.0, 0.0),
            (self.fv_asset, 0.0, 1150.0),
            (self.fv_oci, 0.0, 100.0),
        ])
        self.assertMoveLines(transfer, [
            (self.fv_oci, 250.0, 0.0),
            (self.fv_retained, 0.0, 250.0),
        ])
        self.assertFalse(item.move_ids.line_ids.filtered(
            lambda line: line.account_id == self.fv_gain))
        self.assertAlmostEqual(self.posted_balance(self.fv_retained),
                               -250.0, places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_oci), 0.0,
                               places=2)

    def test_golden_derecognise_fvtpl_nothing_to_recycle(self):
        """FVTPL debt (business model other): the 150 gain went straight to
        P&L, so derecognition at carrying posts only the disposal entry.

        Remeasurement: Dr investments 150 / Cr gain 150.
        Disposal at 1,150 = carrying: Dr cash 1,150 / Cr investments 1,150.
        Exactly two entries in total; nothing recycled.
        """
        item = self._debt(business_model='other',
                          prior_carrying=1000.0, fair_value=1150.0)
        self.assertEqual(item.ifrs9_classification, 'fvtpl')
        self.assertEqual(item.routing, 'pl')
        item.action_remeasure()
        item.action_derecognise(
            proceeds=1150.0, accounting_date=self._DERECOGNITION_DATE)
        self.assertEqual(len(item.move_ids), 2)
        disposal = item.move_ids.sorted('id')[1]
        self.assertMoveLines(disposal, [
            (self.account_cash, 1150.0, 0.0),
            (self.fv_asset, 0.0, 1150.0),
        ])
        self.assertEqual(item.state, 'derecognised')
        self.assertFalse(item.recycled)
        self.assertAlmostEqual(self.posted_balance(self.fv_gain), -150.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_oci), 0.0,
                               places=2)

    def test_golden_derecognise_liability_settled_below_carrying(self):
        """A held-for-trading financial liability carried at 1,200 is
        settled for 1,100 cash: the 100 difference is a gain
        (IFRS 9.3.3.3).

        The engine posts deltas only (prior_carrying is typed, not
        journalled), so the opening 1,000 obligation is seeded here:
            Dr cash 1,000 / Cr liability 1,000.
        Remeasurement 1,000 -> 1,200 (a rise in a liability is a loss):
            Dr loss 200 / Cr liability 200.
        Settlement: Dr liability 1,200 / Cr cash 1,100 / Cr gain 100.
        The liability account nets to nil (-1,000 - 200 + 1,200).
        """
        self.post_balanced_move([
            {'account': self.account_cash, 'debit': 1000.0,
             'name': 'issue'},
            {'account': self.fv_liability, 'credit': 1000.0,
             'name': 'issue'},
        ])
        item = self._item(nature='financial_liability',
                          instrument_type='debt', held_for_trading=True,
                          balance_sheet_account_id=self.fv_liability.id,
                          prior_carrying=1000.0, fair_value=1200.0)
        self.assertEqual(item.ifrs9_classification, 'fvtpl')
        item.action_remeasure()
        remeasure = item.move_ids
        self.assertMoveLines(remeasure, [
            (self.fv_gain, 200.0, 0.0),
            (self.fv_liability, 0.0, 200.0),
        ])
        item.action_derecognise(
            proceeds=1100.0, accounting_date=self._DERECOGNITION_DATE)
        settlement = item.move_ids - remeasure
        self.assertMoveLines(settlement, [
            (self.fv_liability, 1200.0, 0.0),
            (self.account_cash, 0.0, 1100.0),
            (self.fv_gain, 0.0, 100.0),
        ])
        # P&L: loss 200 debit less gain 100 credit = 100 net debit.
        self.assertAlmostEqual(self.posted_balance(self.fv_gain), 100.0,
                               places=2)
        self.assertAlmostEqual(self.posted_balance(self.fv_liability), 0.0,
                               places=2)

    def test_derecognise_guards(self):
        # Must be measured first (carrying and reserve current).
        draft = self._debt()
        with self.assertRaises(UserError):
            draft.action_derecognise(proceeds=1000.0)
        # Manager gate.
        item = self._debt(fair_value=1150.0)
        item.action_remeasure()
        plain = self.env['res.users'].create({
            'name': 'fv plain', 'login': 'fv_plain_ifrs9@test',
            'email': 'fv_plain_ifrs9@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            item.with_user(plain).action_derecognise(proceeds=1150.0)
        # No wall-clock fallback: an explicit reviewable accounting date is
        # mandatory and a failed gate posts nothing or changes no evidence.
        moves_before = item.move_ids
        with self.assertRaisesRegex(UserError, 'explicit derecognition'):
            item.action_derecognise(proceeds=1150.0)
        self.assertEqual(item.state, 'measured')
        self.assertEqual(item.move_ids, moves_before)
        self.assertFalse(item.derecognition_date)
        # Disposal cannot be inserted before the sealed measurement that
        # established the carrying value and reserve being settled.
        with self.assertRaisesRegex(UserError, 'latest posted fair-value'):
            item.action_derecognise(
                proceeds=1150.0, accounting_date='2026-03-30')
        self.assertEqual(item.move_ids, moves_before)
        with self.assertRaisesRegex(UserError, 'must match.*measurement'):
            item.action_derecognise(
                proceeds=1150.0, accounting_date='2026-05-01')
        self.assertEqual(item.move_ids, moves_before)
        # Negative proceeds refused.
        with self.assertRaises(UserError):
            item.action_derecognise(
                proceeds=-5.0, accounting_date=self._DERECOGNITION_DATE)
        # An OCI reserve without any classification cannot be settled: the
        # engine cannot know whether it recycles or transfers.
        legacy = self._item(routing='oci', fair_value=1300.0)
        legacy.action_remeasure()
        with self.assertRaises(UserError):
            legacy.action_derecognise(
                proceeds=1300.0,
                accounting_date=self._DERECOGNITION_DATE)

    # ------------------------------------------------------------------
    # Level 3 reconciliation: tie enforcement and ledger-fed gains
    # ------------------------------------------------------------------

    def test_l3_close_blocked_when_untied(self):
        # Closing 1,000 (opening only) vs fair value 999: off by 1, blocked.
        item = self._item(level='3', fair_value=999.0)
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id, 'opening_balance': 1000.0})
        self.assertAlmostEqual(rf.closing_balance, 1000.0, places=2)
        with self.assertRaises(UserError):
            rf.action_close()
        self.assertEqual(rf.state, 'draft')

    def test_l3_close_when_tied_then_frozen(self):
        # Opening 1,000 + PL 150 + OCI 50 + purchases 400 + issues 20
        # - sales 300 - settlements 30 + in 200 - out 90 = 1,400 = FV.
        item = self._item(level='3', fair_value=1400.0)
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'opening_balance': 1000.0,
            'gains_losses_in_pl': 150.0,
            'gains_losses_in_oci': 50.0,
            'purchases': 400.0,
            'issues': 20.0,
            'sales': 300.0,
            'settlements': 30.0,
            'transfers_into_level3': 200.0,
            'transfers_out_of_level3': 90.0,
        })
        rf.action_close()
        self.assertEqual(rf.state, 'closed')
        # Frozen: movements, deletion and re-pulling all blocked.
        with self.assertRaises(UserError):
            rf.write({'purchases': 500.0})
        with self.assertRaises(UserError):
            rf.unlink()
        with self.assertRaises(UserError):
            rf.action_pull_ledger()
        # Reopen lifts the freeze.
        rf.action_reopen()
        self.assertEqual(rf.state, 'draft')
        rf.write({'notes': 'reopened'})

    def test_l3_close_requires_manager(self):
        item = self._item(level='3', fair_value=1000.0)
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id, 'opening_balance': 1000.0})
        plain = self.env['res.users'].create({
            'name': 'fv plain2', 'login': 'fv_plain_ifrs9b@test',
            'email': 'fv_plain_ifrs9b@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            rf.with_user(plain).action_close()

    def test_golden_l3_ledger_fed_gains(self):
        """The gains columns are fed from the posted ledger, not typed.

        A Level 3 item carried at 1,000 is remeasured to 1,150 through P&L
        on 2026-03-15: the posted entry credits the gain account 150. The
        reconciliation is created with a deliberately wrong typed figure
        (999); pulling the ledger links the entry and overwrites the column
        with the ledger amount 150, so closing = 1,000 + 150 = 1,150 = fair
        value and the period can close.
        """
        item = self._item(level='3', routing='pl',
                          prior_carrying=1000.0, fair_value=1150.0,
                          measurement_date='2026-03-15')
        item.action_remeasure()
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'opening_balance': 1000.0,
            'gains_losses_in_pl': 999.0,
        })
        # Typed value stands while nothing is linked.
        self.assertAlmostEqual(rf.gains_losses_in_pl, 999.0, places=2)
        rf.action_pull_ledger()
        self.assertEqual(rf.move_ids, item.move_ids)
        self.assertAlmostEqual(rf.gains_losses_in_pl, 150.0, places=2)
        self.assertAlmostEqual(rf.gains_losses_in_oci, 0.0, places=2)
        self.assertAlmostEqual(rf.closing_balance, 1150.0, places=2)
        self.assertTrue(item.ties_to_fair_value)
        rf.action_close()
        self.assertEqual(rf.state, 'closed')

    def test_golden_l3_ledger_fed_oci_gains(self):
        """Same mechanism for an OCI-routed instrument: an FVOCI-debt item
        remeasured 1,000 -> 1,150 credits OCI 150; after the pull the OCI
        column reads 150 from the ledger and the P&L column reads 0.
        """
        item = self._debt(business_model='hold_collect_sell', level='3',
                          prior_carrying=1000.0, fair_value=1150.0,
                          measurement_date='2026-06-30')
        item.action_remeasure()
        rf = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'opening_balance': 1000.0,
        })
        rf.action_pull_ledger()
        self.assertAlmostEqual(rf.gains_losses_in_oci, 150.0, places=2)
        self.assertAlmostEqual(rf.gains_losses_in_pl, 0.0, places=2)
        self.assertAlmostEqual(rf.closing_balance, 1150.0, places=2)

    # ------------------------------------------------------------------
    # sensitivity engine (IFRS 13.93(h))
    # ------------------------------------------------------------------

    def test_golden_sensitivity_linear(self):
        """delta = fair value x shock% x factor, literal:

        10,000 x 5 / 100 x 1.0 = 500.00
        10,000 x -5 / 100 x 1.0 = -500.00
        10,000 x 5 / 100 x 2.0 = 1,000.00
        """
        item = self._item(level='3', fair_value=10000.0)
        sens = self.env['eh.fair.value.sensitivity']
        up = sens.create({'item_id': item.id, 'input_name': 'Discount rate',
                          'shock_pct': 5.0})
        self.assertAlmostEqual(up.value_delta, 500.0, places=2)
        down = sens.create({'item_id': item.id,
                            'input_name': 'Discount rate',
                            'shock_pct': -5.0})
        self.assertAlmostEqual(down.value_delta, -500.0, places=2)
        scaled = sens.create({'item_id': item.id,
                              'input_name': 'Terminal growth',
                              'shock_pct': 5.0,
                              'sensitivity_factor': 2.0})
        self.assertAlmostEqual(scaled.value_delta, 1000.0, places=2)
        # The delta tracks the measurement: 20,000 x 5% x 1 = 1,000.
        item.fair_value = 20000.0
        self.assertAlmostEqual(up.value_delta, 1000.0, places=2)

    def test_sensitivity_zero_shock_blocked(self):
        item = self._item(level='3', fair_value=10000.0)
        with self.assertRaises(Exception), self.env.cr.savepoint():
            self.env['eh.fair.value.sensitivity'].create({
                'item_id': item.id, 'input_name': 'Discount rate',
                'shock_pct': 0.0})
            self.env.flush_all()
