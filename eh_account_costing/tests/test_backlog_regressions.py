# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the Costing medium/low remediation batch."""

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_costing', 'post_install', '-at_install')
class TestCostingBacklogRegressions(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.variance_accounts = {
            'price_variance_account_id': cls._ensure_account(
                cls.env, '5910', 'Backlog Price Variance', 'expense'),
            'usage_variance_account_id': cls._ensure_account(
                cls.env, '5911', 'Backlog Usage Variance', 'expense'),
            'rate_variance_account_id': cls._ensure_account(
                cls.env, '5912', 'Backlog Rate Variance', 'expense'),
            'efficiency_variance_account_id': cls._ensure_account(
                cls.env, '5913', 'Backlog Efficiency Variance', 'expense'),
            'spend_variance_account_id': cls._ensure_account(
                cls.env, '5914', 'Backlog Spend Variance', 'expense'),
            'volume_variance_account_id': cls._ensure_account(
                cls.env, '5915', 'Backlog Volume Variance', 'expense'),
            'absorption_account_id': cls._ensure_account(
                cls.env, '5909', 'Backlog Absorption', 'expense'),
        }

    def _card(self, label='Backlog Widget', product=None, price=10.0,
              elements=('material',)):
        card = self.env['eh.cost.card'].create({
            'product_id': product.id if product else False,
            'item_name': False if product else label,
            'normal_capacity': 100.0,
            'line_ids': [(0, 0, {
                'element': element,
                'std_qty': 1.0,
                'std_price': price,
            }) for element in elements],
        })
        card.action_activate()
        return card

    def _actual(self, card, start='2026-01-01', end='2026-01-31',
                elements=None):
        elements = elements if elements is not None \
            else card.line_ids.mapped('element')
        return self.env['eh.cost.actual'].create({
            'card_id': card.id,
            'period_start': start,
            'period_end': end,
            'units_produced': 10.0,
            'line_ids': [(0, 0, {
                'element': element,
                'actual_qty_total': 10.0 if element != 'fixed_overhead'
                else 0.0,
                'actual_cost_total': 100.0,
            }) for element in elements],
        })

    def _run(self, actual, **extra):
        values = {
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'actual_ids': [(6, 0, actual.ids)],
        }
        values.update(extra)
        return self.env['eh.cost.variance.run'].create(values)

    def _posting_values(self):
        values = {
            'post_variances': True,
            'journal_id': self.journal_misc.id,
        }
        values.update({name: account.id for name, account
                       in self.variance_accounts.items()})
        return values

    def _invoice(self, product, quantity, price, move_type='out_invoice',
                 env=None, journal=None, account=None):
        env = env or self.env
        account = account or self.account_revenue
        values = {
            'move_type': move_type,
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-01-15',
            'date': '2026-01-15',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'quantity': quantity,
                'price_unit': price,
                'account_id': account.id,
                'tax_ids': [(5, 0, 0)],
            })],
        }
        if journal:
            values['journal_id'] = journal.id
        move = env['account.move'].create(values)
        move.action_post()
        return move

    def test_loss_making_mix_has_explicit_no_break_even(self):
        card = self._card(price=100.0)
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'fixed_costs': 100.0,
            'target_profit': 50.0,
            'line_ids': [(0, 0, {
                'card_id': card.id,
                'units_sold': 10.0,
                'revenue': 500.0,
            })],
        })
        self.assertEqual(report.total_contribution, -500.0)
        self.assertEqual(report.cvp_status, 'no_positive_contribution')
        self.assertEqual(report.breakeven_units, 0.0)
        self.assertEqual(report.breakeven_revenue, 0.0)
        self.assertEqual(report.margin_of_safety_pct, 0.0)
        self.assertEqual(report.target_profit_units, 0.0)
        self.assertEqual(report.operating_leverage, 0.0)

    def test_break_even_revenue_uses_exact_aggregate_ratio(self):
        card = self._card(price=2.0)
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'fixed_costs': 100000.0,
            'line_ids': [(0, 0, {
                'card_id': card.id,
                'units_sold': 1.0,
                'revenue': 3.0,
            })],
        })
        self.assertEqual(report.cm_ratio_pct, 33.3333)
        # Exact 100,000 * 3 / 1. The rounded 33.3333 display ratio would
        # incorrectly yield 300,000.30.
        self.assertEqual(report.breakeven_revenue, 300000.0)

    def test_compute_rejects_missing_element_and_outside_period(self):
        card = self._card(elements=('material', 'labour'))
        missing = self._actual(card, elements=('material',))
        with self.assertRaisesRegex(UserError, 'explicit actual line'):
            self._run(missing).action_compute()

        complete = self._actual(
            card, start='2025-12-15', end='2026-01-15')
        with self.assertRaisesRegex(UserError, 'fully contained'):
            self._run(complete).action_compute()

    def test_cancel_clears_lines_and_aggregate_totals(self):
        run = self._run(self._actual(self._card()))
        run.action_compute()
        self.assertTrue(run.line_ids)
        self.assertNotEqual(run.total_actual_cost, 0.0)
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        self.assertFalse(run.line_ids)
        self.assertEqual(run.total_actual_cost, 0.0)
        self.assertEqual(run.total_absorbed_cost, 0.0)
        self.assertEqual(run.total_variance, 0.0)

    def test_ledger_fetch_batches_products_and_subtracts_refunds(self):
        products = self.env['product.product'].create([
            {
                'name': 'Batched Ledger A',
                'type': 'consu',
                'property_account_income_id': self.account_revenue.id,
            },
            {
                'name': 'Batched Ledger B',
                'type': 'consu',
                'property_account_income_id': self.account_revenue.id,
            },
        ])
        cards = [self._card(product=product) for product in products]
        self._invoice(products[0], 10.0, 100.0)
        self._invoice(products[0], 2.0, 100.0, move_type='out_refund')
        self._invoice(products[1], 5.0, 20.0)
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'line_ids': [(0, 0, {
                'card_id': card.id,
                'revenue_source': 'ledger',
            }) for card in cards],
        })

        aml_class = type(self.env['account.move.line'])
        original = aml_class.read_group
        calls = []

        def counted(records, *args, **kwargs):
            calls.append(args)
            return original(records, *args, **kwargs)

        with patch.object(aml_class, 'read_group', counted):
            report.action_fetch_ledger_revenue()

        self.assertEqual(len(calls), 2)
        by_product = {line.product_id: line for line in report.line_ids}
        self.assertEqual(by_product[products[0]].revenue, 800.0)
        self.assertEqual(by_product[products[0]].units_sold, 8.0)
        self.assertEqual(by_product[products[1]].revenue, 100.0)
        self.assertEqual(by_product[products[1]].units_sold, 5.0)

    def test_ledger_fetch_can_include_authorised_branches(self):
        Company = self.env['res.company']
        if (
            'parent_id' not in Company._fields
            or not hasattr(self.company, '_accessible_branches')
        ):
            self.skipTest('this Odoo series has no accounting branches')
        branch = self._create_accounting_branch({
            'name': 'Costing Ledger Branch',
            'parent_id': self.company.id,
        })
        product = self.env['product.product'].create({
            'name': 'Branch Ledger Product',
            'type': 'consu',
            'property_account_income_id': self.account_revenue.id,
        })
        card = self._card(product=product)
        allowed_ids = [self.company.id, branch.id]
        branch_env = self.env['account.move'].with_context(
            allowed_company_ids=allowed_ids,
        ).with_company(branch).env
        branch_journal = self._ensure_journal(
            branch_env, branch, 'sale', 'CBR', 'Costing Branch Sales',
            default_account=self.account_revenue,
        )
        self._invoice(
            product, 4.0, 125.0, env=branch_env,
            journal=branch_journal, account=self.account_revenue,
        )
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'include_branch_companies': True,
            'line_ids': [(0, 0, {
                'card_id': card.id,
                'revenue_source': 'ledger',
            })],
        })
        report.with_context(
            allowed_company_ids=allowed_ids,
        ).action_fetch_ledger_revenue()
        self.assertEqual(report.line_ids.revenue, 500.0)
        self.assertEqual(report.line_ids.units_sold, 4.0)

    def test_report_company_cannot_orphan_existing_lines(self):
        report = self.env['eh.contribution.report'].create({
            'period_start': '2026-01-01',
            'period_end': '2026-01-31',
            'line_ids': [(0, 0, {'card_id': self._card().id})],
        })
        other = self.env['res.company'].create({
            'name': 'Costing Report Other Company',
        })
        with self.assertRaises(ValidationError):
            report.company_id = other

    def test_regular_costing_writes_do_not_lock_company(self):
        card = self._card()
        actual = self._actual(card)
        run = self._run(actual)
        real_execute = self.env.cr.execute
        statements = []

        def record_execute(query, params=None, *args, **kwargs):
            statements.append(str(query).upper())
            return real_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', record_execute):
            card.write({'notes': 'narrow row lock only'})
            actual.write({'notes': 'narrow actual/card locks only'})
            run.write({'period_end': '2026-02-01'})
        for table in ('EH_COST_CARD', 'EH_COST_ACTUAL',
                      'EH_COST_VARIANCE_RUN'):
            self.assertTrue(any(
                table in query and 'FOR UPDATE' in query
                for query in statements
            ), table)
        self.assertFalse(any(
            'RES_COMPANY' in query and 'FOR UPDATE' in query
            for query in statements
        ))

    def test_variance_line_membership_is_validated_once_per_batch(self):
        actual = self._actual(self._card())
        run = self._run(actual)
        values = [{
            'run_id': run.id,
            'actual_id': actual.id,
            'name': label,
            'element': 'material',
            'kind': kind,
            'amount': 0.0,
        } for label, kind in (('Price', 'price'), ('Usage', 'usage'))]
        run_class = type(run)
        original = run_class._eh_raw_actual_ids
        calls = []

        def counted(records):
            calls.append(tuple(records.ids))
            return original(records)

        with patch.object(run_class, '_eh_raw_actual_ids', counted):
            lines = self.env['eh.cost.variance.line'].with_context(
                eh_costing_engine=True,
            ).create(values)
        self.assertEqual(calls, [(run.id,)])
        lines.with_context(eh_costing_engine=True).unlink()

    def test_manager_gate_and_real_non_superuser_posting_flow(self):
        actual = self._actual(self._card())
        actual.line_ids.actual_cost_total = 120.0
        run = self._run(actual, **self._posting_values())
        run.action_compute()
        clerk = new_test_user(
            self.env,
            login='costing_backlog_clerk',
            groups='eh_account_base.group_eh_user',
        )
        manager = new_test_user(
            self.env,
            login='costing_backlog_manager',
            groups='eh_account_base.group_eh_manager',
        )
        manager.email = 'costing-manager@example.com'
        self.assertFalse(run.with_user(manager).env.su)
        with self.assertRaises(AccessError):
            run.with_user(clerk).action_post()
        run.with_user(manager).action_post()
        self.assertEqual(run.state, 'posted')
        self.assertEqual(len(run.move_ids), 1)
        with self.assertRaises(UserError):
            run.with_user(manager).action_post()
        self.assertEqual(len(run.move_ids), 1)

    def test_unlink_acl_is_checked_before_locking(self):
        actual = self._actual(self._card())
        run = self._run(actual)
        clerk = new_test_user(
            self.env,
            login='costing_backlog_unlink_clerk',
            groups='eh_account_base.group_eh_user',
        )
        with patch.object(
            type(actual), '_eh_lock_sources',
            side_effect=AssertionError('lock reached before unlink ACL'),
        ):
            with self.assertRaises(AccessError):
                actual.with_user(clerk).unlink()
        with patch.object(
            type(run), '_eh_lock_for_transition',
            side_effect=AssertionError('lock reached before unlink ACL'),
        ):
            with self.assertRaises(AccessError):
                run.with_user(clerk).unlink()

    def test_sequences_are_company_and_business_date_scoped(self):
        companies = self.env['res.company'].create([
            {'name': 'Costing Sequence A'},
            {'name': 'Costing Sequence B'},
        ])
        cards = self.env['eh.cost.card']
        for company in companies:
            card = self.env['eh.cost.card'].with_company(company).create({
                'company_id': company.id,
                'item_name': 'Sequence Card',
                'date_from': '2024-02-01',
                'line_ids': [(0, 0, {
                    'element': 'material',
                    'std_qty': 1.0,
                    'std_price': 1.0,
                })],
            })
            self.assertRegex(card.name, r'^SCC/2024/0001$')
            cards |= card
        sequences = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'eh.cost.card'),
            ('company_id', 'in', companies.ids),
        ])
        self.assertEqual(set(sequences.mapped('company_id').ids),
                         set(companies.ids))
        self.assertTrue(all(sequences.mapped('use_date_range')))

        company = companies[0]
        card = cards.filtered(lambda item: item.company_id == company)
        card.action_activate()
        actual = self.env['eh.cost.actual'].with_company(company).create({
            'company_id': company.id,
            'card_id': card.id,
            'period_start': '2023-01-01',
            'period_end': '2023-01-31',
            'units_produced': 1.0,
            'line_ids': [(0, 0, {
                'element': 'material',
                'actual_qty_total': 1.0,
                'actual_cost_total': 1.0,
            })],
        })
        run = self.env['eh.cost.variance.run'].with_company(company).create({
            'company_id': company.id,
            'period_start': '2023-01-01',
            'period_end': '2023-01-31',
            'actual_ids': [(6, 0, actual.ids)],
        })
        report = self.env['eh.contribution.report'].with_company(
            company,
        ).create({
            'company_id': company.id,
            'period_start': '2023-01-01',
            'period_end': '2023-01-31',
        })
        self.assertRegex(actual.name, r'^CACT/2023/0001$')
        self.assertRegex(run.name, r'^CVAR/2023/0001$')
        self.assertRegex(report.name, r'^CMR/2023/0001$')

    def test_sequence_migration_continues_historical_year_counters(self):
        company = self.env['res.company'].create({
            'name': 'Costing Historical Sequence Company',
        })
        costing = self.env['eh.cost.card'].with_company(company).env
        card = costing['eh.cost.card'].create({
            'name': 'SCC/2024/0042',
            'company_id': company.id,
            'item_name': 'Historical Sequence Card',
            'date_from': '2024-01-01',
            'line_ids': [(0, 0, {
                'element': 'material',
                'std_qty': 1.0,
                'std_price': 1.0,
            })],
        })
        card.action_activate()
        actual = costing['eh.cost.actual'].create({
            'name': 'CACT/2024/0042',
            'company_id': company.id,
            'card_id': card.id,
            'period_start': '2024-01-01',
            'period_end': '2024-01-31',
            'units_produced': 1.0,
            'line_ids': [(0, 0, {
                'element': 'material',
                'actual_qty_total': 1.0,
                'actual_cost_total': 1.0,
            })],
        })
        costing['eh.cost.variance.run'].create({
            'name': 'CVAR/2024/0042',
            'company_id': company.id,
            'period_start': '2024-01-01',
            'period_end': '2024-01-31',
            'actual_ids': [(6, 0, actual.ids)],
        })
        costing['eh.contribution.report'].create({
            'name': 'CMR/2024/0042',
            'company_id': company.id,
            'period_start': '2024-01-01',
            'period_end': '2024-01-31',
        })

        module_path = Path(get_module_path('eh_account_costing'))
        migration_path = (
            module_path / 'migrations' / '19.0.1.0.6'
            / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_costing_sequence_migration_106', migration_path,
        )
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.0.5')
        self.env.invalidate_all()

        new_card = costing['eh.cost.card'].create({
            'company_id': company.id,
            'item_name': 'Next Historical Sequence Card',
            'date_from': '2024-02-01',
        })
        new_actual = costing['eh.cost.actual'].create({
            'company_id': company.id,
            'card_id': card.id,
            'period_start': '2024-02-01',
            'period_end': '2024-02-29',
            'units_produced': 1.0,
            'line_ids': [(0, 0, {
                'element': 'material',
                'actual_qty_total': 1.0,
                'actual_cost_total': 1.0,
            })],
        })
        new_run = costing['eh.cost.variance.run'].create({
            'company_id': company.id,
            'period_start': '2024-02-01',
            'period_end': '2024-02-29',
            'actual_ids': [(6, 0, new_actual.ids)],
        })
        new_report = costing['eh.contribution.report'].create({
            'company_id': company.id,
            'period_start': '2024-02-01',
            'period_end': '2024-02-29',
        })
        self.assertEqual(new_card.name, 'SCC/2024/0043')
        self.assertEqual(new_actual.name, 'CACT/2024/0043')
        self.assertEqual(new_run.name, 'CVAR/2024/0043')
        self.assertEqual(new_report.name, 'CMR/2024/0043')

    def test_search_views_and_computed_form_contract(self):
        for xmlid in (
            'view_eh_cost_card_search',
            'view_eh_cost_actual_search',
            'view_eh_cost_variance_run_search',
            'view_eh_contribution_report_search',
        ):
            view = self.env.ref('eh_account_costing.%s' % xmlid)
            self.assertEqual(etree.fromstring(
                str(view.arch_db).encode()).tag, 'search')

        arch = etree.fromstring(str(self.env.ref(
            'eh_account_costing.view_eh_cost_variance_run_form',
        ).arch_db).encode())
        for field_name in (
            'period_start', 'period_end', 'actual_ids', 'post_variances',
            'journal_id', 'absorption_account_id',
            'price_variance_account_id', 'usage_variance_account_id',
            'rate_variance_account_id', 'efficiency_variance_account_id',
            'spend_variance_account_id', 'volume_variance_account_id',
        ):
            node = arch.xpath(".//field[@name='%s']" % field_name)[0]
            readonly_contract = node.get('readonly', '')
            if not readonly_contract:
                # Odoo 16's backport expresses the same modifier through the
                # legacy attrs domain instead of the 17+ inline expression.
                readonly_contract = repr(
                    ast.literal_eval(node.get('attrs', '{}')).get(
                        'readonly', [],
                    )
                )
            self.assertIn('computed', readonly_contract)

    def test_variance_line_labels_are_distinct_in_decomposition_view(self):
        line_model = self.env['eh.cost.variance.line']
        self.assertEqual(line_model._fields['name'].string, 'Variance')
        self.assertEqual(
            line_model._fields['amount'].string, 'Variance Amount',
        )

        arch = etree.fromstring(str(self.env.ref(
            'eh_account_costing.view_eh_cost_variance_run_form',
        ).arch_db).encode())
        line_fields = arch.xpath(
            ".//field[@name='line_ids']/*[self::list or self::tree]"
            "/field[@name='name' or @name='amount']"
        )
        self.assertEqual(
            [node.get('name') for node in line_fields], ['name', 'amount'],
        )
        self.assertTrue(all(
            node.get('optional') != 'hide' for node in line_fields
        ))
        self.assertEqual(
            [line_model._fields[node.get('name')].string
             for node in line_fields],
            ['Variance', 'Variance Amount'],
        )

    def test_listing_version_and_cvp_claim_match_manifest(self):
        module_path = Path(get_module_path('eh_account_costing'))
        manifest_path = module_path / '__manifest__.py'
        parsed = ast.parse(manifest_path.read_text(encoding='utf-8'))
        manifest = ast.literal_eval(parsed.body[-1].value)
        listing = (
            module_path / 'static' / 'description' / 'index.html'
        ).read_text(encoding='utf-8')
        self.assertEqual(manifest['version'], '19.0.1.0.7')
        self.assertIn('v%s' % manifest['version'], listing)
        self.assertIn('Meaningful CVP only', listing)
        self.assertNotIn('Every CVP ratio guards', listing)
