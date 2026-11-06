# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 12 deferred tax engine tests."""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_deferred_tax', 'integration', 'post_install', '-at_install')
class TestDeferredTax(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.dta = cls._ensure_account(
            cls.env, '1810', 'Deferred Tax Asset', 'asset_non_current')
        cls.dtl = cls._ensure_account(
            cls.env, '2810', 'Deferred Tax Liability', 'liability_non_current')
        cls.dtax_expense = cls._ensure_account(
            cls.env, '5810', 'Deferred Tax Expense', 'expense')
        cls.oci = cls._ensure_account(
            cls.env, '3810', 'OCI Reserve', 'equity')

    def _run(self, rate=25.0, period_end='2026-12-31', **vals):
        base = {
            'statutory_rate': rate,
            'period_end': period_end,
            'dta_account_id': self.dta.id,
            'dtl_account_id': self.dtl.id,
            'deferred_tax_expense_account_id': self.dtax_expense.id,
            'oci_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.deferred.tax.run'].create(base)

    def _debit(self, move, account):
        return sum(move.line_ids.filtered(
            lambda l: l.account_id == account).mapped('debit'))

    def _credit(self, move, account):
        return sum(move.line_ids.filtered(
            lambda l: l.account_id == account).mapped('credit'))

    def test_compute_seeds_rate(self):
        run = self._run(rate=30.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Plant', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.line_ids.tax_rate, 30.0, places=3)

    def test_dta_partial_recoverability_cap(self):
        """IAS 12.24/34: a DTA is recognised only to the extent of projected
        future taxable profit. recoverable_amount caps the deductible
        difference; zero leaves the full DTA; the Boolean is a hard
        off-switch. The unrecognised remainder is disclosed (IAS 12.81(e))."""
        run = self._run(rate=25.0)
        line = self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Deductible', 'nature': 'asset',
            'carrying_amount': 600.0, 'tax_base': 1000.0,
        })
        run.action_compute()
        # Deductible difference 400 at 25% -> full DTA 100 when unconstrained.
        self.assertAlmostEqual(line.deductible_diff, 400.0, places=2)
        self.assertAlmostEqual(line.closing_dta, 100.0, places=2)
        self.assertAlmostEqual(line.unrecognised_dta, 0.0, places=2)

        # Projected recoverable profit of 250 caps the recognised difference:
        # DTA on 250 = 62.5, the remaining 37.5 is unrecognised.
        line.recoverable_amount = 250.0
        self.assertAlmostEqual(line.closing_dta, 62.5, places=2)
        self.assertAlmostEqual(line.unrecognised_dta, 37.5, places=2)
        self.assertAlmostEqual(run.closing_dta, 62.5, places=2)
        self.assertAlmostEqual(run.unrecognised_dta, 37.5, places=2)

        # The Boolean remains a hard off-switch: nothing recognised, the whole
        # 100 disclosed as unrecognised.
        line.recoverable = False
        self.assertAlmostEqual(line.closing_dta, 0.0, places=2)
        self.assertAlmostEqual(line.unrecognised_dta, 100.0, places=2)

    def test_taxable_difference_books_dtl(self):
        """Asset carrying > tax base -> DTL; movement is Cr DTL / Dr expense."""
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Accelerated depreciation',
            'nature': 'asset', 'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        # 400 taxable diff x 25% = 100 DTL.
        self.assertAlmostEqual(run.closing_dtl, 100.0, places=2)
        self.assertAlmostEqual(run.closing_dta, 0.0, places=2)
        self.assertAlmostEqual(run.pl_movement, 100.0, places=2)
        run.action_post()
        self.assertEqual(run.state, 'posted')
        move = run.move_id
        self.assertTrue(move)
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(self._credit(move, self.dtl), 100.0, places=2)
        self.assertAlmostEqual(self._debit(move, self.dtax_expense), 100.0,
                               places=2)

    def test_deductible_difference_books_dta(self):
        """Asset carrying < tax base -> DTA; Dr DTA / Cr income."""
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Warranty provision', 'nature': 'liability',
            'carrying_amount': 400.0, 'tax_base': 0.0,
        })
        run.action_compute()
        # Liability carrying 400 > tax base 0 -> deductible 400 x 25% = 100 DTA.
        self.assertAlmostEqual(run.closing_dta, 100.0, places=2)
        self.assertAlmostEqual(run.pl_movement, -100.0, places=2)
        run.action_post()
        move = run.move_id
        self.assertAlmostEqual(self._debit(move, self.dta), 100.0, places=2)
        self.assertAlmostEqual(self._credit(move, self.dtax_expense), 100.0,
                               places=2)

    def test_unrecoverable_dta_excluded(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Tax loss', 'nature': 'tax_loss',
            'carrying_amount': 800.0, 'recoverable': False,
        })
        run.action_compute()
        self.assertAlmostEqual(run.closing_dta, 0.0, places=2)
        with self.assertRaises(UserError):
            run.action_post()  # nil movement

    def test_recoverable_tax_loss_books_dta(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Tax loss', 'nature': 'tax_loss',
            'carrying_amount': 800.0, 'recoverable': True,
        })
        run.action_compute()
        self.assertAlmostEqual(run.closing_dta, 200.0, places=2)
        run.action_post()
        self.assertAlmostEqual(self._debit(run.move_id, self.dta), 200.0,
                               places=2)

    def test_oci_movement_routes_to_oci(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Revaluation surplus', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 200.0, 'through_oci': True,
        })
        run.action_compute()
        # 800 taxable x 25% = 200 DTL, all via OCI.
        self.assertAlmostEqual(run.oci_movement, 200.0, places=2)
        self.assertAlmostEqual(run.pl_movement, 0.0, places=2)
        run.action_post()
        move = run.move_id
        self.assertAlmostEqual(self._credit(move, self.dtl), 200.0, places=2)
        self.assertAlmostEqual(self._debit(move, self.oci), 200.0, places=2)
        self.assertFalse(self._debit(move, self.dtax_expense))

    def test_movement_uses_opening_position(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Depreciation', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
            'opening_dtl': 60.0,
        })
        run.action_compute()
        # Closing DTL 100, opening 60 -> movement 40 to P&L.
        self.assertAlmostEqual(run.pl_movement, 40.0, places=2)
        run.action_post()
        self.assertAlmostEqual(self._credit(run.move_id, self.dtl), 40.0,
                               places=2)

    def test_opening_rolls_forward_from_prior_run(self):
        """A later run defaults each line's opening from the prior posted
        run's closing, so the movement ties to the ledger with no re-keying."""
        prior = self._run(rate=25.0, period_end='2025-12-31')
        self.env['eh.deferred.tax.line'].create({
            'run_id': prior.id, 'name': 'Accelerated depreciation',
            'nature': 'asset', 'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        prior.action_compute()
        prior.action_post()
        self.assertAlmostEqual(prior.closing_dtl, 100.0, places=2)

        current = self._run(rate=25.0, period_end='2026-12-31')
        line = self.env['eh.deferred.tax.line'].create({
            'run_id': current.id, 'name': 'Accelerated depreciation',
            'nature': 'asset', 'carrying_amount': 1000.0, 'tax_base': 400.0,
        })
        # Opening left blank; compute must roll it forward from prior closing.
        self.assertAlmostEqual(line.opening_dtl, 0.0, places=2)
        current.action_compute()
        self.assertAlmostEqual(line.opening_dtl, 100.0, places=2)
        # Closing 150, opening rolled to 100 -> movement is 50, not 150.
        self.assertAlmostEqual(line.closing_dtl, 150.0, places=2)
        self.assertAlmostEqual(current.pl_movement, 50.0, places=2)
        self.assertFalse(current.opening_tie_out)

    def test_opening_mismatch_is_flagged(self):
        """A hand-keyed opening that disagrees with the prior run's closing
        raises the tie-out flag on the line and the run."""
        prior = self._run(rate=25.0, period_end='2025-12-31')
        self.env['eh.deferred.tax.line'].create({
            'run_id': prior.id, 'name': 'Accelerated depreciation',
            'nature': 'asset', 'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        prior.action_compute()
        prior.action_post()

        current = self._run(rate=25.0, period_end='2026-12-31')
        line = self.env['eh.deferred.tax.line'].create({
            'run_id': current.id, 'name': 'Accelerated depreciation',
            'nature': 'asset', 'carrying_amount': 1000.0, 'tax_base': 400.0,
            'opening_dtl': 60.0,  # keying error: prior closing was 100.
        })
        self.assertAlmostEqual(line.expected_opening_dtl, 100.0, places=2)
        self.assertTrue(line.opening_tie_out)
        self.assertTrue(current.opening_tie_out)
        # A correct manual opening ties out and clears the flag.
        line.opening_dtl = 100.0
        self.assertFalse(line.opening_tie_out)
        self.assertFalse(current.opening_tie_out)

    def test_reconciliation(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Depreciation', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.write({
            'accounting_profit': 1000.0,
            'current_tax_expense': 150.0,
        })
        run.action_compute()
        # Expected 1000 x 25% = 250; total = 150 current + 100 deferred = 250.
        self.assertAlmostEqual(run.expected_tax, 250.0, places=2)
        self.assertAlmostEqual(run.total_tax_expense, 250.0, places=2)
        self.assertAlmostEqual(run.reconciliation_residual, 0.0, places=2)
        self.assertAlmostEqual(run.effective_rate, 25.0, places=2)

    def test_post_requires_manager(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        user = self.env['res.users'].create({
            'name': 'plain', 'login': 'dtax_plain@test',
            'email': 'dtax_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])],
        })
        with self.assertRaises(UserError):
            run.with_user(user).action_post()

    def test_cancel_requires_manager(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        user = self.env['res.users'].create({
            'name': 'plain', 'login': 'dtax_cancel_plain@test',
            'email': 'dtax_cancel_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])],
        })
        with self.assertRaises(UserError):
            run.with_user(user).action_cancel()

    def test_lines_frozen_after_post(self):
        run = self._run(rate=25.0)
        line = self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            line.carrying_amount = 2000.0

    def test_reverse(self):
        run = self._run(rate=25.0, period_end='2025-12-31')
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        run.action_post()
        run.reversal_date = run.period_end
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.reversal_move_id.state, 'posted')
        self.assertEqual(run.reversal_move_id.date, run.period_end)

    def test_reversed_period_allows_one_corrected_live_run(self):
        run = self._run(rate=25.0, period_end='2025-12-31')
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Original', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(ValidationError):
            self._run(rate=25.0, period_end=run.period_end)

        run.reversal_date = run.period_end
        run.action_reverse()
        corrected = self._run(rate=25.0, period_end=run.period_end)
        self.assertEqual(corrected.state, 'draft')
        with self.assertRaises(ValidationError):
            self._run(rate=25.0, period_end=run.period_end)

    def test_post_rejects_line_added_after_compute_until_recomputed(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Initial', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        late = self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Late', 'nature': 'asset',
            'carrying_amount': 400.0, 'tax_base': 200.0,
        })
        self.assertEqual(late.tax_rate, 0.0)

        with self.assertRaises(UserError):
            run.action_post()
        self.assertFalse(run.move_id)
        run.action_compute()
        run.action_post()

        self.assertEqual(late.tax_rate, 25.0)
        self.assertAlmostEqual(run.closing_dtl, 150.0, places=2)
        self.assertAlmostEqual(self._credit(run.move_id, self.dtl), 150.0,
                               places=2)

    def test_run_frozen_and_unlink_and_flow(self):
        """Run-level control hole: once posted the input fields are frozen at
        the ORM write layer and the run cannot be deleted, while the normal
        compute -> post -> reverse flow still writes state successfully."""
        run = self._run(rate=25.0, period_end='2025-12-31')
        run.write({
            'accounting_profit': 1000.0,
            # This lifecycle regression is not an ETR worked example; its
            # materiality documents that scope before Compute.
            'reconciliation_materiality': 1000.0,
        })
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        # Draft: input still editable.
        run.write({'statutory_rate': 30.0})
        self.assertAlmostEqual(run.statutory_rate, 30.0, places=3)

        run.action_compute()   # state write in 'computed' must pass
        run.action_post()      # manager-only state write must pass
        self.assertEqual(run.state, 'posted')

        # (a) a posted run's measurement / input field is frozen.
        with self.assertRaises(UserError):
            run.write({'statutory_rate': 40.0})
        with self.assertRaises(UserError):
            run.write({'accounting_profit': 5000.0})
        with self.assertRaises(UserError):
            run.write({'journal_id': self.journal_misc.id,
                       'oci_account_id': self.oci.id})
        # A non-frozen audit/note write and the state transition still pass.
        run.write({'notes': 'reviewed'})

        # (b) a posted run cannot be unlinked.
        with self.assertRaises(UserError):
            run.unlink()

        # (c) the normal reverse flow still works (a pure state write).
        run.reversal_date = run.period_end
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        # Frozen after reversal too.
        with self.assertRaises(UserError):
            run.write({'statutory_rate': 10.0})
        with self.assertRaises(UserError):
            run.unlink()

    def _plain_user(self):
        """An accounting user with group_eh_user only (no manager)."""
        user = self.env['res.users'].create({
            'name': 'DT Plain User',
            'login': 'eh_dt_plain_user',
            'email': 'eh_dt_plain_user@example.com',
            'group_ids': [
                (6, 0, self.env.ref('eh_account_base.group_eh_user').ids)],
        })
        self.assertFalse(
            user.has_group('eh_account_base.group_eh_manager'))
        return user

    def _posted_run(self):
        run = self._run(rate=25.0)
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'x', 'nature': 'asset',
            'carrying_amount': 1000.0, 'tax_base': 600.0,
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        return run

    def test_line_cannot_move_into_posted_run(self):
        """A line built on a draft run cannot be re-pointed into a posted
        run; that would re-trigger the posted run's stored computes and
        drift its frozen closing figures off the posted GL movement. The
        source-parent freeze alone does not catch a move INTO a posted run.
        """
        posted = self._posted_run()
        before = posted.closing_dtl
        # A distinct period: the run is unique per (company, period_end).
        draft = self._run(rate=25.0, period_end='2025-12-31')
        line = self.env['eh.deferred.tax.line'].create({
            'run_id': draft.id, 'name': 'd', 'nature': 'asset',
            'carrying_amount': 500.0, 'tax_base': 200.0,
        })
        with self.assertRaises(UserError):
            line.write({'run_id': posted.id})
        posted.invalidate_recordset(['closing_dtl'])
        self.assertAlmostEqual(posted.closing_dtl, before, places=2)

    def test_plain_user_cannot_reset_posted_run(self):
        """A posted run's state is a manager-gated control point: a plain
        user cannot reset it to draft at the ORM layer to lift the figure
        freeze. The manager-gated action path still works."""
        run = self._posted_run()
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).write({'state': 'draft'})
        self.assertEqual(run.state, 'posted')
