# -*- encoding: utf-8 -*-
"""Focused regressions for the Fair Value medium/low audit tranche."""

import ast
from pathlib import Path

from lxml import etree

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_fair_value', 'post_install', '-at_install')
class TestFairValueMediumLowBacklog(EhAccountIntegrationTestCase):

    _DATE = '2026-06-30'
    _SPPI = {
        'sppi_fixed_dates': True,
        'sppi_interest_only': True,
        'sppi_no_leverage': True,
        'sppi_no_contingent_returns': True,
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.fv_asset = cls._ensure_account(
            cls.env, '1617', 'Backlog Investments at FV', 'asset_current',
        )
        cls.fv_liability = cls._ensure_account(
            cls.env, '2617', 'Backlog Liabilities at FV',
            'liability_current',
        )
        cls.fv_gain = cls._ensure_account(
            cls.env, '4617', 'Backlog FV Gain Loss', 'income_other',
        )
        cls.fv_oci = cls._ensure_account(
            cls.env, '3617', 'Backlog FV OCI Reserve', 'equity',
        )
        # Odoo 17/18 enforce one current-year-earnings account per company.
        # Reuse the chart account when present instead of manufacturing a
        # second ``equity_unaffected`` row merely for the domain regression.
        Account = cls.env['account.account']
        company_field = (
            'company_ids' if 'company_ids' in Account._fields else 'company_id'
        )
        cls.fv_retained = Account.search([
            ('account_type', '=', 'equity_unaffected'),
            (company_field, 'in', cls.company.ids),
        ], limit=1) or cls._ensure_account(
            cls.env, '3627', 'Backlog Retained Earnings',
            'equity_unaffected',
        )

    def _item(self, **values):
        vals = {
            'name': '/',
            'nature': 'financial_asset',
            'level': '1',
            'measurement_date': self._DATE,
            'prior_carrying': 1000.0,
            'fair_value': 1200.0,
            'routing': 'pl',
            'balance_sheet_account_id': self.fv_asset.id,
            'gain_loss_account_id': self.fv_gain.id,
            'oci_account_id': self.fv_oci.id,
            'settlement_account_id': self.account_cash.id,
            'retained_earnings_account_id': self.fv_retained.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(values)
        if vals.get('instrument_type'):
            vals.pop('routing', None)
        return self.env['eh.fair.value.item'].create(vals)

    def _debt(self, **values):
        vals = {
            'instrument_type': 'debt',
            'business_model': 'hold_collect_sell',
            **self._SPPI,
        }
        vals.update(values)
        return self._item(**vals)

    def _item_account_balance(self, item, account):
        lines = item.move_ids.line_ids.filtered(
            lambda line: line.account_id == account
        )
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_rollforward_pull_excludes_disposal_and_reserve_entries(self):
        item = self._debt(level='3')
        line = self.env['eh.fair.value.rollforward'].create({
            'item_id': item.id,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
            'opening_balance': 1000.0,
        })
        item.action_remeasure()
        measurement = item.move_ids.filtered(
            lambda move: move.eh_fair_value_move_role == 'remeasurement'
        )
        item.write({
            'disposal_proceeds': 1200.0,
            'disposal_date': self._DATE,
        })
        item.action_derecognise()
        self.assertEqual(
            set(item.move_ids.mapped('eh_fair_value_move_role')),
            {'remeasurement', 'derecognition', 'reserve_settlement'},
        )

        line.action_pull_ledger()
        line.invalidate_recordset(['move_ids', 'gains_losses_in_oci'])
        line._compute_ledger_gains()
        self.assertEqual(line.move_ids, measurement)
        self.assertAlmostEqual(line.gains_losses_in_oci, 200.0, places=2)
        self.assertAlmostEqual(line.gains_losses_in_pl, 0.0, places=2)

    def test_latest_remeasurement_has_sealed_controlled_reversal(self):
        item = self._item()
        item.action_remeasure()
        original = item.move_ids.filtered(
            lambda move: move.eh_fair_value_move_role == 'remeasurement'
        )
        item.write({
            'reversal_date': self._DATE,
            'reversal_reason': 'Valuation source was superseded.',
        })
        item.action_reverse_remeasurement()
        reversal = item.move_ids.filtered(
            lambda move: move.eh_fair_value_move_role == 'reversal'
        )

        self.assertEqual(item.state, 'draft')
        self.assertAlmostEqual(item.prior_carrying, 1000.0, places=2)
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertEqual(reversal.date, fields.Date.to_date(self._DATE))
        self.assertTrue(original.eh_sealed and reversal.eh_sealed)
        self.assertAlmostEqual(
            self._item_account_balance(item, self.fv_asset), 0.0, places=2,
        )
        self.assertAlmostEqual(
            self._item_account_balance(item, self.fv_gain), 0.0, places=2,
        )

    def test_foreign_fvoci_debt_splits_fx_to_pl_and_preserves_currency(self):
        foreign = self.env['res.currency'].create({
            'name': 'FVX',
            'symbol': 'FVX',
            'rounding': 0.01,
            'active': True,
        })
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id,
            'company_id': self.company.id,
            'name': self._DATE,
            'rate': 2.0,
        })
        item = self._debt(
            currency_id=foreign.id,
            prior_carrying=100.0,
            prior_company_carrying=40.0,
            fair_value=120.0,
        )
        self.assertAlmostEqual(item.company_fair_value, 60.0, places=2)
        self.assertAlmostEqual(item.fx_remeasurement, 10.0, places=2)
        self.assertAlmostEqual(item.non_fx_remeasurement, 10.0, places=2)

        item.action_remeasure()
        move = item.move_ids.filtered(
            lambda row: row.eh_fair_value_move_role == 'remeasurement'
        )
        self.assertAlmostEqual(
            self._item_account_balance(item, self.fv_asset), 20.0, places=2,
        )
        self.assertAlmostEqual(
            self._item_account_balance(item, self.fv_gain), -10.0, places=2,
        )
        self.assertAlmostEqual(
            self._item_account_balance(item, self.fv_oci), -10.0, places=2,
        )
        foreign_lines = move.line_ids.filtered(
            lambda line: line.currency_id == foreign
        )
        self.assertAlmostEqual(
            sum(foreign_lines.mapped('amount_currency')), 0.0, places=2,
        )
        position_lines = foreign_lines.filtered(
            lambda line: line.account_id == self.fv_asset
        )
        self.assertAlmostEqual(
            sum(position_lines.mapped('amount_currency')), 20.0, places=2,
        )

    def test_zero_change_is_a_measurement_and_can_be_derecognised(self):
        item = self._item(fair_value=1000.0)
        item.action_remeasure()
        self.assertEqual(item.state, 'measured')
        self.assertFalse(item.move_ids)
        with self.assertRaisesRegex(UserError, 'nothing to post'):
            item.action_remeasure()

        item.write({
            'disposal_proceeds': 1000.0,
            'disposal_date': self._DATE,
        })
        item.action_derecognise()
        self.assertEqual(item.state, 'derecognised')
        self.assertEqual(item.move_ids.eh_fair_value_move_role,
                         'derecognition')

    def test_legacy_fvoci_label_rejects_non_asset_natures(self):
        for nature in ('financial_liability', 'non_financial_asset'):
            for label in ('fvoci_debt', 'fvoci_equity'):
                with self.subTest(nature=nature, label=label):
                    with self.assertRaisesRegex(
                        ValidationError, 'financial asset',
                    ):
                        self._item(
                            nature=nature,
                            fvoci_classification=label,
                            balance_sheet_account_id=(
                                self.fv_liability.id
                                if nature == 'financial_liability'
                                else self.fv_asset.id
                            ),
                        )

    def test_measured_level_change_requires_immutable_transfer_record(self):
        item = self._item(level='2')
        item.action_remeasure()
        with self.assertRaisesRegex(UserError, 'posted history'):
            item.write({'level': '3'})
        item.write({
            'proposed_level': '3',
            'level_transfer_date': self._DATE,
            'level_transfer_reason': 'Inputs became unobservable.',
        })
        item.action_transfer_level()
        transfer = item.level_transfer_ids
        self.assertEqual(item.level, '3')
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer.from_level, '2')
        self.assertEqual(transfer.to_level, '3')
        self.assertEqual(transfer.recognition_policy, 'event_date')
        self.assertAlmostEqual(transfer.transfer_amount, 1200.0, places=2)
        with self.assertRaisesRegex(UserError, 'immutable'):
            transfer.write({'reason': 'Rewrite history'})
        with self.assertRaisesRegex(UserError, 'immutable'):
            transfer.unlink()

    def test_ifrs9_415_designation_overrides_truthful_sppi_result(self):
        designated = self._debt(
            business_model='hold_to_collect',
            fvtpl_designation=True,
            fvtpl_designation_basis='accounting_mismatch',
            fvtpl_designation_reason=(
                'Liability is managed at fair value through profit or loss.'
            ),
        )
        self.assertTrue(designated.sppi_pass)
        self.assertEqual(designated.ifrs9_classification, 'fvtpl')
        self.assertEqual(designated.routing, 'pl')
        designated.action_remeasure()

        control = self._debt(business_model='hold_to_collect')
        self.assertEqual(control.ifrs9_classification, 'amortised_cost')
        with self.assertRaisesRegex(UserError, 'amortised cost'):
            control.action_remeasure()
        with self.assertRaisesRegex(ValidationError, 'mismatch'):
            self._debt(
                business_model='hold_to_collect',
                fvtpl_designation=True,
            )

    def test_non_financial_oci_reserve_transfers_on_derecognition(self):
        item = self._item(
            nature='non_financial_asset',
            routing='oci',
            fvoci_classification=False,
        )
        item.action_remeasure()
        item.write({
            'disposal_proceeds': 1200.0,
            'disposal_date': self._DATE,
        })
        item.action_derecognise()
        reserve_move = item.move_ids.filtered(
            lambda move: move.eh_fair_value_move_role == 'reserve_settlement'
        )
        retained_line = reserve_move.line_ids.filtered(
            lambda line: line.account_id == self.fv_retained
        )
        self.assertFalse(item._effective_classification())
        self.assertTrue(item.recycled)
        self.assertEqual(len(reserve_move), 1)
        self.assertAlmostEqual(retained_line.credit, 200.0, places=2)
        item.invalidate_recordset(['oci_reserve_balance'])
        self.assertAlmostEqual(item.oci_reserve_balance, 0.0, places=2)

    def test_cancel_group_and_retained_earnings_domain_match_server(self):
        form = self.env.ref(
            'eh_account_fair_value.view_eh_fair_value_item_form',
        )
        root = etree.fromstring(form.arch.encode())
        cancel = root.xpath("//button[@name='action_cancel']")
        self.assertEqual(len(cancel), 1)
        self.assertIn(
            'eh_account_base.group_eh_manager',
            cancel[0].get('groups', ''),
        )
        retained_domain = str(
            self.env['eh.fair.value.item']._fields[
                'retained_earnings_account_id'
            ].domain
        )
        self.assertIn('equity_unaffected', retained_domain)

    def test_rollforward_defaults_advance_to_the_next_period(self):
        item = self._item(level='3')
        Rollforward = self.env['eh.fair.value.rollforward'].with_context(
            default_item_id=item.id,
        )
        first = Rollforward.create({'item_id': item.id})
        second_defaults = Rollforward.default_get([
            'period_start', 'period_end',
        ])
        second = Rollforward.create({'item_id': item.id})
        self.assertEqual(
            second_defaults['period_start'], fields.Date.add(
                first.period_end, days=1,
            ),
        )
        self.assertEqual(second.period_start, second_defaults['period_start'])
        self.assertEqual(second.period_end, second_defaults['period_end'])
        self.assertNotEqual(
            (first.period_start, first.period_end),
            (second.period_start, second.period_end),
        )

    def test_manifest_listing_and_migration_are_release_current(self):
        module = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval((module / '__manifest__.py').read_text())
        self.assertRegex(
            manifest['version'], r'^(16|17|18|19)\.0\.1\.1\.8$',
        )
        self.assertLess(len(manifest['summary']), 200)
        listing = (module / 'static/description/index.html').read_text()
        self.assertEqual(listing.count('v%s' % manifest['version']), 2)
        migration = (
            module / 'migrations' / '19.0.1.1.7' / 'post-migration.py'
        )
        if not migration.exists():
            migration = next(
                (module / 'migrations').glob('*.0.1.1.7/post-migration.py')
            )
        source = migration.read_text()
        self.assertIn('eh_fair_value_move_role', source)
        self.assertIn('prior_company_carrying', source)
        self.assertIn('eh_fair_value_rollforward_id = NULL', source)
