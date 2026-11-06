# -*- encoding: utf-8 -*-
"""Focused regressions for Revenue medium/low backlog remediation."""

from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
import inspect
from pathlib import Path

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_revenue', 'integration', 'post_install', '-at_install')
class TestRevenueMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )
        cls.asset = cls._ensure_account(
            cls.env, '1371', 'Backlog Contract Asset', 'asset_current'
        )
        cls.liability = cls._ensure_account(
            cls.env, '2371', 'Backlog Contract Liability',
            'liability_current',
        )
        cls.interest_income = cls._ensure_account(
            cls.env, '4271', 'Backlog Financing Income', 'income_other'
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.11' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_revenue_post_1211', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _contract(self, price=100.0, obligations=None, **extra):
        normalized = []
        for source in obligations or [{
            'name': 'Service', 'standalone_price': price,
        }]:
            vals = dict(source)
            if (
                vals.get('satisfaction') == 'over_time'
                and 'method_basis' not in vals
            ):
                vals['method_basis'] = (
                    'Measured progress depicts service transferred to date.'
                )
            normalized.append(vals)
        values = {
            'partner_id': self.partner_a.id,
            'transaction_price': price,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.asset.id,
            'contract_liability_account_id': self.liability.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, vals) for vals in normalized],
        }
        values.update(extra)
        return self.env['eh.revenue.contract'].create(values)

    @staticmethod
    def _balance(contract, account):
        return sum(contract.move_ids.filtered(
            lambda move: move.state == 'posted'
        ).line_ids.filtered(
            lambda line: line.account_id == account
        ).mapped('balance'))

    def test_rounded_allocation_reconciles_and_contract_closes(self):
        contract = self._contract(price=100.0, obligations=[
            {'name': 'A', 'standalone_price': 1.0, 'satisfied': True},
            {'name': 'B', 'standalone_price': 1.0, 'satisfied': True},
            {'name': 'C', 'standalone_price': 1.0, 'satisfied': True},
        ])
        allocations = contract.obligation_ids.sorted(
            key=lambda line: line.sequence
        ).mapped('allocated_price')
        self.assertEqual(allocations, [33.33, 33.33, 33.34])
        self.assertEqual(sum(allocations), 100.0)
        contract.action_activate()
        contract.action_recognise()
        contract.bill_amount = 100.0
        contract.action_bill()
        contract.action_close()
        self.assertEqual(contract.state, 'done')

    def test_arrears_financing_requires_exact_horizon_and_transfer(self):
        start = date(2026, 1, 31)
        with self.assertRaisesRegex(ValidationError, 'must equal'):
            self._contract(
                price=1000.0,
                financing_component=True,
                financing_direction='arrears',
                financing_rate=0.10,
                financing_period_months=12,
                financing_payment_date=date(2027, 2, 1),
                financing_account_id=self.interest_income.id,
                contract_date=start,
            )
        contract = self._contract(
            price=1000.0,
            financing_component=True,
            financing_direction='arrears',
            financing_rate=0.10,
            financing_period_months=12,
            financing_account_id=self.interest_income.id,
            contract_date=start,
        )
        with self.assertRaisesRegex(ValidationError, 'exact payment'):
            contract.action_activate()

    def test_arrears_billing_uses_contra_receivable_and_caps_nominal(self):
        start = date(2026, 1, 1)
        contract = self._contract(
            price=1000.0,
            obligations=[{
                'name': 'Financed goods', 'standalone_price': 1000.0,
                'satisfied': True,
            }],
            financing_component=True,
            financing_direction='arrears',
            financing_rate=0.10,
            financing_period_months=24,
            financing_payment_date=start + relativedelta(months=24),
            financing_account_id=self.interest_income.id,
            contract_date=start,
        )
        contract.action_activate()
        contract.action_recognise(accounting_date=start)
        self.assertAlmostEqual(contract.amount_recognised, 826.45, places=2)
        contract.bill_amount = 1000.0
        contract.action_bill(accounting_date=start)
        self.assertAlmostEqual(
            self._balance(contract, self.account_receivable), 826.45,
            places=2,
        )
        self.assertAlmostEqual(
            self._balance(contract, self.liability), 0.0, places=2
        )
        contract.bill_amount = 0.01
        with self.assertRaisesRegex(UserError, 'exceed'):
            contract.action_bill(accounting_date=start)

    def test_net_zero_reallocation_synchronises_each_anchor(self):
        contract = self._contract(price=100.0, obligations=[
            {
                'name': 'First', 'standalone_price': 50.0,
                'satisfaction': 'over_time', 'percent_complete': 100.0,
            },
            {
                'name': 'Second', 'standalone_price': 50.0,
                'satisfaction': 'over_time', 'percent_complete': 0.0,
            },
        ])
        contract.action_activate()
        contract.action_recognise()
        moves_before = len(contract.move_ids)
        first = contract.obligation_ids.filtered(lambda ob: ob.name == 'First')
        second = contract.obligation_ids - first
        first.percent_complete = 80.0
        second.percent_complete = 20.0
        self.assertAlmostEqual(first.to_recognise, -10.0, places=2)
        self.assertAlmostEqual(second.to_recognise, 10.0, places=2)
        contract.action_recognise()
        self.assertEqual(len(contract.move_ids), moves_before)
        self.assertAlmostEqual(first.recognised_amount, 40.0, places=2)
        self.assertAlmostEqual(second.recognised_amount, 10.0, places=2)
        self.assertAlmostEqual(first.to_recognise, 0.0, places=2)
        self.assertAlmostEqual(second.to_recognise, 0.0, places=2)

    def test_modification_wizard_carries_progress_basis_and_validates(self):
        contract = self._contract()
        contract.action_activate()
        wizard = self.env['eh.revenue.modification.wizard'].create({
            'contract_id': contract.id,
            'method': 'separate',
            'effective_date': contract.contract_date,
            'set_new_price': True,
            'new_transaction_price': 50.0,
            'line_ids': [(0, 0, {
                'name': 'Added service',
                'standalone_price': 50.0,
                'satisfaction': 'over_time',
                'progress_method': 'input_time',
                'method_basis': 'Elapsed service faithfully depicts transfer.',
            })],
        })
        action = wizard.action_apply()
        separate = self.env['eh.revenue.contract'].browse(action['res_id'])
        self.assertEqual(separate.state, 'active')
        self.assertEqual(separate.obligation_ids.progress_method, 'input_time')
        self.assertEqual(
            separate.obligation_ids.method_basis,
            'Elapsed service faithfully depicts transfer.',
        )
        with self.assertRaisesRegex(UserError, 'zero total'):
            contract._apply_modification(
                'separate',
                added_obligations=[{
                    'name': 'Invalid zero SSP', 'standalone_price': 0.0,
                }],
                new_transaction_price=0.0,
                effective_date=contract.contract_date,
            )

    def test_contract_search_and_posted_account_readonly_semantics(self):
        search = etree.fromstring(self.env.ref(
            'eh_account_revenue.view_eh_revenue_contract_search'
        ).arch_db.encode())
        self.assertTrue(search.xpath(".//filter[@name='active']"))
        action = self.env.ref('eh_account_revenue.action_eh_revenue_contract')
        self.assertEqual(
            action.search_view_id,
            self.env.ref('eh_account_revenue.view_eh_revenue_contract_search'),
        )
        form = etree.fromstring(self.env.ref(
            'eh_account_revenue.view_eh_revenue_contract_form'
        ).arch_db.encode())
        for field_name in (
            'revenue_account_id', 'contract_asset_account_id',
            'contract_liability_account_id', 'receivable_account_id',
            'journal_id', 'financing_account_id',
        ):
            node = form.xpath(".//field[@name='%s']" % field_name)[0]
            semantics = node.get('readonly', '') + node.get('attrs', '')
            self.assertIn('move_count', semantics, field_name)
        method = form.xpath(
            ".//field[@name='obligation_ids']//field[@name='method_basis']"
        )[0]
        self.assertEqual(method.get('optional'), 'show')

    def test_sequence_is_exact_company_scoped(self):
        other = self.env['res.company'].create({
            'name': 'Revenue Sequence Scope Company',
            'currency_id': self.company.currency_id.id,
        })
        self.env.user.sudo().write({'company_ids': [(4, other.id)]})
        other_env = self.env['eh.revenue.contract'].with_company(other).with_context(
            allowed_company_ids=[other.id],
        )
        contract = other_env.create({
            'partner_id': self.partner_a.id,
            'company_id': other.id,
            'transaction_price': 1.0,
        })
        self.assertNotEqual(contract.name, '/')
        sequences = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'eh.revenue.contract'),
            ('company_id', 'in', [self.company.id, other.id]),
        ])
        self.assertEqual(set(sequences.mapped('company_id').ids), {
            self.company.id, other.id,
        })
        self.assertFalse(self.env['ir.sequence'].sudo().search([
            ('code', '=', 'eh.revenue.contract'),
            ('company_id', '=', False),
        ], limit=1))

    def test_locked_accounting_date_refuses_recognition(self):
        if 'fiscalyear_lock_date' not in self.company._fields:
            self.skipTest('fiscalyear_lock_date unavailable')
        posting_date = date(2026, 6, 30)
        contract = self._contract(obligations=[{
            'name': 'Locked service', 'standalone_price': 100.0,
            'satisfied': True,
        }])
        contract.action_activate()
        original = self.company.fiscalyear_lock_date
        try:
            self.env.cr.execute(
                "UPDATE res_company SET fiscalyear_lock_date = %s "
                "WHERE id = %s",
                (posting_date, self.company.id),
            )
            self.company.invalidate_recordset(['fiscalyear_lock_date'])
            with self.assertRaises(UserError):
                contract.action_recognise(accounting_date=posting_date)
            self.assertFalse(contract.move_ids)
        finally:
            self.env.cr.execute(
                "UPDATE res_company SET fiscalyear_lock_date = %s "
                "WHERE id = %s",
                (original or None, self.company.id),
            )
            self.company.invalidate_recordset(['fiscalyear_lock_date'])

    def test_upgrade_marks_only_legacy_basis_and_scopes_sequence(self):
        contract = self._contract()
        obligation = contract.obligation_ids
        posted = self._contract(obligations=[{
            'name': 'Posted legacy service', 'standalone_price': 100.0,
            'satisfied': True,
        }])
        posted.action_activate()
        posted.action_recognise()
        sequence = self.env.ref(
            'eh_account_revenue.seq_eh_revenue_contract'
        ).sudo()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_revenue_obligation "
            "SET satisfaction = 'over_time', method_basis = NULL "
            "WHERE id = %s",
            (obligation.id,),
        )
        self.env.cr.execute(
            "UPDATE ir_sequence SET company_id = NULL WHERE id = %s",
            (sequence.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_revenue_contract SET amount_recognised = 999.0 "
            "WHERE id = %s",
            (posted.id,),
        )
        self.migration.migrate(self.env.cr, '19.0.1.2.10')
        obligation.invalidate_recordset(['satisfaction', 'method_basis'])
        sequence.invalidate_recordset(['company_id'])
        posted.invalidate_recordset(['amount_recognised'])
        self.assertEqual(obligation.method_basis, 'migrated')
        self.assertEqual(sequence.company_id, self.env.ref('base.main_company'))
        self.assertEqual(posted.amount_recognised, 100.0)

    def test_reference_validation_once_and_sql_wrapper_queries(self):
        Contract = type(self.env['eh.revenue.contract'])
        prepare_source = inspect.getsource(Contract._eh_prepare_action)
        self.assertEqual(prepare_source.count('_eh_validate_references()'), 1)
        ledger_source = inspect.getsource(
            Contract._eh_ledger_contract_account_balances
        )
        lock_source = inspect.getsource(Contract._eh_lock_for_transition)
        self.assertIn('SQL(', ledger_source)
        self.assertIn('SQL(', lock_source)
        self.assertNotIn('date_clause', ledger_source)
