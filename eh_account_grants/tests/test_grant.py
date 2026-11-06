# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 20 government grant tests."""

from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_grants', 'integration', 'post_install', '-at_install')
class TestGrant(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.deferred = cls._ensure_account(
            cls.env, '2600', 'Deferred Grant Income', 'liability_current')
        cls.grant_income = cls._ensure_account(
            cls.env, '4650', 'Grant Income', 'income_other')
        cls.asset = cls._ensure_account(
            cls.env, '1650', 'Grant-funded Asset', 'asset_non_current')
        cls.repay_expense = cls._ensure_account(
            cls.env, '5650', 'Grant Repayment Expense', 'expense')
        cls.clawback_liab = cls._ensure_account(
            cls.env, '2650', 'Grant Clawback Liability', 'liability_current')
        cls.grantor = cls.env['res.partner'].create({
            'name': 'Department of Industry',
        })

    def _grant(self, **vals):
        base = {
            'name': '/', 'grant_type': 'income_related', 'amount': 1200.0,
            'cash_account_id': self.account_cash.id,
            'deferred_income_account_id': self.deferred.id,
            'grant_income_account_id': self.grant_income.id,
            'asset_account_id': self.asset.id,
            'repayment_expense_account_id': self.repay_expense.id,
            'journal_id': self.journal_misc.id,
            'grantor_id': self.grantor.id,
            'agreement_reference': 'AWARD-2026-001',
        }
        base.update(vals)
        return self.env['eh.gov.grant'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_receive_deferred_income(self):
        g = self._grant(amount=1200.0)
        g.action_receive()
        self.assertEqual(g.state, 'received')
        self.assertAlmostEqual(self._bal(self.account_cash), 1200.0, places=2)
        self.assertAlmostEqual(self._bal(self.deferred), -1200.0, places=2)
        self.assertAlmostEqual(g.remaining, 1200.0, places=2)
        move = g.move_ids
        self.assertEqual(move.eh_gov_grant_id, g)
        with self.assertRaises(AccessError):
            move.write({'eh_gov_grant_id': False})

    def test_engine_anchors_cannot_be_seeded_or_forged(self):
        with self.assertRaises(AccessError):
            self.env['eh.gov.grant'].create({
                'amount': 1000.0, 'recognised_amount': 900.0,
            })
        with self.assertRaises(AccessError):
            self.env['eh.gov.grant'].create({
                'amount': 1000.0, 'remaining': 999999.0,
            })
        g = self._grant()
        with self.assertRaises(AccessError):
            g.write({'clawback_accrued': 500.0})
        with self.assertRaises(AccessError):
            g.write({'remaining': 999999.0})

    def test_condition_policy_rows_and_clawback_basis_freeze_on_receipt(self):
        g = self._grant(
            defer_until_conditions=True,
            clawback_amount=400.0,
            condition_ids=[(0, 0, {'name': 'Maintain employment'})],
        )
        condition = g.condition_ids
        g.action_receive()
        for values in (
                {'defer_until_conditions': False},
                {'clawback_amount': 1.0}):
            with self.assertRaises(UserError):
                g.write(values)
        with self.assertRaises(UserError):
            condition.write({'name': 'Rewritten after signature'})
        with self.assertRaises(UserError):
            self.env['eh.gov.grant.condition'].create({
                'grant_id': g.id, 'name': 'Late-added condition',
            })

    def test_received_grant_recognised_amount_frozen(self):
        """recognised_amount is a posted figure (grant income to date). A raw
        ORM write on a received grant is refused, while action_amortise still
        moves it through the sanctioned, manager-gated path."""
        g = self._grant(amount=1200.0)
        g.action_receive()
        with self.assertRaises(UserError):
            g.recognised_amount = 500.0
        g.amortise_amount = 400.0
        g.action_amortise()
        self.assertAlmostEqual(g.recognised_amount, 400.0, places=2)

    def test_amortise_to_income(self):
        g = self._grant(amount=1200.0)
        g.action_receive()
        g.amortise_amount = 100.0
        g.action_amortise()
        self.assertAlmostEqual(g.recognised_amount, 100.0, places=2)
        self.assertAlmostEqual(g.remaining, 1100.0, places=2)
        self.assertAlmostEqual(self._bal(self.deferred), -1100.0, places=2)
        self.assertAlmostEqual(self._bal(self.grant_income), -100.0, places=2)

    def test_amortise_closes_when_fully_released(self):
        g = self._grant(amount=200.0)
        g.action_receive()
        g.amortise_amount = 200.0
        g.action_amortise()
        self.assertEqual(g.state, 'closed')
        self.assertAlmostEqual(g.remaining, 0.0, places=2)

    def test_over_amortise_blocked(self):
        g = self._grant(amount=200.0)
        g.action_receive()
        g.amortise_amount = 300.0
        with self.assertRaises(UserError):
            g.action_amortise()

    def test_asset_netting_reduces_asset(self):
        g = self._grant(grant_type='asset_related',
                        asset_approach='deduct_asset', amount=500.0)
        g.action_receive()
        self.assertAlmostEqual(self._bal(self.account_cash), 500.0, places=2)
        self.assertAlmostEqual(self._bal(self.asset), -500.0, places=2)
        self.assertAlmostEqual(g.remaining, 0.0, places=2)
        # A netting grant is fully recognised on receipt and completes.
        self.assertEqual(g.state, 'closed')
        # A netting grant is not amortised separately.
        g.amortise_amount = 50.0
        with self.assertRaises(UserError):
            g.action_amortise()

    def test_netting_claim_matches_posted_behaviour(self):
        # IAS 20.17 honesty: the netting path deducts the grant from the
        # asset's carrying amount in full on receipt (IAS 20.27). It posts NO
        # depreciation or separate amortisation entry, so the amortise and
        # repay error messages must not claim recognition "through reduced
        # depreciation" over the asset life - a capability this record never
        # exercises. This test asserts the documented behaviour matches what
        # actually posts and pins the honest wording.
        g = self._grant(grant_type='asset_related',
                        asset_approach='deduct_asset', amount=500.0)
        g.action_receive()
        # What actually posts: a single receipt move, Dr cash / Cr asset for
        # the full amount. No later depreciation or amortisation entry exists.
        self.assertEqual(len(g.move_ids), 1)
        self.assertAlmostEqual(self._bal(self.asset), -500.0, places=2)
        self.assertEqual(g.recognised_amount, 0.0)
        self.assertEqual(g.state, 'closed')

        # The amortise block must describe the real behaviour (full deduction
        # on receipt), not a non-existent reduced-depreciation schedule.
        g2 = self._grant(grant_type='asset_related',
                         asset_approach='deduct_asset', amount=500.0)
        # Amortisation is only reachable from 'received'; force that state to
        # exercise the netting guard message directly.
        g2.action_receive()
        g2.state = 'received'
        g2.amortise_amount = 50.0
        with self.assertRaises(UserError) as cm:
            g2.action_amortise()
        msg = str(cm.exception)
        self.assertIn('carrying amount', msg)
        self.assertNotIn('recognised through reduced depreciation', msg)

        # The repay block likewise must not claim it "adjusts depreciation".
        g3 = self._grant(grant_type='asset_related',
                         asset_approach='deduct_asset', amount=500.0)
        g3.action_receive()
        g3.state = 'received'
        g3.repayment_amount = 100.0
        with self.assertRaises(UserError) as cm:
            g3.action_repay()
        rmsg = str(cm.exception)
        self.assertIn('carrying amount', rmsg)
        self.assertNotIn('adjusts the asset and depreciation directly', rmsg)

    def test_repay_within_deferred_income(self):
        # Repayment fully within the unamortised deferred income balance:
        # it reverses deferred income only, with no charge to profit or loss.
        g = self._grant(amount=1000.0)
        g.action_receive()
        g.amortise_amount = 200.0
        g.action_amortise()
        self.assertAlmostEqual(g.remaining, 800.0, places=2)
        g.repayment_amount = 500.0
        g.action_repay()
        self.assertEqual(g.state, 'received')
        self.assertAlmostEqual(g.remaining, 300.0, places=2)
        # 500 out of the 800 deferred income reversed; cash out 500.
        self.assertAlmostEqual(self._bal(self.deferred), -300.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), 500.0, places=2)
        # No excess, so nothing hits the repayment expense account.
        self.assertAlmostEqual(self._bal(self.repay_expense), 0.0, places=2)
        # Remaining deferred credit stays usable instead of becoming stranded
        # behind a terminal state.
        g.amortise_amount = 300.0
        g.action_amortise()
        self.assertEqual(g.state, 'closed')
        self.assertAlmostEqual(g.remaining, 0.0, places=2)
        for move in g.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_repay_exceeding_deferred_income_charges_pl(self):
        # Repayment exceeding the unamortised deferred income balance:
        # the excess is charged to profit or loss (IAS 20.32).
        g = self._grant(amount=1000.0)
        g.action_receive()
        g.amortise_amount = 300.0
        g.action_amortise()
        self.assertAlmostEqual(g.remaining, 700.0, places=2)
        g.repayment_amount = 900.0
        g.action_repay()
        self.assertEqual(g.state, 'repaid')
        # 700 deferred income reversed to nil, 200 excess to P&L, cash out 900.
        self.assertAlmostEqual(self._bal(self.deferred), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.repay_expense), 200.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), 100.0, places=2)
        for move in g.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_repay_requires_manager(self):
        g = self._grant(amount=1000.0)
        g.action_receive()
        g.repayment_amount = 100.0
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'grant_repay_plain@test',
            'email': 'grant_repay_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            g.with_user(user).action_repay()

    def test_real_manager_can_update_server_owned_grant_anchors(self):
        manager = new_test_user(
            self.env,
            login='grant_real_manager',
            groups=(
                'account.group_account_manager,'
                'eh_account_base.group_eh_manager'
            ),
        )
        grant = self._grant(amount=1000.0)
        actor = grant.with_user(manager).with_company(self.company)
        actor.action_receive()
        actor.write({'amortise_amount': 200.0})
        actor.action_amortise()
        self.assertAlmostEqual(grant.recognised_amount, 200.0, places=2)
        actor.write({'repayment_amount': 500.0})
        actor.action_repay()
        self.assertEqual(grant.state, 'received')
        self.assertAlmostEqual(grant.deferred_reversed, 500.0, places=2)
        self.assertAlmostEqual(grant.remaining, 300.0, places=2)

    def test_receive_requires_manager(self):
        g = self._grant(amount=1000.0)
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'grant_plain@test',
            'email': 'grant_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            g.with_user(user).action_receive()

    def test_amount_frozen_after_receipt(self):
        # IAS 20 integrity: once a grant is received, its amount and account
        # fields are frozen so deferred income cannot be re-based above what
        # was ever credited. Editing amount on a received grant is blocked.
        g = self._grant(amount=1000.0)
        g.action_receive()
        self.assertEqual(g.state, 'received')
        with self.assertRaises(UserError):
            g.write({'amount': 5000.0})
        with self.assertRaises(UserError):
            g.write({'deferred_income_account_id': self.grant_income.id})
        # A draft grant may still be edited freely.
        d = self._grant(amount=100.0)
        d.write({'amount': 250.0})
        self.assertAlmostEqual(d.amount, 250.0, places=2)
        # Workflow fields still write on a received grant (amortisation path).
        g.amortise_amount = 100.0
        g.action_amortise()
        self.assertAlmostEqual(g.recognised_amount, 100.0, places=2)

    def test_later_use_configuration_fills_once_then_freezes(self):
        g = self._grant(
            amount=1000.0,
            grant_income_account_id=False,
            repayment_expense_account_id=False,
            clawback_amount=0.0,
            clawback_liability_account_id=False,
            condition_ids=[(0, 0, {'name': 'Maintain output'})],
        )
        g.action_receive()
        g.write({
            'grant_income_account_id': self.grant_income.id,
            'repayment_expense_account_id': self.repay_expense.id,
            'clawback_amount': 400.0,
            'clawback_liability_account_id': self.clawback_liab.id,
        })
        g.condition_ids.action_breach()
        self.assertEqual(g.state, 'repayable')
        self.assertAlmostEqual(g.clawback_accrued, 400.0, places=2)
        with self.assertRaises(UserError):
            g.write({'clawback_amount': 500.0})
        with self.assertRaises(UserError):
            g.write({'repayment_expense_account_id': self.account_expense.id})

        non_monetary = self._grant(
            amount=0.0,
            grant_kind='non_monetary',
            asset_fair_value=100.0,
            received_asset_account_id=self.asset.id,
            cash_account_id=False,
        )
        non_monetary.action_receive()
        non_monetary.write({'cash_account_id': self.account_cash.id})
        with self.assertRaises(UserError):
            non_monetary.write({'cash_account_id': False})

    def test_locked_posting_date_fails_without_silent_redating(self):
        locked_date = date(2026, 1, 31)
        self.company.fiscalyear_lock_date = locked_date
        g = self._grant(amount=1000.0, grant_date=locked_date)
        before = self.env['account.move'].search_count([
            ('eh_gov_grant_id', '=', g.id),
        ])
        with self.assertRaisesRegex(UserError, 'locked'):
            g.action_receive()
        self.assertEqual(g.state, 'draft')
        self.assertEqual(self.env['account.move'].search_count([
            ('eh_gov_grant_id', '=', g.id),
        ]), before)

    def test_child_branch_accepts_root_owned_chart_accounts(self):
        Account = self.env['account.account']
        if not callable(getattr(Account, '_check_company_domain', None)):
            self.skipTest(
                "This Odoo series has no hierarchical company domain hook."
            )
        branch = self._create_accounting_branch({
            'name': 'Grant Child Branch',
            'parent_id': self.company.id,
            'currency_id': self.company.currency_id.id,
        })
        journal = self._ensure_journal(
            self.env, branch, 'general', 'GCBR', 'Grant Child Branch Journal',
        )
        self.assertTrue(self.account_cash.filtered_domain(
            self.account_cash._check_company_domain(branch),
        ))
        grant = self.env['eh.gov.grant'].with_company(branch).create({
            'name': '/',
            'company_id': branch.id,
            'grant_type': 'income_related',
            'amount': 1000.0,
            'cash_account_id': self.account_cash.id,
            'deferred_income_account_id': self.deferred.id,
            'grant_income_account_id': self.grant_income.id,
            'repayment_expense_account_id': self.repay_expense.id,
            'journal_id': journal.id,
            'grantor_id': self.grantor.id,
            'agreement_reference': 'CHILD-BRANCH-AWARD',
        })
        grant.action_receive()
        self.assertEqual(grant.state, 'received')
        self.assertEqual(grant.move_ids.company_id, branch)
        self.assertEqual(
            set(grant.move_ids.line_ids.account_id.ids),
            {self.account_cash.id, self.deferred.id},
        )

    def test_posted_grant_has_controlled_exact_sealed_reversal(self):
        g = self._grant(amount=1000.0)
        g.action_receive()
        g.amortise_amount = 250.0
        g.action_amortise()
        originals = g.move_ids
        self.assertEqual(len(originals), 2)
        g.reversal_date = fields.Date.context_today(g)
        g.action_reverse()

        self.assertEqual(g.state, 'reversed')
        self.assertEqual(g.reversed_by_id, self.env.user)
        self.assertTrue(g.reversed_at)
        reversals = self.env['account.move'].search([
            ('reversed_entry_id', 'in', originals.ids),
        ])
        self.assertEqual(len(reversals), 2)
        self.assertTrue(all(reversals.mapped('eh_sealed')))
        self.assertEqual(reversals.mapped('eh_gov_grant_id'), g)
        self.assertEqual(set(reversals.mapped('date')), {g.reversal_date})
        for original in originals:
            reversal = reversals.filtered(
                lambda move: move.reversed_entry_id == original,
            )
            expected = sorted(
                (account_id, credit, debit)
                for account_id, debit, credit
                in g._eh_move_signature(original)
            )
            self.assertEqual(g._eh_move_signature(reversal), expected)
        with self.assertRaises(UserError):
            g.action_reverse()
        with self.assertRaises(UserError):
            g.write({'amount': 2000.0})

    def test_amortise_posts_in_earning_period_not_grant_period(self):
        # IAS 20.12 systematic matching and period cutoff: an amortisation
        # release must post in the period whose costs it matches, not the
        # grant's original period. A grant received in January, amortised in
        # a later month, must date its release move in that later month.
        grant_day = date(2026, 1, 15)
        release_day = date(2026, 6, 30)
        g = self._grant(amount=1200.0, grant_date=grant_day)
        g.action_receive()
        # The receipt move dates on the grant / receipt date.
        receipt_move = g.move_ids
        self.assertEqual(receipt_move.date, grant_day)
        g.amortise_amount = 100.0
        g.amortise_date = release_day
        g.action_amortise()
        release_move = g.move_ids - receipt_move
        self.assertEqual(len(release_move), 1)
        # The release posts in its earning period, not the grant period.
        self.assertEqual(release_move.date, release_day)
        self.assertNotEqual(release_move.date, grant_day)

    def test_posted_grant_integrity_controls(self):
        # A received grant carries a posted GL move. Its measurement inputs are
        # frozen at write, the record cannot be deleted, and a plain user
        # cannot raw-reset its state back to draft to lift the freeze; the
        # normal receive / amortise flow still works.
        g = self._grant(amount=1000.0)
        g.action_receive()
        self.assertEqual(g.state, 'received')

        # (a) a posted record's input is frozen at write
        with self.assertRaises(UserError):
            g.write({'amount': 9000.0})

        # (b) a posted record cannot be unlinked (its move would be orphaned)
        with self.assertRaises(UserError):
            g.unlink()
        self.assertTrue(g.exists())

        # (c) a plain user cannot raw-reset the state out of the posted set
        user = self.env['res.users'].create({
            'name': 'p3', 'login': 'grant_reset_plain@test',
            'email': 'grant_reset_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            g.with_user(user).write({'state': 'draft'})
        self.assertEqual(g.state, 'received')

        # (d) the normal action flow still works (state writes are not blocked)
        g.amortise_amount = 400.0
        g.action_amortise()
        self.assertAlmostEqual(g.recognised_amount, 400.0, places=2)
        g.repayment_amount = 200.0
        g.action_repay()
        self.assertEqual(g.state, 'received')
        self.assertAlmostEqual(g.remaining, 400.0, places=2)

    def test_entries_balance(self):
        g = self._grant(amount=1200.0)
        g.action_receive()
        g.amortise_amount = 300.0
        g.action_amortise()
        for move in g.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
