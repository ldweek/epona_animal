# -*- encoding: utf-8 -*-
"""Focused regressions for the provisions medium/low closure wave."""

import ast
from pathlib import Path
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_provisions', 'post_install', '-at_install')
class TestProvisionBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provision_liab = cls._ensure_account(
            cls.env, '2987', 'Backlog Provisions', 'liability_current')
        cls.finance_cost = cls._ensure_account(
            cls.env, '5787', 'Backlog Finance Cost', 'expense')
        cls.reimbursement_asset = cls._ensure_account(
            cls.env, '1487', 'Backlog Reimbursement', 'asset_current')
        cls.settlement_excess = cls._ensure_account(
            cls.env, '6787', 'Settlement Above Provision', 'expense')
        cls.settlement_payable = cls._ensure_account(
            cls.env, '2187', 'Provision Claimants', 'liability_payable')
        cls.partner = cls.env['res.partner'].create({
            'name': 'Provision Claimant',
        })
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Backlog Provision Manager',
            'login': 'backlog_provision_manager',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Backlog Provision Operator',
            'login': 'backlog_provision_operator',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })

    def _provision(self, **overrides):
        vals = {
            'name': '/',
            'classification': 'provision',
            'best_estimate': 1000.0,
            'provision_account_id': self.provision_liab.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': self.finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'settlement_excess_account_id': self.settlement_excess.id,
            'reimbursement_account_id': self.reimbursement_asset.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2026-01-01',
        }
        vals.update(overrides)
        provision = self.env['eh.provision'].with_user(
            self.manager_user).create(vals)
        self.assertFalse(provision.env.su)
        return provision

    def test_current_rate_and_calendar_horizon_drive_remeasurement(self):
        provision = self._provision(
            best_estimate=1000.0,
            discount_rate=10.0,
            periods_to_settlement=99,
            expected_settlement_date='2027-01-01',
            rate_basis='risk_free_govt',
            rate_source='Recognition-date curve',
        )
        provision.action_recognise('2025-01-01')
        self.assertEqual(provision.periods_to_settlement, 2)
        self.assertAlmostEqual(provision.carrying_amount, 826.45, places=2)

        provision.write({
            'remeasure_estimate': 1100.0,
            'remeasure_discount_rate': 20.0,
            'remeasure_rate_basis': 'risk_free_govt',
            'remeasure_rate_source': '2027 reporting-date curve',
        })
        provision.action_remeasure('2026-01-01')
        self.assertAlmostEqual(provision.carrying_amount, 916.67, places=2)
        self.assertAlmostEqual(provision.discount_rate, 20.0, places=3)
        self.assertEqual(provision.rate_source, '2027 reporting-date curve')
        self.assertEqual(provision.periods_to_settlement, 1)
        self.assertEqual(
            provision._unwind_boundaries(),
            [provision.expected_settlement_date],
        )

    def test_discounted_remeasure_refuses_implicit_frozen_rate(self):
        provision = self._provision(
            discount_rate=5.0,
            periods_to_settlement=2,
            expected_settlement_date='2027-01-01',
            rate_source='Original rate',
        )
        provision.action_recognise('2025-01-01')
        provision.remeasure_estimate = 900.0
        with self.assertRaises(UserError):
            provision.action_remeasure('2026-01-01')

    def test_payable_settlement_carries_partner_and_excess_to_pnl(self):
        provision = self._provision(
            settlement_account_id=self.settlement_payable.id,
            settlement_partner_id=self.partner.id,
        )
        provision.action_recognise()
        provision.utilise_amount = 1200.0
        provision.action_utilise()
        utilisation = provision.move_ids.filtered(
            lambda move: move.eh_provision_event == 'utilisation')
        payable_line = utilisation.line_ids.filtered(
            lambda line: line.account_id == self.settlement_payable)
        excess_line = utilisation.line_ids.filtered(
            lambda line: line.account_id == self.settlement_excess)
        self.assertEqual(payable_line.partner_id, self.partner)
        self.assertAlmostEqual(payable_line.credit, 1200.0, places=2)
        self.assertAlmostEqual(excess_line.debit, 200.0, places=2)
        self.assertAlmostEqual(provision.utilised_amount, 1200.0, places=2)
        self.assertEqual(provision.state, 'settled')

    def test_payable_settlement_requires_counterparty(self):
        provision = self._provision(
            settlement_account_id=self.settlement_payable.id,
            settlement_partner_id=False,
        )
        provision.action_recognise()
        provision.utilise_amount = 100.0
        before = provision.move_ids
        with self.assertRaises(UserError):
            provision.action_utilise()
        self.assertEqual(provision.move_ids, before)

    def test_ias37_disclosure_rollforward_and_contingency_output(self):
        provision = self._provision(
            disclosure_nature='Customer warranty claims',
            disclosure_uncertainties='Claim frequency and repair cost',
        )
        provision.action_recognise('2025-12-31')
        provision.remeasure_estimate = 1200.0
        provision.action_remeasure('2026-03-31')
        provision.utilise_amount = 300.0
        provision.action_utilise('2026-06-30')
        provision.reimbursement_amount = 200.0
        provision.action_recognise_reimbursement('2026-07-01')
        contingent = self._provision(
            classification='contingent_liability',
            best_estimate=500.0,
            disclosure_nature='Litigation exposure',
            disclosure_uncertainties='Outcome depends on court ruling',
        )

        wizard = self.env['eh.provision.disclosure.wizard'].with_user(
            self.manager_user).create({
                'company_id': self.company.id,
                'date_from': '2026-01-01',
                'date_to': '2026-08-01',
            })
        action = wizard.action_generate()
        self.assertEqual(action['res_model'], 'eh.provision.disclosure.line')
        line = wizard.line_ids.filtered(
            lambda item: item.provision_id == provision)
        self.assertAlmostEqual(line.opening_amount, 1000.0, places=2)
        self.assertAlmostEqual(line.additions_amount, 200.0, places=2)
        self.assertAlmostEqual(line.utilised_amount, 300.0, places=2)
        self.assertAlmostEqual(line.closing_amount, 900.0, places=2)
        self.assertAlmostEqual(line.reimbursement_amount, 200.0, places=2)
        self.assertEqual(line.nature, 'Customer warranty claims')
        contingent_line = wizard.line_ids.filtered(
            lambda item: item.provision_id == contingent)
        self.assertAlmostEqual(
            contingent_line.contingent_estimate, 500.0, places=2)
        self.assertIn('court', contingent_line.uncertainties)

    def test_integrity_quarantine_release_revalidates_and_audits(self):
        provision = self._provision()
        provision.action_recognise()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_provision SET eh_integrity_review_required = TRUE, "
            "eh_integrity_review_reason = %s WHERE id = %s",
            ['Upgrade review', provision.id],
        )
        self.env.invalidate_all()
        provision.write({
            'eh_integrity_review_release_reason':
                'Matched sealed move, accounts, company and journal',
        })
        provision.action_release_integrity_quarantine()
        self.assertFalse(provision.eh_integrity_review_required)
        self.assertEqual(
            provision.eh_integrity_released_by_id, self.manager_user)
        self.assertTrue(provision.eh_integrity_released_on)

        unsafe = self._provision()
        unsafe.action_recognise()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_provision SET eh_integrity_review_required = TRUE "
            "WHERE id = %s",
            [unsafe.id],
        )
        self.env.cr.execute(
            "UPDATE account_move SET ref = 'FORGED' "
            "WHERE eh_provision_id = %s",
            [unsafe.id],
        )
        self.env.invalidate_all()
        unsafe.eh_integrity_review_release_reason = 'Attempted review'
        with self.assertRaises(UserError):
            unsafe.action_release_integrity_quarantine()

    def test_restructuring_remeasure_regates_and_adopts_staged_components(self):
        provision = self._provision(
            provision_type='restructuring',
            best_estimate=1000.0,
            restructuring_plan=True,
            restructuring_expectation=True,
            restructuring_announcement_date='2025-12-01',
            restructuring_announcement='Plan announced to affected staff',
            restructuring_line_ids=[(0, 0, {
                'name': 'Termination benefits',
                'cost_kind': 'termination',
                'amount': 1000.0,
            })],
        )
        provision.action_recognise()
        Stage = self.env[
            'eh.provision.restructuring.remeasure.line'
        ].with_user(self.manager_user)
        Stage.create({
            'provision_id': provision.id,
            'name': 'Revised termination benefits',
            'cost_kind': 'termination',
            'amount': 600.0,
        })
        Stage.create({
            'provision_id': provision.id,
            'name': 'Future retraining',
            'cost_kind': 'retraining',
            'amount': 400.0,
        })
        provision.action_remeasure('2026-03-31')
        self.assertAlmostEqual(provision.best_estimate, 600.0, places=2)
        self.assertAlmostEqual(
            provision.restructuring_component_total, 600.0, places=2)
        self.assertAlmostEqual(
            provision.restructuring_excluded_total, 400.0, places=2)
        self.assertFalse(provision.remeasure_restructuring_line_ids)

    def test_remeasure_to_zero_closes_as_reversed(self):
        provision = self._provision()
        provision.action_recognise()
        provision.remeasure_estimate = 0.0
        provision.action_remeasure('2026-03-31')
        self.assertEqual(provision.state, 'reversed')
        self.assertAlmostEqual(provision.carrying_amount, 0.0, places=2)
        self.assertEqual(
            provision.move_ids.sorted('date')[-1].eh_provision_event,
            'remeasure_decrease')

    def test_posted_name_is_frozen_and_authorisation_uses_accesserror(self):
        provision = self._provision()
        with self.assertRaises(AccessError):
            provision.with_user(self.plain_user).action_recognise()
        provision.action_recognise()
        original_name = provision.name
        with self.assertRaises(UserError):
            provision.name = 'FORGED-REFERENCE'
        self.assertEqual(provision.name, original_name)
        self.assertEqual(provision.move_ids.ref, original_name)

    def test_notes_write_avoids_transition_lock_but_financial_write_keeps_it(self):
        provision = self._provision()
        seen = []
        real_execute = self.env.cr.execute

        def record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', record_execute):
            provision.write({'notes': 'Qualitative disclosure update'})
        self.assertFalse(any('FOR UPDATE' in query for query in seen))
        seen.clear()
        with patch.object(self.env.cr, 'execute', record_execute):
            provision.write({'rate_source': 'Approved current memo'})
        self.assertTrue(any(
            'eh_provision' in query and 'FOR UPDATE' in query
            for query in seen))

    def test_reimbursement_asset_is_a_separate_deferred_tax_item(self):
        provision = self._provision()
        provision.action_recognise()
        provision.reimbursement_amount = 300.0
        provision.action_recognise_reimbursement('2026-02-01')
        items = provision.eh_deferred_tax_temp_diffs('2026-03-31')
        by_key = {item['source_key']: item for item in items}
        reimbursement = by_key[
            'provision_reimbursement:%s' % provision.id]
        self.assertEqual(reimbursement['nature'], 'asset')
        self.assertAlmostEqual(
            reimbursement['carrying_amount'], 300.0, places=2)
        self.assertAlmostEqual(reimbursement['tax_base'], 0.0, places=2)

    def test_company_sequences_are_independent(self):
        company_a = self.env['res.company'].create({'name': 'Sequence Co A'})
        company_b = self.env['res.company'].create({'name': 'Sequence Co B'})
        provision_a = self.env['eh.provision'].create({
            'name': '/', 'company_id': company_a.id, 'best_estimate': 1.0,
        })
        provision_b = self.env['eh.provision'].create({
            'name': '/', 'company_id': company_b.id, 'best_estimate': 1.0,
        })
        self.assertEqual(provision_a.name, provision_b.name)
        self.assertTrue(provision_a.name.endswith('/0001'))

    def test_views_listing_and_store_name_match_shipped_controls(self):
        view = self.env.ref('eh_account_provisions.view_eh_provision_form')
        search = self.env.ref('eh_account_provisions.view_eh_provision_search')
        form_arch = str(view.arch_db)
        search_arch = str(search.arch_db)
        self.assertIn('action_cancel', form_arch)
        self.assertIn('group_eh_manager', form_arch)
        self.assertTrue(
            'readonly="state != \'draft\'"' in form_arch
            or "'readonly': [('state', '!=', 'draft')]" in form_arch
        )
        self.assertIn('group_classification', search_arch)
        module_root = Path(__file__).parents[1]
        manifest = ast.literal_eval(
            (module_root / '__manifest__.py').read_text())
        self.assertLessEqual(len(manifest['name']), 25)
        self.assertEqual(manifest['version'], '19.0.1.2.0')
        listing = (module_root / 'static' / 'description' / 'index.html') \
            .read_text()
        self.assertNotIn('does not build the IAS 37 disclosure', listing)
        self.assertNotIn('does not reimburse', listing)
        self.assertNotIn('does not measure onerous contracts', listing)

    def test_rare_disclosure_exemption_requires_reason(self):
        with self.assertRaises(ValidationError):
            self._provision(disclosure_rare_exemption=True)
