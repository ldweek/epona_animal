# -*- encoding: utf-8 -*-
"""Focused regressions for the disclosure medium/low remediation batch."""

import json
import runpy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_disclosures.models.integrity import (
    DISCLOSURE_ENGINE_CAPABILITY, DISCLOSURE_ENGINE_CONTEXT,
)


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestDisclosureBacklogControls(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_dimension_rows_are_unique(self):
        note = self.env['eh.fin.credit.note'].create({})
        self.env['eh.fin.credit.stage.line'].create({
            'note_id': note.id, 'stage': '1', 'gross_carrying': 100.0,
        })
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.fin.credit.stage.line'].create({
                'note_id': note.id, 'stage': '1', 'gross_carrying': 1.0,
            })
        engine = {DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY}
        Recon = self.env['eh.fin.credit.recon.line'].with_context(**engine)
        Recon.create({'note_id': note.id, 'stage': '2', 'closing': 10.0})
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            Recon.create({'note_id': note.id, 'stage': '2', 'closing': 1.0})
        party = self.env['eh.related.party'].create({'name': 'KMP Unique'})
        Comp = self.env['eh.related.party.compensation']
        Comp.create({
            'party_id': party.id, 'category': 'short_term', 'amount': 10.0,
        })
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            Comp.create({
                'party_id': party.id, 'category': 'short_term', 'amount': 1.0,
            })

    def test_duplicate_dimension_migration_preserves_disclosed_totals(self):
        note = self.env['eh.fin.credit.note'].create({})
        party = self.env['eh.related.party'].create({
            'name': 'Migrated KMP dimensions',
        })
        constraints = (
            ('eh_fin_credit_stage_line',
             'eh_fin_credit_stage_line_unique_note_stage'),
            ('eh_fin_credit_recon_line',
             'eh_fin_credit_recon_line_unique_note_stage'),
            ('eh_related_party_compensation',
             'eh_related_party_compensation_unique_party_category'),
        )
        for table, constraint in constraints:
            statement = SQL(
                'ALTER TABLE %s DROP CONSTRAINT %s',
                SQL.identifier(table), SQL.identifier(constraint),
            )
            self.env.cr.execute(statement.code, statement.params)

        Stage = self.env['eh.fin.credit.stage.line']
        Stage.create({
            'note_id': note.id, 'stage': '1',
            'gross_carrying': 100.0, 'allowance': 10.0,
        })
        Stage.create({
            'note_id': note.id, 'stage': '1',
            'gross_carrying': 50.0, 'allowance': 5.0,
        })
        engine = {DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY}
        Recon = self.env['eh.fin.credit.recon.line'].with_context(**engine)
        Recon.create({
            'note_id': note.id, 'stage': '2',
            'opening': 3.0, 'closing': 8.0,
        })
        Recon.create({
            'note_id': note.id, 'stage': '2',
            'opening': 7.0, 'closing': 12.0,
        })
        Comp = self.env['eh.related.party.compensation']
        Comp.create({
            'party_id': party.id, 'category': 'short_term', 'amount': 40.0,
        })
        Comp.create({
            'party_id': party.id, 'category': 'short_term', 'amount': 60.0,
        })

        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.2' / 'pre-migration.py'
        )
        runpy.run_path(str(migration_path))['migrate'](
            self.env.cr, '19.0.1.2.1',
        )
        self.env.invalidate_all()

        stages = Stage.search([
            ('note_id', '=', note.id), ('stage', '=', '1'),
        ])
        self.assertEqual(len(stages), 1)
        self.assertAlmostEqual(stages.gross_carrying, 150.0, places=2)
        self.assertAlmostEqual(stages.allowance, 15.0, places=2)
        recons = Recon.search([
            ('note_id', '=', note.id), ('stage', '=', '2'),
        ])
        self.assertEqual(len(recons), 1)
        self.assertAlmostEqual(recons.opening, 10.0, places=2)
        self.assertAlmostEqual(recons.closing, 20.0, places=2)
        compensation = Comp.search([
            ('party_id', '=', party.id), ('category', '=', 'short_term'),
        ])
        self.assertEqual(len(compensation), 1)
        self.assertAlmostEqual(compensation.amount, 100.0, places=2)

    def test_draft_parents_can_be_deleted_by_eh_user(self):
        user = new_test_user(
            self.env, login='disclosure_draft_delete_user',
            groups='eh_account_base.group_eh_user',
        )
        records = (
            self.env['eh.fin.risk'].create({'name': 'Delete risk'})
            | self.env['eh.fin.risk']
        )
        records.with_user(user).unlink()
        for model, vals in {
            'eh.related.party': {'name': 'Delete party'},
            'eh.segment.report': {},
            'eh.entity.interest': {'name': 'Delete interest'},
            'eh.fin.maturity.run': {},
            'eh.fin.credit.note': {},
            'eh.fin.sensitivity': {},
        }.items():
            record = self.env[model].create(vals)
            record.with_user(user).unlink()
            self.assertFalse(record.exists(), model)

    def test_ifrs12_is_tracked_and_manual_summary_clears_provenance(self):
        Model = self.env['eh.entity.interest']
        self.assertTrue(Model._fields['message_ids'])
        self.assertTrue(Model._fields['state'].tracking)
        interest = Model.create({
            'name': 'Manual IFRS 12',
            'consol_run_res_id': 99,
            'consol_run_name': 'Old source',
            'summarised_assets': 100.0,
        })
        user = new_test_user(
            self.env, login='disclosure_ifrs12_manual_user',
            groups='eh_account_base.group_eh_user',
        )
        interest.with_user(user).write({'summarised_assets': 125.0})
        self.assertAlmostEqual(interest.summarised_assets, 125.0, places=2)
        self.assertFalse(interest.consol_run_res_id)
        self.assertFalse(interest.consol_run_name)

    def test_percentage_dimensions_are_bounded(self):
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.segment.report'].create({
                'major_customer_threshold_pct': 100.01,
            })
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.entity.interest'].create({
                'name': 'Invalid ownership', 'ownership_pct': -0.01,
            })
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self.env['eh.entity.interest'].create({
                'name': 'Invalid votes', 'voting_rights_pct': 100.01,
            })

    def test_ifrs8_reconciliations_and_75_percent_coverage(self):
        report = self.env['eh.segment.report'].create({
            'entity_revenue': 1000.0,
            'entity_result': 100.0,
            'entity_assets': 1000.0,
            'entity_liabilities': 300.0,
            'segment_ids': [(0, 0, {
                'name': 'Major', 'revenue': 700.0, 'result': 80.0,
                'assets': 900.0, 'liabilities': 260.0,
            })] + [(0, 0, {
                'name': 'Small %s' % index, 'revenue': 75.0,
                'result': 5.0, 'assets': 25.0, 'liabilities': 10.0,
            }) for index in range(4)],
        })
        self.assertAlmostEqual(report.revenue_reconciliation, 0.0, places=2)
        self.assertAlmostEqual(report.result_reconciliation, 0.0, places=2)
        self.assertAlmostEqual(report.assets_reconciliation, 0.0, places=2)
        self.assertAlmostEqual(
            report.liabilities_reconciliation, 0.0, places=2)
        self.assertTrue(report.segments_tie_out)
        self.assertTrue(report.result_tie_out)
        self.assertTrue(report.assets_tie_out)
        self.assertTrue(report.liabilities_tie_out)
        self.assertAlmostEqual(
            report.reportable_external_revenue, 700.0, places=2)
        self.assertAlmostEqual(report.reportable_coverage_pct, 70.0, places=4)
        self.assertFalse(report.coverage_75_met)

    def test_bullet_coupon_uses_full_contractual_term(self):
        Instrument = self.env['eh.fin.maturity.instrument']
        run = self.env['eh.fin.maturity.run'].create({
            'reporting_date': date(2026, 12, 31),
        })
        instrument = Instrument.create({
            'run_id': run.id,
            'name': 'Three-year bullet',
            'principal': 1000.0,
            'annual_rate': 10.0,
            'coupon_frequency': 'bullet',
            'interest_start_date': date(2024, 1, 1),
            'maturity_date': date(2027, 1, 1),
        })
        flows = list(instrument._contractual_cash_flows(run.reporting_date))
        days = (instrument.maturity_date - instrument.interest_start_date).days
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(
            flows[0][1], 1000.0 + 1000.0 * 0.10 * days / 365.0,
            places=2,
        )
        missing_start = Instrument.create({
            'run_id': run.id,
            'name': 'Unprovable bullet',
            'principal': 1000.0,
            'annual_rate': 10.0,
            'coupon_frequency': 'bullet',
            'maturity_date': date(2027, 1, 1),
        })
        with self.assertRaises(UserError):
            list(missing_start._contractual_cash_flows(run.reporting_date))

    def test_actual_manager_and_nonmanager_engine_permissions(self):
        manager = new_test_user(
            self.env, login='disclosure_real_manager',
            groups='eh_account_base.group_eh_manager',
        )
        models_to_finalise = {
            'eh.fin.risk': {'name': 'Manager-seeded risk'},
            'eh.fin.maturity.run': {},
            'eh.fin.credit.note': {},
            'eh.entity.interest': {'name': 'Manager-seeded interest'},
            'eh.fin.sensitivity': {},
            'eh.segment.report': {},
            'eh.related.party': {'name': 'Manager-seeded party'},
        }
        for model, vals in models_to_finalise.items():
            with self.subTest(model=model):
                record = self.env[model].with_user(manager).create({
                    **vals, 'state': 'finalised',
                })
                # A manager is still a non-superuser: guarded create input is
                # stripped and the real action performs the transition.
                self.assertEqual(record.state, 'draft')
                record.action_finalise()
                self.assertEqual(record.state, 'finalised')

        draft_risk = self.env['eh.fin.risk'].with_user(manager).create({
            'name': 'Forged output guard',
        })
        with self.assertRaises(AccessError):
            draft_risk.write({'ecl_basis': 'lifetime'})
        nonmanager = new_test_user(
            self.env, login='disclosure_engine_nonmanager',
            groups='eh_account_base.group_eh_user',
        )
        sensitivity = self.env['eh.fin.sensitivity'].create({})
        with self.assertRaises(AccessError):
            sensitivity.with_user(nonmanager).action_compute()

    def test_engine_capability_is_json_safe_and_not_boolean(self):
        self.assertIsInstance(DISCLOSURE_ENGINE_CAPABILITY, str)
        self.assertTrue(DISCLOSURE_ENGINE_CAPABILITY)
        json.dumps({DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY})
        self.assertNotEqual(DISCLOSURE_ENGINE_CAPABILITY, True)

    def test_search_views_and_auditor_menus_are_reachable(self):
        for action_name in (
            'action_eh_related_party', 'action_eh_segment_report',
            'action_eh_fin_risk', 'action_eh_entity_interest',
            'action_eh_fin_maturity_run', 'action_eh_fin_credit_note',
            'action_eh_fin_sensitivity',
        ):
            self.assertTrue(
                self.env.ref('eh_account_disclosures.%s' % action_name)
                .search_view_id,
                action_name,
            )
        auditor = self.env.ref('eh_account_base.group_eh_auditor')
        for menu_name in (
            'eh_account_base.menu_eh_reports_advanced',
            'eh_account_disclosures.menu_eh_disclosures_root',
            'eh_account_disclosures.menu_eh_fin_credit_note',
        ):
            self.assertIn(auditor, self.env.ref(menu_name).group_ids)

    def test_company_sequences_have_no_global_fallback(self):
        other = self.env['res.company'].create({'name': 'Sequence Branch'})
        self.env['eh.fin.credit.note'].with_company(other).create({
            'company_id': other.id,
        })
        Sequence = self.env['ir.sequence']
        codes = (
            'eh.segment.report', 'eh.fin.maturity.run',
            'eh.fin.credit.note', 'eh.fin.sensitivity',
        )
        self.assertFalse(Sequence.search([
            ('code', 'in', codes), ('company_id', '=', False),
        ]))
        self.assertEqual(len(Sequence.search([
            ('code', '=', 'eh.fin.credit.note'),
            ('company_id', '=', other.id),
        ])), 1)

    def test_branch_scope_fails_closed_when_descendant_is_unreadable(self):
        if 'parent_id' not in self.env['res.company']._fields:
            self.skipTest('company hierarchy is unavailable on this series')
        branch = self.env['res.company'].create({
            'name': 'Unreadable Disclosure Branch',
            'parent_id': self.env.company.id,
        })
        user = new_test_user(
            self.env, login='disclosure_branch_scope_user',
            groups='eh_account_base.group_eh_user',
        )
        user.sudo().write({'company_ids': [(6, 0, [self.env.company.id])]})
        self.assertNotIn(branch, user.company_ids)
        report = self.env['eh.segment.report'].create({
            'include_branch_companies': True,
        })
        with self.assertRaises(AccessError):
            report.with_user(user)._eh_disclosure_company_ids()

    def test_engine_buttons_are_manager_only_and_final_fields_readonly(self):
        view = self.env.ref(
            'eh_account_disclosures.view_eh_fin_credit_note_form')
        arch = view.arch_db
        self.assertIn('groups="eh_account_base.group_eh_manager"', arch)
        self.assertTrue(
            "readonly=\"state == 'finalised'\"" in arch
            or "'readonly': [('state', '=', 'finalised')]" in arch
        )
        self.assertTrue(
            "readonly=\"origin != 'manual'\"" in arch
            or "'readonly': [('origin', '!=', 'manual')]" in arch
        )


class _FakeLines(list):
    def filtered(self, function):
        return _FakeLines(item for item in self if function(item))

    def mapped(self, name):
        return [getattr(item, name) for item in self]


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestDisclosureBacklogAccounting(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def _post_risk_balance(self, account, amount):
        self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': fields.Date.context_today(self.env.user),
            'line_ids': [
                (0, 0, {
                    'name': 'Risk balance', 'account_id': account.id,
                    'debit': amount,
                }),
                (0, 0, {
                    'name': 'Risk counterpart',
                    'account_id': self.account_revenue.id,
                    'credit': amount,
                }),
            ],
        }).action_post()

    def test_financial_risk_tieout_reads_current_ledger(self):
        account = self._ensure_account(
            self.env, '1188', 'Live risk backing', 'asset_current')
        self._post_risk_balance(account, 100.0)
        risk = self.env['eh.fin.risk'].create({
            'name': 'Live risk tieout',
            'carrying_amount': 150.0,
            'ledger_account_ids': [(6, 0, account.ids)],
        })
        self.assertAlmostEqual(risk.ledger_carrying_amount, 100.0, places=2)
        self._post_risk_balance(account, 50.0)
        risk.invalidate_recordset([
            'ledger_carrying_amount', 'carrying_residual', 'carrying_tied',
        ])
        self.assertAlmostEqual(risk.ledger_carrying_amount, 150.0, places=2)
        self.assertTrue(risk.carrying_tied)

    def test_segment_ledger_compute_scans_once_per_report(self):
        plan = self.env['account.analytic.plan'].create({
            'name': 'Disclosure batch plan',
        })
        analytic_a = self.env['account.analytic.account'].create({
            'name': 'Segment A batch', 'plan_id': plan.id,
        })
        analytic_b = self.env['account.analytic.account'].create({
            'name': 'Segment B batch', 'plan_id': plan.id,
        })
        report = self.env['eh.segment.report'].create({
            'segment_ids': [
                (0, 0, {
                    'name': 'A', 'analytic_account_id': analytic_a.id,
                }),
                (0, 0, {
                    'name': 'B', 'analytic_account_id': analytic_b.id,
                }),
            ],
        })
        MoveLine = self.env['account.move.line']
        real_search = type(MoveLine).search
        calls = []

        def counted_search(model, *args, **kwargs):
            calls.append(args[0] if args else kwargs.get('domain'))
            return real_search(model, *args, **kwargs)

        with patch.object(type(MoveLine), 'search', new=counted_search):
            report.segment_ids.with_context(**{
                DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY,
            })._compute_ledger()
        self.assertEqual(len(calls), 1)

    def test_proportional_consolidation_is_unscaled_and_converted(self):
        interest = self.env['eh.entity.interest'].create({
            'name': 'Proportional member',
        })
        member = SimpleNamespace(
            ownership_pct=60.0, method='proportional', display_name='Member',
        )
        source_currency = SimpleNamespace(
            _convert=lambda amount, target, company, on_date: amount * 2.0,
        )
        lines = _FakeLines([
            SimpleNamespace(
                member_id=member, kind='subsidiary_balance', amount=600.0,
                account_id=SimpleNamespace(account_type='asset_current'),
            ),
            SimpleNamespace(
                member_id=member, kind='subsidiary_balance', amount=-300.0,
                account_id=SimpleNamespace(account_type='liability_current'),
            ),
            SimpleNamespace(
                member_id=member, kind='subsidiary_balance', amount=-240.0,
                account_id=SimpleNamespace(account_type='income'),
            ),
            SimpleNamespace(
                member_id=member, kind='subsidiary_balance', amount=60.0,
                account_id=SimpleNamespace(account_type='expense'),
            ),
        ])
        run = SimpleNamespace(
            id=777, display_name='Proportional run',
            presentation_currency_id=source_currency,
            period_to=date(2026, 6, 30), line_ids=lines,
        )
        interest._apply_consolidation_figures(run, member)
        # Divide the 60%-scaled run figures back to full-member values, then
        # translate source presentation currency to disclosure currency x2.
        self.assertAlmostEqual(interest.summarised_assets, 2000.0, places=2)
        self.assertAlmostEqual(
            interest.summarised_liabilities, 1000.0, places=2)
        self.assertAlmostEqual(interest.summarised_revenue, 800.0, places=2)
        self.assertAlmostEqual(interest.summarised_profit, 600.0, places=2)
