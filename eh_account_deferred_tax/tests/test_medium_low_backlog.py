# -*- encoding: utf-8 -*-
"""Focused regressions for the Deferred Tax medium/low audit backlog."""

import ast
import inspect
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_deferred_tax', 'post_install', '-at_install')
class TestDeferredTaxMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.dta = cls._ensure_account(
            cls.env, '1817', 'Backlog Deferred Tax Asset',
            'asset_non_current',
        )
        cls.dtl = cls._ensure_account(
            cls.env, '2817', 'Backlog Deferred Tax Liability',
            'liability_non_current',
        )
        cls.expense = cls._ensure_account(
            cls.env, '5817', 'Backlog Deferred Tax Expense', 'expense',
        )
        cls.oci = cls._ensure_account(
            cls.env, '3817', 'Backlog Deferred Tax OCI', 'equity',
        )

    def _run(self, period_end='2026-12-31', **values):
        vals = {
            'period_end': period_end,
            'statutory_rate': 25.0,
            'dta_account_id': self.dta.id,
            'dtl_account_id': self.dtl.id,
            'deferred_tax_expense_account_id': self.expense.id,
            'oci_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
        }
        vals.update(values)
        return self.env['eh.deferred.tax.run'].create(vals)

    def _line(self, run, name='Difference', **values):
        vals = {
            'run_id': run.id,
            'name': name,
            'nature': 'asset',
            'carrying_amount': 1000.0,
            'tax_base': 600.0,
        }
        vals.update(values)
        return self.env['eh.deferred.tax.line'].create(vals)

    @staticmethod
    def _net_debit(move, account):
        lines = move.line_ids.filtered(lambda line: line.account_id == account)
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def _auto_line(self, run, source_key='existing'):
        return self.env['eh.deferred.tax.line'].sudo().with_context(
            eh_deferred_tax_engine=True,
        ).create({
            'run_id': run.id,
            'name': 'Existing gathered evidence',
            'category': 'provision',
            'nature': 'liability',
            'carrying_amount': 100.0,
            'tax_base': 0.0,
            'eh_auto_gathered': True,
            'source_model': 'res.company',
            'source_key': source_key,
        })

    def test_policy_switch_requires_reason_and_reclassifies_opening(self):
        prior = self._run('2025-12-31', offsetting_policy='gross')
        self._line(
            prior, 'Provision', nature='liability',
            carrying_amount=400.0, tax_base=0.0,
        )
        self._line(
            prior, 'Depreciation', nature='asset',
            carrying_amount=320.0, tax_base=0.0,
        )
        prior.action_compute()
        prior.action_post()
        self.assertAlmostEqual(self._net_debit(prior.move_id, self.dta), 100.0)
        self.assertAlmostEqual(self._net_debit(prior.move_id, self.dtl), -80.0)

        current = self._run(
            '2026-12-31', offsetting_policy='net_by_jurisdiction',
        )
        self._line(
            current, 'Provision', nature='liability',
            carrying_amount=400.0, tax_base=0.0,
        )
        self._line(
            current, 'Depreciation', nature='asset',
            carrying_amount=320.0, tax_base=0.0,
        )
        current.action_compute()
        self.assertTrue(current.offsetting_policy_changed)
        with self.assertRaises(UserError):
            current.action_post()
        self.assertFalse(current.move_id)

        current.offsetting_policy_change_reason = (
            'Legally enforceable set-off criteria confirmed for 2026.'
        )
        current.action_compute()
        current.action_post()
        # Same net position, but the new policy must reclassify the actual
        # gross opening: Cr DTA 80 / Dr DTL 80, with no P&L movement.
        self.assertAlmostEqual(self._net_debit(current.move_id, self.dta), -80.0)
        self.assertAlmostEqual(self._net_debit(current.move_id, self.dtl), 80.0)
        self.assertAlmostEqual(
            self._net_debit(current.move_id, self.expense), 0.0,
        )

    def test_posted_line_identity_category_and_gather_flag_are_frozen(self):
        run = self._run()
        line = self._line(run, category='depreciation')
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            line.write({'name': 'Re-keyed after posting'})
        with self.assertRaises(UserError):
            line.write({'category': 'provision'})
        with self.assertRaises(AccessError):
            line.write({'eh_auto_gathered': True})

    def test_provider_failure_is_atomic_and_posts_no_success_message(self):
        run = self._run()
        existing = self._auto_line(run)
        before_messages = len(run.message_ids)

        def broken_provider(records, reporting_date):
            records.env.cr.execute('SELECT 1 / 0')

        Company = type(self.env['res.company'])
        with patch.object(
            type(run), '_eh_deferred_tax_providers',
            return_value=['res.company'],
        ), patch.object(
            Company, 'eh_deferred_tax_temp_diffs',
            broken_provider, create=True,
        ), self.assertRaises(UserError):
            run.action_gather_from_engines()
        run.invalidate_recordset(['line_ids', 'message_ids'])
        self.assertIn(existing, run.line_ids)
        self.assertEqual(len(run.message_ids), before_messages)
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_gathered_line_create_failure_is_controlled_and_atomic(self):
        run = self._run()
        existing = self._auto_line(run)

        def provider(records, reporting_date):
            return [{
                'name': 'Replacement provision',
                'source_key': 'replacement',
                'category': 'provision',
                'nature': 'liability',
                'carrying_amount': 500.0,
                'tax_base': 0.0,
            }]

        Company = type(self.env['res.company'])
        LineClass = type(self.env['eh.deferred.tax.line'])
        with patch.object(
            type(run), '_eh_deferred_tax_providers',
            return_value=['res.company'],
        ), patch.object(
            Company, 'eh_deferred_tax_temp_diffs', provider, create=True,
        ), patch.object(
            LineClass, 'create', side_effect=RuntimeError('bad provider row'),
        ), self.assertRaises(UserError):
            run.action_gather_from_engines()
        run.invalidate_recordset(['line_ids'])
        self.assertIn(existing, run.line_ids)
        self.assertEqual(len(run.line_ids.filtered('eh_auto_gathered')), 1)
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_compute_uses_one_transition_lock_and_batched_prior_snapshot(self):
        prior = self._run('2025-12-31')
        for index in range(24):
            self._line(
                prior, 'Difference %02d' % index,
                carrying_amount=1000.0 + index,
            )
        prior.action_compute()
        prior.action_post()
        current = self._run('2026-12-31')
        for index in range(24):
            self._line(
                current, 'Difference %02d' % index,
                carrying_amount=1200.0 + index,
            )

        RunClass = type(current)
        original_lock = RunClass._eh_lock_for_transition
        original_prior = RunClass._prior_posted_run
        lock_calls = []
        prior_calls = []

        def lock_spy(records):
            lock_calls.append(tuple(records.ids))
            return original_lock(records)

        def prior_spy(records):
            prior_calls.append(tuple(records.ids))
            return original_prior(records)

        with patch.object(RunClass, '_eh_lock_for_transition', lock_spy), \
                patch.object(RunClass, '_prior_posted_run', prior_spy):
            current.action_compute()
        # One action lock, one disclosure rebuild write, and one final state
        # stamp: constant in line count and no per-line lock amplification.
        self.assertLessEqual(len(lock_calls), 3)
        self.assertLessEqual(len(prior_calls), 4)
        self.assertNotIn(
            'res_company', inspect.getsource(RunClass._eh_lock_for_transition),
        )

    def test_material_reconciliation_residual_blocks_until_explained(self):
        run = self._run(
            accounting_profit=1000.0,
            current_tax_expense=0.0,
            reconciliation_materiality=1.0,
        )
        self._line(run)  # DTL movement 100; expected tax 250 -> residual -150.
        run.action_compute()
        self.assertAlmostEqual(
            run.unexplained_reconciliation_residual, -150.0,
        )
        self.assertTrue(run.reconciliation_residual_material)
        with self.assertRaises(UserError):
            run.action_post()
        self.env['eh.deferred.tax.recon.line'].create({
            'run_id': run.id,
            'kind': 'other',
            'name': 'Current-tax timing explanation',
            'amount': -150.0,
        })
        run.action_compute()
        self.assertFalse(run.reconciliation_residual_material)
        self.assertAlmostEqual(run.unexplained_reconciliation_residual, 0.0)
        run.action_post()

    def test_view_search_scopes_and_manager_buttons_match_server(self):
        form = self.env.ref(
            'eh_account_deferred_tax.view_eh_deferred_tax_run_form',
        )
        root = etree.fromstring(form.arch.encode())
        manager = 'eh_account_base.group_eh_manager'
        for action in ('action_cancel', 'action_set_to_draft'):
            buttons = root.xpath("//button[@name='%s']" % action)
            self.assertEqual(len(buttons), 1)
            self.assertIn(manager, buttons[0].get('groups', ''))
        self.assertTrue(root.xpath("//field[@name='eh_auto_gathered']"))
        self.assertTrue(root.xpath(
            "//field[@name='jurisdiction_id'][contains(@domain, 'company_id')]"
        ))
        if 'company_ids' in self.env['account.account']._fields:
            self.assertIn("'company_ids'", form.arch)
        else:
            self.assertNotIn("'company_ids'", form.arch)
        search = self.env.ref(
            'eh_account_deferred_tax.view_eh_deferred_tax_run_search',
        )
        for token in ('period_end', 'state', 'company_id', 'group_year'):
            self.assertIn(token, search.arch)
        action = self.env.ref(
            'eh_account_deferred_tax.action_eh_deferred_tax_run',
        )
        self.assertEqual(action.search_view_id, search)

    def test_fiscal_and_hard_locks_fail_before_move_creation(self):
        run = self._run(period_end='2026-03-31')
        self._line(run)
        run.action_compute()
        physical = [
            name for name in ('fiscalyear_lock_date', 'hard_lock_date')
            if name in self.company._fields
        ]
        if not physical:
            self.skipTest('No physical accounting lock fields on this series')
        original = {name: self.company[name] for name in physical}
        before = self.env['account.move'].search_count([
            ('journal_id', '=', self.journal_misc.id),
        ])
        try:
            for field_name in physical:
                with self.subTest(lock=field_name):
                    self.env.flush_all()
                    assignments = ', '.join('%s = %%s' % name for name in physical)
                    values = [
                        '2026-03-31' if name == field_name else None
                        for name in physical
                    ]
                    self.env.cr.execute(
                        'UPDATE res_company SET %s WHERE id = %%s' % assignments,
                        values + [self.company.id],
                    )
                    self.company.invalidate_recordset(physical)
                    with self.assertRaises(UserError):
                        run.action_post()
                    self.assertFalse(run.move_id)
                    self.assertEqual(
                        self.env['account.move'].search_count([
                            ('journal_id', '=', self.journal_misc.id),
                        ]),
                        before,
                    )
        finally:
            self.env.flush_all()
            assignments = ', '.join('%s = %%s' % name for name in physical)
            self.env.cr.execute(
                'UPDATE res_company SET %s WHERE id = %%s' % assignments,
                [original[name] or None for name in physical]
                + [self.company.id],
            )
            self.company.invalidate_recordset(physical)

    def test_record_rules_isolate_another_company(self):
        other = self.env['res.company'].create({'name': 'Deferred Tax Other'})
        hidden = self.env['eh.deferred.tax.run'].sudo().with_company(other).create({
            'company_id': other.id,
            'period_end': '2026-12-31',
            'statutory_rate': 25.0,
        })
        user = new_test_user(
            self.env,
            login='eh_dtax_isolated_user',
            groups='eh_account_base.group_eh_user',
            company_id=self.company.id,
            company_ids=[(6, 0, self.company.ids)],
        )
        visible = self.env['eh.deferred.tax.run'].with_user(user).with_company(
            self.company,
        ).search([('id', '=', hidden.id)])
        self.assertFalse(visible)

    def test_enacted_rate_change_makes_computed_run_stale(self):
        jurisdiction = self.env['eh.tax.jurisdiction'].create({
            'name': 'Backlog Rate Jurisdiction',
            'company_id': self.company.id,
        })
        self.env['eh.tax.rate'].create({
            'jurisdiction_id': jurisdiction.id,
            'effective_from': '2020-01-01',
            'enacted_date': '2019-01-01',
            'rate': 25.0,
        })
        run = self._run()
        line = self._line(run, jurisdiction_id=jurisdiction.id)
        run.action_compute()
        self.assertEqual(line.tax_rate, 25.0)
        self.env['eh.tax.rate'].create({
            'jurisdiction_id': jurisdiction.id,
            'effective_from': '2026-01-01',
            'enacted_date': '2025-06-01',
            'rate': 30.0,
        })
        with self.assertRaises(UserError):
            run.action_post()
        self.assertFalse(run.move_id)
        run.action_compute()
        self.assertEqual(line.tax_rate, 30.0)
        run.action_post()

    def test_one_default_jurisdiction_and_reporting_year_sequence(self):
        default = self.env['eh.tax.jurisdiction']._get_company_default(
            self.company,
        )
        self.assertTrue(default.is_company_default)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['eh.tax.jurisdiction'].create({
                'name': 'Second Default',
                'company_id': self.company.id,
                'is_company_default': True,
            })
        self.assertEqual(self.env['eh.tax.jurisdiction'].search_count([
            ('company_id', '=', self.company.id),
            ('is_company_default', '=', True),
        ]), 1)
        run = self._run(period_end='2034-06-30')
        self.assertIn('DTAX/2034/', run.name)

    def test_structured_disclosure_and_printable_note(self):
        run = self._run()
        self._line(
            run,
            name='Expiring tax loss',
            category='tax_loss',
            nature='tax_loss',
            carrying_amount=1000.0,
            tax_base=0.0,
            recoverable=False,
            expiry_date='2027-06-30',
        )
        self._line(
            run,
            name='Tax depreciation',
            category='depreciation',
            nature='asset',
            carrying_amount=800.0,
            tax_base=400.0,
        )
        run.action_compute()
        rows = run._ias12_disclosure_rows()
        loss = next(row for row in rows if row['category'] == 'tax_loss')
        self.assertEqual(loss['expiry_band'], 'within_1y')
        self.assertAlmostEqual(loss['gross_deductible'], 1000.0)
        self.assertAlmostEqual(loss['unrecognised_gross'], 1000.0)
        self.assertAlmostEqual(loss['unrecognised_dta'], 250.0)
        self.assertIn('Gross deductible', run.disclosure_note_html)
        report = self.env.ref(
            'eh_account_deferred_tax.action_report_eh_deferred_tax_ias12_note',
        )
        html, file_type = report._render_qweb_html(report.report_name, run.ids)
        self.assertEqual(file_type, 'html')
        self.assertIn(b'IAS 12 Deferred Tax Note', html)
        self.assertIn(b'Tax loss / credit carried forward', html)

    def test_manifest_listing_and_dead_alias_are_current(self):
        module = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval((module / '__manifest__.py').read_text())
        self.assertRegex(
            manifest['version'], r'^(16|17|18|19)\.0\.1\.1\.8$',
        )
        self.assertLess(len(manifest['summary']), 200)
        listing = (module / 'static/description/index.html').read_text()
        self.assertEqual(listing.count('v%s' % manifest['version']), 2)
        for stale in (
            'single enacted rate you enter per line',
            'does not maintain separate tax jurisdictions',
            'record rules on both models',
        ):
            self.assertNotIn(stale, listing)
        source = (module / 'models/deferred_tax_run.py').read_text()
        self.assertNotIn('def _eh_lock_for_post', source)
