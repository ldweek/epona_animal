# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Audit pack integrity and sign-off tests."""

from datetime import date
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_audit_pack', 'integration', 'post_install', '-at_install')
@freeze_time('2027-01-02')
class TestAuditPack(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        cls.other_manager = cls.env['res.users'].create({
            'name': 'Reviewer', 'login': 'audit_rev@test',
            'email': 'audit_rev@test',
            'group_ids': [(6, 0, [cls.group_manager.id])]})
        cls.third_manager = cls.env['res.users'].create({
            'name': 'Third', 'login': 'audit_third@test',
            'email': 'audit_third@test',
            'group_ids': [(6, 0, [cls.group_manager.id])]})

    def _pack(self):
        return self.env['eh.audit.pack'].create({
            'period_from': '2026-01-01', 'period_to': '2026-12-31'})

    @classmethod
    def _hash_chain_available(cls):
        return (
            'inalterable_hash' in cls.env['account.move']._fields
            and 'restrict_mode_hash_table'
            in cls.env['account.journal']._fields)

    def _pack_with_hash_chain(self):
        """A pack on a company whose posting journals carry an active hash
        chain: the precondition for an audit-grade sign-off."""
        pack = self._pack()
        if self._hash_chain_available():
            pack.action_enable_hash_chain()
        return pack

    def test_run_checks_clean_period(self):
        pack = self._pack()
        pack.action_run_checks()
        self.assertEqual(pack.state, 'checks_run')
        # A clean, empty period has no blocking failures.
        self.assertFalse(pack.has_blocking_failure)
        self.assertTrue(pack.check_ids)

    def test_period_order_and_future_evidence_fail_closed(self):
        with self.assertRaises(ValidationError):
            self.env['eh.audit.pack'].create({
                'period_from': '2026-12-31',
                'period_to': '2026-01-01',
            })
        future = self.env['eh.audit.pack'].create({
            'period_from': '2027-01-03',
            'period_to': '2027-01-31',
        })
        with self.assertRaises(UserError):
            future.action_run_checks()
        self.assertEqual(future.state, 'draft')

    def test_draft_entry_blocks(self):
        # A draft move in the period is a blocking failure.
        self.env['account.move'].create({
            'move_type': 'entry', 'date': '2026-06-01',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'a', 'account_id': self.account_cash.id,
                        'debit': 100.0, 'credit': 0.0}),
                (0, 0, {'name': 'b', 'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 100.0}),
            ]})
        pack = self._pack()
        pack.action_run_checks()
        self.assertTrue(pack.has_blocking_failure)
        with self.assertRaises(UserError):
            pack.action_sign_off()

    def test_audit_check_rows_cannot_be_hand_edited(self):
        """Audit check rows ARE the sign-off gate, so they are system-written:
        a direct write, unlink or create is refused for everyone, closing the
        path where a failed blocking check is flipped to pass."""
        self.env['account.move'].create({
            'move_type': 'entry', 'date': '2026-06-01',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'a', 'account_id': self.account_cash.id,
                        'debit': 100.0, 'credit': 0.0}),
                (0, 0, {'name': 'b', 'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 100.0})]})
        pack = self._pack()
        pack.action_run_checks()
        fail_check = pack.check_ids.filtered(
            lambda c: c.is_blocking and c.status == 'fail')
        self.assertTrue(fail_check)
        with self.assertRaises(UserError):
            fail_check[0].status = 'pass'
        with self.assertRaises(UserError):
            fail_check[0].unlink()
        with self.assertRaises(UserError):
            self.env['eh.audit.check'].create({
                'pack_id': pack.id, 'code': 'x', 'name': 'x',
                'status': 'pass', 'is_blocking': True})
        with self.assertRaises(UserError):
            self.env['eh.audit.check'].with_context(
                eh_audit_check_write=True,
            ).create({
                'pack_id': pack.id, 'code': 'x', 'name': 'x',
                'status': 'pass', 'is_blocking': True})

    def test_sign_off_recomputes_against_live_ledger(self):
        """A stale stored check row cannot vouch a period: the checks re-run
        at sign-off, so a draft entry introduced after Run Checks still
        blocks the sign-off."""
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()  # clean period -> stored rows pass
        self.assertFalse(pack.has_blocking_failure)
        self.env['account.move'].create({  # draft appears AFTER the checks
            'move_type': 'entry', 'date': '2026-06-02',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'a', 'account_id': self.account_cash.id,
                        'debit': 50.0, 'credit': 0.0}),
                (0, 0, {'name': 'b', 'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 50.0})]})
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()

    def test_sign_off_segregation_of_duties(self):
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()  # prepared by current user
        # Same user cannot sign off.
        with self.assertRaises(UserError):
            pack.action_sign_off()
        # Another manager can.
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')
        self.assertEqual(pack.signed_by_id, self.other_manager)

    def test_rerun_does_not_reset_preparer(self):
        # Preparer-of-record is frozen on first run. A re-run by a different
        # manager must not silently reset prepared_by_id, otherwise the
        # original preparer could then sign off and defeat preparer!=signer.
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()  # prepared by current user
        original_preparer = pack.prepared_by_id
        original_prepared_at = pack.prepared_at
        self.assertEqual(original_preparer, self.env.user)
        # A different manager re-runs the checks.
        pack.with_user(self.other_manager).action_run_checks()
        self.assertEqual(pack.prepared_by_id, original_preparer)
        self.assertEqual(pack.prepared_at, original_prepared_at)
        # The original preparer still cannot sign off.
        with self.assertRaises(UserError):
            pack.action_sign_off()
        # Only a manager who is not the preparer-of-record can sign off.
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')
        self.assertEqual(pack.signed_by_id, self.other_manager)

    def test_direct_write_cannot_reassign_sod_anchors(self):
        # A direct RPC write must not be able to forge the SoD identity
        # anchors once the workflow has set them. prepared_by_id is set on
        # run_checks and signed_by_id on sign-off; neither may then be
        # reassigned to another user or cleared.
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()  # prepared by current user
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.prepared_by_id, self.env.user)
        self.assertEqual(pack.signed_by_id, self.other_manager)
        with self.assertRaises(AccessError):
            pack.write({'notes': 'rewritten after sign-off'})
        with self.assertRaises(UserError):
            pack.unlink()
        # Reassigning the preparer/signer, clearing an anchor, or backdating
        # its timestamp is forbidden even to the administrator test actor.
        with self.assertRaises(UserError):
            pack.write({'prepared_by_id': self.other_manager.id})
        with self.assertRaises(UserError):
            pack.write({'signed_by_id': self.third_manager.id})
        with self.assertRaises(UserError):
            pack.write({'prepared_by_id': False})
        with self.assertRaises(UserError):
            pack.write({'signed_at': '2000-01-01 00:00:00'})
        self.assertEqual(pack.prepared_by_id, self.env.user)
        self.assertEqual(pack.signed_by_id, self.other_manager)

    def test_sod_anchors_cannot_be_forged_at_create(self):
        with self.assertRaises(AccessError):
            self.env['eh.audit.pack'].create({
                'period_from': '2027-01-01',
                'period_to': '2027-12-31',
                'prepared_by_id': self.env.user.id,
                'prepared_at': '2027-12-31 00:00:00',
            })
    def test_sign_off_requires_checks(self):
        pack = self._pack()
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()

    def test_enable_hash_chain(self):
        pack = self._pack()
        Journal = self.env['account.journal']
        if 'restrict_mode_hash_table' not in Journal._fields:
            self.skipTest("no hash chain field in this version")
        pack.action_enable_hash_chain()
        journals = pack._posting_journals(pack.company_id)
        self.assertTrue(all(j.restrict_mode_hash_table for j in journals))
        pack.invalidate_recordset(['hash_chain_enabled'])
        self.assertTrue(pack.hash_chain_enabled)

    def test_enable_hash_chain_rolls_back_historical_hash_failure(self):
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-06-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Historical debit',
                    'account_id': self.account_cash.id,
                    'debit': 10.0,
                }),
                (0, 0, {
                    'name': 'Historical credit',
                    'account_id': self.account_revenue.id,
                    'credit': 10.0,
                }),
            ],
        })
        move.action_post()
        self.assertFalse(move.inalterable_hash)
        pack = self._pack()
        MoveClass = type(self.env['account.move'])
        with patch.object(
            MoveClass,
            '_hash_moves',
            side_effect=UserError("historical chain rejected"),
        ):
            with self.assertRaisesRegex(
                UserError, "activation was rolled back",
            ):
                pack.action_enable_hash_chain()
        journals = pack._posting_journals(pack.company_id)
        journals.invalidate_recordset(['restrict_mode_hash_table'])
        self.assertFalse(any(journals.mapped('restrict_mode_hash_table')))

    def test_enable_hash_chain_secures_historical_entries(self):
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-06-16',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Historical debit',
                    'account_id': self.account_cash.id,
                    'debit': 11.0,
                }),
                (0, 0, {
                    'name': 'Historical credit',
                    'account_id': self.account_revenue.id,
                    'credit': 11.0,
                }),
            ],
        })
        move.action_post()
        self.assertFalse(move.inalterable_hash)
        self._pack().action_enable_hash_chain()
        move.invalidate_recordset(['inalterable_hash'])
        self.assertTrue(move.inalterable_hash)

    def test_sign_off_blocked_without_hash_chain(self):
        # Audit inalterability: sign-off (and the lock-date advance it drives)
        # must not complete when the inalterable hash chain is absent or off
        # on the posting journals. Without the fix _check_hashed only warns and
        # action_sign_off never verifies the chain, so a manager could sign off
        # and advance the lock date over an unprotected ledger.
        pack = self._pack()  # hash chain deliberately NOT enabled
        pack.action_run_checks()
        self.assertEqual(pack.state, 'checks_run')
        # Sign-off is blocked because no inalterable chain is in force, so the
        # lock date is never advanced. The guard is _assert_hash_chain_active:
        # it fires whether the version lacks the hash capability entirely or
        # the hash table is merely off on the posting journals.
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        self.assertNotEqual(pack.state, 'signed_off')
        if 'fiscalyear_lock_date' in self.env.company._fields:
            self.assertNotEqual(
                self.env.company.fiscalyear_lock_date, pack.period_to)

    def test_tampered_hash_chain_fails_check(self):
        # Hash-chain integrity: the audit-pack gate must RECOMPUTE the
        # inalterable chain, not merely observe that a move carries a hash.
        # We enable the chain, post a hashed move, then tamper its stored
        # inalterable_hash directly in the database (an attacker editing the
        # ledger). The stored hash is still non-empty, so a presence-only check
        # would pass; a recompute-and-verify check must fail because the link no
        # longer re-derives. Without the fix _check_hashed only checks presence
        # and the 'hashed' check passes over the tampered chain.
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        move = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date=date(2026, 6, 1))
        self.assertTrue(move.inalterable_hash, "move should be hashed on post")
        # Sanity: an untampered chain passes the hash check.
        pack.action_run_checks()
        clean = pack.check_ids.filtered(lambda c: c.code == 'hashed')
        self.assertEqual(clean.status, 'pass')
        self.assertFalse(pack._hash_chain_corrupt())
        # Tamper the stored hash directly in the DB, preserving the version
        # prefix shape so it still looks like a real hash but no longer matches.
        forged = (move.inalterable_hash[:-1]
                  + ('0' if move.inalterable_hash[-1] != '0' else '1'))
        self.assertNotEqual(forged, move.inalterable_hash)
        self.env.cr.execute(
            "UPDATE account_move SET inalterable_hash = %s WHERE id = %s",
            (forged, move.id))
        move.invalidate_recordset(['inalterable_hash'])
        self.assertEqual(move.inalterable_hash, forged)
        # The recompute-and-verify check (delegated to core's per-prefix
        # integrity walk) flags the tampered chain.
        self.assertTrue(pack._hash_chain_corrupt())
        # The 'hashed' integrity check now fails and blocks sign-off.
        pack.action_run_checks()
        hashed = pack.check_ids.filtered(lambda c: c.code == 'hashed')
        self.assertEqual(
            hashed.status, 'fail',
            "a tampered hash chain must fail the audit-pack hash check")
        self.assertTrue(hashed.is_blocking)
        self.assertTrue(pack.has_blocking_failure)
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        self.assertNotEqual(pack.state, 'signed_off')

    def test_sign_off_advances_lock_date(self):
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()
        pack.with_user(self.other_manager).action_sign_off()
        if 'fiscalyear_lock_date' in self.env.company._fields:
            self.assertEqual(self.env.company.fiscalyear_lock_date,
                             pack.period_to)
        if 'hard_lock_date' in self.env.company._fields:
            self.assertEqual(self.env.company.hard_lock_date, pack.period_to)

    def test_sign_off_never_lowers_existing_lock_dates(self):
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        later = date(2027, 1, 1)
        values = {}
        for field_name in ('fiscalyear_lock_date', 'hard_lock_date'):
            if field_name in self.env.company._fields:
                values[field_name] = later
        if not values:
            self.skipTest("no supported company lock date fields")
        pack = self._pack_with_hash_chain()
        self.env.company.sudo().write(values)
        pack.action_run_checks()
        pack.with_user(self.other_manager).action_sign_off()
        for field_name in values:
            self.assertEqual(self.env.company[field_name], later)

    # ---- period-control chain (opt-in) ----

    def test_unlinked_pack_signs_off_normally(self):
        # Opt-in guarantee: a pack with no chain links behaves exactly as
        # before. It produces the original four checks and signs off with no
        # chain gate firing.
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()
        codes = set(pack.check_ids.mapped('code'))
        self.assertNotIn('chain_close_approved', codes)
        self.assertNotIn('chain_year_end_posted', codes)
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')

    def test_raw_or_dangling_chain_id_is_rejected(self):
        # The former integer fields silently treated a typo as an inactive
        # gate. Reference fields accept only a selected record of the optional
        # model, so a guessed raw id cannot disable the chain.
        pack = self._pack()
        with self.assertRaises(ValueError):
            pack.close_run_ref = 999999999
        with self.assertRaises(ValueError):
            pack.year_end_run_ref = 999999999


@tagged('eh_account_audit_pack', 'integration', 'post_install', '-at_install')
@freeze_time('2027-01-02')
class TestAuditPackChain(EhAccountIntegrationTestCase):
    """End-to-end period-control chain: the audit-pack sign-off is gated on a
    linked period-close run being approved and a linked year-end close being
    posted.

    These tests need the close-workflow and year-end modules installed
    alongside the audit pack, which happens in the suite-wide test run. When
    a module is absent (e.g. a stand-alone per-module run) the case skips, and
    the opt-in behaviour is still covered by ``TestAuditPack`` above.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        cls.other_manager = cls.env['res.users'].create({
            'name': 'Chain Reviewer', 'login': 'chain_rev@test',
            'email': 'chain_rev@test',
            'group_ids': [(6, 0, [cls.group_manager.id])]})
        cls.third_manager = cls.env['res.users'].create({
            'name': 'Chain Third', 'login': 'chain_third@test',
            'email': 'chain_third@test',
            'group_ids': [(6, 0, [cls.group_manager.id])]})
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)
        # Turn the inalterable hash chain on the company's posting journals
        # ONCE, before any test posts a move, so every source move a chain
        # test posts is hashed and the pack's _check_hashed leg passes. That
        # isolates these tests to the chain gate under test (the hash-chain
        # control is exercised by TestAuditPack).
        if cls._hash_chain_available():
            seed = cls.env['eh.audit.pack'].create({
                'period_from': cls.fy_start, 'period_to': cls.fy_end})
            seed.action_enable_hash_chain()
            seed.unlink()

    @classmethod
    def _hash_chain_available(cls):
        return (
            'inalterable_hash' in cls.env['account.move']._fields
            and 'restrict_mode_hash_table'
            in cls.env['account.journal']._fields)

    def _require_chain_modules(self):
        if 'eh.close.run' not in self.env or 'eh.year.end.run' not in self.env:
            self.skipTest(
                "close-workflow / year-end modules not installed in this run")
        if not self._hash_chain_available():
            self.skipTest("no hash chain field in this version")

    def _pack(self, **overrides):
        vals = {'period_from': self.fy_start, 'period_to': self.fy_end}
        vals.update(overrides)
        return self.env['eh.audit.pack'].create(vals)

    # ---- close-run leg ----

    def _make_close_run(self):
        Checklist = self.env['eh.close.checklist']
        checklist = Checklist.create({
            'name': 'Chain Checklist', 'code': 'chain_checklist',
            'ledger_lock_policy': 'signoff_only',
            'task_template_ids': [
                (0, 0, {'sequence': 10, 'name': 'Reconcile',
                        'responsible_role': 'accountant'}),
            ]})
        run = self.env['eh.close.run'].create({
            'name': 'Chain Close', 'checklist_id': checklist.id,
            'period_from': self.fy_start, 'period_to': self.fy_end})
        return run

    def _approve_close_run(self, run):
        # Drive the close run through its lifecycle to the approved (closed)
        # terminal state, honouring segregation of duties.
        for task in run.task_ids:
            task.action_mark_done()
        run.action_start()  # prepared by current user
        run.with_user(self.other_manager).action_request_approval()
        # Approver must differ from both preparer (current user) and reviewer
        # (other_manager) per segregation of duties.
        run.with_user(self.third_manager).action_approve()
        self.assertEqual(run.state, 'closed')

    def test_close_run_unapproved_blocks_sign_off(self):
        self._require_chain_modules()
        close = self._make_close_run()
        pack = self._pack()
        pack.close_run_ref = close
        pack.action_run_checks()
        # The chain check is present and failing.
        check = pack.check_ids.filtered(
            lambda c: c.code == 'chain_close_approved')
        self.assertTrue(check)
        self.assertEqual(check.status, 'fail')
        self.assertTrue(pack.has_blocking_failure)
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        self.assertNotEqual(pack.state, 'signed_off')

    def test_close_run_approved_allows_sign_off(self):
        self._require_chain_modules()
        close = self._make_close_run()
        self._approve_close_run(close)
        pack = self._pack()
        pack.close_run_ref = close
        pack.action_run_checks()
        check = pack.check_ids.filtered(
            lambda c: c.code == 'chain_close_approved')
        self.assertEqual(check.status, 'pass')
        self.assertFalse(pack.has_blocking_failure)
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')

    # ---- year-end leg ----

    def _make_posted_year_end(self):
        # Post a small fiscal year of income and expense so the year-end
        # close has balances to zero, then compute and post the run.
        self.post_balanced_move([
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ], date=date(2026, 3, 15))
        self.post_balanced_move([
            {'account': self.account_expense, 'debit': 2000.0},
            {'account': self.account_cash, 'credit': 2000.0},
        ], date=date(2026, 4, 1))
        retained = self._ensure_account(
            self.env, '3100', 'Retained Earnings', 'equity')
        run = self.env['eh.year.end.run'].create({
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': retained.id,
            'lock_after_post': False,
            # Disabling the lock requires a documented reason on post.
            'no_lock_reason': 'test fixture: audit pack chain probe'})
        run.action_compute()
        return run

    def test_year_end_unposted_blocks_sign_off(self):
        self._require_chain_modules()
        run = self._make_posted_year_end()  # computed, not yet posted
        pack = self._pack()
        pack.year_end_run_ref = run
        pack.action_run_checks()
        check = pack.check_ids.filtered(
            lambda c: c.code == 'chain_year_end_posted')
        self.assertTrue(check)
        self.assertEqual(check.status, 'fail')
        self.assertTrue(pack.has_blocking_failure)
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        self.assertNotEqual(pack.state, 'signed_off')

    def test_year_end_posted_allows_sign_off(self):
        self._require_chain_modules()
        run = self._make_posted_year_end()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        pack = self._pack()
        pack.year_end_run_ref = run
        pack.action_run_checks()
        check = pack.check_ids.filtered(
            lambda c: c.code == 'chain_year_end_posted')
        self.assertEqual(check.status, 'pass')
        self.assertFalse(pack.has_blocking_failure)
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')

    # ---- both legs + live re-verification ----

    def test_full_chain_gate(self):
        # Both legs linked: sign-off only succeeds once close is approved
        # AND year-end is posted.
        self._require_chain_modules()
        close = self._make_close_run()
        year_end = self._make_posted_year_end()
        pack = self._pack()
        pack.close_run_ref = close
        pack.year_end_run_ref = year_end
        # Neither leg complete: blocked.
        pack.action_run_checks()
        self.assertTrue(pack.has_blocking_failure)
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        # Post year-end before close approval: approving the close advances
        # the fiscal lock date, so reversing this order must fail closed.
        year_end.action_post()
        pack.action_run_checks()
        with self.assertRaises(UserError):
            pack.with_user(self.other_manager).action_sign_off()
        # Complete close leg: chain complete, sign-off succeeds.
        self._approve_close_run(close)
        pack.action_run_checks()
        self.assertFalse(pack.has_blocking_failure)
        pack.with_user(self.other_manager).action_sign_off()
        self.assertEqual(pack.state, 'signed_off')

    def test_chain_links_are_frozen_after_checks(self):
        # A preparer cannot add, remove or repoint a control link after the
        # manager-facing evidence was generated.
        self._require_chain_modules()
        pack = self._pack()
        pack.action_run_checks()  # clean, no chain links yet
        self.assertFalse(pack.has_blocking_failure)
        # A direct write after checks is rejected before it can change the gate.
        close = self._make_close_run()
        with self.assertRaises(AccessError):
            pack.close_run_ref = close
        self.assertNotEqual(pack.state, 'signed_off')
