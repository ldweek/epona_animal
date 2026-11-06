# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 2 inventory net-realisable-value tests."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_inventory_nrv', 'integration', 'post_install', '-at_install')
class TestNrv(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.writedown_expense = cls._ensure_account(
            cls.env, '5150', 'Inventory Write-down', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1490', 'Inventory Write-down Allowance', 'asset_current')

    def _run(self, reporting_date='2026-06-30', lines=None):
        prepared_lines = []
        for source in (lines or []):
            values = dict(source)
            if 'opening_writedown' in values:
                values.update({
                    'opening_writedown_manual': True,
                    'manual_opening_writedown': values.pop(
                        'opening_writedown'),
                    'opening_override_reason': 'Seeded test opening balance',
                })
            prepared_lines.append(values)
        return self.env['eh.nrv.run'].create({
            'reporting_date': reporting_date,
            'writedown_expense_account_id': self.writedown_expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, values) for values in prepared_lines],
        })

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_posted_line_opening_manual_flag_frozen(self):
        """opening_writedown_manual is a compute trigger for opening_writedown;
        flipping it on a posted run's line would silently re-derive the
        opening and drift the posted figure, so it is frozen too."""
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        run.action_compute()
        run.action_post()
        line = run.line_ids[0]
        with self.assertRaises(UserError):
            line.opening_writedown_manual = not line.opening_writedown_manual

    def test_writedown_lower_of_cost_and_nrv(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0},
            {'name': 'B', 'cost': 500.0, 'net_realisable_value': 800.0},
        ])
        # A written down 300; B at cost (NRV above cost) -> 0.
        self.assertAlmostEqual(run.closing_writedown, 300.0, places=2)

    def test_post_writedown(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertAlmostEqual(self._bal(self.writedown_expense), 300.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.allowance), -300.0, places=2)

    def test_recovery_reverses_capped(self):
        # Opening write-down 300; NRV recovers so required is only 100.
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 900.0,
             'opening_writedown': 300.0}])
        run.action_compute()
        # required 100, opening 300 -> movement -200 (recovery).
        self.assertAlmostEqual(run.movement, -200.0, places=2)
        run.action_post()
        self.assertAlmostEqual(self._bal(self.allowance), 200.0, places=2)
        self.assertAlmostEqual(self._bal(self.writedown_expense), -200.0,
                               places=2)

    def test_recovery_never_above_cost(self):
        # NRV above cost but a big opening write-down: required floored at 0,
        # so the reversal is capped at the opening write-down, not more.
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 1500.0,
             'opening_writedown': 300.0}])
        run.action_compute()
        self.assertAlmostEqual(run.closing_writedown, 0.0, places=2)
        self.assertAlmostEqual(run.movement, -300.0, places=2)

    def test_nil_movement_posts_audited_closing_anchor(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 1200.0}])
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertFalse(run.move_id)
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertFalse(run.reversal_move_id)

    def test_post_requires_manager(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        run.action_compute()
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'nrv_plain@test', 'email': 'nrv_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            run.with_user(user).action_post()

    def test_lines_frozen_after_post(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        line = run.line_ids
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            line.net_realisable_value = 500.0

    def test_opening_rolls_forward_from_prior_run(self):
        product = self.env['product.product'].create({'name': 'NRV widget'})
        # Prior period: cost 1000, NRV 700 -> closing write-down 300, posted.
        prior = self._run(reporting_date='2026-03-31', lines=[
            {'name': 'A', 'product_id': product.id,
             'cost': 1000.0, 'net_realisable_value': 700.0}])
        prior.action_compute()
        prior.action_post()
        self.assertAlmostEqual(prior.closing_writedown, 300.0, places=2)
        # Next period for the same product, no manual opening supplied.
        run = self._run(reporting_date='2026-06-30', lines=[
            {'name': 'A', 'product_id': product.id,
             'cost': 1000.0, 'net_realisable_value': 800.0}])
        line = run.line_ids
        # Opening must roll forward from the prior posted run's 300.
        self.assertAlmostEqual(line.prior_closing_writedown, 300.0, places=2)
        self.assertAlmostEqual(line.opening_writedown, 300.0, places=2)
        self.assertFalse(line.opening_tieout)
        # Required now 200 (1000 - 800); movement = 200 - 300 = -100 recovery.
        self.assertAlmostEqual(line.movement, -100.0, places=2)

    def test_manual_opening_survives_product_change(self):
        # A manually set opening write-down must not be silently discarded
        # when the line's product changes (which re-fires the roll-forward
        # compute).
        p1 = self.env['product.product'].create({'name': 'NRV P1'})
        p2 = self.env['product.product'].create({'name': 'NRV P2'})
        prior = self._run(reporting_date='2026-03-31', lines=[
            {'name': 'A', 'product_id': p2.id,
             'cost': 1000.0, 'net_realisable_value': 700.0}])
        prior.action_compute()
        prior.action_post()
        run = self._run(reporting_date='2026-06-30', lines=[
            {'name': 'A', 'product_id': p1.id,
             'cost': 1000.0, 'net_realisable_value': 800.0}])
        line = run.line_ids
        line.write({
            'opening_writedown_manual': True,
            'manual_opening_writedown': 250.0,
            'opening_override_reason': 'Manager-approved test override',
        })
        self.assertTrue(line.opening_writedown_manual)
        # Switching to p2 (prior posted closing 300) must NOT clobber 250.
        line.product_id = p2.id
        self.assertAlmostEqual(line.opening_writedown, 250.0, places=2)

    def test_opening_manual_override_flags_tieout(self):
        product = self.env['product.product'].create({'name': 'NRV gadget'})
        prior = self._run(reporting_date='2026-03-31', lines=[
            {'name': 'A', 'product_id': product.id,
             'cost': 1000.0, 'net_realisable_value': 700.0}])
        prior.action_compute()
        prior.action_post()
        # Manual opening that disagrees with prior closing (300).
        run = self._run(reporting_date='2026-06-30', lines=[
            {'name': 'A', 'product_id': product.id, 'opening_writedown': 250.0,
             'cost': 1000.0, 'net_realisable_value': 800.0}])
        line = run.line_ids
        self.assertAlmostEqual(line.opening_writedown, 250.0, places=2)
        self.assertAlmostEqual(line.prior_closing_writedown, 300.0, places=2)
        self.assertTrue(line.opening_tieout)

    def test_reverse(self):
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        run.action_compute()
        run.action_post()
        with self.assertRaisesRegex(UserError, 'reversal accounting date'):
            run.action_reverse()
        run.reversal_date = '2026-07-15'
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.reversal_move_id.date, run.reversal_date)

    def test_reversed_date_can_be_rerun_as_a_new_immutable_revision(self):
        first = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0},
        ])
        self.assertEqual(first.revision, 1)
        with self.assertRaisesRegex(UserError, 'already owns'):
            self._run(lines=[
                {'name': 'duplicate', 'cost': 100.0,
                 'net_realisable_value': 80.0},
            ])
        first.action_compute()
        first.action_post()
        first.reversal_date = '2026-07-15'
        first.action_reverse()
        second = self._run(lines=[
            {'name': 'corrected', 'cost': 1000.0,
             'net_realisable_value': 750.0},
        ])
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.supersedes_run_id, first)
        with self.assertRaises(AccessError):
            second.write({'revision': 99})
        with self.assertRaises(AccessError):
            self.env['eh.nrv.run'].create({
                'reporting_date': '2026-12-31',
                'revision': 99,
            })

    def test_zero_movement_close_rolls_forward_to_next_period(self):
        product = self.env['product.product'].create({
            'name': 'NRV zero-movement anchor product',
        })
        prior = self._run(reporting_date='2026-03-31', lines=[{
            'name': 'Anchor', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 80.0,
            'opening_writedown': 20.0,
        }])
        self.assertEqual(prior.movement, 0.0)
        prior.action_compute()
        prior.action_post()
        self.assertFalse(prior.move_id)
        current = self._run(reporting_date='2026-06-30', lines=[{
            'name': 'Current', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 90.0,
        }])
        self.assertEqual(current.line_ids.opening_writedown, 20.0)
        self.assertEqual(current.movement, -10.0)

    def test_post_and_reverse_enforce_latest_first_rollforward_chain(self):
        early = self._run(reporting_date='2026-03-31', lines=[{
            'name': 'Early', 'cost': 100.0, 'net_realisable_value': 80.0,
        }])
        late = self._run(reporting_date='2026-06-30', lines=[{
            'name': 'Late', 'cost': 100.0, 'net_realisable_value': 70.0,
        }])
        late.action_compute()
        late.action_post()
        early.action_compute()
        with self.assertRaisesRegex(UserError, 'later posted NRV run'):
            early.action_post()

        # A valid chain must also unwind latest-first.
        late.reversal_date = '2026-07-01'
        late.action_reverse()
        early.action_post()
        rerun_late = self._run(reporting_date='2026-06-30', lines=[{
            'name': 'Late corrected', 'cost': 100.0,
            'net_realisable_value': 75.0,
        }])
        rerun_late.action_compute()
        rerun_late.action_post()
        early.reversal_date = '2026-07-01'
        with self.assertRaisesRegex(UserError, 'later posted NRV run'):
            early.action_reverse()

    def test_post_rejects_stale_automatic_opening_until_recomputed(self):
        product = self.env['product.product'].create({
            'name': 'NRV stale opening product',
        })
        current = self._run(reporting_date='2026-06-30', lines=[{
            'name': 'Current', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 70.0,
        }])
        current.action_compute()
        prior = self._run(reporting_date='2026-03-31', lines=[{
            'name': 'Prior', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 80.0,
        }])
        prior.action_compute()
        prior.action_post()
        with self.assertRaisesRegex(UserError, 'Compute again'):
            current.action_post()
        current.action_compute()
        self.assertEqual(current.line_ids.opening_writedown, 20.0)
        current.action_post()
        self.assertEqual(current.movement, 10.0)

    def test_duplicate_product_has_no_ambiguous_rollforward(self):
        product = self.env['product.product'].create({
            'name': 'NRV duplicate-line product',
        })
        run = self._run(lines=[
            {
                'name': 'First assessment', 'product_id': product.id,
                'cost': 100.0, 'net_realisable_value': 80.0,
            },
            {
                'name': 'Duplicate assessment', 'product_id': product.id,
                'cost': 50.0, 'net_realisable_value': 40.0,
            },
        ])
        with self.assertRaisesRegex(UserError, 'appear only once'):
            run.action_compute()

    def test_branch_run_uses_parent_company_journal_and_accounts(self):
        if 'parent_ids' not in self.env['res.company']._fields:
            self.skipTest('Odoo series has no branch accounting semantics')
        branch = self._create_accounting_branch({
            'name': 'NRV Accounting Branch',
            'parent_id': self.env.company.id,
        })
        self.env.user.company_ids = [(4, branch.id)]
        run = self.env['eh.nrv.run'].with_company(branch).create({
            'company_id': branch.id,
            'reporting_date': '2026-09-30',
            'writedown_expense_account_id': self.writedown_expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Branch stock', 'cost': 100.0,
                'net_realisable_value': 75.0,
            })],
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.move_id.company_id, branch)
        self.assertEqual(run.move_id.journal_id.company_id, self.env.company)
        self.assertEqual(
            set(run.move_id.line_ids.account_id.ids),
            {self.writedown_expense.id, self.allowance.id},
        )

    def test_posted_run_controls(self):
        """A posted run freezes its inputs, resists a raw reset by a plain
        user, and cannot be deleted; the reverse flow still works."""
        run = self._run(lines=[
            {'name': 'A', 'cost': 1000.0, 'net_realisable_value': 700.0}])
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')

        # (a) a posted run's input is frozen at write.
        with self.assertRaises(UserError):
            run.reporting_date = '2026-07-31'

        # (b) a posted run cannot be unlinked.
        with self.assertRaises(UserError):
            run.unlink()

        # (c) a plain user cannot raw-reset the state to lift the freeze.
        plain = self.env['res.users'].create({
            'name': 'plain', 'login': 'nrv_ctrl@test',
            'email': 'nrv_ctrl@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            run.with_user(plain).write({'state': 'draft'})

        # (d) the sanctioned reverse flow still moves the state out of posted.
        run.reversal_date = '2026-07-15'
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
