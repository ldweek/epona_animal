# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the revalidated Inventory NRV backlog."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from lxml import etree
from psycopg2.errors import CheckViolation

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_inventory_nrv', 'post_install', '-at_install')
class TestInventoryNrvMediumLow(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.expense = cls._ensure_account(
            cls.env, '5170', 'NRV Backlog Expense', 'expense')
        cls.allowance = cls._ensure_account(
            cls.env, '1470', 'NRV Backlog Allowance', 'asset_current')
        cls.plain_user = cls.env['res.users'].with_context(
            no_reset_password=True,
            mail_create_nosubscribe=True,
        ).create({
            'name': 'NRV backlog preparer',
            'login': 'nrv_backlog_preparer',
            'email': 'nrv_backlog_preparer@example.com',
            'company_id': cls.company.id,
            'group_ids': [(6, 0, [cls.env.ref(
                'eh_account_base.group_eh_user').id])],
        })
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.1' / 'pre-migration.py'
        )
        spec = spec_from_file_location(
            'eh_inventory_nrv_migration_121_pre_test', migration_path,
        )
        cls.pre_migration_121 = module_from_spec(spec)
        spec.loader.exec_module(cls.pre_migration_121)

    def _run(self, reporting_date='2028-06-30', lines=None, **values):
        payload = {
            'reporting_date': reporting_date,
            'writedown_expense_account_id': self.expense.id,
            'allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, line) for line in (lines or [{
                'name': 'Stock', 'cost': 100.0,
                'net_realisable_value': 80.0,
            }])],
        }
        payload.update(values)
        return self.env['eh.nrv.run'].create(payload)

    @staticmethod
    def _manual_line(name, cost, nrv, opening):
        return {
            'name': name,
            'cost': cost,
            'net_realisable_value': nrv,
            'opening_writedown_manual': True,
            'manual_opening_writedown': opening,
            'opening_override_reason': 'Manager-approved opening evidence',
        }

    def test_nrv_and_allowances_are_nonnegative_and_bounded_by_cost(self):
        with self.assertRaises(CheckViolation):
            with self.env.cr.savepoint():
                self._run(lines=[{
                    'name': 'Negative NRV', 'cost': 100.0,
                    'net_realisable_value': -10.0,
                }])
        with self.assertRaises(CheckViolation):
            with self.env.cr.savepoint():
                self._run(lines=[self._manual_line(
                    'Opening above cost', 100.0, 80.0, 110.0,
                )])
        run = self._run(lines=[{
            'name': 'Zero NRV', 'cost': 100.0,
            'net_realisable_value': 0.0,
        }])
        self.assertEqual(run.line_ids.required_writedown, 100.0)
        run._eh_validate_measurements()

    def test_manual_opening_is_explicit_manager_approved_and_reasoned(self):
        product = self.env['product.product'].create({
            'name': 'NRV opening provenance product',
        })
        prior = self._run(reporting_date='2028-03-31', lines=[{
            'name': 'Prior', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 70.0,
        }])
        prior.action_compute()
        prior.action_post()
        current = self._run(lines=[{
            'name': 'Current', 'product_id': product.id,
            'cost': 100.0, 'net_realisable_value': 80.0,
            # Simulate an onchange-derived client echo. It is not intent.
            'opening_writedown': 99.0,
        }])
        line = current.line_ids
        self.assertFalse(line.opening_writedown_manual)
        self.assertEqual(line.opening_source, 'automatic')
        self.assertEqual(line.opening_writedown, 30.0)
        with self.assertRaises(AccessError):
            line.with_user(self.plain_user).write({
                'opening_writedown_manual': True,
                'manual_opening_writedown': 25.0,
            })

        line.write({
            'opening_writedown_manual': True,
            'manual_opening_writedown': 25.0,
        })
        current.action_compute()
        with self.assertRaisesRegex(UserError, 'reason'):
            current.action_post()
        line.opening_override_reason = 'Agreed to signed legacy allowance file'
        current.action_post()
        self.assertEqual(line.opening_source, 'manual')
        self.assertEqual(line.opening_override_by_id, self.env.user)
        self.assertTrue(line.opening_override_at)

    def test_prior_opening_queries_are_constant_for_a_populated_run(self):
        products = self.env['product.product'].create([
            {'name': 'NRV query product %s' % index}
            for index in range(20)
        ])
        prior = self._run(reporting_date='2028-03-31', lines=[{
            'name': product.name,
            'product_id': product.id,
            'cost': 100.0,
            'net_realisable_value': 80.0,
        } for product in products])
        prior.action_compute()
        prior.action_post()
        current = self._run(lines=[{
            'name': product.name,
            'product_id': product.id,
            'cost': 100.0,
            'net_realisable_value': 90.0,
        } for product in products])
        LineClass = type(current.line_ids)
        real_search = LineClass.search
        calls = []

        def counted_search(records, *args, **kwargs):
            calls.append(args[0] if args else kwargs.get('domain'))
            return real_search(records, *args, **kwargs)

        with patch.object(LineClass, 'search', new=counted_search):
            expected_by_line = current.line_ids._prior_closing_writedown_map()
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(expected_by_line), set(current.line_ids.ids))
        self.assertEqual(set(expected_by_line.values()), {20.0})
        self.assertEqual(set(current.line_ids.mapped('opening_writedown')), {20.0})

    def test_draft_edits_lock_runs_without_locking_company(self):
        run = self._run()
        RunClass = type(run)
        with patch.object(
                RunClass, '_eh_lock_company_rows',
                side_effect=AssertionError('draft edit locked company')):
            run.notes = 'Plain disclosure note'
            created = self.env['eh.nrv.line'].create({
                'run_id': run.id,
                'name': 'Additional stock',
                'cost': 50.0,
                'net_realisable_value': 45.0,
            })
            created.cost = 55.0
            created.unlink()

    def test_preparer_acl_and_navigation_are_aligned(self):
        run_acl = self.env.ref(
            'eh_account_inventory_nrv.access_eh_nrv_run_user')
        line_acl = self.env.ref(
            'eh_account_inventory_nrv.access_eh_nrv_line_user')
        self.assertTrue(run_acl.perm_create and run_acl.perm_write)
        self.assertTrue(run_acl.perm_unlink and line_acl.perm_unlink)
        menu = self.env.ref('eh_account_inventory_nrv.menu_eh_nrv_run')
        menu_groups = (
            menu.group_ids if 'group_ids' in menu._fields else menu.groups_id
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_user'), menu_groups,
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_auditor'), menu_groups,
        )

    def test_gross_writedown_and_reversal_reconcile_to_net_movement(self):
        run = self._run(lines=[
            self._manual_line('Charge', 100.0, 60.0, 10.0),
            self._manual_line('Recovery', 100.0, 90.0, 30.0),
        ])
        self.assertEqual(run.gross_writedown, 30.0)
        self.assertEqual(run.gross_reversal, 20.0)
        self.assertEqual(run.movement, 10.0)
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.expense, 10.0, 0.0),
            (self.allowance, 0.0, 10.0),
        ])

    def test_accounts_are_distinct_typed_and_company_scoped(self):
        with self.assertRaisesRegex(ValidationError, 'different posting legs'):
            with self.env.cr.savepoint():
                self._run(
                    writedown_expense_account_id=self.expense.id,
                    allowance_account_id=self.expense.id,
                )
        with self.assertRaisesRegex(ValidationError, 'must be an expense'):
            with self.env.cr.savepoint():
                wrong_expense = self._ensure_account(
                    self.env, '1481', 'NRV wrong expense type',
                    'asset_current',
                )
                self._run(writedown_expense_account_id=wrong_expense.id)
        expense_domain = self.env['eh.nrv.run']._fields[
            'writedown_expense_account_id'
        ].domain
        allowance_domain = self.env['eh.nrv.run']._fields[
            'allowance_account_id'
        ].domain
        for domain in (expense_domain, allowance_domain):
            self.assertIn("('company_ids', 'parent_of', company_id)", domain)

    def test_reversal_nets_ledger_and_navigation_exposes_evidence(self):
        run = self._run()
        run.action_compute()
        run.action_post()
        if 'fiscalyear_lock_date' in self.company._fields:
            self.env.cr.execute(
                "UPDATE res_company SET fiscalyear_lock_date = %s "
                "WHERE id = %s",
                ('2028-07-15', self.company.id),
            )
            self.company.invalidate_recordset(['fiscalyear_lock_date'])
            run.reversal_date = '2028-07-15'
            with self.assertRaises(UserError):
                with self.env.cr.savepoint():
                    run.action_reverse()
            run.invalidate_recordset(['state', 'reversal_move_id'])
        run.reversal_date = '2028-07-16'
        run.action_reverse()
        self.assertEqual(run.reversal_move_id.date, run.reversal_date)
        combined = run.move_id.line_ids | run.reversal_move_id.line_ids
        for account in self.expense | self.allowance:
            lines = combined.filtered(lambda line: line.account_id == account)
            self.assertAlmostEqual(
                sum(lines.mapped('debit')) - sum(lines.mapped('credit')),
                0.0,
                places=2,
            )
        self.assertEqual(
            run.action_view_reversal_move()['res_id'], run.reversal_move_id.id,
        )
        replacement = self._run()
        self.assertEqual(replacement.supersedes_run_id, run)
        self.assertEqual(
            replacement.action_view_superseded_run()['res_id'], run.id,
        )
        search_arch = etree.fromstring(self.env.ref(
            'eh_account_inventory_nrv.view_eh_nrv_run_search'
        ).arch_db.encode())
        self.assertEqual(len(search_arch.xpath(
            "//filter[@name='filter_reversal_entries']"
        )), 1)
        self.assertEqual(len(search_arch.xpath(
            "//filter[@name='filter_revisions']"
        )), 1)

    def test_reference_validation_is_not_repeated_three_times(self):
        run = self._run()
        RunClass = type(run)
        real_validate = RunClass._eh_validate_references
        calls = []

        def counted_validate(records, *args, **kwargs):
            calls.append(True)
            return real_validate(records, *args, **kwargs)

        with patch.object(
                RunClass, '_eh_validate_references', new=counted_validate):
            run.write({'journal_id': run.journal_id.id})
        self.assertEqual(len(calls), 2)

    def test_authorization_failures_are_access_errors(self):
        run = self._run()
        run.action_compute()
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).action_post()
        run.action_post()
        run.reversal_date = '2028-07-16'
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).action_reverse()

    def test_opening_provenance_fields_are_exposed(self):
        arch = etree.fromstring(self.env.ref(
            'eh_account_inventory_nrv.view_eh_nrv_run_form'
        ).arch_db.encode())
        for field_name in (
            'opening_source', 'opening_writedown_manual',
            'manual_opening_writedown', 'opening_override_reason',
            'opening_override_by_id', 'opening_override_at',
        ):
            self.assertTrue(arch.xpath(
                "//field[@name='%s']" % field_name,
            ))

    def test_migration_preserves_existing_explicit_manual_opening(self):
        run = self._run(lines=[self._manual_line(
            'Legacy explicit opening', 100.0, 80.0, 15.0,
        )])
        line = run.line_ids
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_nrv_line SET manual_opening_writedown = NULL "
            "WHERE id = %s",
            (line.id,),
        )
        self.pre_migration_121.migrate(self.env.cr, '19.0.1.2.0')
        line.invalidate_recordset(['manual_opening_writedown'])
        self.assertEqual(line.manual_opening_writedown, 15.0)
