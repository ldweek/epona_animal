# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 20 worked examples: non-monetary grants, condition gating,
and breach clawback accrual.

Every expected amount is hand-derived in a comment from the inputs stated
in the test; nothing is read back from the engine to build an expectation.

Conventions asserted here (read from models/gov_grant.py):

* Non-monetary grant (IAS 20.23): recognised at the fair value of the
  asset received. Receipt debits the received asset at fair value and
  credits deferred income (income approach) or the asset contra (netting
  approach); releases flow off the fair-value base.
* Condition gate (IAS 20.7/8): opt-in flag defer_until_conditions; when
  set, a release to income is refused while any registered condition is
  open or breached. Default off preserves prior behaviour.
* Breach clawback (IAS 20.32, prospective): the clawback first reverses
  the unamortised deferred income, the excess is charged immediately to
  profit or loss, and the full clawback is credited to a clawback
  liability until the Repay action settles it against cash.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_grants', 'post_install', '-at_install')
class TestGoldenIas20(EhGoldenTestCase):
    """IAS 20 golden worked examples and scenario sweep."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Posting is manager-gated. The group_ids / groups_id field split
        # across Odoo series is resolved at runtime for backport parity.
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})
        cls.deferred = cls._ensure_account(
            cls.env, '2600', 'Deferred Grant Income', 'liability_current')
        cls.grant_income = cls._ensure_account(
            cls.env, '4650', 'Grant Income', 'income_other')
        cls.asset = cls._ensure_account(
            cls.env, '1650', 'Grant-funded Asset', 'asset_non_current')
        cls.received_asset = cls._ensure_account(
            cls.env, '1660', 'Granted Asset at Fair Value',
            'asset_non_current')
        cls.repay_expense = cls._ensure_account(
            cls.env, '5650', 'Grant Repayment Expense', 'expense')
        cls.clawback_liab = cls._ensure_account(
            cls.env, '2650', 'Grant Clawback Liability', 'liability_current')
        cls.grantor = cls.env['res.partner'].create({
            'name': 'Golden Government Grantor',
        })

    def _grant(self, **vals):
        base = {
            'name': '/', 'grant_type': 'income_related', 'amount': 1200.0,
            'cash_account_id': self.account_cash.id,
            'deferred_income_account_id': self.deferred.id,
            'grant_income_account_id': self.grant_income.id,
            'asset_account_id': self.asset.id,
            'received_asset_account_id': self.received_asset.id,
            'repayment_expense_account_id': self.repay_expense.id,
            'clawback_liability_account_id': self.clawback_liab.id,
            'journal_id': self.journal_misc.id,
            'grantor_id': self.grantor.id,
            'agreement_reference': 'GOLDEN-IAS20-AWARD',
        }
        base.update(vals)
        return self.env['eh.gov.grant'].create(base)

    # ------------------------------------------------------------------
    # non-monetary grants (IAS 20.23)
    # ------------------------------------------------------------------
    def test_golden_non_monetary_receipt_and_release(self):
        """A government transfers land with a fair value of 250,000,
        released over 5 years on the deferred-income basis.

        Receipt (IAS 20.23, fair value of the asset received):
          Dr granted asset 250,000 / Cr deferred income 250,000.
        Year 1 of 5 straight-line release: 250,000 / 5 = 50,000
          Dr deferred income 50,000 / Cr grant income 50,000.
        After year 1: recognised 50,000, remaining 250,000 - 50,000
        = 200,000.
        """
        g = self._grant(grant_kind='non_monetary', amount=0.0,
                        asset_fair_value=250000.0)
        g.action_receive()
        self.assertEqual(g.state, 'received')
        self.assertEqual(len(g.move_ids), 1)
        self.assertMoveLines(g.move_ids, [
            (self.received_asset, 250000.0, 0.0),
            (self.deferred, 0.0, 250000.0),
        ])
        self.assertBalanced(g.move_ids)
        # The fair value, not the (zero) cash amount, is the base.
        self.assertAlmostEqual(g.remaining, 250000.0, places=2)

        g.amortise_amount = 50000.0
        g.action_amortise()
        release = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(release, [
            (self.deferred, 50000.0, 0.0),
            (self.grant_income, 0.0, 50000.0),
        ])
        self.assertAlmostEqual(g.recognised_amount, 50000.0, places=2)
        self.assertAlmostEqual(g.remaining, 200000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.received_asset), 250000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.deferred), -200000.0, places=2)

    def test_golden_non_monetary_netting(self):
        """Non-monetary asset-related grant on the netting basis.

        Fair value 250,000; IAS 20.24 alternative presentation deducts the
        grant from the asset's carrying amount:
          Dr granted asset 250,000 / Cr asset (contra) 250,000.
        Fully recognised through the reduced carrying amount on receipt;
        the lifecycle closes with no deferred income.
        """
        g = self._grant(grant_kind='non_monetary', amount=0.0,
                        asset_fair_value=250000.0,
                        grant_type='asset_related',
                        asset_approach='deduct_asset')
        g.action_receive()
        self.assertEqual(g.state, 'closed')
        self.assertEqual(len(g.move_ids), 1)
        self.assertMoveLines(g.move_ids, [
            (self.received_asset, 250000.0, 0.0),
            (self.asset, 0.0, 250000.0),
        ])
        self.assertAlmostEqual(g.remaining, 0.0, places=2)

    def test_non_monetary_requires_fair_value_and_account(self):
        # No fair value: the measurement base of a non-monetary grant is
        # the asset's fair value (IAS 20.23), so receipt is refused.
        g = self._grant(grant_kind='non_monetary', amount=0.0,
                        asset_fair_value=0.0)
        with self.assertRaises(UserError):
            g.action_receive()
        # Fair value but no received-asset account: nothing to debit.
        g2 = self._grant(grant_kind='non_monetary', amount=0.0,
                         asset_fair_value=1000.0,
                         received_asset_account_id=False)
        with self.assertRaises(UserError):
            g2.action_receive()

    def test_non_monetary_base_frozen_after_receipt(self):
        # The fair value is the measurement base; once recognition begins
        # it is frozen like the monetary amount.
        g = self._grant(grant_kind='non_monetary', amount=0.0,
                        asset_fair_value=1000.0)
        g.action_receive()
        with self.assertRaises(UserError):
            g.asset_fair_value = 2000.0
        with self.assertRaises(UserError):
            g.grant_kind = 'monetary'

    # ------------------------------------------------------------------
    # condition gate (IAS 20.7/8)
    # ------------------------------------------------------------------
    def test_condition_gate_blocks_then_releases(self):
        """Grant 100,000 with one attached condition and the deferral flag.

        While the condition is open the release is refused (IAS 20.7/8:
        recognition only on reasonable assurance of compliance). Once the
        condition is fulfilled the release posts normally:
          Dr deferred income 20,000 / Cr grant income 20,000.
        """
        g = self._grant(amount=100000.0, defer_until_conditions=True,
                        condition_ids=[(0, 0, {
                            'name': 'Employ 20 apprentices',
                            'due_date': '2027-06-30'})])
        g.action_receive()
        g.amortise_amount = 20000.0
        with self.assertRaises(UserError):
            g.action_amortise()
        cond = g.condition_ids
        cond.action_fulfil()
        self.assertEqual(cond.state, 'fulfilled')
        self.assertTrue(cond.fulfilled_date)
        g.action_amortise()
        release = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(release, [
            (self.deferred, 20000.0, 0.0),
            (self.grant_income, 0.0, 20000.0),
        ])
        self.assertAlmostEqual(g.recognised_amount, 20000.0, places=2)

    def test_condition_gate_off_by_default(self):
        # Default behaviour preserved: an open condition without the
        # deferral flag does not block the release.
        g = self._grant(amount=1000.0, condition_ids=[(0, 0, {
            'name': 'Report annually'})])
        self.assertFalse(g.defer_until_conditions)
        g.action_receive()
        g.amortise_amount = 100.0
        g.action_amortise()
        self.assertAlmostEqual(g.recognised_amount, 100.0, places=2)

    def test_reopened_condition_blocks_again(self):
        g = self._grant(amount=1000.0, defer_until_conditions=True,
                        condition_ids=[(0, 0, {'name': 'Milestone 1'})])
        g.action_receive()
        cond = g.condition_ids
        cond.action_fulfil()
        g.amortise_amount = 100.0
        g.action_amortise()
        cond.action_reopen()
        self.assertEqual(cond.state, 'open')
        self.assertFalse(cond.fulfilled_date)
        g.amortise_amount = 100.0
        with self.assertRaises(UserError):
            g.action_amortise()

    # ------------------------------------------------------------------
    # breach clawback (IAS 20.32)
    # ------------------------------------------------------------------
    def test_golden_breach_clawback_accrual_and_settlement(self):
        """Grant 250,000 received; 100,000 released; the condition is then
        breached with a clawback of 200,000 repayable.

        Position at breach: deferred income remaining
        = 250,000 - 100,000 = 150,000.

        Accrual (IAS 20.32, prospective; cash not yet paid):
          reverse deferred first: min(200,000, 150,000) = 150,000
          excess to profit or loss: 200,000 - 150,000 = 50,000
          Dr deferred income   150,000
          Dr repayment expense  50,000
          Cr clawback liability 200,000
        The liability stands at 200,000 until settled; no further income
        may be released (remaining = 250,000 - 100,000 - 150,000 = 0).

        Settlement via the existing Repay action:
          Dr clawback liability 200,000 / Cr cash 200,000.
        Cash: +250,000 receipt - 200,000 settlement = 50,000.
        """
        g = self._grant(amount=250000.0, clawback_amount=200000.0,
                        condition_ids=[(0, 0, {
                            'name': 'Operate the facility 5 years'})])
        g.action_receive()
        g.amortise_amount = 100000.0
        g.action_amortise()
        self.assertAlmostEqual(g.remaining, 150000.0, places=2)

        g.condition_ids.action_breach()
        self.assertEqual(g.condition_ids.state, 'breached')
        self.assertEqual(len(g.move_ids), 3)
        accrual = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(accrual, [
            (self.deferred, 150000.0, 0.0),
            (self.repay_expense, 50000.0, 0.0),
            (self.clawback_liab, 0.0, 200000.0),
        ])
        self.assertBalanced(accrual)
        self.assertAlmostEqual(g.clawback_accrued, 200000.0, places=2)
        self.assertAlmostEqual(g.remaining, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.clawback_liab), -200000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.deferred), 0.0, places=2)

        # Repayable: no further release while the clawback is outstanding.
        g.amortise_amount = 1000.0
        with self.assertRaises(UserError):
            g.action_amortise()
        self.assertEqual(g.state, 'repayable')

        g.action_repay()
        self.assertEqual(g.state, 'repaid')
        self.assertAlmostEqual(g.clawback_accrued, 0.0, places=2)
        settlement = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(settlement, [
            (self.clawback_liab, 200000.0, 0.0),
            (self.account_cash, 0.0, 200000.0),
        ])
        self.assertAlmostEqual(
            self.posted_balance(self.clawback_liab), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.account_cash), 50000.0, places=2)

    def test_golden_breach_clawback_within_deferred(self):
        """Clawback smaller than the unamortised deferred income.

        Grant 1,000 received, nothing released; clawback 400.
        Accrual: reverse deferred min(400, 1,000) = 400, no excess:
          Dr deferred income 400 / Cr clawback liability 400.
        Remaining afterwards = 1,000 - 0 - 400 = 600; expense untouched.
        """
        g = self._grant(amount=1000.0, clawback_amount=400.0,
                        condition_ids=[(0, 0, {'name': 'Keep 10 staff'})])
        g.action_receive()
        g.condition_ids.action_breach()
        accrual = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(accrual, [
            (self.deferred, 400.0, 0.0),
            (self.clawback_liab, 0.0, 400.0),
        ])
        self.assertAlmostEqual(g.remaining, 600.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.repay_expense), 0.0, places=2)
        g.action_repay()
        self.assertEqual(g.state, 'received')
        self.assertAlmostEqual(g.remaining, 600.0, places=2)
        self.assertAlmostEqual(g.clawback_settled, 400.0, places=2)
        with self.assertRaises(UserError):
            g.action_accrue_clawback()

    def test_fully_amortised_grant_later_breach_posts_excess_and_settles(self):
        """IAS 20.32 applies after income recognition is complete.

        Grant 1,000 is fully amortised, leaving no deferred credit. A later
        400 clawback is therefore entirely current-period expense, followed by
        liability settlement. Old code treated ``closed`` as terminal and
        posted neither entry.
        """
        g = self._grant(
            amount=1000.0,
            clawback_amount=400.0,
            condition_ids=[(0, 0, {'name': 'Operate for five years'})],
        )
        g.action_receive()
        g.amortise_amount = 1000.0
        g.action_amortise()
        self.assertEqual(g.state, 'closed')
        self.assertAlmostEqual(g.remaining, 0.0, places=2)

        g.condition_ids.action_breach()
        self.assertEqual(g.state, 'repayable')
        accrual = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(accrual, [
            (self.repay_expense, 400.0, 0.0),
            (self.clawback_liab, 0.0, 400.0),
        ])
        self.assertAlmostEqual(g.clawback_accrued, 400.0, places=2)

        g.action_repay()
        self.assertEqual(g.state, 'repaid')
        settlement = g.move_ids.sorted('id')[-1]
        self.assertMoveLines(settlement, [
            (self.clawback_liab, 400.0, 0.0),
            (self.account_cash, 0.0, 400.0),
        ])
        self.assertAlmostEqual(g.clawback_settled, 400.0, places=2)

    def test_breach_requires_clawback_amount_and_account(self):
        g = self._grant(amount=1000.0, clawback_amount=0.0,
                        condition_ids=[(0, 0, {'name': 'C1'})])
        g.action_receive()
        with self.assertRaises(UserError):
            g.condition_ids.action_breach()
        g2 = self._grant(amount=1000.0, clawback_amount=500.0,
                         clawback_liability_account_id=False,
                         condition_ids=[(0, 0, {'name': 'C2'})])
        g2.action_receive()
        with self.assertRaises(UserError):
            g2.condition_ids.action_breach()

    def test_breach_on_draft_grant_only_marks_register(self):
        # Nothing recognised yet, so nothing accrues; the register and
        # chatter still carry the breach.
        g = self._grant(amount=1000.0, clawback_amount=500.0,
                        condition_ids=[(0, 0, {'name': 'C3'})])
        g.condition_ids.action_breach()
        self.assertEqual(g.condition_ids.state, 'breached')
        self.assertFalse(g.move_ids)
        self.assertAlmostEqual(g.clawback_accrued, 0.0, places=2)

    def test_clawback_figures_frozen(self):
        g = self._grant(amount=1000.0, clawback_amount=400.0,
                        condition_ids=[(0, 0, {'name': 'C4'})])
        g.action_receive()
        # Posted-figure freeze: raw ORM writes on the clawback figures of a
        # received grant are refused.
        with self.assertRaises(UserError):
            g.clawback_accrued = 123.0
        with self.assertRaises(UserError):
            g.deferred_reversed = 123.0
        g.condition_ids.action_breach()
        # The settlement must debit the account carrying the accrual.
        with self.assertRaises(UserError):
            g.clawback_liability_account_id = self.deferred.id
        # A second accrual on the same grant is refused.
        with self.assertRaises(UserError):
            g.action_accrue_clawback()

    def test_breached_condition_cannot_be_fulfilled(self):
        g = self._grant(amount=1000.0, clawback_amount=400.0,
                        condition_ids=[(0, 0, {'name': 'C5'})])
        g.action_receive()
        g.condition_ids.action_breach()
        with self.assertRaises(UserError):
            g.condition_ids.action_fulfil()

    # ------------------------------------------------------------------
    # pairwise sweep: kind x presentation x deferral gate
    # ------------------------------------------------------------------
    def test_pairwise_kind_presentation_gate(self):
        """Receipt JE shape and release gating across the scenario axes.

        Base 1,000 for every case (cash amount or asset fair value).
        Expected receipt entry, by hand:
          debit leg:  cash 1,000 (monetary) / received asset 1,000
                      (non-monetary, IAS 20.23 fair value)
          credit leg: deferred income 1,000 (deferred presentation)
                      / asset contra 1,000 (netting, IAS 20.24)
        Release of 100 afterwards:
          netting: refused (recognised in full on receipt, IAS 20.27)
          deferred + gate on with an open condition: refused (IAS 20.7/8)
          deferred otherwise: Dr deferred 100 / Cr grant income 100.
        """
        axes = {
            'kind': ['monetary', 'non_monetary'],
            'presentation': ['deferred', 'netting'],
            'gate': ['off', 'on'],
        }
        for case in pairwise_cases(axes):
            vals = {
                'grant_kind': case['kind'],
                'defer_until_conditions': case['gate'] == 'on',
                'condition_ids': [(0, 0, {'name': 'Sweep condition'})],
            }
            if case['kind'] == 'non_monetary':
                vals.update(amount=0.0, asset_fair_value=1000.0)
            else:
                vals.update(amount=1000.0)
            if case['presentation'] == 'netting':
                vals.update(grant_type='asset_related',
                            asset_approach='deduct_asset')
            g = self._grant(**vals)
            g.action_receive()

            debit_account = (self.received_asset
                             if case['kind'] == 'non_monetary'
                             else self.account_cash)
            credit_account = (self.asset
                              if case['presentation'] == 'netting'
                              else self.deferred)
            self.assertMoveLines(
                g.move_ids.sorted('id')[0],
                [(debit_account, 1000.0, 0.0),
                 (credit_account, 0.0, 1000.0)],
                msg='receipt mismatch for %s' % case)

            g.amortise_amount = 100.0
            if case['presentation'] == 'netting':
                # Closed in full on receipt; no deferred income to release.
                self.assertEqual(g.state, 'closed',
                                 'netting must close for %s' % case)
                g.state = 'received'  # exercise the guard directly
                with self.assertRaises(UserError):
                    g.action_amortise()
            elif case['gate'] == 'on':
                with self.assertRaises(UserError):
                    g.action_amortise()
            else:
                g.action_amortise()
                release = g.move_ids.sorted('id')[-1]
                self.assertMoveLines(
                    release,
                    [(self.deferred, 100.0, 0.0),
                     (self.grant_income, 0.0, 100.0)],
                    msg='release mismatch for %s' % case)
