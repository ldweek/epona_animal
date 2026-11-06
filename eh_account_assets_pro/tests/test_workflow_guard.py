# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the eh.workflow.guard mixin must block a plain user from
RPC-writing a workflow state straight past the model's own actions (and the
journal entries / posting checks those actions run).

The guard only fires for a non-superuser without the eh_workflow_action
context flag. The test env runs as SUPERUSER, so every attempt below is made
with_user(a normal, non-manager accounting user); a superuser write would
(correctly) not be blocked.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'post_install', '-at_install')
class TestWorkflowGuard(EhAssetTestCase):

    def setUp(self):
        super().setUp()
        # A plain accounting user: has read/write ACL on all three models,
        # so any refusal below comes from the workflow guard, not a missing
        # access-control-list right.
        self.clerk = self._make_non_manager_user()

    def test_asset_state_write_blocked_for_plain_user(self):
        asset = self._make_asset()
        self.assertEqual(asset.state, 'draft')
        # Sanity: the clerk CAN write a non-guarded field, proving the
        # refusal below is the guard and not a blanket ACL denial.
        asset.with_user(self.clerk).write({'code': 'IT-RENAMED'})
        # Jumping straight to 'running' skips action_activate, its posting
        # setup validation and schedule build. The guard must refuse it.
        with self.assertRaises(AccessError):
            asset.with_user(self.clerk).write({'state': 'running'})
        # And the terminal 'disposed' state (normally the dispose wizard's
        # balanced gain/loss entry) is equally protected.
        with self.assertRaises(AccessError):
            asset.with_user(self.clerk).write({'state': 'disposed'})
        self.assertEqual(asset.state, 'draft')

    def test_lease_state_write_blocked_for_plain_user(self):
        lease = self._make_lease()
        self.assertEqual(lease.state, 'draft')
        with self.assertRaises(AccessError):
            lease.with_user(self.clerk).write({'state': 'active'})
        self.assertEqual(lease.state, 'draft')

    def test_impairment_state_write_blocked_for_plain_user(self):
        asset = self._make_asset()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 5_000.0,
            'is_reversal': False,
            'reason': 'Recoverable amount fell below carrying amount',
        })
        self.assertEqual(impairment.state, 'draft')
        # Flipping to 'posted' by hand skips action_post and its GL entry.
        with self.assertRaises(AccessError):
            impairment.with_user(self.clerk).write({'state': 'posted'})
        self.assertEqual(impairment.state, 'draft')

    def test_legitimate_action_still_transitions_state(self):
        # The guard must not break the sanctioned path: action_activate
        # (run as the manager test user) still moves draft -> running.
        asset = self._make_asset()
        asset.action_activate()
        self.assertEqual(asset.state, 'running')

    @staticmethod
    def _direct_write_value(record, field_name):
        """Return a syntactically valid no-op value for a guarded field."""
        value = record[field_name]
        if record._fields[field_name].type == 'many2one':
            return value.id or False
        return value

    def test_every_assets_server_owned_field_blocks_plain_rpc_write(self):
        """Exercise every declared guard, including readonly audit fields."""
        asset = self._make_asset(code='RPC-GUARD-ASSET')
        asset.action_compute_schedule()
        lease = self._make_lease(reference='RPC-GUARD-LEASE')
        lease.action_compute_schedule()
        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-06-30',
            'amount': 500.0,
            'reason': 'RPC guard coverage',
        })
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'RPC Guard Pool',
            'company_id': self.company.id,
        })
        pool_line = self.env['eh.asset.lvp.pool.line'].create({
            'pool_id': pool.id,
            'year': 2026,
            'amount': 100.0,
        })
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'RPC Guard CGU',
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
        })
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'RPC Guard Book',
            'book_type': 'tax',
            'method': 'straight_line',
            'useful_life_months': 12,
        })
        book.action_compute_schedule()
        guarded_records = (
            asset,
            asset.depreciation_line_ids[:1],
            lease,
            lease.schedule_line_ids[:1],
            impairment,
            pool_line,
            cgu,
            book.line_ids[:1],
        )
        for record in guarded_records:
            self.assertTrue(record, "expected guard fixture record")
            for field_name in record._eh_guarded_fields:
                with self.subTest(
                    model=record._name,
                    field=field_name,
                ):
                    value = self._direct_write_value(record, field_name)
                    with self.assertRaises(AccessError):
                        record.with_user(self.clerk).write({
                            field_name: value,
                        })

    def test_foreign_company_ids_cannot_cross_workflow_elevation(self):
        """Guessed ids must fail record rules before helper calls sudo."""
        asset = self._make_asset()
        asset.action_activate()
        foreign_company = self.env['res.company'].with_context(
            default_group_rfq='default',
        ).create({'name': 'Assets Foreign Company'})
        foreign_manager = new_test_user(
            self.env,
            login='assets_foreign_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=foreign_company.id,
            company_ids=[(6, 0, [foreign_company.id])],
        )
        foreign_asset = asset.with_user(foreign_manager).with_context(
            allowed_company_ids=[foreign_company.id],
        )
        with self.assertRaises(AccessError):
            foreign_asset.action_pause()
        self.assertEqual(asset.state, 'running')

        line = asset.depreciation_line_ids[:1]
        with self.assertRaises(AccessError):
            line.with_user(foreign_manager).with_context(
                allowed_company_ids=[foreign_company.id],
            ).action_post()
        self.assertFalse(line.is_posted)

    def test_generic_account_manager_cannot_bypass_eh_manager_wizards(self):
        """Suite workflows require the suite's own SoD manager privilege."""
        generic_manager = new_test_user(
            self.env,
            login='assets_generic_account_manager',
            groups=(
                'eh_account_base.group_eh_user,'
                'account.group_account_manager'
            ),
            company_id=self.company.id,
            company_ids=[(6, 0, [self.company.id])],
        )
        self.assertTrue(
            generic_manager.has_group('account.group_account_manager'),
        )
        self.assertFalse(
            generic_manager.has_group('eh_account_base.group_eh_manager'),
        )

        asset = self._make_asset(code='GENERIC-MANAGER-ASSET')
        asset.action_activate()
        lease = self._make_lease(reference='GENERIC-MANAGER-LEASE')
        lease.action_activate()

        wizards = (
            self.env['eh.asset.revalue.wizard'].create({
                'asset_id': asset.id,
                'direction': 'uplift',
                'amount': 100.0,
                'counterpart_account_id': self.account_reval_reserve.id,
            }),
            self.env['eh.asset.dispose.wizard'].create({
                'asset_id': asset.id,
            }),
            self.env['eh.lease.modify.wizard'].create({
                'lease_id': lease.id,
                'new_term_months': 24,
                'new_payment_amount': 900.0,
                'new_ibr': 7.0,
            }),
            self.env['eh.lease.terminate.wizard'].create({
                'lease_id': lease.id,
                'pl_account_id': self.account_termination_pl.id,
            }),
        )
        actions = (
            'action_revalue',
            'action_dispose',
            'action_modify',
            'action_terminate',
        )
        move_count = self.env['account.move'].search_count([])
        for wizard, action in zip(wizards, actions):
            with self.subTest(action=action):
                with self.assertRaises(UserError):
                    getattr(wizard.with_user(generic_manager), action)()
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertEqual(asset.state, 'running')
        self.assertEqual(lease.state, 'active')

    def test_foreign_company_ids_cannot_reach_wizard_side_effects(self):
        """Public transient actions must check their source before posting."""
        asset = self._make_asset(code='FOREIGN-WIZARD-ASSET')
        asset.action_activate()
        lease = self._make_lease(reference='FOREIGN-WIZARD-LEASE')
        lease.action_activate()

        foreign_company = self.env['res.company'].with_context(
            default_group_rfq='default',
        ).create({'name': 'Assets Foreign Wizard Company'})
        foreign_manager = new_test_user(
            self.env,
            login='assets_foreign_wizard_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=foreign_company.id,
            company_ids=[(6, 0, [foreign_company.id])],
        )
        wizards = (
            self.env['eh.asset.revalue.wizard'].create({
                'asset_id': asset.id,
                'direction': 'uplift',
                'amount': 100.0,
                'counterpart_account_id': self.account_reval_reserve.id,
            }),
            self.env['eh.asset.dispose.wizard'].create({
                'asset_id': asset.id,
            }),
            self.env['eh.lease.modify.wizard'].create({
                'lease_id': lease.id,
                'new_term_months': 24,
                'new_payment_amount': 900.0,
                'new_ibr': 7.0,
            }),
            self.env['eh.lease.terminate.wizard'].create({
                'lease_id': lease.id,
                'pl_account_id': self.account_termination_pl.id,
            }),
        )
        actions = (
            'action_revalue',
            'action_dispose',
            'action_modify',
            'action_terminate',
        )
        move_count = self.env['account.move'].search_count([])
        foreign_context = {'allowed_company_ids': [foreign_company.id]}
        for wizard, action in zip(wizards, actions):
            with self.subTest(action=action):
                foreign_wizard = wizard.with_user(
                    foreign_manager,
                ).with_context(**foreign_context)
                with self.assertRaises(AccessError):
                    getattr(foreign_wizard, action)()
        self.assertEqual(self.env['account.move'].search_count([]), move_count)
        self.assertEqual(asset.state, 'running')
        self.assertEqual(lease.state, 'active')

    def test_foreign_company_direct_writes_and_unlinks_fail_record_rules(self):
        """Pre-guard reads must not leak or bypass company isolation."""
        asset = self._make_asset(
            code='FOREIGN-DIRECT-ASSET',
            in_service_date='2025-12-31',
            acquisition_cost=120000.0,
            useful_life_months=120,
        )
        asset.action_activate()
        asset.depreciation_line_ids.sorted('sequence')[0].action_post()
        dep_line = asset.depreciation_line_ids.filtered('is_posted')[:1]
        self.assertTrue(dep_line)

        lease = self._make_lease(
            reference='FOREIGN-DIRECT-LEASE',
            commencement_date='2025-01-31',
            term_months=12,
        )
        lease.action_activate()
        lease_line = lease.schedule_line_ids[:1]
        lease_line.action_post()

        impairment = self.env['eh.asset.impairment'].create({
            'asset_id': asset.id,
            'impairment_date': '2026-03-31',
            'amount': 500.0,
            'reason': 'Foreign-company guard fixture',
            'impairment_account_id': self.account_impairment.id,
            'accumulated_account_id': self.account_accum_dep.id,
        })
        impairment.action_post()

        pool_account = self._ensure_account(
            self.env, '1549', 'Foreign Direct LVP Pool', 'asset_fixed',
        )
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'Foreign Direct Pool',
            'company_id': self.company.id,
            'threshold': 1000.0,
            'pool_account_id': pool_account.id,
            'accumulated_account_id': self.account_accum_dep.id,
            'expense_account_id': self.account_dep_expense.id,
            'journal_id': self.journal_misc.id,
        })
        small = self._make_asset(
            code='FOREIGN-DIRECT-LVP',
            acquisition_cost=1000.0,
            in_service_date='2026-03-15',
        )
        pool.action_transfer_asset(small, transfer_date='2026-03-15')
        pool_line = pool.action_compute_year(year=2026)
        pool_line.action_post()

        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Foreign Direct Book',
            'book_type': 'statutory',
            'method': 'straight_line',
            'useful_life_months': 12,
        })
        book.action_compute_schedule()
        book_line = book.line_ids.sorted('sequence')[:1]
        cgu = self.env['eh.asset.cgu'].create({
            'name': 'Foreign Direct CGU',
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
        })

        foreign_company = self.env['res.company'].with_context(
            default_group_rfq='default',
        ).create({'name': 'Assets Foreign Direct Company'})
        foreign_manager = new_test_user(
            self.env,
            login='assets_foreign_direct_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=foreign_company.id,
            company_ids=[(6, 0, [foreign_company.id])],
        )

        attempts = (
            (asset, {'acquisition_cost': asset.acquisition_cost}),
            (dep_line, {'amount': dep_line.amount}),
            (lease, {'rou_initial_value': lease.rou_initial_value}),
            (lease_line, {'principal': lease_line.principal}),
            (impairment, {'reason': impairment.reason}),
            (pool_line, {'amount': pool_line.amount}),
            (book, {'method': book.method}),
            (book_line, {'amount': book_line.amount}),
            (cgu, {'last_test_date': cgu.last_test_date}),
        )
        for record, vals in attempts:
            foreign_record = record.with_user(foreign_manager).with_context(
                allowed_company_ids=[foreign_company.id],
            )
            with self.subTest(model=record._name, operation='write'):
                with self.assertRaises(AccessError):
                    foreign_record.write(vals)
            with self.subTest(model=record._name, operation='unlink'):
                with self.assertRaises(AccessError):
                    foreign_record.unlink()
