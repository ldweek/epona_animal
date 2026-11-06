# -*- encoding: utf-8 -*-
"""Focused regressions for the Audit Pack medium/low audit backlog."""

from datetime import date
from unittest.mock import patch

from freezegun import freeze_time

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_audit_pack', 'integration', 'post_install', '-at_install')
@freeze_time('2027-01-02')
class TestAuditPackMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group_user = cls.env.ref('eh_account_base.group_eh_user')
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        cls.reviewer = cls.env['res.users'].create({
            'name': 'Audit Backlog Reviewer',
            'login': 'audit_backlog_reviewer@test',
            'email': 'audit_backlog_reviewer@test',
            'group_ids': [Command.set([cls.group_manager.id])],
        })
        cls.second_reviewer = cls.env['res.users'].create({
            'name': 'Audit Backlog Second Reviewer',
            'login': 'audit_backlog_second@test',
            'email': 'audit_backlog_second@test',
            'group_ids': [Command.set([cls.group_manager.id])],
        })
        cls.preparer = cls.env['res.users'].create({
            'name': 'Audit Backlog Preparer',
            'login': 'audit_backlog_preparer@test',
            'email': 'audit_backlog_preparer@test',
            'group_ids': [Command.set([
                cls.env.ref('base.group_user').id,
                cls.group_user.id,
            ])],
        })

    def _pack(self, **extra):
        vals = {
            'period_from': date(2026, 1, 1),
            'period_to': date(2026, 12, 31),
        }
        vals.update(extra)
        return self.env['eh.audit.pack'].create(vals)

    def _pack_with_hash_chain(self):
        pack = self._pack()
        if (
            'inalterable_hash' in self.env['account.move']._fields
            and 'restrict_mode_hash_table'
            in self.env['account.journal']._fields
        ):
            pack.action_enable_hash_chain()
        return pack

    def test_parent_scope_includes_branch_domains_and_journals(self):
        pack = self._pack()
        self.assertIn(
            ('company_id', 'child_of', self.company.id),
            pack._move_domain(),
        )
        branch = self._create_accounting_branch({
            'name': 'Audit Pack Branch',
            'parent_id': self.company.id,
        })
        self.env.user.write({'company_ids': [Command.link(branch.id)]})
        branch_env = self.env['res.company'].with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).with_company(branch).env
        branch_journal = self._ensure_journal(
            branch_env, branch, 'general', 'APBR', 'Audit Branch Journal',
        )
        self.assertIn(branch_journal, pack._posting_journals(self.company))

    def test_control_inputs_freeze_after_checks_and_lock_is_mandatory(self):
        with self.assertRaises(ValidationError):
            self._pack(advance_lock=False)
        pack = self._pack()
        pack.action_run_checks()
        for vals in (
            {'period_from': date(2026, 2, 1)},
            {'period_to': date(2026, 11, 30)},
            {'company_id': self.company.id},
            {'advance_lock': False},
            {'close_run_ref': False},
            {'year_end_run_ref': False},
        ):
            with self.assertRaises(AccessError):
                pack.with_user(self.preparer).write(vals)

    def test_balance_check_aggregates_and_detects_database_imbalance(self):
        move = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 20.0},
            {'account': self.account_revenue, 'credit': 20.0},
        ], date=date(2026, 6, 1))
        pack = self._pack()
        self.assertEqual(pack._check_balanced()['status'], 'pass')
        line = move.line_ids.filtered(lambda item: item.debit)[:1]
        self.env.cr.execute(
            "UPDATE account_move_line SET balance = balance + 1 WHERE id = %s",
            (line.id,),
        )
        line.invalidate_recordset(['balance'])
        result = pack._check_balanced()
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['count'], 1)

    def test_sign_off_reuses_unchanged_core_hash_verification(self):
        if 'inalterable_hash' not in self.env['account.move']._fields:
            self.skipTest('inalterable hash chain unavailable')
        pack = self._pack_with_hash_chain()
        PackClass = type(pack)
        original = PackClass._hash_chain_corrupt
        calls = []

        def counted(record):
            calls.append(record.id)
            return original(record)

        with patch.object(PackClass, '_hash_chain_corrupt', counted):
            pack.action_run_checks()
            pack.with_user(self.reviewer).action_sign_off()
        self.assertEqual(calls, [pack.id])

    def test_core_hash_database_error_is_contained_by_savepoint(self):
        pack = self._pack()
        CompanyClass = type(pack.company_id)

        def abort_cursor(company):
            company.env.cr.execute('SELECT 1 / 0')

        with patch.object(
            CompanyClass, '_check_hash_integrity', abort_cursor,
        ):
            self.assertTrue(pack._hash_chain_corrupt())
        # The next ORM call proves the failed cursor was rolled back locally.
        self.assertTrue(self.env['res.company'].search_count([
            ('id', '=', self.company.id),
        ]))

    def test_open_suspense_is_a_blocking_lock_preflight(self):
        bank = self._ensure_journal(
            self.env, self.company, 'bank', 'APBK', 'Audit Pack Bank',
            default_account=self.account_cash,
        )
        suspense = bank.suspense_account_id
        if not suspense:
            self.skipTest('bank suspense account unavailable')
        self.post_balanced_move([
            {'account': suspense, 'debit': 12.0},
            {'account': self.account_revenue, 'credit': 12.0},
        ], date=date(2026, 5, 1))
        result = self._pack()._check_suspense()
        self.assertEqual(result['status'], 'fail')
        self.assertTrue(result['is_blocking'])

    def test_authorisation_refusals_use_access_error(self):
        pack = self._pack()
        with self.assertRaises(AccessError):
            pack.with_user(self.preparer).action_enable_hash_chain()
        with self.assertRaises(AccessError):
            pack.with_user(self.preparer).action_sign_off()

    def test_period_year_and_company_owned_sequence(self):
        pack_2025 = self.env['eh.audit.pack'].create({
            'period_from': date(2025, 1, 1),
            'period_to': date(2025, 12, 31),
        })
        self.assertIn('/2025/', pack_2025.name)
        self.assertTrue(pack_2025.name.startswith(
            'AUDIT/%s/' % self.company.id,
        ))
        sequence = self.env['ir.sequence'].with_context(
            active_test=False,
        ).search([
            ('code', '=', 'eh.audit.pack'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertTrue(sequence)
        self.assertTrue(sequence.use_date_range)

    def test_signed_pack_has_immutable_controlled_amendment(self):
        if 'inalterable_hash' not in self.env['account.move']._fields:
            self.skipTest('inalterable hash chain unavailable')
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()
        pack.with_user(self.reviewer).action_sign_off()
        original_signer = pack.signed_by_id
        self.assertTrue(pack.lock_intact)
        action = pack._create_amendment('Correct linked supporting evidence')
        amendment = self.env['eh.audit.pack'].browse(action['res_id'])
        self.assertEqual(pack.state, 'signed_off')
        self.assertEqual(pack.signed_by_id, original_signer)
        self.assertEqual(pack.superseded_by_id, amendment)
        self.assertEqual(amendment.supersedes_id, pack)
        self.assertEqual(amendment.revision, 2)
        self.assertEqual(amendment.state, 'draft')
        self.assertEqual(
            amendment.amendment_reason,
            'Correct linked supporting evidence',
        )
        with self.assertRaises(UserError):
            pack._create_amendment('Duplicate amendment')

    def test_lock_integrity_is_live_and_required_for_amendment(self):
        if 'inalterable_hash' not in self.env['account.move']._fields:
            self.skipTest('inalterable hash chain unavailable')
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()
        pack.with_user(self.reviewer).action_sign_off()
        pack.flush_recordset()
        lock_fields = [
            name for name in ('fiscalyear_lock_date', 'hard_lock_date')
            if name in self.company._fields
        ]
        self.company.flush_recordset(lock_fields)
        self.env.cr.execute(
            "UPDATE res_company SET %s WHERE id = %%s" % ', '.join(
                '%s = %%s' % name for name in lock_fields
            ),
            [date(2025, 12, 31)] * len(lock_fields) + [self.company.id],
        )
        self.company.invalidate_recordset(lock_fields, flush=False)
        pack.invalidate_recordset(['state', 'lock_intact'], flush=False)
        self.assertEqual(pack.state, 'signed_off')
        for field_name in lock_fields:
            self.assertEqual(
                self.company[field_name], date(2025, 12, 31),
            )
        self.assertFalse(pack._locks_cover_signed_period())
        self.assertFalse(pack.lock_intact)
        with self.assertRaises(UserError):
            pack._create_amendment('Locks were weakened')

    def test_second_sign_off_never_overwrites_original_signer(self):
        if 'inalterable_hash' not in self.env['account.move']._fields:
            self.skipTest('inalterable hash chain unavailable')
        pack = self._pack_with_hash_chain()
        pack.action_run_checks()
        pack.with_user(self.reviewer).action_sign_off()
        with self.assertRaises(UserError):
            pack.with_user(self.second_reviewer).action_sign_off()
        self.assertEqual(pack.signed_by_id, self.reviewer)

    def test_reference_search_menu_currency_and_dead_helpers(self):
        Pack = self.env['eh.audit.pack']
        self.assertEqual(Pack._fields['close_run_ref'].type, 'reference')
        self.assertEqual(Pack._fields['year_end_run_ref'].type, 'reference')
        self.assertFalse(hasattr(Pack, '_hash_supported'))
        self.assertFalse(hasattr(Pack, '_recompute_move_hash'))
        menu = self.env.ref('eh_account_audit_pack.menu_eh_audit_pack')
        self.assertIn(self.group_user, menu.group_ids)
        search = self.env.ref(
            'eh_account_audit_pack.view_eh_audit_pack_search',
        )
        self.assertIn('group_company', search.arch)
        form = self.env.ref('eh_account_audit_pack.view_eh_audit_pack_form')
        self.assertIn('currency_id', form.arch)
