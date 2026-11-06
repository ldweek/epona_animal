# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 3 goodwill and IAS 28 equity method tests."""

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_business_combination', 'integration', 'post_install',
        '-at_install')
class TestBusinessCombination(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.goodwill_acc = cls._ensure_account(
            cls.env, '1800', 'Goodwill', 'asset_non_current')
        cls.clearing = cls._ensure_account(
            cls.env, '1801', 'Acquisition Clearing', 'asset_current')
        cls.gain = cls._ensure_account(
            cls.env, '4800', 'Bargain Purchase Gain', 'income_other')
        cls.nci_equity = cls._ensure_account(
            cls.env, '3800', 'Non-controlling Interest', 'equity')
        cls.ppa_ppe = cls._ensure_account(
            cls.env, '1600', 'Property Plant Equipment', 'asset_fixed')
        cls.ppa_payable = cls._ensure_account(
            cls.env, '2600', 'Assumed Payables', 'liability_current')
        cls.deferred_tax = cls._ensure_account(
            cls.env, '2700', 'Deferred Tax Liability', 'liability_non_current')
        cls.investment = cls._ensure_account(
            cls.env, '1820', 'Investment in Associate', 'asset_non_current')
        cls.share_profit = cls._ensure_account(
            cls.env, '4820', 'Share of Profit of Associates', 'income_other')
        cls.impairment = cls._ensure_account(
            cls.env, '5820', 'Impairment of Associate', 'expense')
        cls.disposal_gain = cls._ensure_account(
            cls.env, '4830', 'Gain on Disposal of Associate', 'income_other')
        cls.disposal_loss = cls._ensure_account(
            cls.env, '5830', 'Loss on Disposal of Associate', 'expense')
        cls.retained_interest = cls._ensure_account(
            cls.env, '1830', 'Retained Financial Interest',
            'asset_non_current')
        cls.oci_reserve = cls._ensure_account(
            cls.env, '3850', 'Associate OCI Reserve', 'equity')
        cls.accumulated_depreciation = cls._ensure_account(
            cls.env, '1690', 'Acquisition Accumulated Depreciation',
            'asset_fixed')
        cls.catchup_expense = cls._ensure_account(
            cls.env, '5840', 'Acquisition Catch-up Depreciation',
            'expense_depreciation')
        cls.comparative_equity = cls._ensure_account(
            cls.env, '3860', 'Comparative Restatement Reserve', 'equity')
        cls.contingent_liability = cls._ensure_account(
            cls.env, '2840', 'Contingent Consideration',
            'liability_current')
        cls.contingent_pnl = cls._ensure_account(
            cls.env, '4840', 'Contingent Remeasurement', 'income_other')

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    # ---- IFRS 3 ----

    def _combination(self, **vals):
        base = {
            'name': '/', 'acquiree_name': 'Target Ltd',
            'consideration_transferred': 1000.0, 'nci_amount': 200.0,
            'fv_identifiable_net_assets': 900.0,
            'goodwill_account_id': self.goodwill_acc.id,
            'clearing_account_id': self.clearing.id,
            'gain_account_id': self.gain.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.business.combination'].create(base)

    def test_goodwill_computed(self):
        # 1000 + 200 + 0 - 900 = 300 goodwill.
        c = self._combination()
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 0.0, places=2)

    def test_goodwill_posted(self):
        c = self._combination(
            nci_amount=0.0, fv_identifiable_net_assets=700.0)
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -300.0, places=2)
        self.assertEqual(c.move_id.eh_business_combination_id, c)
        with self.assertRaises(AccessError):
            c.move_id.write({'eh_business_combination_id': False})

    def test_simple_path_requires_ppa_for_nci_or_prior_interest(self):
        nci = self._combination(nci_amount=200.0)
        with self.assertRaisesRegex(UserError, 'full purchase price allocation'):
            nci.action_recognise()
        self.assertFalse(nci.move_id)

        prior = self._combination(
            nci_amount=0.0,
            previously_held_interest_fv=150.0,
        )
        with self.assertRaisesRegex(UserError, 'full purchase price allocation'):
            prior.action_recognise()
        self.assertFalse(prior.move_id)

    def test_percentage_and_line_fair_value_validation(self):
        with self.assertRaises(ValidationError):
            self._combination(nci_pct=100.0001)
        with self.assertRaises(ValidationError):
            self._combination(tax_rate=-0.0001)
        with self.assertRaises(ValidationError):
            self._combination_ppa(asset_line_ids=[
                (0, 0, {
                    'name': 'Negative PPE',
                    'account_id': self.ppa_ppe.id,
                    'fair_value': -1.0,
                    'is_liability': False,
                }),
            ])

    def test_search_views_readonly_contract_and_manager_buttons(self):
        module = self.env['ir.module.module'].search([
            ('name', '=', 'eh_account_business_combination'),
        ], limit=1)
        self.assertLessEqual(len(module.shortdesc), 25)
        self.assertEqual(module.shortdesc, 'IFRS 3 & IAS 28')
        self.assertEqual(
            self.env.ref(
                'eh_account_business_combination.'
                'view_eh_business_combination_search'
            ).type,
            'search',
        )
        self.assertEqual(
            self.env.ref(
                'eh_account_business_combination.'
                'view_eh_equity_investment_search'
            ).type,
            'search',
        )
        combo_arch = etree.fromstring(self.env.ref(
            'eh_account_business_combination.'
            'view_eh_business_combination_form').arch_db.encode())
        consideration = combo_arch.xpath(
            ".//field[@name='consideration_transferred']")[0]
        readonly_contract = (
            consideration.get('readonly') or consideration.get('attrs') or '')
        self.assertIn('state', readonly_contract)
        self.assertIn('draft', readonly_contract)
        equity_arch = etree.fromstring(self.env.ref(
            'eh_account_business_combination.'
            'view_eh_equity_investment_form').arch_db.encode())
        for action in ('action_activate', 'action_dispose'):
            button = equity_arch.xpath(
                ".//button[@name='%s']" % action)[0]
            self.assertEqual(
                button.get('groups'),
                'eh_account_base.group_eh_manager',
            )
        impairment_domain = self.env['eh.equity.investment']._fields[
            'impairment_account_id'].domain
        self.assertIn('expense_other', impairment_domain)

    def test_bargain_purchase(self):
        # 1000 + 0 - 1300 = -300 -> bargain gain 300.
        c = self._combination(nci_amount=0.0,
                              fv_identifiable_net_assets=1300.0)
        self.assertAlmostEqual(c.goodwill, 0.0, places=2)
        self.assertAlmostEqual(c.bargain_purchase_gain, 300.0, places=2)
        c.action_recognise()
        self.assertAlmostEqual(self._bal(self.gain), -300.0, places=2)

    def test_step_acquisition_prior_interest(self):
        # 1000 + 200 + 150 - 900 = 450.
        c = self._combination(previously_held_interest_fv=150.0)
        self.assertAlmostEqual(c.goodwill, 450.0, places=2)

    def test_equity_carrying_anchors_are_engine_owned(self):
        inv = self.env['eh.equity.investment'].create({
            'name': '/', 'investee_name': 'Anchor Target',
            'ownership_pct': 25.0,
        })
        with self.assertRaises(AccessError):
            inv.write({'carrying_amount': 999999.0})
        inv.action_activate()
        with self.assertRaises(UserError):
            inv.write({'ownership_pct': 99.0})

    def test_combination_action_requires_exact_current_company(self):
        other = self.env['res.company'].create({'name': 'IFRS 3 Other Co'})
        combo = self.env['eh.business.combination'].create({
            'name': '/', 'acquiree_name': 'Other Target',
            'company_id': other.id,
        })
        manager = self.env['res.users'].create({
            'name': 'IFRS 3 Multi Manager', 'login': 'ifrs3_multi_manager',
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id, other.id])],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        with self.assertRaises(AccessError):
            combo.with_user(manager).with_company(self.company).action_cancel()

    # ---- IFRS 3 full purchase price allocation ----

    def _combination_ppa(self, **vals):
        # Assets 1500, liability 600 -> identifiable net assets 900.
        base = {
            'asset_line_ids': [
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'is_liability': True}),
            ],
            'nci_account_id': self.nci_equity.id,
        }
        base.update(vals)
        return self._combination(**base)

    def test_ppa_identifiable_net_assets_from_lines(self):
        c = self._combination_ppa()
        self.assertAlmostEqual(c.identifiable_net_assets, 900.0, places=2)
        # Lines drive goodwill: 1000 + 200 + 0 - 900 = 300.
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)

    def test_ppa_full_entry_posts_balanced(self):
        c = self._combination_ppa()
        c.action_recognise_ppa()
        self.assertEqual(c.state, 'recognised')
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # Assets at fair value, goodwill, consideration, NCI.
        self.assertAlmostEqual(self._bal(self.ppa_ppe), 1500.0, places=2)
        self.assertAlmostEqual(self._bal(self.ppa_payable), -600.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.nci_equity), -200.0, places=2)

    def test_ppa_bargain_full_entry_balanced(self):
        # Consideration 500, nci 0 -> 500 - 900 = -400 bargain gain.
        c = self._combination_ppa(consideration_transferred=500.0,
                                   nci_amount=0.0)
        self.assertAlmostEqual(c.bargain_purchase_gain, 400.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(self._bal(self.gain), -400.0, places=2)

    def test_ppa_prior_interest_balances(self):
        # Prior interest is carried via clearing so the entry still balances.
        c = self._combination_ppa(previously_held_interest_fv=150.0)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # Clearing carries consideration 1000 + prior 150 = 1150.
        self.assertAlmostEqual(self._bal(self.clearing), -1150.0, places=2)

    def test_nci_proportionate_computed(self):
        # IFRS 3.19 proportionate basis: NCI = minority% x FV of identifiable
        # net assets, computed, not a manual figure. 20% x 900 = 180.
        c = self._combination(nci_measurement='proportionate', nci_pct=20.0,
                              nci_amount=999.0)
        self.assertAlmostEqual(c.nci_amount, 180.0, places=2)
        # Goodwill uses the computed NCI: 1000 + 180 + 0 - 900 = 280.
        self.assertAlmostEqual(c.goodwill, 280.0, places=2)

    def test_nci_proportionate_net_of_deferred_tax(self):
        # IFRS 3.19 / IAS 12.66: proportionate NCI is a share of the
        # *recognised* identifiable net assets, which are net of the deferred
        # tax on the fair-value step-up, not the pre-tax fair value. PPE FV
        # 1500 over a 1000 tax base is a 500 taxable difference; at 20% the
        # deferred tax liability is 100, so recognised net assets are
        # 900 - 100 = 800. NCI at 20% is 160 (the pre-tax base would wrongly
        # give 180), and goodwill = 1000 + 160 - 900 + 100 = 360 (not 380).
        c = self._combination_ppa(
            nci_measurement='proportionate', nci_pct=20.0, nci_amount=999.0,
            tax_rate=20.0, deferred_tax_account_id=self.deferred_tax.id,
            asset_line_ids=[
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'tax_base': 1000.0,
                        'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'tax_base': 600.0,
                        'is_liability': True}),
            ])
        self.assertAlmostEqual(c.deferred_tax, 100.0, places=2)
        self.assertAlmostEqual(c.identifiable_net_assets, 900.0, places=2)
        # Post-tax base drives NCI: (900 - 100) x 20% = 160.
        self.assertAlmostEqual(c.nci_amount, 160.0, places=2)
        self.assertAlmostEqual(c.goodwill, 360.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction, with NCI and goodwill at the post-tax
        # figures.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(self._bal(self.nci_equity), -160.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 360.0, places=2)

    def test_nci_fair_value_kept(self):
        # Fair-value basis keeps the entered amount (byte-identical default).
        c = self._combination(nci_measurement='fair_value', nci_amount=200.0)
        self.assertAlmostEqual(c.nci_amount, 200.0, places=2)

    def test_recognised_combination_frozen(self):
        # IFRS 3: a recognised, posted combination cannot be retro-edited out
        # from under its move.
        c = self._combination(
            nci_amount=0.0, fv_identifiable_net_assets=700.0)
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        with self.assertRaises(UserError):
            c.consideration_transferred = 2000.0
        with self.assertRaises(UserError):
            c.unlink()

    def test_no_lines_still_goodwill_plug(self):
        # Regression: no asset lines -> byte-identical goodwill plug.
        c = self._combination(
            nci_amount=0.0, fv_identifiable_net_assets=700.0)
        self.assertFalse(c.asset_line_ids)
        c.action_recognise()
        self.assertEqual(c.state, 'recognised')
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 300.0, places=2)
        self.assertAlmostEqual(self._bal(self.clearing), -300.0, places=2)

    def test_measurement_period_posts_current_catchup_on_exact_date(self):
        combination = self._combination_ppa(
            acquisition_date='2026-01-15',
            nci_amount=0.0,
        )
        combination.action_recognise_ppa()
        ppe_line = combination.asset_line_ids.filtered(
            lambda line: not line.is_liability)
        adjustment = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combination.id,
            'name': 'Final depreciation inputs',
            'date': '2026-06-15',
            'accounting_date': '2026-06-30',
            'catchup_depreciation_amortisation': 25.0,
            'catchup_treatment': 'current_period',
            'catchup_accumulated_account_id':
                self.accumulated_depreciation.id,
            'catchup_expense_account_id': self.catchup_expense.id,
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1600.0,
            })],
        })
        adjustment.action_apply()
        self.assertEqual(str(adjustment.retrospective_effective_date),
                         '2026-01-15')
        self.assertEqual(str(adjustment.move_id.date), '2026-06-30')
        expense_line = adjustment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.catchup_expense)
        accumulated_line = adjustment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.accumulated_depreciation)
        self.assertAlmostEqual(expense_line.debit, 25.0, places=2)
        self.assertAlmostEqual(accumulated_line.credit, 25.0, places=2)

    def test_measurement_period_supports_comparative_restatement(self):
        combination = self._combination_ppa(
            acquisition_date='2026-01-15',
            nci_amount=0.0,
        )
        combination.action_recognise_ppa()
        ppe_line = combination.asset_line_ids.filtered(
            lambda line: not line.is_liability)
        adjustment = self.env['eh.bizcombo.adjustment'].create({
            'combination_id': combination.id,
            'name': 'Comparative appraisal completion',
            'date': '2026-06-15',
            'accounting_date': '2026-03-31',
            'catchup_depreciation_amortisation': 25.0,
            'catchup_treatment': 'comparative_restatement',
            'catchup_accumulated_account_id':
                self.accumulated_depreciation.id,
            'comparative_equity_account_id': self.comparative_equity.id,
            'comparative_restatement_note':
                '2026 comparative reopened under controller approval BC-42.',
            'line_ids': [(0, 0, {
                'asset_line_id': ppe_line.id,
                'revised_fair_value': 1600.0,
            })],
        })
        adjustment.action_apply()
        self.assertEqual(str(adjustment.move_id.date), '2026-03-31')
        equity_line = adjustment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.comparative_equity)
        accumulated_line = adjustment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.accumulated_depreciation)
        self.assertAlmostEqual(equity_line.debit, 25.0, places=2)
        self.assertAlmostEqual(accumulated_line.credit, 25.0, places=2)

    def test_contingent_remeasurements_enforce_acquisition_and_apply_order(self):
        combination = self._combination_ppa(
            acquisition_date='2026-01-15',
            nci_amount=0.0,
            contingent_consideration_initial_fv=100.0,
            contingent_classification='liability',
            contingent_account_id=self.contingent_liability.id,
            contingent_pnl_account_id=self.contingent_pnl.id,
        )
        combination.action_recognise_ppa()
        with self.assertRaises(ValidationError):
            self.env['eh.bizcombo.contingent.remeasure'].create({
                'combination_id': combination.id,
                'date': '2026-01-14',
                'new_fair_value': 110.0,
            })
        first = self.env['eh.bizcombo.contingent.remeasure'].create({
            'combination_id': combination.id,
            'date': '2026-06-30',
            'new_fair_value': 120.0,
        })
        first.action_apply()
        out_of_order = self.env[
            'eh.bizcombo.contingent.remeasure'].create({
                'combination_id': combination.id,
                'date': '2026-05-31',
                'new_fair_value': 130.0,
            })
        with self.assertRaisesRegex(UserError, 'in date order'):
            out_of_order.action_apply()
        self.assertEqual(out_of_order.state, 'draft')
        self.assertFalse(out_of_order.move_id)

    # ---- IAS 12 deferred tax on the fair-value step-up ----

    def test_deferred_tax_default_off(self):
        # Regression: no tax rate -> no deferred tax, goodwill unchanged.
        c = self._combination_ppa()
        self.assertAlmostEqual(c.deferred_tax, 0.0, places=2)
        self.assertAlmostEqual(c.goodwill, 300.0, places=2)

    def test_deferred_tax_raises_goodwill_and_books_dtl(self):
        # IAS 12.19/24/.66: PPE stepped up 1500 over a 1000 tax base is a 500
        # taxable temporary difference; the payable's fair value equals its tax
        # base so it adds nothing. At 20% the deferred tax liability is 100,
        # which raises goodwill from 300 to 400.
        c = self._combination_ppa(
            tax_rate=20.0,
            deferred_tax_account_id=self.deferred_tax.id,
            asset_line_ids=[
                (0, 0, {'name': 'PPE', 'account_id': self.ppa_ppe.id,
                        'fair_value': 1500.0, 'tax_base': 1000.0,
                        'is_liability': False}),
                (0, 0, {'name': 'Payables',
                        'account_id': self.ppa_payable.id,
                        'fair_value': 600.0, 'tax_base': 600.0,
                        'is_liability': True}),
            ])
        self.assertAlmostEqual(c.fair_value_step_up, 500.0, places=2)
        self.assertAlmostEqual(c.deferred_tax, 100.0, places=2)
        # Goodwill lifted by the DTL: 300 base -> 400.
        self.assertAlmostEqual(c.goodwill, 400.0, places=2)
        c.action_recognise_ppa()
        move = c.move_id
        self.assertTrue(move)
        # Balanced by construction.
        self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                               sum(move.line_ids.mapped('credit')), places=2)
        # DTL credited 100, goodwill carries the uplift.
        self.assertAlmostEqual(self._bal(self.deferred_tax), -100.0, places=2)
        self.assertAlmostEqual(self._bal(self.goodwill_acc), 400.0, places=2)

    # ---- IAS 28 ----

    def _investment(self, **vals):
        base = {
            'name': '/', 'investee_name': 'Assoc Ltd', 'ownership_pct': 30.0,
            'cost_of_investment': 1000.0,
            'investment_account_id': self.investment.id,
            'share_of_profit_account_id': self.share_profit.id,
            'cash_account_id': self.account_cash.id,
            'impairment_account_id': self.impairment.id,
            'disposal_gain_loss_account_id': self.disposal_gain.id,
            'retained_interest_account_id': self.retained_interest.id,
            'oci_reserve_account_id': self.oci_reserve.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.equity.investment'].create(base)

    def _posted_opening_move(self, amount=1000.0):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-01-31',
            'journal_id': self.journal_misc.id,
            'ref': 'Existing associate recognition',
            'line_ids': [
                (0, 0, {
                    'name': 'Existing investment',
                    'account_id': self.investment.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'Existing consideration',
                    'account_id': self.account_cash.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ],
        })
        move.action_post()
        return move

    def test_activate_adopts_existing_recognition_without_duplicate_cash(self):
        opening = self._posted_opening_move()
        investment = self._investment(
            initial_recognition_method='already_recognised',
            opening_move_id=opening.id,
            accounting_date='2026-01-31',
        )
        investment.action_activate()
        self.assertEqual(investment.state, 'active')
        self.assertEqual(investment.opening_move_id, opening)
        self.assertFalse(investment.move_ids)
        self.assertEqual(investment.move_count, 1)
        self.assertAlmostEqual(investment.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.investment), 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), -1000.0, places=2)

    def test_activate_rejects_unproved_opening_balance(self):
        opening = self._posted_opening_move(amount=900.0)
        investment = self._investment(
            initial_recognition_method='already_recognised',
            opening_move_id=opening.id,
            cost_of_investment=1000.0,
        )
        with self.assertRaisesRegex(UserError, 'expected exact opening cost'):
            investment.action_activate()
        self.assertEqual(investment.state, 'draft')

    def test_partial_disposal_rolls_carrying_ownership_oci_and_memoranda(self):
        investment = self._investment(
            ownership_pct=30.0,
            cost_of_investment=1000.0,
        )
        investment.action_activate()
        investment.write({
            'disposal_treatment': 'continue_equity_method',
            'disposal_percentage': 40.0,
            'disposal_proceeds': 500.0,
            'oci_recycling_amount': 20.0,
        })
        investment.action_dispose()
        self.assertEqual(investment.state, 'active')
        self.assertAlmostEqual(investment.carrying_amount, 600.0, places=2)
        self.assertAlmostEqual(
            investment.current_ownership_pct, 18.0, places=4)
        self.assertAlmostEqual(
            investment.cumulative_disposed_carrying, 400.0, places=2)
        self.assertAlmostEqual(
            investment.cumulative_disposal_proceeds, 500.0, places=2)
        self.assertAlmostEqual(
            investment.cumulative_oci_recycled, 20.0, places=2)
        self.assertAlmostEqual(self._bal(self.oci_reserve), 20.0, places=2)
        self.assertAlmostEqual(self._bal(self.disposal_gain), -120.0, places=2)
        investment.investee_profit = 100.0
        investment.action_pickup_profit()
        self.assertAlmostEqual(investment.carrying_amount, 618.0, places=2)

    def test_cease_equity_method_recognises_retained_interest_at_fair_value(self):
        investment = self._investment(
            ownership_pct=30.0,
            cost_of_investment=1000.0,
        )
        investment.action_activate()
        investment.write({
            'disposal_treatment': 'cease_equity_method',
            'disposal_percentage': 50.0,
            'disposal_proceeds': 700.0,
            'retained_interest_fair_value': 400.0,
        })
        investment.action_dispose()
        self.assertEqual(investment.state, 'disposed')
        self.assertAlmostEqual(investment.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(
            investment.current_ownership_pct, 15.0, places=4)
        self.assertAlmostEqual(
            investment.cumulative_retained_interest_fv, 400.0, places=2)
        self.assertAlmostEqual(self._bal(self.retained_interest), 400.0,
                               places=2)
        self.assertAlmostEqual(self._bal(self.disposal_gain), -100.0, places=2)

    def test_dispose_posts_gain(self):
        # IAS 28.22: proceeds above the carrying amount are a gain to P&L and
        # the carrying amount is derecognised.
        inv = self._investment(cost_of_investment=1000.0)
        inv.action_activate()
        inv.disposal_proceeds = 1250.0
        inv.action_dispose()
        self.assertEqual(inv.state, 'disposed')
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        # Acquisition Dr 1000 then derecognition Cr 1000 net the investment
        # account to zero; gain 250 to P&L; cash = 1250 proceeds - 1000 paid.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.disposal_gain), -250.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), 250.0, places=2)
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_dispose_posts_loss(self):
        # Proceeds below the carrying amount are a loss to P&L.
        inv = self._investment(cost_of_investment=1000.0,
                               disposal_gain_loss_account_id=self.disposal_loss.id)
        inv.action_activate()
        inv.disposal_proceeds = 700.0
        inv.action_dispose()
        # Acquisition Dr 1000 nets against derecognition Cr 1000 -> zero.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.disposal_loss), 300.0, places=2)
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_dispose_requires_active(self):
        inv = self._investment()
        with self.assertRaises(UserError):
            inv.action_dispose()

    def test_equity_method_roll_forward(self):
        inv = self._investment(ownership_pct=30.0, cost_of_investment=1000.0)
        inv.action_activate()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        # Investee profit 500 x 30% = 150.
        inv.investee_profit = 500.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.carrying_amount, 1150.0, places=2)
        # Acquisition Dr 1000 + share of profit Dr 150 = 1150 on the account.
        self.assertAlmostEqual(self._bal(self.investment), 1150.0, places=2)
        self.assertAlmostEqual(self._bal(self.share_profit), -150.0, places=2)
        # Dividend 60 reduces carrying, is not income.
        inv.dividend_received = 60.0
        inv.action_record_dividend()
        self.assertAlmostEqual(inv.carrying_amount, 1090.0, places=2)
        # Impair 90.
        inv.impairment_amount = 90.0
        inv.action_impair()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        self.assertAlmostEqual(self._bal(self.impairment), 90.0, places=2)

    def test_dividend_cannot_drive_carrying_amount_negative(self):
        inv = self._investment(cost_of_investment=1000.0)
        inv.action_activate()
        move_count = len(inv.move_ids)
        inv.dividend_received = 1000.01
        with self.assertRaisesRegex(UserError, 'cannot exceed'):
            inv.action_record_dividend()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        self.assertEqual(len(inv.move_ids), move_count)

    def test_impairment_cannot_drive_carrying_amount_negative(self):
        inv = self._investment(cost_of_investment=1000.0)
        inv.action_activate()
        move_count = len(inv.move_ids)
        inv.impairment_amount = 1000.01
        with self.assertRaisesRegex(UserError, 'cannot exceed'):
            inv.action_impair()
        self.assertAlmostEqual(inv.carrying_amount, 1000.0, places=2)
        self.assertEqual(len(inv.move_ids), move_count)

    def test_equity_method_entries_use_explicit_accounting_date(self):
        inv = self._investment(
            cost_of_investment=1000.0,
            accounting_date='2025-12-31',
        )
        inv.action_activate()
        acquisition_move = inv.move_ids.sorted('id')[-1]
        self.assertEqual(str(acquisition_move.date), '2025-12-31')

        inv.write({
            'accounting_date': '2026-06-30',
            'investee_profit': 500.0,
        })
        inv.action_pickup_profit()
        pickup_move = inv.move_ids.sorted('id')[-1]
        self.assertEqual(str(pickup_move.date), '2026-06-30')
        self.assertNotEqual(acquisition_move, pickup_move)

    def test_equity_method_share_of_loss(self):
        inv = self._investment(ownership_pct=40.0, cost_of_investment=1000.0)
        inv.action_activate()
        inv.investee_profit = -250.0  # loss
        inv.action_pickup_profit()
        # 40% of -250 = -100.
        self.assertAlmostEqual(inv.carrying_amount, 900.0, places=2)

    def test_equity_method_loss_floored_at_zero(self):
        # IAS 28.38: a share of loss exceeding the carrying amount is capped
        # so the carrying amount floors at zero, not negative.
        inv = self._investment(ownership_pct=50.0, cost_of_investment=1000.0)
        inv.action_activate()
        # 50% of -3000 = -1500, which exceeds the 1000 carrying amount.
        inv.investee_profit = -3000.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        # Acquisition Dr 1000 then share-of-loss Cr 1000 (floored at nil) net
        # the investment account to zero.
        self.assertAlmostEqual(self._bal(self.investment), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.share_profit), 1000.0, places=2)
        # The entry balances by construction.
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
        # Once at nil, no further loss can be recognised.
        inv.investee_profit = -500.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.unrecognised_loss, 750.0, places=2)
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)

    def test_future_profit_absorbs_unrecognised_losses_before_pickup(self):
        inv = self._investment(
            ownership_pct=50.0,
            cost_of_investment=1000.0,
        )
        inv.action_activate()
        inv.investee_profit = -3000.0
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(inv.unrecognised_loss, 500.0, places=2)

        move_count = len(inv.move_ids)
        inv.investee_profit = 600.0  # investor share 300: fully absorbed
        inv.action_pickup_profit()
        self.assertEqual(len(inv.move_ids), move_count)
        self.assertAlmostEqual(inv.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(inv.unrecognised_loss, 200.0, places=2)

        inv.investee_profit = 500.0  # share 250: absorb 200, recognise 50
        inv.action_pickup_profit()
        self.assertAlmostEqual(inv.unrecognised_loss, 0.0, places=2)
        self.assertAlmostEqual(inv.carrying_amount, 50.0, places=2)
        self.assertAlmostEqual(inv.cumulative_share_of_profit, -950.0, places=2)
        self.assertAlmostEqual(self._bal(self.investment), 50.0, places=2)

    def test_pickup_requires_active(self):
        inv = self._investment()
        inv.investee_profit = 100.0
        with self.assertRaises(UserError):
            inv.action_pickup_profit()

    def test_entries_balance(self):
        inv = self._investment()
        inv.action_activate()
        inv.investee_profit = 500.0
        inv.action_pickup_profit()
        for move in inv.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)
