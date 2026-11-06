# -*- encoding: utf-8 -*-
"""Focused regressions for the Employee Benefits medium/low audit tranche."""

import ast
from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_employee_benefits', 'post_install', '-at_install')
class TestEmployeeBenefitsMediumLowBacklog(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.service = cls._ensure_account(
            cls.env, '6281', 'EB Backlog Service Cost', 'expense')
        cls.interest = cls._ensure_account(
            cls.env, '6282', 'EB Backlog Net Interest', 'expense')
        cls.oci = cls._ensure_account(
            cls.env, '3281', 'EB Backlog OCI', 'equity')
        cls.net_position = cls._ensure_account(
            cls.env, '2981', 'EB Backlog Net Position',
            'liability_non_current')
        cls.gross_asset = cls._ensure_account(
            cls.env, '1981', 'EB Backlog Gross Asset', 'asset_non_current')
        cls.clearing = cls._ensure_account(
            cls.env, '2181', 'EB Backlog Contribution Clearing',
            'liability_current')
        cls.benefit_payment = cls._ensure_account(
            cls.env, '2182', 'EB Backlog Benefit Payments',
            'liability_current')
        cls.dc_expense = cls._ensure_account(
            cls.env, '6283', 'EB Backlog DC Expense', 'expense')
        cls.dc_payable = cls._ensure_account(
            cls.env, '2183', 'EB Backlog DC Payable',
            'liability_current')

    def _plan(self, name, **values):
        payload = {
            'name': name,
            'funded': True,
            'contributions_posted_elsewhere': True,
            'service_cost_account_id': self.service.id,
            'net_interest_account_id': self.interest.id,
            'oci_account_id': self.oci.id,
            'dbo_account_id': self.net_position.id,
            'plan_asset_account_id': self.net_position.id,
            'contribution_account_id': self.clearing.id,
            'contribution_cash_account_id': self.account_cash.id,
            'benefit_payment_account_id': self.benefit_payment.id,
            'journal_id': self.journal_misc.id,
        }
        payload.update(values)
        plan = self.env['eh.benefit.plan'].create(payload)
        plan.action_activate()
        return plan

    def _valuation(self, plan, period='2026-12-31', **values):
        payload = {'plan_id': plan.id, 'period_end': period}
        payload.update(values)
        return self.env['eh.benefit.valuation'].create(payload)

    @staticmethod
    def _account_net(move, account):
        return sum(
            line.debit - line.credit
            for line in move.line_ids
            if line.account_id == account
        )

    def _dc(self, period='2026-12-31', amount=1000.0):
        return self.env['eh.benefit.dc.accrual'].create({
            'period_date': period,
            'amount': amount,
            'expense_account_id': self.dc_expense.id,
            'payable_account_id': self.dc_payable.id,
            'journal_id': self.journal_misc.id,
        })

    def test_closed_plan_reversal_is_a_complete_sealed_transition(self):
        plan = self._plan('Closed-plan reversal')
        valuation = self._valuation(
            plan, opening_dbo=100000.0, current_service_cost=10000.0)
        valuation.action_post()
        primary = valuation.move_id
        plan.action_close()

        valuation.action_reverse()

        self.assertEqual(plan.state, 'closed')
        self.assertEqual(valuation.state, 'reversed')
        reversal = valuation.move_ids - primary
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.reversed_entry_id, primary)
        self.assertTrue(primary.eh_sealed)
        self.assertTrue(reversal.eh_sealed)

    def test_contribution_flag_changes_the_actual_credit_account(self):
        clearing_plan = self._plan('Clearing-routed contribution')
        cash_plan = self._plan(
            'Cash-routed contribution',
            contributions_posted_elsewhere=False,
        )
        clearing = self._valuation(
            clearing_plan, contributions_employer=1000.0)
        cash = self._valuation(cash_plan, contributions_employer=1000.0)
        clearing.action_post()
        cash.action_post()

        self.assertAlmostEqual(
            self._account_net(clearing.move_id, self.clearing),
            -1000.0, places=2)
        self.assertAlmostEqual(
            self._account_net(clearing.move_id, self.account_cash),
            0.0, places=2)
        self.assertAlmostEqual(
            self._account_net(cash.move_id, self.account_cash),
            -1000.0, places=2)
        self.assertAlmostEqual(
            self._account_net(cash.move_id, self.clearing),
            0.0, places=2)

    def test_rendered_disclosure_consumes_rollforward_and_presents_net(self):
        plan = self._plan('Rendered disclosure plan')
        valuation = self._valuation(
            plan,
            opening_dbo=100000.0,
            opening_assets=40000.0,
            current_service_cost=10000.0,
        )
        valuation.action_post()

        values = plan.get_disclosure_values()
        self.assertEqual(len(values['periods']), 1)
        row = values['periods'][0]
        self.assertAlmostEqual(row['closing_dbo'], 110000.0, places=2)
        self.assertAlmostEqual(row['closing_assets'], 40000.0, places=2)
        self.assertEqual(row['presentation_label'],
                         'Net defined benefit liability')
        self.assertAlmostEqual(row['presentation_amount'], 70000.0, places=2)

        report = self.env.ref(
            'eh_account_employee_benefits.action_report_benefit_disclosure')
        action = plan.action_print_disclosure()
        self.assertEqual(action['type'], 'ir.actions.report')
        self.assertEqual(action['report_type'], 'qweb-pdf')
        html, file_type = report._render_qweb_html(
            report.report_name, plan.ids)
        self.assertEqual(file_type, 'html')
        self.assertIn(b'Statement of Financial Position', html)
        self.assertIn(b'Net defined benefit liability', html)
        self.assertIn(b'Defined Benefit Obligation Reconciliation', html)

        unfunded_plan = self._plan('Unfunded disclosure plan', funded=False)
        unfunded = self._valuation(
            unfunded_plan,
            period='2027-12-31',
            opening_dbo=50000.0,
            current_service_cost=5000.0,
        )
        unfunded.action_post()
        unfunded_html, _file_type = report._render_qweb_html(
            report.report_name, unfunded_plan.ids)
        self.assertIn(b'Defined Benefit Obligation Reconciliation',
                      unfunded_html)
        self.assertNotIn(b'Plan Asset Reconciliation', unfunded_html)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['eh.benefit.plan'].create({
                'name': 'Gross presentation refused',
                'funded': True,
                'dbo_account_id': self.net_position.id,
                'plan_asset_account_id': self.gross_asset.id,
            })

    def test_dc_accrual_is_post_once_per_period_and_ledger_route(self):
        first = self._dc(amount=1000.0)
        duplicate = self._dc(amount=1200.0)
        first.action_post()
        with self.assertRaisesRegex(UserError, 'already posted'):
            duplicate.action_post()
        duplicate.invalidate_recordset(['state', 'move_id'])
        self.assertEqual(duplicate.state, 'draft')
        self.assertFalse(duplicate.move_id)

        first.action_reverse()
        duplicate.action_post()
        self.assertEqual(first.state, 'reversed')
        self.assertEqual(duplicate.state, 'posted')

    def test_references_are_company_owned_and_use_the_business_year(self):
        companies = self.env['res.company'].sudo().create([
            {
                'name': 'EB Sequence Company A',
                'currency_id': self.company.currency_id.id,
            },
            {
                'name': 'EB Sequence Company B',
                'currency_id': self.company.currency_id.id,
            },
        ])
        valuation_names = []
        accrual_names = []
        for company in companies:
            context = {'allowed_company_ids': [company.id]}
            Plan = self.env['eh.benefit.plan'].sudo().with_company(
                company).with_context(
                allowed_company_ids=[company.id])
            plan = Plan.create({
                'name': 'Sequence plan %s' % company.name,
                'company_id': company.id,
            })
            valuation = self.env['eh.benefit.valuation'].sudo().with_company(
                company).with_context(**context).create({
                'plan_id': plan.id,
                'period_end': '2024-06-30',
            })
            accrual = self.env['eh.benefit.dc.accrual'].sudo().with_company(
                company).with_context(**context).create({
                'company_id': company.id,
                'period_date': '2023-09-30',
                'amount': 1.0,
            })
            valuation_names.append(valuation.name)
            accrual_names.append(accrual.name)
        self.assertEqual(valuation_names, ['DBV/2024/0001'] * 2)
        self.assertEqual(accrual_names, ['DCA/2023/0001'] * 2)

    def test_multiyear_asset_ceiling_at_nonzero_rate(self):
        plan = self._plan('Two-year ceiling plan')
        first = self._valuation(
            plan, period='2025-12-31',
            opening_dbo=100000.0,
            opening_assets=200000.0,
            discount_rate=10.0,
            apply_asset_ceiling=True,
            asset_ceiling=50000.0,
        )
        first.action_post()
        self.assertAlmostEqual(first.interest_cost, 10000.0, places=2)
        self.assertAlmostEqual(first.interest_income, 20000.0, places=2)
        self.assertAlmostEqual(first.ceiling_effect, 60000.0, places=2)

        second = self._valuation(
            plan, period='2026-12-31',
            discount_rate=10.0,
            apply_asset_ceiling=True,
            asset_ceiling=50000.0,
        )
        self.assertAlmostEqual(second.opening_dbo, 110000.0, places=2)
        self.assertAlmostEqual(second.opening_assets, 220000.0, places=2)
        self.assertAlmostEqual(second.opening_ceiling_effect, 60000.0,
                               places=2)
        self.assertAlmostEqual(second.interest_cost, 11000.0, places=2)
        self.assertAlmostEqual(second.interest_income, 22000.0, places=2)
        self.assertAlmostEqual(second.closing_dbo, 121000.0, places=2)
        self.assertAlmostEqual(second.closing_assets, 242000.0, places=2)
        self.assertAlmostEqual(second.ceiling_effect, 71000.0, places=2)
        self.assertAlmostEqual(second.ceiling_effect_delta, 11000.0,
                               places=2)
        self.assertAlmostEqual(second.recognised_net_position, -50000.0,
                               places=2)
        second.action_post()
        self.assertBalanced(second.move_id)

    def test_locked_posting_and_reversal_fail_before_creating_a_move(self):
        plan = self._plan('Locked reversal plan')
        valuation = self._valuation(
            plan, opening_dbo=100000.0, current_service_cost=10000.0)
        valuation.action_post()
        before = valuation.move_ids
        later = self._valuation(
            plan, period='2027-12-31', current_service_cost=1000.0)
        accrual = self._dc(period='2028-12-31')
        MoveClass = type(self.env['account.move'])

        def _locked(_move, _posting_date, _has_tax):
            return [(date(2026, 12, 31), 'fiscalyear_lock_date')]

        self.patch(MoveClass, '_get_violated_lock_dates', _locked)
        with self.assertRaisesRegex(UserError, 'accounting date.*locked'):
            later.action_post()
        with self.assertRaisesRegex(UserError, 'accounting date.*locked'):
            accrual.action_post()
        with self.assertRaisesRegex(UserError, 'accounting date.*locked'):
            valuation.action_reverse()
        later.invalidate_recordset(['state', 'move_ids'])
        accrual.invalidate_recordset(['state', 'move_ids'])
        valuation.invalidate_recordset(['state', 'move_ids'])
        self.assertEqual(later.state, 'draft')
        self.assertFalse(later.move_ids)
        self.assertEqual(accrual.state, 'draft')
        self.assertFalse(accrual.move_ids)
        self.assertEqual(valuation.state, 'posted')
        self.assertEqual(valuation.move_ids, before)

    def test_ui_access_search_analytics_and_store_contract(self):
        plan_form = etree.fromstring(self.env.ref(
            'eh_account_employee_benefits.view_eh_benefit_plan_form'
        ).arch_db.encode())
        activate = plan_form.xpath("//button[@name='action_activate']")
        self.assertEqual(len(activate), 1)
        self.assertEqual(
            activate[0].get('groups'),
            'eh_account_base.group_eh_manager',
        )
        for xmlid in (
            'view_eh_benefit_plan_search',
            'view_eh_benefit_valuation_search',
            'view_eh_benefit_dc_accrual_search',
        ):
            view = self.env.ref('eh_account_employee_benefits.%s' % xmlid)
            self.assertEqual(etree.fromstring(view.arch_db.encode()).tag,
                             'search')
        auditor = self.env.ref('eh_account_base.group_eh_auditor')
        menu_group_field = (
            'group_ids'
            if 'group_ids' in self.env['ir.ui.menu']._fields
            else 'groups_id'
        )
        self.assertIn(
            auditor,
            self.env.ref('eh_account_base.menu_eh_ifrs')[menu_group_field],
        )
        for xmlid in (
            'menu_eh_benefit_plan',
            'menu_eh_benefit_valuation',
            'menu_eh_benefit_dc_accrual',
        ):
            self.assertIn(
                auditor,
                self.env.ref(
                    'eh_account_employee_benefits.%s' % xmlid
                )[menu_group_field],
            )
        for xmlid in (
            'eh_account_employee_benefits_benefit_valuation_view_graph',
            'eh_account_employee_benefits_benefit_valuation_view_pivot',
        ):
            arch = self.env.ref(
                'eh_account_employee_benefits.%s' % xmlid
            ).arch_db
            self.assertNotIn('name="closing_dbo"', arch)
            self.assertNotIn('name="closing_assets"', arch)
        valuation_fields = self.env['eh.benefit.valuation']._fields
        aggregation_attr = (
            'aggregator'
            if hasattr(valuation_fields['closing_dbo'], 'aggregator')
            else 'group_operator'
        )
        for field_name in ('closing_dbo', 'closing_assets'):
            self.assertFalse(
                getattr(valuation_fields[field_name], aggregation_attr),
                '%s must not be offered as an additive measure' % field_name,
            )

        root = Path(__file__).resolve().parents[1]
        manifest_tree = ast.parse(
            (root / '__manifest__.py').read_text(encoding='utf-8'))
        manifest = ast.literal_eval(manifest_tree.body[0].value)
        listing = (root / 'static' / 'description' / 'index.html').read_text(
            encoding='utf-8')
        source = '\n'.join(
            path.read_text(encoding='utf-8')
            for path in (
                root / '__manifest__.py',
                root / 'models' / 'benefit_dc_accrual.py',
                root / 'static' / 'description' / 'index.html',
            )
        )
        self.assertLessEqual(len(manifest['name']), 25)
        self.assertLessEqual(len(manifest['summary']), 100)
        self.assertIn(manifest['version'], listing)
        self.assertNotIn("whole of IAS 19's scope", source)
        self.assertIn('reimbursement rights', source)
        self.assertIn('Rendered disclosure schedule', listing)

        tour = (root / 'static' / 'tests' / 'tours' /
                'benefit_test_tour.js').read_text(encoding='utf-8')
        tour_test = (root / 'tests' / 'test_tour.py').read_text(
            encoding='utf-8')
        self.assertIn("button[name='action_activate']", tour)
        self.assertIn("plan.state, 'active'", tour_test)
        migration_test = (root / 'tests' / 'test_migration_103.py').read_text(
            encoding='utf-8')
        self.assertIn("'19.0.1.0.0'", migration_test)

    def test_authorisation_failures_are_access_errors(self):
        user = self.env['res.users'].create({
            'name': 'EB Backlog Ordinary User',
            'login': 'eb_backlog_ordinary_user',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        plan = self._plan('AccessError plan')
        valuation = self._valuation(
            plan, opening_dbo=1000.0, current_service_cost=100.0)
        accrual = self._dc(period='2027-12-31')
        with self.assertRaises(AccessError):
            valuation.with_user(user)._check_manager()
        with self.assertRaises(AccessError):
            accrual.with_user(user)._check_manager()

    def test_107_migration_backfills_only_proven_cash_routing(self):
        plan = self.env['eh.benefit.plan'].create({
            'name': 'Legacy direct-cash route',
            'contributions_posted_elsewhere': False,
            'contribution_account_id': self.account_cash.id,
        })
        self.assertFalse(plan.contribution_cash_account_id)
        root = Path(__file__).resolve().parents[1]
        migration_path = next(
            path / 'post-migration.py'
            for path in (root / 'migrations').iterdir()
            if path.name.endswith('.1.0.7')
        )
        spec = spec_from_file_location(
            'eh_benefit_migration_107_post_test', migration_path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.env.flush_all()
        migration.migrate(self.env.cr, '19.0.1.0.6')
        plan.invalidate_recordset(['contribution_cash_account_id'])
        self.assertEqual(plan.contribution_cash_account_id, self.account_cash)
