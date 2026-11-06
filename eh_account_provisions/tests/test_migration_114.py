# -*- encoding: utf-8 -*-
"""Upgrade regressions for provision anchors and journal provenance."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_provisions', 'post_install', '-at_install')
class TestProvisionMigration114(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.provision_liab = cls._ensure_account(
            cls.env, '2994', 'Migration Provisions', 'liability_current')
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.1.4' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_provision_post_migration_114', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _provision(self, **overrides):
        vals = {
            'name': '/',
            'classification': 'provision',
            'best_estimate': 1000.0,
            'discount_rate': 10.0,
            'periods_to_settlement': 1,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        vals.update(overrides)
        return self.env['eh.provision'].create(vals)

    def _unrelated_move(self):
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': 'Unrelated sealed journal',
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': 'unrelated',
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'unrelated',
                    'account_id': self.account_equity.id,
                    'debit': 0.0,
                    'credit': 100.0,
                }),
            ],
        })
        move.action_post()
        return move

    def _allowed_account_forgery(self, provision):
        """A sealed same-ref move is still invalid without engine line shape."""
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': provision.name,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': 'forged',
                    'account_id': self.account_expense.id,
                    'debit': 100.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'forged',
                    'account_id': self.provision_liab.id,
                    'debit': 0.0,
                    'credit': 60.0,
                }),
                (0, 0, {
                    'name': 'forged',
                    'account_id': self.provision_liab.id,
                    'debit': 0.0,
                    'credit': 40.0,
                }),
            ],
        })
        move.action_post()
        return move

    def _legacy_sql(self, query, params):
        """Flush ORM work before simulating a legacy RPC/database value."""
        self.env.flush_all()
        self.env.cr.execute(query, params)
        self.env.invalidate_all()

    def _migrate(self):
        """Mirror a real upgrade boundary, where preceding ORM work is durable."""
        self.env.flush_all()
        self.migration.migrate(self.env.cr, '19.0.1.1.3')
        self.env.invalidate_all()

    def test_valid_engine_move_remains_available(self):
        provision = self._provision()
        provision.action_recognise()
        self._migrate()
        provision.invalidate_recordset()
        self.assertFalse(provision.eh_integrity_review_required)
        self.assertTrue(provision.eh_deferred_tax_temp_diffs('2026-12-31'))

    def test_unrelated_legacy_move_is_quarantined_before_tax_feed(self):
        provision = self._provision()
        move = self._unrelated_move()
        self._legacy_sql(
            "UPDATE account_move SET eh_provision_id = %s WHERE id = %s",
            [provision.id, move.id],
        )
        self._migrate()
        provision.invalidate_recordset()
        self.assertTrue(provision.eh_integrity_review_required)
        with self.assertRaises(UserError):
            provision.eh_deferred_tax_temp_diffs('2026-12-31')
        with self.assertRaises(UserError):
            provision.action_recognise()

    def test_allowed_accounts_do_not_bless_forged_line_shape(self):
        provision = self._provision()
        move = self._allowed_account_forgery(provision)
        self._legacy_sql(
            "UPDATE account_move SET eh_provision_id = %s WHERE id = %s",
            [provision.id, move.id],
        )
        self._legacy_sql(
            "UPDATE eh_provision SET state = 'recognised' WHERE id = %s",
            [provision.id],
        )
        self._migrate()
        provision.invalidate_recordset()
        self.assertTrue(provision.eh_integrity_review_required)
        with self.assertRaises(UserError):
            provision.eh_deferred_tax_temp_diffs('2026-12-31')

    def test_legacy_rpc_anchors_are_rebuilt(self):
        provision = self._provision()
        eur = self.env.ref('base.EUR')
        self._legacy_sql(
            "UPDATE eh_provision SET currency_id = %s, present_value = %s "
            "WHERE id = %s",
            [eur.id, 999999.0, provision.id],
        )
        self._migrate()
        provision.invalidate_recordset(['currency_id', 'present_value'])
        self.assertEqual(provision.currency_id, self.company.currency_id)
        self.assertAlmostEqual(provision.present_value, 909.09, places=2)
